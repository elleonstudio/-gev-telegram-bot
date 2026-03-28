import os
import logging
import math
import json
import traceback
import re
import io
import pandas as pd
from datetime import datetime
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

# ======== НАСТРОЙКИ ========
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = "3278c4d1fb0e80c4b6e5f261d0631ed2"
PACKAGES_DATABASE_ID = "32a8c4d1fb0e806ebb98f5995704d0e5"
CARGO_NOTION_DATABASE_ID = os.getenv('CARGO_NOTION_DB_ID', "СЮДА_ID_БАЗЫ_CARGO") 

BOX_PRICE_CNY = 7.77
MAX_BOX_WEIGHT = 30.0

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None
orders = {}
cargo_drafts = {}

# ======== УТИЛИТЫ ========
def normalize_client_name(name):
    return re.sub(r'\s+', '', name).strip().capitalize()

def get_code(client):
    return f"{client.upper()}-{datetime.now().strftime('%y%m%d')}"

def generate_cargo_id():
    import random
    return f"CARGO-{random.randint(100, 999)}"

def optimize_boxes_with_weight(items):
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    boxes = []
    all_units = []
    for item in items:
        for _ in range(item.get('qty', 0)):
            l, w, h = item.get('dims', (1,1,1))
            all_units.append({'name': item['name'], 'dims': (l, w, h), 'weight': item.get('weight', 0.0), 'vol': l * w * h})

    for unit in all_units:
        placed = False
        for box in boxes:
            if box['rem_vol'] >= unit['vol'] and (box['cur_weight'] + unit['weight']) <= MAX_BOX_WEIGHT:
                box['items'].append(unit)
                box['rem_vol'] -= unit['vol']
                box['cur_weight'] += unit['weight']
                placed = True
                break
        if not placed:
            boxes.append({'items': [unit], 'rem_vol': (MAX_L * MAX_W * MAX_H) - unit['vol'], 'cur_weight': unit['weight']})
    return boxes

# ======== ГЛАВНОЕ МЕНЮ И РУКОВОДСТВО ========
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("🚫 Действие отменено. Вы вернулись в главное меню.\nНапишите /menu чтобы увидеть все команды.")
    elif update.callback_query:
        await update.callback_query.message.reply_text("🚫 Действие отменено.")
    return ConversationHandler.END

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = """🎛 <b>ГЛАВНОЕ МЕНЮ БОТА</b>

📦 <b>ЛОГИСТИКА И КАРГО</b>
• /cargo — Управление Карго (расчет упаковки, тарифы).
• /ff — Фулфилмент в Китае (коробки до 30 кг).
• /dostavka_new — Доставка РФ (независимый расчет с нуля).
• /dostavka — Доставка РФ (привязка к текущему выкупу).

🛒 <b>ВЫКУП ТОВАРОВ</b>
• /zakaz [Имя] — Ручной пошаговый ввод нового заказа.
• /paste [Текст] — Создание заказа из текста поставщика.
• /calc [Текст] — Быстрый калькулятор инвойса.

⚙️ <b>СИСТЕМА</b>
• /cancel — Прервать любое действие и сбросить бота.

📊 В конце каждого расчета вам доступны кнопки Airtable, Excel и Notion."""

    kb = [[InlineKeyboardButton("📖 Открыть подробное руководство", callback_data='guide_open')]]
    
    if update.message:
        await update.message.reply_text(menu_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    elif update.callback_query:
        await update.callback_query.edit_message_text(menu_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

async def guide_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    guide_text = """📖 <b>РУКОВОДСТВО ПО ИСПОЛЬЗОВАНИЮ БОТА</b>

<b>1. Выкуп из текста (/paste или /calc)</b>
Скопируйте текст от поставщика и отправьте боту.

<b>2. Интерактивный выкуп (/zakaz)</b>
Напишите <code>/zakaz Zaven</code> и бот сам спросит название, количество, цены и размеры.

<b>3. Фулфилмент (/ff)</b>
Запускается после выкупа. Распределяет товары по коробкам (до 30 кг в одной).

<b>4. Логистика Карго (/cargo)</b>
Напишите <code>/cargo</code>. Считает прибыль между тарифами, накидывает вес упаковки (+1 кг картон, +10 кг дерево).

<b>5. Доставка по РФ (/dostavka и /dostavka_new)</b>
• <code>/dostavka</code> — продолжает работу с текущим просчитанным клиентом (после /zakaz или /paste). Дает кнопки для обновления Notion и общего инвойса.
• <code>/dostavka_new</code> — начинает расчет с нуля для нового клиента (внешний груз).
<i>Как бот считает РФ:</i> Автоматически делит коробки на паллеты (от 11 шт — это 1 паллет). Применяет оптовые грейды (от 6 паллет тариф ниже). Прибавляет услуги FILLX: 100₽/приемка, 50₽/разбор, 9000₽/забор груза."""
    
    kb = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data='menu_back')]]
    await query.edit_message_text(guide_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

# ======== ЕДИНЫЙ ЦЕНТР РАСЧЕТОВ ТОВАРОВ ========
async def finalize_order(uid, message_obj):
    data = orders[uid]
    subtotal_cny = sum((i['price'] * i['qty']) + i.get('delivery_factory', 0) for i in data['items'])
    comm_amd_3pct = (subtotal_cny * 0.03) * data['client_rate']
    
    rule_applied = comm_amd_3pct < 10000
    actual_comm_amd = 10000 if rule_applied else int(comm_amd_3pct)
    actual_comm_cny = 10000 / data['client_rate'] if rule_applied else subtotal_cny * 0.03
    final_total_amd = int((subtotal_cny * data['client_rate']) + actual_comm_amd)
    
    data.update({'final_total_amd': final_total_amd, 'total_cny_netto': subtotal_cny, 'rule_applied': rule_applied, 'actual_comm_cny': actual_comm_cny, 'ff_boxes_qty': data.get('ff_boxes_qty', 0)})

    audit = [f"• Применено правило 10 000 AMD (3% было {int(comm_amd_3pct)} AMD)"] if rule_applied else []
    missing = [i['name'] for i in data['items'] if i.get('dims', (0,0,0)) == (0,0,0)]
    if missing: 
        audit.append(f"• У <b>{', '.join(missing)}</b> нет размеров. Спрошу в /ff.")
        
    if audit: 
        await message_obj.reply_text("⚠️ <b>Аудит расчета:</b>\n" + "\n".join(audit), parse_mode='HTML')

    inv_lines = "".join([f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i.get('delivery_factory', 0)} = {(i['price'] * i['qty']) + i.get('delivery_factory', 0):.1f}¥\n" for i in data['items']])
    msg_client = f"""<b>COMMERCIAL INVOICE: {data['client'].upper()}</b>
📅 Date: {datetime.now().strftime('%d.%m.%Y')}

<b>1. ТОВАРНАЯ ВЕДОМОСТЬ (Logistics Included)</b>
{inv_lines}<code>────────────────────────</code>
<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥

<b>2. КОМИССИЯ И СЕРВИС (Service Fee)</b>
({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥

<b>3. ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>
• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥
• Курс: {data['client_rate']}

✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"""

    await message_obj.reply_text(msg_client, parse_mode='HTML')

    purchase_cny = sum(i.get('purchase', 0) * i['qty'] for i in data['items'])
    total_delivery_cny = sum(i.get('delivery_factory', 0) for i in data['items'])
    real_expenses_amd = int((purchase_cny + total_delivery_cny) * data['real_rate'])
    profit_amd = final_total_amd - real_expenses_amd

    msg_admin = f"""💼 <b>ВНУТРЕННИЙ РАСЧЕТ: {data['client'].upper()}</b>

<b>РАСХОДЫ (Курс закупа: {data['real_rate']}):</b>
• Закупка товара: {purchase_cny:.1f}¥
• Доставка по Китаю: {total_delivery_cny:.1f}¥
Итого расход: <b>{real_expenses_amd:,} AMD</b>

<b>ДОХОДЫ:</b>
• Взяли с клиента: <b>{final_total_amd:,} AMD</b>
• Комиссия в чеке: {actual_comm_amd:,} AMD

💰 <b>ЧИСТАЯ ПРИБЫЛЬ: {profit_amd:,} AMD</b>"""

    client_orders, _ = await get_client_orders_from_notion(data['client'])
    keyboard = [[InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')], [InlineKeyboardButton("📑 Export Airtable", callback_data='export_airtable')]]
    
    if client_orders:
        orders[uid]['existing_notion_page_id'] = client_orders[0]['id']
        msg_admin += f"\n\n⚠️ <b>Клиент найден в базе!</b> (Заказ от: {client_orders[0].get('date')})"
        keyboard.append([InlineKeyboardButton("🔄 Обновить старый", callback_data='paste_update'), InlineKeyboardButton("➕ Создать НОВЫЙ", callback_data='paste_new')])
    else:
        keyboard.append([InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_new')])

    await message_obj.reply_text(msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def create_excel_invoice(uid):
    data = orders[uid]
    items_data = []
    for i, item in enumerate(data['items'], 1):
        delivery = item.get('delivery_factory', 0)
        items_data.append({"№": i, "Название товара": item['name'], "Кол-во (шт)": item['qty'], "Цена (¥)": item['price'], "Логистика (¥)": delivery, "Итого (¥)": (item['price'] * item['qty']) + delivery})
        
    items_data.extend([
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "", "Итого (¥)": ""},
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "SUBTOTAL:", "Итого (¥)": f"{data.get('total_cny_netto', 0):.1f} ¥"},
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "Комиссия:", "Итого (¥)": f"{data.get('actual_comm_cny', 0):.1f} ¥"},
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "ИТОГО К ОПЛАТЕ:", "Итого (¥)": f"{data.get('final_total_amd', 0):,} AMD"}
    ])
    df = pd.DataFrame(items_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Invoice')
    output.seek(0)
    return output

# ======== NOTION API ========
async def get_packages_from_notion():
    if not notion or not PACKAGES_DATABASE_ID: 
        return []
    try:
        res = notion.databases.query(database_id=PACKAGES_DATABASE_ID)
        return [{'name': p['properties'].get('Название', {}).get('title', [{}])[0].get('text', {}).get('content', ''), 'price': p['properties'].get('Цена', {}).get('number', 0)} for p in res.get('results', []) if p['properties'].get('Название', {}).get('title') and p['properties'].get('Цена', {}).get('number', 0) > 0]
    except: 
        return []

async def get_client_orders_from_notion(client_name):
    if not notion or not NOTION_DATABASE_ID: 
        return None, "Notion не настроен"
    try:
        res = notion.databases.query(database_id=NOTION_DATABASE_ID, sorts=[{"timestamp": "created_time", "direction": "descending"}], page_size=100)
        client_norm = normalize_client_name(client_name).lower()
        filtered = [p for p in res.get('results', []) if p['properties'].get('Клиент', {}).get('select', {}) and normalize_client_name(p['properties']['Клиент']['select'].get('name', '')).lower() == client_norm]
        if not filtered: 
            return [], None
        return [{'id': p['id'], 'date': p.get('created_time', '')[:10]} for p in filtered[:5]], None
    except Exception as e: 
        return None, str(e)

async def save_to_notion(uid):
    if not notion: 
        return None
    try:
        data = orders[uid]
        properties = {
            "Код заказа": {"title": [{"text": {"content": get_code(data['client'])}}]},
            "Клиент": {"select": {"name": data['client']}},
            "Количество": {"number": float(sum(i['qty'] for i in data['items']))},
            " К ОПЛАТЕ (AMD)": {"number": float(data['final_total_amd'])},
            "Статус": {"select": {"name": "Новый"}},
            "Date": {"date": {"start": datetime.now().strftime('%Y-%m-%d')}}
        }
        if 'ff_total_yuan' in data: 
            properties["ИТОГО (CNY)"] = {"number": float(data['ff_total_yuan'])}

        page_id = data.get('notion_page_id')
        
        if page_id: 
            res = notion.pages.update(page_id=page_id, properties=properties)
        else:
            res = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
            orders[uid]['notion_page_id'] = res['id']
            
        return f"https://notion.so/{res['id'].replace('-', '')}"
    except: 
        return None

# ======== СУПЕР-ПАРСЕР /PASTE & /CALC ========
def parse_paste_text(text):
    keywords = ['Количество:', 'Цена клиенту:', 'Закупка:', 'Доставка:', 'Размеры:', 'Курс клиенту:', 'Мой курс:']
    for kw in keywords: 
        text = re.sub(f"(?i)({kw})", r"\n\1", text)
        
    data = {'client': 'Unknown', 'items': [], 'client_rate': 58.0, 'real_rate': 55.0}
    current_item = None
    for line in text.split('\n'):
        l = line.strip().lower()
        if not l: continue
        
        if 'клиент:' in l: 
            data['client'] = normalize_client_name(l.split('клиент:')[-1])
        elif 'товар' in l:
            if current_item: 
                data['items'].append(current_item)
            current_item = {'name': 'Товар', 'qty': 0, 'price': 0.0, 'purchase': 0.0, 'delivery_factory': 0.0, 'dims': (0,0,0), 'weight': 0.0}
        elif 'название:' in l and current_item is not None: 
            current_item['name'] = line[line.lower().find('название:') + 9:].strip().title()
        elif 'количество:' in l and current_item is not None: 
            nums = re.findall(r'\d+', l.split('количество:')[-1])
            if nums: 
                current_item['qty'] = int(nums[0])
        elif 'цена клиенту:' in l and current_item is not None: 
            nums = re.findall(r'\d+\.?\d*', l.split('цена клиенту:')[-1].replace(',', '.'))
            if nums: 
                current_item['price'] = float(nums[0])
        elif 'закупка:' in l and current_item is not None: 
            nums = re.findall(r'\d+\.?\d*', l.split('закупка:')[-1].replace(',', '.'))
            if nums: 
                current_item['purchase'] = float(nums[0])
        elif 'доставка:' in l and current_item is not None: 
            nums = re.findall(r'\d+\.?\d*', l.split('доставка:')[-1].replace(',', '.'))
            if nums: 
                current_item['delivery_factory'] = float(nums[0])
        elif 'размеры:' in l and current_item is not None:
            nums = re.findall(r'\d+\.?\d*', l.split('размеры:')[-1].replace(',', '.'))
            if len(nums) >= 3:
                current_item['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: 
                    current_item['weight'] = float(nums[3])
        elif 'курс клиенту:' in l: 
            nums = re.findall(r'\d+\.?\d*', l.split('курс клиенту:')[-1].replace(',', '.'))
            if nums: 
                data['client_rate'] = float(nums[0])
        elif 'мой курс:' in l: 
            nums = re.findall(r'\d+\.?\d*', l.split('мой курс:')[-1].replace(',', '.'))
            if nums: 
                data['real_rate'] = float(nums[0])
            
    if current_item: 
        data['items'].append(current_item)
    return data

async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/paste', '').strip()
    if not text: 
        return
    data = parse_paste_text(text)
    if not data['items']: 
        return await update.message.reply_text("❌ Ошибка: товары не найдены.")
    orders[uid] = data
    await finalize_order(uid, update.message)

async def cmd_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/calc', '').strip()
    if not text: 
        return
    data = parse_paste_text(text)
    if not data['items']: 
        return await update.message.reply_text("❌ Ошибка: товары не найдены.")
    
    subtotal_cny = sum((i['price'] * i['qty']) + i.get('delivery_factory', 0) for i in data['items'])
    comm_amd_3pct = (subtotal_cny * 0.03) * data['client_rate']
    rule_applied = comm_amd_3pct < 10000
    actual_comm_amd = 10000 if rule_applied else int(comm_amd_3pct)
    actual_comm_cny = 10000 / data['client_rate'] if rule_applied else subtotal_cny * 0.03
    final_total_amd = int((subtotal_cny * data['client_rate']) + actual_comm_amd)
    
    data.update({'final_total_amd': final_total_amd, 'total_cny_netto': subtotal_cny, 'rule_applied': rule_applied, 'actual_comm_cny': actual_comm_cny, 'ff_boxes_qty': 0})
    orders[uid] = data

    inv_lines = "".join([f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i.get('delivery_factory', 0)} = {(i['price'] * i['qty']) + i.get('delivery_factory', 0):.1f}¥\n" for i in data['items']])
    msg_client = f"""<b>COMMERCIAL INVOICE: {data['client'].upper()}</b>\n📅 Date: {datetime.now().strftime('%d.%m.%Y')}\n\n<b>1. ТОВАРНАЯ ВЕДОМОСТЬ</b>\n{inv_lines}<code>────────────────────────</code>\n<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥\n\n<b>2. КОМИССИЯ</b>\n({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥\n\n<b>3. ИТОГОВЫЙ РАСЧЕТ</b>\n• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥\n• Курс: {data['client_rate']}\n\n✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"""
    await update.message.reply_text(msg_client, parse_mode='HTML')

    kb = [[InlineKeyboardButton("✍️ Дополнить расчет", callback_data='calc_fill'), InlineKeyboardButton("📊 Export Excel", callback_data='gen_excel')]]
    await update.message.reply_text("⚠️ <b>Внимание:</b> Для финала не хватает цен закупки и размеров.", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

C_PURCHASE, C_DIMS = range(50, 52)

async def calc_fill_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    missing_idx = -1
    for idx, item in enumerate(orders[uid]['items']):
        if item.get('purchase', 0.0) == 0.0 or item.get('dims', (0,0,0)) == (0,0,0):
            missing_idx = idx
            break
            
    if missing_idx == -1:
        await query.message.reply_text("✅ Все данные уже заполнены! Вывожу расчет...")
        await finalize_order(uid, query.message)
        return ConversationHandler.END
        
    orders[uid]['calc_missing_idx'] = missing_idx
    await query.message.reply_text(f"Введи цену закупки (CNY) для товара <b>{orders[uid]['items'][missing_idx]['name']}</b>:", parse_mode='HTML')
    return C_PURCHASE

async def c_get_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: 
        orders[uid]['items'][orders[uid]['calc_missing_idx']]['purchase'] = float(update.message.text.replace(',', '.'))
    except: 
        await update.message.reply_text("❌ Пожалуйста, введи число:")
        return C_PURCHASE
        
    await update.message.reply_text(f"Введи размеры и вес (Д Ш В Вес) для <b>{orders[uid]['items'][orders[uid]['calc_missing_idx']]['name']}</b> (или '-'):", parse_mode='HTML')
    return C_DIMS

async def c_get_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip()
    idx = orders[uid]['calc_missing_idx']
    
    if text != '-':
        try:
            nums = re.findall(r'\d+\.?\d*', text.replace(',', '.'))
            if len(nums) >= 3:
                orders[uid]['items'][idx]['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: 
                    orders[uid]['items'][idx]['weight'] = float(nums[3])
            else: 
                raise ValueError
        except: 
            await update.message.reply_text("❌ Введи 4 числа через пробел (или '-'):")
            return C_DIMS
            
    missing_idx = next((i for i, item in enumerate(orders[uid]['items']) if item.get('purchase', 0.0) == 0.0 or item.get('dims', (0,0,0)) == (0,0,0)), -1)
    if missing_idx == -1: 
        await update.message.reply_text("✅ Данные собраны!")
        await finalize_order(uid, update.message)
        return ConversationHandler.END
        
    orders[uid]['calc_missing_idx'] = missing_idx
    await update.message.reply_text(f"Цена закупки (CNY) для <b>{orders[uid]['items'][missing_idx]['name']}</b>:", parse_mode='HTML')
    return C_PURCHASE

# ======== РУЧНОЙ ВВОД ЗАКАЗА (/ZAKAZ) ========
Z_NAME, Z_QTY, Z_PRICE, Z_PURCHASE, Z_DELIVERY, Z_DIMS, Z_MORE, Z_CLIENT_RATE, Z_REAL_RATE = range(40, 49)

async def cmd_zakaz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args: 
        await update.message.reply_text("❌ Напиши имя клиента после команды. Например: /zakaz Zaven8291")
        return ConversationHandler.END
        
    client = normalize_client_name(' '.join(context.args))
    orders[uid] = {'client': client, 'items': [], 'client_rate': 58.0, 'real_rate': 55.0}
    await update.message.reply_text(f"Клиент: {client}\n\nНазвание первого товара:")
    return Z_NAME

async def z_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    orders[uid]['current'] = {'name': update.message.text.strip().title(), 'dims': (0,0,0), 'weight': 0.0}
    await update.message.reply_text("Количество:")
    return Z_QTY

async def z_get_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['qty'] = int(re.findall(r'\d+', update.message.text)[0])
    except:
        await update.message.reply_text("❌ Введи число:")
        return Z_QTY
    await update.message.reply_text("Цена клиенту (CNY):")
    return Z_PRICE

async def z_get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['price'] = float(re.findall(r'\d+\.?\d*', update.message.text.replace(',', '.'))[0])
    except:
        await update.message.reply_text("❌ Введи число:")
        return Z_PRICE
    await update.message.reply_text("Закупка (CNY):")
    return Z_PURCHASE

async def z_get_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['purchase'] = float(re.findall(r'\d+\.?\d*', update.message.text.replace(',', '.'))[0])
    except:
        await update.message.reply_text("❌ Введи число:")
        return Z_PURCHASE
    await update.message.reply_text("Доставка до склада (CNY):")
    return Z_DELIVERY

async def z_get_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['delivery_factory'] = float(re.findall(r'\d+\.?\d*', update.message.text.replace(',', '.'))[0])
    except:
        await update.message.reply_text("❌ Введи число:")
        return Z_DELIVERY
    await update.message.reply_text("Размеры (Д Ш В Вес) или '-':")
    return Z_DIMS

async def z_get_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip()
    if text != '-':
        try:
            nums = re.findall(r'\d+\.?\d*', text.replace(',', '.'))
            if len(nums) >= 3:
                orders[uid]['current']['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: 
                    orders[uid]['current']['weight'] = float(nums[3])
        except:
            await update.message.reply_text("❌ Ошибка формата. Введи размеры или '-':")
            return Z_DIMS
                
    orders[uid]['items'].append(orders[uid]['current'])
    kb = [[InlineKeyboardButton("✅ Добавить еще", callback_data='z_more_yes')], [InlineKeyboardButton("❌ Готово, к расчету", callback_data='z_more_no')]]
    await update.message.reply_text("Еще товар?", reply_markup=InlineKeyboardMarkup(kb))
    return Z_MORE

async def z_more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'z_more_yes': 
        await query.edit_message_text("Название следующего товара:")
        return Z_NAME
        
    await query.edit_message_text("Укажи курс клиенту (например 58):")
    return Z_CLIENT_RATE

async def z_client_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['client_rate'] = float(re.findall(r'\d+\.?\d*', update.message.text.replace(',', '.'))[0])
    except:
        await update.message.reply_text("❌ Введи число:")
        return Z_CLIENT_RATE
    await update.message.reply_text("Реальный курс закупа (например 55):")
    return Z_REAL_RATE

async def z_real_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['real_rate'] = float(re.findall(r'\d+\.?\d*', update.message.text.replace(',', '.'))[0])
    except:
        await update.message.reply_text("❌ Введи число:")
        return Z_REAL_RATE
    await finalize_order(uid, update.message)
    return ConversationHandler.END

# ======== FF MENU И НАБОРЫ ========
F_MAIN_MENU, F_SINGLE_DIMS, F_BUNDLE_CREATE, F_BUNDLE_NAME, F_BUNDLE_DIMS, F_BUNDLE_PACKAGE, F_BUNDLE_WORK, F_SUMMARY = range(20, 28)

async def cmd_ff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in orders or not orders[uid].get('items'):
        await update.message.reply_text("Сначала /paste или /zakaz")
        return ConversationHandler.END
        
    orders[uid]['ff_bundles'] = orders[uid].get('ff_bundles', [])
    orders[uid]['ff_items_in_bundles'] = orders[uid].get('ff_items_in_bundles', set())
    return await show_ff_main_menu(update, uid)

async def show_ff_main_menu(update_or_query, uid):
    items = orders[uid]['items']
    items_in_bundles = orders[uid]['ff_items_in_bundles']
    bundles = orders[uid]['ff_bundles']
    
    msg = "📦 <b>FF Китай — Выбор режима</b>\n\n<b>Товары:</b>\n"
    for idx, item in enumerate(items): 
        msg += f"{'☑️ <s>' if idx in items_in_bundles else '☐ '}{item['name']}{'</s>' if idx in items_in_bundles else ''}\n"
        
    msg += f"\n<b>Создано наборов:</b> {len(bundles)}\n"
    for b in bundles: 
        msg += f"  📦 {b.get('name', 'Без имени')} (Кол-во: {b.get('qty', 1)})\n"
        
    keyboard = [[InlineKeyboardButton("📦 Собрать набор", callback_data='ff_mode_bundle')], [InlineKeyboardButton("✅ Завершить FF →", callback_data='ff_mode_continue')]]
    if hasattr(update_or_query, 'edit_message_text'): 
        await update_or_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else: 
        await update_or_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return F_MAIN_MENU

async def ff_main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'ff_back_menu': 
        return await show_ff_main_menu(query, uid)
        
    elif query.data == 'ff_mode_bundle':
        available = [(idx, item) for idx, item in enumerate(orders[uid]['items']) if idx not in orders[uid]['ff_items_in_bundles']]
        if not available: 
            await query.answer("Нет товаров для набора", show_alert=True)
            return F_MAIN_MENU
            
        orders[uid]['ff_bundle_selected'] = set()
        orders[uid]['ff_bundle_available'] = available
        return await show_bundle_item_selection(type('obj', (object,), {'edit_message_text': query.edit_message_text}), uid)
        
    elif query.data == 'ff_mode_continue':
        unpacked_indices = [idx for idx, item in enumerate(orders[uid]['items']) if idx not in orders[uid]['ff_items_in_bundles']]
        missing = [orders[uid]['items'][idx]['name'] for idx in unpacked_indices if orders[uid]['items'][idx].get('dims', (0,0,0)) == (0,0,0)]
        if missing:
            await query.edit_message_text(f"⚠️ У товара <b>{missing[0]}</b> нет размеров! Введи размеры (Д Ш В Вес):", parse_mode='HTML')
            orders[uid]['ff_missing_idx'] = unpacked_indices[0]
            return F_SINGLE_DIMS
            
        await query.edit_message_text("Напиши стоимость сборки/работы для ВСЕХ оставшихся одиночных товаров суммарно (¥) или 0:")
        return F_SUMMARY

async def ff_single_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        nums = tuple(map(float, update.message.text.replace(',', '.').split()))
    except:
        await update.message.reply_text("❌ Введи цифры через пробел:")
        return F_SINGLE_DIMS
        
    idx = orders[uid]['ff_missing_idx']
    if len(nums) >= 3: 
        orders[uid]['items'][idx]['dims'] = (nums[0], nums[1], nums[2])
    if len(nums) >= 4: 
        orders[uid]['items'][idx]['weight'] = nums[3]
        
    return await show_ff_main_menu(type('obj', (object,), {'message': update.message, 'reply_text': update.message.reply_text}), uid)

async def show_bundle_item_selection(update_or_query, uid):
    available = orders[uid]['ff_bundle_available']
    selected = orders[uid]['ff_bundle_selected']
    keyboard = [[InlineKeyboardButton(f"{'☑️' if item_idx in selected else '☐'} {item['name']} x {item['qty']}", callback_data=f'ff_b_sel_{item_idx}')] for idx, (item_idx, item) in enumerate(available)]
    keyboard.append([InlineKeyboardButton("✅ Далее", callback_data='ff_b_next')])
    await update_or_query.edit_message_text("Выбери товары для набора:", reply_markup=InlineKeyboardMarkup(keyboard))
    return F_BUNDLE_CREATE

async def ff_bundle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data.startswith('ff_b_sel_'):
        i_idx = int(query.data.replace('ff_b_sel_', ''))
        if i_idx in orders[uid]['ff_bundle_selected']: 
            orders[uid]['ff_bundle_selected'].remove(i_idx)
        else: 
            orders[uid]['ff_bundle_selected'].add(i_idx)
        return await show_bundle_item_selection(type('obj', (object,), {'edit_message_text': query.edit_message_text}), uid)
        
    elif query.data == 'ff_b_next': 
        await query.edit_message_text("Введи имя набора:")
        return F_BUNDLE_NAME

async def ff_bundle_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    orders[uid]['ff_b_name'] = update.message.text.strip()
    await update.message.reply_text("Размеры ОДНОГО готового набора (Д Ш В Вес):")
    return F_BUNDLE_DIMS

async def ff_bundle_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        nums = tuple(map(float, update.message.text.replace(',', '.').split()))
        orders[uid]['ff_b_dims'] = (nums[0], nums[1], nums[2])
        orders[uid]['ff_b_weight'] = nums[3] if len(nums) >= 4 else 0.0
    except:
        await update.message.reply_text("❌ Введи цифры через пробел:")
        return F_BUNDLE_DIMS
        
    selected_indices = orders[uid].get('ff_bundle_selected', set())
    bundle_qty = min(orders[uid]['items'][i].get('qty', 1) for i in selected_indices) if selected_indices else 1
    orders[uid]['ff_b_qty'] = bundle_qty
    
    await update.message.reply_text(f"🤖 Получается <b>{bundle_qty} наборов</b>.", parse_mode='HTML')
    packages = await get_packages_from_notion()
    orders[uid]['ff_available_packages'] = packages
    
    keyboard = [[InlineKeyboardButton(f"📦 {p['name']} — {p['price']}¥", callback_data=f'ff_b_pkg_{i}')] for i, p in enumerate(packages)]
    keyboard.append([InlineKeyboardButton("💰 Своя цена", callback_data='ff_b_custom')])
    await update.message.reply_text("Выбери пакет:", reply_markup=InlineKeyboardMarkup(keyboard))
    return F_BUNDLE_PACKAGE

async def ff_bundle_package_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'ff_b_custom': 
        await query.edit_message_text("Цена пакета (¥):")
        return F_BUNDLE_PACKAGE
        
    orders[uid]['ff_b_pkg'] = orders[uid]['ff_available_packages'][int(query.data.replace('ff_b_pkg_', ''))]
    await query.edit_message_text("Цена сборки ЗА 1 НАБОР (¥):")
    return F_BUNDLE_WORK

async def ff_bundle_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['ff_b_pkg'] = {'name': 'Ручной', 'price': float(update.message.text.replace(',', '.'))}
    except:
        await update.message.reply_text("❌ Введи число:")
        return F_BUNDLE_PACKAGE
    await update.message.reply_text("Цена сборки ЗА 1 НАБОР (¥):")
    return F_BUNDLE_WORK

async def ff_bundle_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        work = float(update.message.text.replace(',', '.'))
    except:
        await update.message.reply_text("❌ Введи число:")
        return F_BUNDLE_WORK
        
    orders[uid]['ff_bundles'].append({
        'name': orders[uid]['ff_b_name'], 
        'dims': orders[uid]['ff_b_dims'], 
        'weight': orders[uid].get('ff_b_weight', 0), 
        'qty': orders[uid]['ff_b_qty'], 
        'pkg': orders[uid]['ff_b_pkg'], 
        'work_price': work, 
        'item_indices': list(orders[uid]['ff_bundle_selected'])
    })
    orders[uid]['ff_items_in_bundles'].update(orders[uid]['ff_bundle_selected'])
    return await show_ff_main_menu(type('obj', (object,), {'edit_message_text': update.message.reply_text}), uid)

async def ff_summary_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        single_work = float(update.message.text.replace(',', '.'))
    except:
        await update.message.reply_text("❌ Введи число:")
        return F_SUMMARY
        
    unpacked_items = [item for idx, item in enumerate(orders[uid]['items']) if idx not in orders[uid]['ff_items_in_bundles']]
    bundle_items = [{'name': b['name'], 'dims': b['dims'], 'weight': b['weight'], 'qty': b['qty']} for b in orders[uid]['ff_bundles']]
    
    boxes = optimize_boxes_with_weight(unpacked_items + bundle_items)
    total_boxes = len(boxes)
    total_weight = sum(b['cur_weight'] for b in boxes)
    cost = total_boxes * BOX_PRICE_CNY
    
    orders[uid]['ff_total_yuan'] = cost + single_work + sum((b['pkg']['price'] + b['work_price']) * b['qty'] for b in orders[uid]['ff_bundles'])
    orders[uid]['ff_boxes_qty'] = total_boxes
    
    res = f"📦 <b>Результат FF (Лимит 30кг):</b>\n\nМест: {total_boxes} шт\nОбщий вес: {total_weight:.2f} кг\nСтоимость коробок: {cost:.2f}¥ (по {BOX_PRICE_CNY}¥)\nОбщий итог FF: {orders[uid]['ff_total_yuan']:.2f}¥"
    kb = [[InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')], [InlineKeyboardButton("📑 Export Airtable", callback_data='export_airtable')], [InlineKeyboardButton("💾 Обновить Notion", callback_data='paste_save_direct')]]
    
    await update.message.reply_text(res, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


# ======== ЕДИНЫЙ МОДУЛЬ ДОСТАВКИ РФ (/DOSTAVKA И /DOSTAVKA_NEW) ========

NEW_TARIFFS = {
    'Коледино': {'boxes': [(5, 350), (10, 300)], 'pallets': [(5, 3500), (999, 3000)], 'schedule': 'Ежедневно'},
    'Электросталь': {'boxes': [(10, 350)], 'pallets': [(5, 3500), (999, 3000)], 'schedule': 'Ежедневно'},
    'Тула': {'boxes': [(10, 500)], 'pallets': [(999, 5500)], 'schedule': 'Ежедневно'},
    'Краснодар': {'boxes': [(10, 1100)], 'pallets': [(999, 9500)], 'schedule': 'Забор: Ср, Сб | Доставка: Пт, Пн'},
    'Невинномысск': {'boxes': [(10, 1100)], 'pallets': [(999, 10500)], 'schedule': 'Забор: Вт, Пт | Доставка: Чт, Вс'},
    'Рязань': {'boxes': [(10, 700)], 'pallets': [(999, 5500)], 'schedule': 'Ежедневно'},
    'Котовск': {'boxes': [(10, 750)], 'pallets': [(999, 6500)], 'schedule': 'Забор: Пн, Ср, Пт | Доставка: На след. день'},
    'Казань': {'boxes': [(7, 800), (10, 650)], 'pallets': [(999, 6500)], 'schedule': 'Забор: Пн, Ср, Чт, Сб | Доставка: На след. день'},
    'Новосемейкино': {'boxes': [(10, 1000)], 'pallets': [(999, 9500)], 'schedule': 'Забор: Вт, Пт | Доставка: Чт, Вс'},
    'Воронеж': {'boxes': [(10, 800)], 'pallets': [(999, 6500)], 'schedule': 'Забор: Ср, Сб | Доставка: Чт, Вс'},
    'Пенза': {'boxes': [(10, 800)], 'pallets': [(999, 6500)], 'schedule': 'Забор: Ср, Сб | Доставка: Чт, Вс'},
    'Владимир': {'boxes': [(10, 700)], 'pallets': [(999, 5500)], 'schedule': 'Забор: Пн, Ср, Чт, Сб | Доставка: Вт, Чт, Пт, Вс'},
    'Сарапул': {'boxes': [(10, 1200)], 'pallets': [(999, 11000)], 'schedule': 'Забор: Пн, Ср, Сб | Доставка: Ср, Пт, Пн'},
    'Екатеринбург': {'boxes': [(5, 1400), (10, 1200)], 'pallets': [(999, 12500)], 'schedule': 'По запросу'}
}

DN_CLIENT, DN_WH, DN_BOXES, DN_MORE, DN_RATE = range(70, 75)

def generate_dn_warehouse_keyboard():
    keyboard = []
    row = []
    for c in NEW_TARIFFS.keys():
        row.append(InlineKeyboardButton(c, callback_data=f'dn_wh_{c}'))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: 
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)

# Точка входа для /dostavka_new (без привязки)
async def cmd_dostavka_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    orders[uid] = orders.get(uid, {})
    orders[uid]['dn_wh_list'] = []
    orders[uid]['is_linked_dostavka'] = False
    await update.message.reply_text("Напиши имя клиента для расчета доставки по РФ:")
    return DN_CLIENT

async def dn_get_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    orders[uid]['dn_client'] = normalize_client_name(update.message.text)
    await update.message.reply_text("Выбери склад РФ для отправки:", reply_markup=generate_dn_warehouse_keyboard())
    return DN_WH

# Точка входа для /dostavka (с привязкой к /zakaz или /paste)
async def cmd_dostavka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in orders or 'client' not in orders[uid]: 
        await update.message.reply_text("❌ Сначала создай заказ через /zakaz или /paste")
        return ConversationHandler.END
        
    orders[uid]['dn_client'] = orders[uid]['client']
    orders[uid]['dn_wh_list'] = []
    orders[uid]['is_linked_dostavka'] = True
    
    await update.message.reply_text(f"📦 Расчет доставки для текущего клиента: <b>{orders[uid]['client']}</b>\n\nВыбери склад РФ для отправки:", parse_mode='HTML', reply_markup=generate_dn_warehouse_keyboard())
    return DN_WH

async def dn_warehouse_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    orders[uid]['dn_current_wh'] = query.data.replace('dn_wh_', '')
    await query.edit_message_text(f"Введи количество коробок для склада {orders[uid]['dn_current_wh']}:")
    return DN_BOXES

async def dn_get_boxes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: 
        total_boxes = int(update.message.text.strip())
    except: 
        await update.message.reply_text("❌ Введи целое число:")
        return DN_BOXES
    
    city = orders[uid]['dn_current_wh']
    
    pallets = total_boxes // 16
    rem_boxes = total_boxes % 16
    if rem_boxes >= 11:
        pallets += 1
        rem_boxes = 0
        
    pallet_price = 0
    if pallets > 0:
        for limit, price in NEW_TARIFFS[city]['pallets']:
            if pallets <= limit:
                pallet_price = price
                break
                
    box_price = 0
    if rem_boxes > 0:
        for limit, price in NEW_TARIFFS[city]['boxes']:
            if rem_boxes <= limit:
                box_price = price
                break
                
    cost = (pallets * pallet_price) + (rem_boxes * box_price)
    
    orders[uid]['dn_wh_list'].append({
        'city': city, 
        'total_boxes': total_boxes, 
        'pallets': pallets,
        'pallet_price': pallet_price,
        'rem_boxes': rem_boxes,
        'box_price': box_price,
        'cost': cost,
        'schedule': NEW_TARIFFS[city]['schedule']
    })
    
    kb = [[InlineKeyboardButton("➕ Да, выбрать еще склад", callback_data='dn_more_yes')], [InlineKeyboardButton("➡️ Нет, к расчету", callback_data='dn_more_no')]]
    await update.message.reply_text(f"✅ Добавлено: {city} ({total_boxes} шт).\nЕдем на еще один склад?", reply_markup=InlineKeyboardMarkup(kb))
    return DN_MORE

async def dn_more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'dn_more_yes':
        await query.edit_message_text("Выбери еще один склад РФ:", reply_markup=generate_dn_warehouse_keyboard())
        return DN_WH
        
    await query.edit_message_text("Введи курс ₽ → Драм для клиента (например, 4.8):")
    return DN_RATE

async def dn_get_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: 
        rate = float(update.message.text.replace(',', '.'))
    except: 
        await update.message.reply_text("❌ Введи число:")
        return DN_RATE
    
    orders[uid]['dn_rate'] = rate
    
    total_boxes = sum(w['total_boxes'] for w in orders[uid]['dn_wh_list'])
    total_rub_routes = sum(w['cost'] for w in orders[uid]['dn_wh_list'])
    
    priemka_cost = total_boxes * 100
    razbor_cost = total_boxes * 50
    pickup_cost = 9000
    
    total_rub = total_rub_routes + priemka_cost + razbor_cost + pickup_cost
    total_amd = int(total_rub * rate)
    
    orders[uid]['dn_total_rub'] = total_rub
    orders[uid]['dn_total_amd'] = total_amd
    orders[uid]['dn_total_boxes'] = total_boxes
    
    lines = []
    for w in orders[uid]['dn_wh_list']:
        calc_parts = []
        if w['pallets'] > 0:
            calc_parts.append(f"{w['pallets']} палл × {w['pallet_price']} ₽")
        if w['rem_boxes'] > 0:
            calc_parts.append(f"{w['rem_boxes']} кор × {w['box_price']} ₽")
            
        calc_str = " + ".join(calc_parts)
        lines.append(f"• {w['city']}: {calc_str} = {w['cost']:,} ₽\n  Расписание: {w['schedule']}")
        
    routes_text = "\n".join(lines).replace(',', ' ')
    
    msg_client = f"""ДОСТАВКА ПО РФ
Клиент: {orders[uid]['dn_client'].upper()}

МАРШРУТ И КОРОБКИ:
{routes_text}

УСЛУГИ FILLX:
• Приемка товара коробами: {total_boxes} шт × 100 ₽ = {priemka_cost:,} ₽
• Разбор коробов: {total_boxes} шт × 50 ₽ = {razbor_cost:,} ₽
• Забор груза (ЮВ) (Грузчики/заезд/доставка): 9 000 ₽

Итого в рублях: {total_rub:,} ₽
Курс конвертации: {rate}
К ОПЛАТЕ: {total_amd:,} AMD""".replace(',', ' ')

    if orders[uid].get('is_linked_dostavka'):
        kb = [
            [InlineKeyboardButton("🚚 Excel Доставка", callback_data='dn_export_ex'), InlineKeyboardButton("📊 Excel Товары", callback_data='gen_excel')],
            [InlineKeyboardButton("📑 Airtable Товары", callback_data='export_airtable'), InlineKeyboardButton("📑 Airtable Доставка", callback_data='dn_export_airtable')],
            [InlineKeyboardButton("💾 Обновить Notion", callback_data='paste_save_direct')]
        ]
    else:
        kb = [
            [InlineKeyboardButton("📊 Export Excel", callback_data='dn_export_ex')],
            [InlineKeyboardButton("📑 Export Airtable", callback_data='dn_export_airtable')],
            [InlineKeyboardButton("🗑 Отменить / Удалить", callback_data='dn_delete')]
        ]
        
    await update.message.reply_text(msg_client, reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


# ======== ИНТЕРАКТИВНОЕ УПРАВЛЕНИЕ КАРГО (/CARGO) ========
CG_START, CG_CLIENT, CG_LABEL, CG_ITEM_NAME, CG_PACK, CG_DIMS, CG_MORE_ITEMS, CG_T_CARGO, CG_T_CLIENT, CG_R_CNY, CG_R_AMD = range(80, 91)

async def cmd_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in cargo_drafts: cargo_drafts[uid] = {}
    active_parties = cargo_drafts[uid]

    keyboard = [[InlineKeyboardButton("➕ Создать новую партию", callback_data='cg_new_draft')]]
    for cid, draft in active_parties.items():
        ready = sum(1 for i in draft['items'] if i['pieces'] > 0)
        total = len(draft['items'])
        status = "Готов к расчету" if ready == total else "Ждет данных"
        keyboard.append([InlineKeyboardButton(f"📦 {draft['client']} ({ready}/{total}) - {status}", callback_data=f'cg_open_{cid}')])

    msg = "📂 **Управление Карго:**\nВыберите действие:"
    if update.message:
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.callback_query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    return CG_START

async def cg_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    
    if query.data == 'cg_new_draft':
        await query.edit_message_text("👤 Напиши имя клиента (например: Zaven8291):")
        return CG_CLIENT

    elif query.data.startswith('cg_open_'):
        cid = query.data.replace('cg_open_', '')
        draft = cargo_drafts[uid].get(cid)
        if not draft:
            await query.edit_message_text("❌ Партия не найдена.")
            return ConversationHandler.END

        orders[uid] = orders.get(uid, {})
        orders[uid]['active_cargo_id'] = cid

        missing_idx = next((i for i, item in enumerate(draft['items']) if item['pieces'] == 0), -1)
        if missing_idx != -1:
            orders[uid]['cg_missing_idx'] = missing_idx
            item_name = draft['items'][missing_idx]['name']
            kb = [[InlineKeyboardButton("🟡 Мешок ($5)", callback_data='cg_pack_sack')], [InlineKeyboardButton("📦 Уголки ($6)", callback_data='cg_pack_corners')], [InlineKeyboardButton("🪵 Обрешетка ($8)", callback_data='cg_pack_wood')], [InlineKeyboardButton("⏳ Жду данные", callback_data='cg_pack_wait')]]
            await query.edit_message_text(f"📦 Партия **{draft['client']}**.\nВыбери тип упаковки для товара **{item_name}**:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
            return CG_PACK
        else:
            t_weight = sum(i['weight'] for i in draft['items'])
            t_vol = sum(i['dims'][0] for i in draft['items'])
            t_pieces = sum(i['pieces'] for i in draft['items'])
            density = int(t_weight / t_vol) if t_vol > 0 else 0
            msg = f"📦 **СВОДКА ДЛЯ КАРГО ({draft['client']}):**\n• Общий вес: {t_weight} кг\n• Объем: {t_vol:.2f} м³\n• Мест: {t_pieces} шт\n• Плотность: {density} кг/м³"
            kb = [[InlineKeyboardButton("🧮 Рассчитать Карго", callback_data='cg_calc')], [InlineKeyboardButton("➕ Добавить товар", callback_data='cg_more_yes')]]
            await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
            return CG_MORE_ITEMS

async def cg_get_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid] = orders.get(uid, {})
    orders[uid]['cg_temp_client'] = normalize_client_name(update.message.text)
    await update.message.reply_text("🏷 Напиши метку для груза (например: Одежда и пластик):")
    return CG_LABEL

async def cg_get_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); client = orders[uid]['cg_temp_client']
    label = update.message.text.strip(); cid = generate_cargo_id()
    cargo_drafts[uid][cid] = {'cargo_id': cid, 'client': client, 'label': label, 'items': []}
    orders[uid]['active_cargo_id'] = cid
    await update.message.reply_text("📦 Отлично! Напиши название ПЕРВОГО товара:")
    return CG_ITEM_NAME

async def cg_get_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); cid = orders[uid]['active_cargo_id']
    name = update.message.text.strip().title()
    cargo_drafts[uid][cid]['items'].append({'name': name, 'pieces': 0, 'weight': 0.0, 'dims': (0,0,0), 'pack_type': None, 'pack_price': 0.0})
    orders[uid]['cg_missing_idx'] = len(cargo_drafts[uid][cid]['items']) - 1

    kb = [[InlineKeyboardButton("🟡 Мешок ($5)", callback_data='cg_pack_sack')], [InlineKeyboardButton("📦 Уголки ($6)", callback_data='cg_pack_corners')], [InlineKeyboardButton("🪵 Обрешетка ($8)", callback_data='cg_pack_wood')], [InlineKeyboardButton("⏳ Жду данные", callback_data='cg_pack_wait')]]
    await update.message.reply_text(f"Выбери тип упаковки для товара **{name}**:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    return CG_PACK

async def cg_pack_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    idx = orders[uid]['cg_missing_idx']; cid = orders[uid]['active_cargo_id']

    if query.data == 'cg_pack_wait':
        kb = [[InlineKeyboardButton("➕ Добавить еще товар", callback_data='cg_more_yes')], [InlineKeyboardButton("💾 В черновики", callback_data='cg_more_draft')]]
        await query.edit_message_text(f"⏳ Товар сохранен без габаритов. Что дальше?", reply_markup=InlineKeyboardMarkup(kb))
        return CG_MORE_ITEMS

    if query.data == 'cg_pack_sack': cargo_drafts[uid][cid]['items'][idx].update({'pack_type': 'Мешок', 'pack_price': 5.0})
    elif query.data == 'cg_pack_corners': cargo_drafts[uid][cid]['items'][idx].update({'pack_type': 'Уголки', 'pack_price': 6.0})
    elif query.data == 'cg_pack_wood': cargo_drafts[uid][cid]['items'][idx].update({'pack_type': 'Обрешетка', 'pack_price': 8.0})

    msg = f"📏 Введи данные для **{cargo_drafts[uid][cid]['items'][idx]['name']}**.\n*Если коробок несколько разных, пиши каждую с новой строки.*\n\nНажми ниже, чтобы скопировать шаблон:\n`Кол-во Вес Длина Ширина Высота`"
    await query.edit_message_text(msg, parse_mode='Markdown')
    return CG_DIMS

async def cg_dims_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); cid = orders[uid]['active_cargo_id']; idx = orders[uid]['cg_missing_idx']
    pack_type = cargo_drafts[uid][cid]['items'][idx]['pack_type']

    total_pieces = 0; total_weight = 0.0; total_vol = 0.0
    for line in update.message.text.split('\n'):
        if not line.strip(): continue
        try:
            nums = tuple(map(float, line.replace(',', '.').split()))
            if len(nums) < 5: return await update.message.reply_text("❌ Нужно 5 цифр: Кол-во Вес Д Ш В\nПопробуй еще раз:", parse_mode='Markdown')
            p, w, l, wid, h = int(nums[0]), nums[1], nums[2], nums[3], nums[4]
            if pack_type == 'Уголки': w += 1.0
            elif pack_type == 'Обрешетка': w += 10.0; l += 5; wid += 5; h += 5
            total_pieces += p; total_weight += (p * w); total_vol += (p * (l * wid * h) / 1000000)
        except: return await update.message.reply_text(f"❌ Ошибка в строке: `{line}`\nПопробуй еще раз:", parse_mode='Markdown')

    cargo_drafts[uid][cid]['items'][idx].update({'pieces': total_pieces, 'weight': total_weight, 'dims': (total_vol, 1, 1)})
    missing_idx = next((i for i, item in enumerate(cargo_drafts[uid][cid]['items']) if item['pieces'] == 0), -1)

    if missing_idx != -1:
        orders[uid]['cg_missing_idx'] = missing_idx
        item_name = cargo_drafts[uid][cid]['items'][missing_idx]['name']
        kb = [[InlineKeyboardButton("🟡 Мешок ($5)", callback_data='cg_pack_sack')], [InlineKeyboardButton("📦 Уголки ($6)", callback_data='cg_pack_corners')], [InlineKeyboardButton("🪵 Обрешетка ($8)", callback_data='cg_pack_wood')], [InlineKeyboardButton("⏳ Жду данные", callback_data='cg_pack_wait')]]
        await update.message.reply_text(f"Выбери тип упаковки для товара **{item_name}**:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
        return CG_PACK

    kb = [[InlineKeyboardButton("➕ Добавить еще товар", callback_data='cg_more_yes')], [InlineKeyboardButton("🧮 Рассчитать Карго", callback_data='cg_calc')], [InlineKeyboardButton("💾 В черновики", callback_data='cg_more_draft')]]
    await update.message.reply_text("✅ Товар добавлен! Что делаем дальше?", reply_markup=InlineKeyboardMarkup(kb))
    return CG_MORE_ITEMS

async def cg_more_items_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    
    if query.data == 'cg_more_yes':
        await query.edit_message_text("📦 Напиши название СЛЕДУЮЩЕГО товара:")
        return CG_ITEM_NAME
        
    elif query.data == 'cg_more_draft':
        await query.edit_message_text("💾 Сохранено в черновики. Возвращайтесь, когда будут данные!")
        return ConversationHandler.END
        
    elif query.data == 'cg_calc':
        cid = orders[uid]['active_cargo_id']
        draft = cargo_drafts[uid][cid]
        missing = [i['name'] for i in draft['items'] if i['pieces'] == 0]
        if missing:
            await query.edit_message_text(f"⏳ Не хватает габаритов для: {', '.join(missing)}.\nПартия сохранена в черновик!")
            return ConversationHandler.END

        t_weight = sum(i['weight'] for i in draft['items'])
        t_vol = sum(i['dims'][0] for i in draft['items'])
        t_pieces = sum(i['pieces'] for i in draft['items'])
        density = int(t_weight / t_vol) if t_vol > 0 else 0
        msg = f"📦 **СВОДКА ДЛЯ КАРГО:**\n• Общий вес: {t_weight} кг\n• Объем: {t_vol:.2f} м³\n• Мест: {t_pieces}\n• Плотность: {density} кг/м³\n\n*Скинь это менеджеру Карго, чтобы узнать тариф.*"
        await query.edit_message_text(msg, parse_mode='Markdown')
        await query.message.reply_text("1️⃣ Введи **Тариф Карго** (твоя себестоимость, $/кг):", parse_mode='Markdown')
        return CG_T_CARGO

async def cg_t_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: orders[uid]['cg_tc'] = float(update.message.text.replace(',', '.'))
    except: await update.message.reply_text("❌ Введи число:"); return CG_T_CARGO
    await update.message.reply_text("2️⃣ Введи Тариф для Клиента ($/кг):")
    return CG_T_CLIENT

async def cg_t_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: orders[uid]['cg_tcl'] = float(update.message.text.replace(',', '.'))
    except: await update.message.reply_text("❌ Введи число:"); return CG_T_CLIENT
    await update.message.reply_text("3️⃣ Введи Курс USD → CNY:")
    return CG_R_CNY

async def cg_r_cny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: orders[uid]['cg_rcny'] = float(update.message.text.replace(',', '.'))
    except: await update.message.reply_text("❌ Введи число:"); return CG_R_CNY
    await update.message.reply_text("4️⃣ Введи Курс CNY → AMD:")
    return CG_R_AMD

async def cg_r_amd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: orders[uid]['cg_ramd'] = float(update.message.text.replace(',', '.'))
    except: await update.message.reply_text("❌ Введи число:"); return CG_R_AMD

    cid = orders[uid]['active_cargo_id']
    draft = cargo_drafts[uid][cid]

    t_weight = sum(i['weight'] for i in draft['items'])
    t_vol = sum(i['dims'][0] for i in draft['items'])
    t_pieces = sum(i['pieces'] for i in draft['items'])
    pack_cost = sum(i['pieces'] * i['pack_price'] for i in draft['items'])
    unload_cost = t_pieces * 4.0

    client_weight_usd = t_weight * orders[uid]['cg_tcl']
    client_total_usd = client_weight_usd + pack_cost + unload_cost
    client_total_amd = int(client_total_usd * orders[uid]['cg_rcny'] * orders[uid]['cg_ramd'])

    cargo_total_usd = (t_weight * orders[uid]['cg_tc']) + pack_cost + unload_cost
    cargo_total_cny = int(cargo_total_usd * orders[uid]['cg_rcny'])

    profit_amd = client_total_amd - int(cargo_total_cny * orders[uid]['cg_ramd'])

    draft.update({'t_weight': t_weight, 't_vol': t_vol, 't_pieces': t_pieces, 'density': int(t_weight/t_vol) if t_vol>0 else 0, 'tc': orders[uid]['cg_tc'], 'tcl': orders[uid]['cg_tcl'], 'rcny': orders[uid]['cg_rcny'], 'ramd': orders[uid]['cg_ramd'], 'client_amd': client_total_amd, 'cargo_cny': cargo_total_cny, 'profit_amd': profit_amd})

    msg_client = f"🚛 **CARGO INVOICE: {draft['client'].upper()}**\n🏷 {draft['label']}\n\n**ПАРАМЕТРЫ ГРУЗА:**\n• Вес брутто: {t_weight} кг\n• Объем: {t_vol:.2f} м³\n• Мест: {t_pieces} шт\n\n**РАСЧЕТ СТОИМОСТИ:**\n• Доставка ({t_weight} кг × ${orders[uid]['cg_tcl']}): ${client_weight_usd:.1f}\n• Упаковка и выгрузка: ${pack_cost + unload_cost:.1f}\n\n💵 Итого логистика: ${client_total_usd:.1f}\n🔄 Конвертация: ${client_total_usd:.1f} × {orders[uid]['cg_rcny']} ¥ × {orders[uid]['cg_ramd']} AMD\n✅ **К ОПЛАТЕ: {client_total_amd:,} AMD**"
    await update.message.reply_text(msg_client, parse_mode='Markdown')

    msg_admin = f"💼 **ВНУТРЕННИЙ РАСЧЕТ ({cid}):**\n\n**1. ОТДАЕМ В КАРГО:**\n• Себестоимость (${orders[uid]['cg_tc']}/кг + Услуги): **${cargo_total_usd:.1f}**\n🇨🇳 **Перевести Карго: {cargo_total_cny:,} ¥** *(по курсу {orders[uid]['cg_rcny']})*\n\n**2. ПРИБЫЛЬ:**\n💰 **ЧИСТАЯ ПРИБЫЛЬ: {profit_amd:,} AMD**"
    kb = [[InlineKeyboardButton("📊 Export Excel", callback_data='cg_export_ex')], [InlineKeyboardButton("📑 Export Airtable", callback_data='cg_export_air')], [InlineKeyboardButton("🗑 Завершить и удалить", callback_data='cg_delete')]]
    await update.message.reply_text(msg_admin, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


# ======== ОБЩИЙ ОБРАБОТЧИК ЭКСПОРТА ========
async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'gen_excel':
        try:
            file_stream = await create_excel_invoice(uid)
            await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(file_stream, filename=f"Invoice_{orders[uid]['client']}.xlsx"))
        except: 
            await query.message.reply_text("❌ Ошибка Excel.")
            
    elif query.data == 'export_airtable':
        data = orders.get(uid)
        export_text = f"AIRTABLE_EXPORT_START\nInvoice_ID: {data['client']}\nDate: {datetime.now().strftime('%d.%m.%Y')}\nSum_Client_CNY: {data.get('total_cny_netto', 0)}\nReal_Purchase_CNY: {sum(i.get('purchase', 0) * i.get('qty', 0) for i in data.get('items', []))}\nClient_Rate: {data.get('client_rate', 58.0)}\nReal_Rate: {data.get('real_rate', 55.0)}\nTotal_Qty: {sum(i.get('qty', 0) for i in data.get('items', []))}\nChina_Logistics_CNY: {sum(i.get('delivery_factory', 0) for i in data.get('items', []))}\nFF_Boxes_Qty: {data.get('ff_boxes_qty', 0)}\nAIRTABLE_EXPORT_END"
        await query.message.reply_text(f"<code>{export_text}</code>", parse_mode='HTML')
        
    elif query.data in ['paste_new', 'paste_update', 'paste_save_direct']:
        if query.data == 'paste_update': 
            orders[uid]['notion_page_id'] = orders[uid].get('existing_notion_page_id')
        elif query.data == 'paste_new': 
            orders[uid]['notion_page_id'] = None
            
        url = await save_to_notion(uid)
        try: 
            await query.edit_message_text(f"{query.message.text}\n\n✅ Сохранено:\n{url}" if url else f"{query.message.text}\n\n❌ Ошибка Notion")
        except: 
            await query.message.reply_text(f"✅ Сохранено:\n{url}" if url else "❌ Ошибка Notion")

    elif query.data == 'cg_export_air':
        cid = orders[uid].get('active_cargo_id')
        draft = cargo_drafts[uid][cid]
        export_text = f"AIRTABLE_EXPORT_START\nParty_ID: {cid}\nDate: {datetime.now().strftime('%d.%m.%Y')}\nTotal_Weight_KG: {draft['t_weight']}\nTotal_Volume_CBM: {draft['t_vol']:.2f}\nTotal_Pieces: {draft['t_pieces']}\nDensity: {draft['density']}\nPackaging_Type: Сборная\nTariff_Cargo_USD: {draft['tc']}\nTariff_Client_USD: {draft['tcl']}\nRate_USD_CNY: {draft['rcny']}\nRate_USD_AMD: {draft['ramd']}\nTotal_Client_AMD: {draft['client_amd']}\nTotal_Cargo_CNY: {draft['cargo_cny']}\nNet_Profit_AMD: {draft['profit_amd']}\nAIRTABLE_EXPORT_END"
        await query.message.reply_text(f"```text\n{export_text}\n```", parse_mode='Markdown')
        
    elif query.data == 'cg_export_ex':
        cid = orders[uid].get('active_cargo_id')
        draft = cargo_drafts[uid][cid]
        items_data = []
        for i, item in enumerate(draft['items'], 1): 
            items_data.append({"№": i, "Название товара": item['name'], "Упаковка": item['pack_type'], "Места (шт)": item['pieces'], "Вес (кг)": item['weight'], "Объем (м³)": item['dims'][0]})
        items_data.extend([{"№": "", "Название товара": "ИТОГО ПО ГРУЗУ:", "Упаковка": "", "Места (шт)": draft['t_pieces'], "Вес (кг)": draft['t_weight'], "Объем (м³)": f"{draft['t_vol']:.2f}"}])
        
        df = pd.DataFrame(items_data)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer: 
            df.to_excel(writer, index=False, sheet_name='Packing_List')
        output.seek(0)
        await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(output, filename=f"Cargo_{draft['client']}.xlsx"))

    elif query.data == 'dn_export_ex':
        items_data = []
        for w in orders[uid]['dn_wh_list']:
            if w['pallets'] > 0:
                items_data.append({
                    "Описание услуги": f"Доставка на склад: {w['city']} (Паллеты)", 
                    "Количество": f"{w['pallets']} шт", 
                    "Тариф (RUB)": f"{w['pallet_price']} ₽", 
                    "Сумма (RUB)": f"{w['pallets'] * w['pallet_price']} ₽"
                })
            if w['rem_boxes'] > 0:
                items_data.append({
                    "Описание услуги": f"Доставка на склад: {w['city']} (Короба)", 
                    "Количество": f"{w['rem_boxes']} шт", 
                    "Тариф (RUB)": f"{w['box_price']} ₽", 
                    "Сумма (RUB)": f"{w['rem_boxes'] * w['box_price']} ₽"
                })
            
        items_data.append({"Описание услуги": "Приемка товара коробами", "Количество": f"{orders[uid]['dn_total_boxes']} шт", "Тариф (RUB)": "100 ₽", "Сумма (RUB)": f"{orders[uid]['dn_total_boxes']*100} ₽"})
        items_data.append({"Описание услуги": "Разбор коробов", "Количество": f"{orders[uid]['dn_total_boxes']} шт", "Тариф (RUB)": "50 ₽", "Сумма (RUB)": f"{orders[uid]['dn_total_boxes']*50} ₽"})
        items_data.append({"Описание услуги": "Забор груза (ЮВ) (Грузчики/заезд/доставка)", "Количество": "1 услуга", "Тариф (RUB)": "9000 ₽", "Сумма (RUB)": "9000 ₽"})
        
        items_data.extend([
            {"Описание услуги": "ИТОГО ЛОГИСТИКА (RUB):", "Количество": "", "Тариф (RUB)": "", "Сумма (RUB)": f"{orders[uid]['dn_total_rub']} ₽"}, 
            {"Описание услуги": "КУРС КОНВЕРТАЦИИ:", "Количество": "", "Тариф (RUB)": "", "Сумма (RUB)": str(orders[uid]['dn_rate'])}, 
            {"Описание услуги": "ИТОГО К ОПЛАТЕ (AMD):", "Количество": "", "Тариф (RUB)": "", "Сумма (RUB)": f"{orders[uid]['dn_total_amd']} ֏"}
        ])
        
        df = pd.DataFrame(items_data)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Dostavka_Invoice')
            writer.sheets['Dostavka_Invoice'].set_column('A:A', 40)
            writer.sheets['Dostavka_Invoice'].set_column('B:D', 18)
        output.seek(0)
        await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(output, filename=f"Dostavka_{orders[uid]['dn_client']}.xlsx"))

    elif query.data == 'dn_export_airtable':
        data = orders.get(uid, {})
        destinations = ", ".join([w['city'] for w in data.get('dn_wh_list', [])])
        export_text = f"AIRTABLE_DOSTAVKA_START\nClient_ID: {data.get('dn_client', 'Unknown')}\nDate: {datetime.now().strftime('%d.%m.%Y')}\nTotal_Boxes: {data.get('dn_total_boxes', 0)}\nDestinations: {destinations}\nLogistics_RUB: {data.get('dn_total_rub', 0)}\nRate_RUB_AMD: {data.get('dn_rate', 0)}\nTotal_Client_AMD: {data.get('dn_total_amd', 0)}\nAIRTABLE_DOSTAVKA_END"
        await query.message.reply_text(f"```text\n{export_text}\n```", parse_mode='Markdown')

    elif query.data == 'dn_delete':
        await query.edit_message_text(f"{query.message.text}\n\n✅ Расчет отменен и удален.")

# ======== MAIN ========
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler('start', cmd_menu))
    app.add_handler(CommandHandler('menu', cmd_menu))
    app.add_handler(CommandHandler('cancel', cancel))
    app.add_handler(CallbackQueryHandler(guide_open, pattern='^guide_open$'))
    app.add_handler(CallbackQueryHandler(cmd_menu, pattern='^menu_back$'))
    
    app.add_handler(CommandHandler('paste', cmd_paste))
    app.add_handler(CommandHandler('calc', cmd_calc))
    
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(calc_fill_start, pattern='^calc_fill$')],
        states={
            C_PURCHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_get_purchase)], 
            C_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_get_dims)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('zakaz', cmd_zakaz)],
        states={
            Z_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_name)], 
            Z_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_qty)],
            Z_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_price)], 
            Z_PURCHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_purchase)],
            Z_DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_delivery)], 
            Z_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_dims)],
            Z_MORE: [CallbackQueryHandler(z_more_cb)], 
            Z_CLIENT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_client_rate)],
            Z_REAL_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_real_rate)],
        }, 
        fallbacks=[CommandHandler('cancel', cancel)]
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('ff', cmd_ff)],
        states={
            F_MAIN_MENU: [CallbackQueryHandler(ff_main_menu_cb)], 
            F_SINGLE_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_single_dims)],
            F_BUNDLE_CREATE: [CallbackQueryHandler(ff_bundle_cb)], 
            F_BUNDLE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_name)],
            F_BUNDLE_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_dims)], 
            F_BUNDLE_PACKAGE: [CallbackQueryHandler(ff_bundle_package_cb), MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_price)],
            F_BUNDLE_WORK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_work)], 
            F_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_summary_work)],
        }, 
        fallbacks=[CommandHandler('cancel', cancel)]
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('dostavka', cmd_dostavka)],
        states={
            DN_WH: [CallbackQueryHandler(dn_warehouse_cb)], 
            DN_BOXES: [MessageHandler(filters.TEXT & ~filters.COMMAND, dn_get_boxes)], 
            DN_MORE: [CallbackQueryHandler(dn_more_cb)], 
            DN_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, dn_get_rate)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('dostavka_new', cmd_dostavka_new)],
        states={
            DN_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, dn_get_client)], 
            DN_WH: [CallbackQueryHandler(dn_warehouse_cb)], 
            DN_BOXES: [MessageHandler(filters.TEXT & ~filters.COMMAND, dn_get_boxes)], 
            DN_MORE: [CallbackQueryHandler(dn_more_cb)], 
            DN_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, dn_get_rate)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('cargo', cmd_cargo)],
        states={
            CG_START: [CallbackQueryHandler(cg_start_cb)],
            CG_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_get_client)],
            CG_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_get_label)],
            CG_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_get_item_name)],
            CG_PACK: [CallbackQueryHandler(cg_pack_cb)],
            CG_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_dims_input)],
            CG_MORE_ITEMS: [CallbackQueryHandler(cg_more_items_cb)],
            CG_T_CARGO: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_t_cargo)],
            CG_T_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_t_client)],
            CG_R_CNY: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_r_cny)],
            CG_R_AMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_r_amd)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    
    app.add_handler(CallbackQueryHandler(export_handler, pattern='^gen_excel$|^export_airtable$|^paste_new$|^paste_update$|^paste_save_direct$|^cg_export_|^cg_delete$|^dn_export_ex$|^dn_delete$|^dn_export_airtable$'))
    
    logger.info("Бот запущен. Версия v73 (AIRTABLE FIXED)")
    app.run_polling()

if __name__ == '__main__': 
    main()
