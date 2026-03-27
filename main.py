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

TARIFFS = {
    'Коледино': 350, 'Невинномысск': 1100, 'Электросталь': 400, 'Белые Столбы': 350,
    'Чашниково': 350, 'Санкт-Петербург': 450, 'Казань': 450, 'Екатеринбург': 700,
    'Новосибирск': 850, 'Владивосток': 1000, 'Краснодар': 550, 'Свой тариф': 0
}

# ======== УТИЛИТЫ ========
def normalize_client_name(name): return re.sub(r'\s+', '', name).strip().capitalize()
def get_code(client): return f"{client.upper()}-{datetime.now().strftime('%y%m%d')}"
def generate_cargo_id():
    import random
    return f"CARGO-{random.randint(100, 999)}"
def optimize_boxes_with_weight(items):
    MAX_L, MAX_W, MAX_H = 60, 40, 40; boxes = []; all_units = []
    for item in items:
        for _ in range(item.get('qty', 0)):
            l, w, h = item.get('dims', (1,1,1))
            all_units.append({'name': item['name'], 'dims': (l, w, h), 'weight': item.get('weight', 0.0), 'vol': l * w * h})
    for unit in all_units:
        placed = False
        for box in boxes:
            if box['rem_vol'] >= unit['vol'] and (box['cur_weight'] + unit['weight']) <= MAX_BOX_WEIGHT:
                box['items'].append(unit); box['rem_vol'] -= unit['vol']; box['cur_weight'] += unit['weight']; placed = True; break
        if not placed: boxes.append({'items': [unit], 'rem_vol': (MAX_L * MAX_W * MAX_H) - unit['vol'], 'cur_weight': unit['weight']})
    return boxes

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
    if missing: audit.append(f"• У <b>{', '.join(missing)}</b> нет размеров. Спрошу в /ff.")
    if audit: await message_obj.reply_text("⚠️ <b>Аудит расчета:</b>\n" + "\n".join(audit), parse_mode='HTML')

    inv_lines = "".join([f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i.get('delivery_factory', 0)} = {(i['price'] * i['qty']) + i.get('delivery_factory', 0):.1f}¥\n" for i in data['items']])
    msg_client = f"<b>COMMERCIAL INVOICE: {data['client'].upper()}</b>\n📅 Date: {datetime.now().strftime('%d.%m.%Y')}\n\n<b>1. ТОВАРНАЯ ВЕДОМОСТЬ</b>\n{inv_lines}<code>────────────────────────</code>\n<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥\n\n<b>2. КОМИССИЯ И СЕРВИС</b>\n({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥\n\n<b>3. ИТОГОВЫЙ РАСЧЕТ</b>\n• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥\n• Курс: {data['client_rate']}\n\n✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"
    await message_obj.reply_text(msg_client, parse_mode='HTML')

    purchase_cny = sum(i.get('purchase', 0) * i['qty'] for i in data['items'])
    total_delivery_cny = sum(i.get('delivery_factory', 0) for i in data['items'])
    real_expenses_amd = int((purchase_cny + total_delivery_cny) * data['real_rate'])
    profit_amd = final_total_amd - real_expenses_amd
    msg_admin = f"💼 <b>ВНУТРЕННИЙ РАСЧЕТ: {data['client'].upper()}</b>\n\n<b>РАСХОДЫ (Курс закупа: {data['real_rate']}):</b>\n• Закупка товара: {purchase_cny:.1f}¥\n• Доставка по Китаю: {total_delivery_cny:.1f}¥\nИтого расход: <b>{real_expenses_amd:,} AMD</b>\n\n<b>ДОХОДЫ:</b>\n• Взяли с клиента: <b>{final_total_amd:,} AMD</b>\n• Комиссия: {actual_comm_amd:,} AMD\n\n💰 <b>ЧИСТАЯ ПРИБЫЛЬ: {profit_amd:,} AMD</b>"
    
    client_orders, _ = await get_client_orders_from_notion(data['client'])
    keyboard = [[InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')], [InlineKeyboardButton("📑 Export Airtable", callback_data='export_airtable')]]
    if client_orders:
        orders[uid]['existing_notion_page_id'] = client_orders[0]['id']
        msg_admin += f"\n\n⚠️ <b>Клиент найден!</b> (От: {client_orders[0].get('date')})"
        keyboard.append([InlineKeyboardButton("🔄 Обновить старый", callback_data='paste_update'), InlineKeyboardButton("➕ Создать НОВЫЙ", callback_data='paste_new')])
    else: keyboard.append([InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_new')])
    await message_obj.reply_text(msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def create_excel_invoice(uid):
    data = orders[uid]
    items_data = []
    for i, item in enumerate(data['items'], 1):
        items_data.append({"№": i, "Название товара": item['name'], "Кол-во (шт)": item['qty'], "Цена (¥)": item['price'], "Логистика (¥)": item.get('delivery_factory', 0), "Итого (¥)": (item['price'] * item['qty']) + item.get('delivery_factory', 0)})
    items_data.extend([{"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "", "Итого (¥)": ""}, {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "SUBTOTAL:", "Итого (¥)": f"{data.get('total_cny_netto', 0):.1f} ¥"}, {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "Комиссия:", "Итого (¥)": f"{data.get('actual_comm_cny', 0):.1f} ¥"}, {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "ИТОГО К ОПЛАТЕ:", "Итого (¥)": f"{data.get('final_total_amd', 0):,} AMD"}])
    df = pd.DataFrame(items_data); output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Invoice'); worksheet = writer.sheets['Invoice']
        worksheet.set_column('A:A', 5); worksheet.set_column('B:B', 35); worksheet.set_column('C:E', 12); worksheet.set_column('F:F', 20)
    output.seek(0); return output

async def get_packages_from_notion():
    if not notion or not PACKAGES_DATABASE_ID: return []
    try:
        res = notion.databases.query(database_id=PACKAGES_DATABASE_ID)
        return [{'name': p['properties'].get('Название', {}).get('title', [{}])[0].get('text', {}).get('content', ''), 'price': p['properties'].get('Цена', {}).get('number', 0)} for p in res.get('results', []) if p['properties'].get('Название', {}).get('title')]
    except: return []

async def get_client_orders_from_notion(client_name):
    if not notion or not NOTION_DATABASE_ID: return None, "Notion не настроен"
    try:
        res = notion.databases.query(database_id=NOTION_DATABASE_ID, sorts=[{"timestamp": "created_time", "direction": "descending"}], page_size=100)
        client_norm = normalize_client_name(client_name).lower()
        filtered = [p for p in res.get('results', []) if p['properties'].get('Клиент', {}).get('select', {}) and normalize_client_name(p['properties']['Клиент']['select'].get('name', '')).lower() == client_norm]
        if not filtered: return [], None
        return [{'id': p['id'], 'date': p.get('created_time', '')[:10]} for p in filtered[:5]], None
    except Exception as e: return None, str(e)

async def save_to_notion(uid):
    if not notion: return None
    try:
        data = orders[uid]
        properties = {"Код заказа": {"title": [{"text": {"content": get_code(data['client'])}}]}, "Клиент": {"select": {"name": data['client']}}, "Количество": {"number": float(sum(i['qty'] for i in data['items']))}, " К ОПЛАТЕ (AMD)": {"number": float(data['final_total_amd'])}, "Статус": {"select": {"name": "Новый"}}, "Date": {"date": {"start": datetime.now().strftime('%Y-%m-%d')}}}
        if 'ff_total_yuan' in data: properties["ИТОГО (CNY)"] = {"number": float(data['ff_total_yuan'])}
        if data.get('notion_page_id'): res = notion.pages.update(page_id=data['notion_page_id'], properties=properties)
        else: res = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties); orders[uid]['notion_page_id'] = res['id']
        return f"https://notion.so/{res['id'].replace('-', '')}"
    except: return None

# ======== ГЛАВНОЕ МЕНЮ (/MENU) ========
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = """🎛 **ГЛАВНОЕ МЕНЮ БОТА**

📦 **ЛОГИСТИКА И КАРГО**
• /cargo — Карго (создание, габариты, тарифы).
• /ff — Фулфилмент (сборка наборов, коробки).
• /dostavka — Доставка по РФ.

🛒 **ВЫКУП ТОВАРОВ**
• /zakaz [Имя] — Ручной пошаговый ввод.
• /paste [Текст] — Умный парсер.
• /calc [Текст] — Быстрый калькулятор.

📊 **ЭКСПОРТ И БАЗЫ ДАННЫХ**
• 📑 **Airtable** (Данные для второго ИИ)
• 📊 **Excel** (Инвойсы)"""
    await update.message.reply_text(menu_text, parse_mode='Markdown')

# ======== ПАРСЕР ТОВАРОВ (/PASTE) ========
def parse_paste_text(text):
    for kw in ['Количество:', 'Цена клиенту:', 'Закупка:', 'Доставка:', 'Размеры:', 'Курс клиенту:', 'Мой курс:']: text = re.sub(f"(?i)({kw})", r"\n\1", text)
    data = {'client': 'Unknown', 'items': [], 'client_rate': 58.0, 'real_rate': 55.0}; current = None
    for line in text.split('\n'):
        l = line.strip().lower()
        if not l: continue
        if 'клиент:' in l: data['client'] = normalize_client_name(l.split('клиент:')[-1])
        elif 'товар' in l:
            if current: data['items'].append(current)
            current = {'name': 'Товар', 'qty': 0, 'price': 0.0, 'purchase': 0.0, 'delivery_factory': 0.0, 'dims': (0,0,0), 'weight': 0.0}
        elif 'название:' in l and current: current['name'] = line[line.lower().find('название:')+9:].strip().title()
        elif 'количество:' in l and current: 
            nums = re.findall(r'\d+', l.split('количество:')[-1])
            if nums: current['qty'] = int(nums[0])
        elif 'цена клиенту:' in l and current: 
            nums = re.findall(r'\d+\.?\d*', l.split('цена клиенту:')[-1].replace(',', '.'))
            if nums: current['price'] = float(nums[0])
        elif 'закупка:' in l and current:
            nums = re.findall(r'\d+\.?\d*', l.split('закупка:')[-1].replace(',', '.'))
            if nums: current['purchase'] = float(nums[0])
        elif 'доставка:' in l and current:
            nums = re.findall(r'\d+\.?\d*', l.split('доставка:')[-1].replace(',', '.'))
            if nums: current['delivery_factory'] = float(nums[0])
        elif 'размеры:' in l and current:
            nums = re.findall(r'\d+\.?\d*', l.split('размеры:')[-1].replace(',', '.'))
            if len(nums) >= 3:
                current['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: current['weight'] = float(nums[3])
        elif 'курс клиенту:' in l:
            nums = re.findall(r'\d+\.?\d*', l.split('курс клиенту:')[-1].replace(',', '.'))
            if nums: data['client_rate'] = float(nums[0])
        elif 'мой курс:' in l:
            nums = re.findall(r'\d+\.?\d*', l.split('мой курс:')[-1].replace(',', '.'))
            if nums: data['real_rate'] = float(nums[0])
    if current: data['items'].append(current)
    return data

async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/paste', '').strip()
    if not text: return
    data = parse_paste_text(text)
    if not data['items']: return await update.message.reply_text("❌ Ошибка: товары не найдены.")
    orders[uid] = data; await finalize_order(uid, update.message)

async def cmd_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/calc', '').strip()
    if not text: return
    data = parse_paste_text(text)
    if not data['items']: return await update.message.reply_text("❌ Ошибка: товары не найдены.")
    subtotal_cny = sum((i['price'] * i['qty']) + i.get('delivery_factory', 0) for i in data['items'])
    comm_amd_3pct = (subtotal_cny * 0.03) * data['client_rate']
    rule_applied = comm_amd_3pct < 10000
    actual_comm_amd = 10000 if rule_applied else int(comm_amd_3pct)
    actual_comm_cny = 10000 / data['client_rate'] if rule_applied else subtotal_cny * 0.03
    final_total_amd = int((subtotal_cny * data['client_rate']) + actual_comm_amd)
    data.update({'final_total_amd': final_total_amd, 'total_cny_netto': subtotal_cny, 'rule_applied': rule_applied, 'actual_comm_cny': actual_comm_cny, 'ff_boxes_qty': 0})
    orders[uid] = data
    inv_lines = "".join([f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i.get('delivery_factory', 0)} = {(i['price'] * i['qty']) + i.get('delivery_factory', 0):.1f}¥\n" for i in data['items']])
    msg_client = f"<b>COMMERCIAL INVOICE: {data['client'].upper()}</b>\n📅 Date: {datetime.now().strftime('%d.%m.%Y')}\n\n<b>1. ТОВАРНАЯ ВЕДОМОСТЬ</b>\n{inv_lines}<code>────────────────────────</code>\n<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥\n\n<b>2. КОМИССИЯ И СЕРВИС</b>\n({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥\n\n<b>3. ИТОГОВЫЙ РАСЧЕТ</b>\n• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥\n• Курс: {data['client_rate']}\n\n✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"
    await update.message.reply_text(msg_client, parse_mode='HTML')
    kb = [[InlineKeyboardButton("✍️ Дополнить расчет", callback_data='calc_fill'), InlineKeyboardButton("📊 Export Excel", callback_data='gen_excel')]]
    await update.message.reply_text("⚠️ <b>Внимание:</b> Для внутреннего расчета не хватает цен закупки и размеров.", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

C_PURCHASE, C_DIMS = range(50, 52)
async def calc_fill_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    missing_idx = -1
    for idx, item in enumerate(orders[uid]['items']):
        if item.get('purchase', 0.0) == 0.0 or item.get('dims', (0,0,0)) == (0,0,0): missing_idx = idx; break
    if missing_idx == -1: await query.message.reply_text("✅ Данные заполнены!"); await finalize_order(uid, query.message); return ConversationHandler.END
    orders[uid]['calc_missing_idx'] = missing_idx; item_name = orders[uid]['items'][missing_idx]['name']
    await query.message.reply_text(f"Введи цену закупки (CNY) для товара <b>{item_name}</b>:", parse_mode='HTML'); return C_PURCHASE
async def c_get_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: val = float(update.message.text.replace(',', '.')); idx = orders[uid]['calc_missing_idx']; orders[uid]['items'][idx]['purchase'] = val; item_name = orders[uid]['items'][idx]['name']; await update.message.reply_text(f"Введи размеры (Д Ш В Вес) для <b>{item_name}</b> (или '-'):", parse_mode='HTML'); return C_DIMS
    except: await update.message.reply_text("❌ Введи число:"); return C_PURCHASE
async def c_get_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); text = update.message.text.strip(); idx = orders[uid]['calc_missing_idx']
    if text != '-':
        try:
            nums = re.findall(r'\d+\.?\d*', text.replace(',', '.'))
            if len(nums) >= 3:
                orders[uid]['items'][idx]['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: orders[uid]['items'][idx]['weight'] = float(nums[3])
        except: await update.message.reply_text("❌ Ошибка:"); return C_DIMS
    missing_idx = -1
    for i, item in enumerate(orders[uid]['items']):
        if item.get('purchase', 0.0) == 0.0 or item.get('dims', (0,0,0)) == (0,0,0): missing_idx = i; break
    if missing_idx == -1: await update.message.reply_text("✅ Все данные собраны!"); await finalize_order(uid, update.message); return ConversationHandler.END
    orders[uid]['calc_missing_idx'] = missing_idx; await update.message.reply_text(f"Цена закупки (CNY) для <b>{orders[uid]['items'][missing_idx]['name']}</b>:", parse_mode='HTML'); return C_PURCHASE

async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'gen_excel':
        file_stream = await create_excel_invoice(uid)
        await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(file_stream, filename=f"Invoice_{orders[uid]['client']}.xlsx"))
    elif query.data == 'export_airtable':
        data = orders.get(uid)
        export_text = f"AIRTABLE_EXPORT_START\nInvoice_ID: {data['client']}\nDate: {datetime.now().strftime('%d.%m.%Y')}\nSum_Client_CNY: {data.get('total_cny_netto', 0)}\nReal_Purchase_CNY: {sum(i.get('purchase', 0) * i.get('qty', 0) for i in data.get('items', []))}\nClient_Rate: {data.get('client_rate', 58.0)}\nReal_Rate: {data.get('real_rate', 55.0)}\nTotal_Qty: {sum(i.get('qty', 0) for i in data.get('items', []))}\nChina_Logistics_CNY: {sum(i.get('delivery_factory', 0) for i in data.get('items', []))}\nFF_Boxes_Qty: {data.get('ff_boxes_qty', 0)}\nAIRTABLE_EXPORT_END"
        await query.message.reply_text(f"<code>{export_text}</code>", parse_mode='HTML')
    elif query.data in ['paste_new', 'paste_update']:
        if query.data == 'paste_update': orders[uid]['notion_page_id'] = orders[uid].get('existing_notion_page_id')
        elif query.data == 'paste_new': orders[uid]['notion_page_id'] = None
        url = await save_to_notion(uid)
        try: await query.edit_message_text(f"{query.message.text}\n\n✅ Сохранено:\n{url}" if url else f"{query.message.text}\n\n❌ Ошибка Notion")
        except: pass

Z_NAME, Z_QTY, Z_PRICE, Z_PURCHASE, Z_DELIVERY, Z_DIMS, Z_MORE, Z_CLIENT_RATE, Z_REAL_RATE = range(40, 49)
async def cmd_zakaz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args: await update.message.reply_text("❌ Напиши: /zakaz Имя"); return ConversationHandler.END
    client = normalize_client_name(' '.join(context.args)); orders[uid] = {'client': client, 'items': [], 'client_rate': 58.0, 'real_rate': 55.0}
    await update.message.reply_text(f"Клиент: {client}\n\nНазвание первого товара:"); return Z_NAME
async def z_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['current'] = {'name': update.message.text.strip().title(), 'dims': (0,0,0), 'weight': 0.0}; await update.message.reply_text("Количество:"); return Z_QTY
async def z_get_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['current']['qty'] = int(re.findall(r'\d+', update.message.text)[0]); await update.message.reply_text("Цена клиенту (CNY):"); return Z_PRICE
async def z_get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['current']['price'] = float(re.findall(r'\d+\.?\d*', update.message.text.replace(',', '.'))[0]); await update.message.reply_text("Закупка (CNY):"); return Z_PURCHASE
async def z_get_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['current']['purchase'] = float(re.findall(r'\d+\.?\d*', update.message.text.replace(',', '.'))[0]); await update.message.reply_text("Доставка до склада (CNY):"); return Z_DELIVERY
async def z_get_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['current']['delivery_factory'] = float(re.findall(r'\d+\.?\d*', update.message.text.replace(',', '.'))[0]); await update.message.reply_text("Размеры (Д Ш В Вес) или '-':"); return Z_DIMS
async def z_get_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); text = update.message.text.strip()
    if text != '-':
        nums = re.findall(r'\d+\.?\d*', text.replace(',', '.'))
        if len(nums) >= 3:
            orders[uid]['current']['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
            if len(nums) >= 4: orders[uid]['current']['weight'] = float(nums[3])
    orders[uid]['items'].append(orders[uid]['current'])
    await update.message.reply_text("Еще товар?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Добавить еще", callback_data='z_more_yes')], [InlineKeyboardButton("❌ Готово, к расчету", callback_data='z_more_no')]])); return Z_MORE
async def z_more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == 'z_more_yes': await query.edit_message_text("Название следующего товара:"); return Z_NAME
    await query.edit_message_text("Укажи курс клиенту:"); return Z_CLIENT_RATE
async def z_client_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['client_rate'] = float(re.findall(r'\d+\.?\d*', update.message.text.replace(',', '.'))[0]); await update.message.reply_text("Реальный курс закупа:"); return Z_REAL_RATE
async def z_real_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['real_rate'] = float(re.findall(r'\d+\.?\d*', update.message.text.replace(',', '.'))[0])
    await finalize_order(uid, update.message); return ConversationHandler.END

# ======== ПОЛНЫЙ МОДУЛЬ CARGO С ЧЕРНОВИКАМИ ========
CG_CLIENT, CG_LABEL, CG_ITEM_NAME, CG_PACK, CG_DIMS, CG_T_CARGO, CG_T_CLIENT, CG_R_CNY, CG_R_AMD = range(60, 69)

async def cmd_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in cargo_drafts: cargo_drafts[uid] = {}
    
    active_parties = cargo_drafts[uid]
    msg = "📂 **Ваши активные партии в Китае:**\n\n"
    keyboard = []
    
    if active_parties:
        for cid, draft in active_parties.items():
            ready = sum(1 for i in draft['items'] if i['pieces'] > 0)
            total = len(draft['items'])
            status = "Готов к расчету" if total > 0 and ready == total else "Ждет данных"
            keyboard.append([InlineKeyboardButton(f"📦 {draft['client']} ({ready}/{total}) - {status}", callback_data=f'cg_open_{cid}')])
    else:
        msg = "📂 У вас нет активных партий.\n\n"
        
    keyboard.append([InlineKeyboardButton("➕ Создать новую партию", callback_data='cg_create_new')])
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def cg_create_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.message.reply_text("👤 Напиши имя клиента (например: Zaven8291):")
    return CG_CLIENT

async def cg_get_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); client = normalize_client_name(update.message.text)
    cid = generate_cargo_id()
    orders[uid] = orders.get(uid, {}); orders[uid]['active_cargo_id'] = cid
    cargo_drafts[uid][cid] = {'client': client, 'label': '', 'items': []}
    await update.message.reply_text("🏷 Напиши метку для груза (например: Одежда и пластик):")
    return CG_LABEL

async def cg_get_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); cid = orders[uid]['active_cargo_id']
    cargo_drafts[uid][cid]['label'] = update.message.text.strip()
    await update.message.reply_text("📦 Отлично! Напиши название ПЕРВОГО товара:")
    return CG_ITEM_NAME

async def cg_get_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); cid = orders[uid]['active_cargo_id']
    item_name = update.message.text.strip().title()
    cargo_drafts[uid][cid]['items'].append({'name': item_name, 'pieces': 0, 'weight': 0.0, 'vol': 0.0, 'dims_list': [], 'pack_type': None, 'pack_price': 0.0})
    orders[uid]['cg_missing_idx'] = len(cargo_drafts[uid][cid]['items']) - 1
    
    kb = [[InlineKeyboardButton("🟡 Мешок ($5)", callback_data='cg_pack_sack')], [InlineKeyboardButton("📦 Уголки ($6)", callback_data='cg_pack_corners')], [InlineKeyboardButton("🪵 Обрешетка ($8)", callback_data='cg_pack_wood')], [InlineKeyboardButton("⏳ Жду данные", callback_data='cg_pack_wait')]]
    await update.message.reply_text(f"Выбери тип упаковки для товара **{item_name}**:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    return CG_PACK

async def cg_pack_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    cid = orders[uid]['active_cargo_id']; idx = orders[uid]['cg_missing_idx']
    
    if query.data == 'cg_pack_wait':
        kb = [[InlineKeyboardButton("➕ Добавить еще товар", callback_data='cg_add_more')], [InlineKeyboardButton("💾 В черновики", callback_data='cg_save_draft')]]
        await query.message.reply_text("⏳ Товар добавлен без габаритов. Что делаем дальше?", reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END

    if query.data == 'cg_pack_sack': 
        cargo_drafts[uid][cid]['items'][idx]['pack_type'] = 'Мешок'; cargo_drafts[uid][cid]['items'][idx]['pack_price'] = 5.0
    elif query.data == 'cg_pack_corners':
        cargo_drafts[uid][cid]['items'][idx]['pack_type'] = 'Уголки'; cargo_drafts[uid][cid]['items'][idx]['pack_price'] = 6.0
    elif query.data == 'cg_pack_wood':
        cargo_drafts[uid][cid]['items'][idx]['pack_type'] = 'Обрешетка'; cargo_drafts[uid][cid]['items'][idx]['pack_price'] = 8.0

    item_name = cargo_drafts[uid][cid]['items'][idx]['name']
    msg = f"""📏 Введи данные для **{item_name}**.
*Если коробок несколько разных, пиши каждую с новой строки.*

Нажми на шаблон ниже, чтобы скопировать его:
`Кол-во Вес Длина Ширина Высота`

*Пример (5 мест, по 20 кг):*
`5 20 80 50 50`
*Если есть только кубы, пиши 3 цифры:*
`5 100 1.2`
*Если данных нет, пиши 0:*
`5 0 0`"""
    await query.message.reply_text(msg, parse_mode='Markdown')
    return CG_DIMS

async def cg_dims_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); cid = orders[uid]['active_cargo_id']; idx = orders[uid]['cg_missing_idx']
    text = update.message.text.strip()
    pack_type = cargo_drafts[uid][cid]['items'][idx]['pack_type']
    
    total_p = 0; total_w = 0.0; total_v = 0.0
    lines = text.split('\n')
    
    try:
        for line in lines:
            nums = tuple(map(float, line.replace(',', '.').split()))
            if len(nums) == 5:
                p, w, l, w_dim, h = nums
                if pack_type == 'Уголки': w += 1.0
                elif pack_type == 'Обрешетка': w += 10.0; l += 5; w_dim += 5; h += 5
                total_p += int(p); total_w += (int(p) * w); total_v += (int(p) * (l * w_dim * h) / 1000000)
            elif len(nums) == 3:
                p, w, v = nums
                total_p += int(p); total_w += w; total_v += v
            else: raise ValueError
            
        cargo_drafts[uid][cid]['items'][idx].update({'pieces': total_p, 'weight': total_w, 'vol': total_v})
    except:
        await update.message.reply_text("❌ Ошибка в данных! Убедись, что в каждой строке 5 или 3 цифры. Попробуй еще раз:"); return CG_DIMS
        
    kb = [[InlineKeyboardButton("➕ Добавить еще товар", callback_data='cg_add_more')], [InlineKeyboardButton("🧮 Рассчитать Карго", callback_data='cg_calc_now')], [InlineKeyboardButton("💾 В черновики", callback_data='cg_save_draft')]]
    await update.message.reply_text("✅ Товар успешно добавлен! Что делаем дальше?", reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

async def cg_routing_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'cg_add_more': await query.message.reply_text("📦 Напиши название следующего товара:"); return CG_ITEM_NAME
    elif query.data == 'cg_save_draft': await query.message.reply_text("💾 Сохранено в черновиках. Для возврата отправь /cargo."); return ConversationHandler.END
    elif query.data == 'cg_calc_now': return await trigger_cargo_summary(query.message, uid)

async def cg_open_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    cid = query.data.replace('cg_open_', '')
    orders[uid] = orders.get(uid, {}); orders[uid]['active_cargo_id'] = cid
    draft = cargo_drafts[uid][cid]
    
    missing_idx = -1
    for i, item in enumerate(draft['items']):
        if item['pieces'] == 0: missing_idx = i; break
        
    if missing_idx != -1:
        orders[uid]['cg_missing_idx'] = missing_idx
        item_name = draft['items'][missing_idx]['name']
        kb = [[InlineKeyboardButton("🟡 Мешок ($5)", callback_data='cg_pack_sack')], [InlineKeyboardButton("📦 Уголки ($6)", callback_data='cg_pack_corners')], [InlineKeyboardButton("🪵 Обрешетка ($8)", callback_data='cg_pack_wood')], [InlineKeyboardButton("⏳ Жду данные", callback_data='cg_pack_wait')]]
        await query.message.reply_text(f"Продолжаем заполнять!\nВыбери тип упаковки для товара **{item_name}**:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
        return CG_PACK
    else:
        return await trigger_cargo_summary(query.message, uid)

async def trigger_cargo_summary(message_obj, uid):
    cid = orders[uid]['active_cargo_id']; draft = cargo_drafts[uid][cid]
    t_weight = sum(i['weight'] for i in draft['items']); t_vol = sum(i['vol'] for i in draft['items']); t_pieces = sum(i['pieces'] for i in draft['items'])
    density = int(t_weight / t_vol) if t_vol > 0 else 0
    msg = f"📦 **СВОДКА ДЛЯ КАРГО:**\n• Общий вес: {t_weight} кг\n• Объем: {t_vol:.2f} м³\n• Мест: {t_pieces}\n• Плотность: {density} кг/м³\n\n*Скинь это менеджеру Карго, чтобы узнать тариф.*"
    kb = [[InlineKeyboardButton("➡️ Ввести тарифы", callback_data='cg_start_calc')]]
    await message_obj.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

async def cg_start_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.message.reply_text("1️⃣ Введи **Тариф Карго** (твоя себестоимость, $/кг):"); return CG_T_CARGO
async def cg_t_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['cg_tc'] = float(update.message.text.replace(',', '.')); await update.message.reply_text("2️⃣ Введи **Тариф для Клиента** ($/кг):"); return CG_T_CLIENT
async def cg_t_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['cg_tcl'] = float(update.message.text.replace(',', '.')); await update.message.reply_text("3️⃣ Введи **Курс USD → CNY** (для Карго):"); return CG_R_CNY
async def cg_r_cny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['cg_rcny'] = float(update.message.text.replace(',', '.')); await update.message.reply_text("4️⃣ Введи **Курс CNY → AMD** (для Клиента):"); return CG_R_AMD

async def cg_r_amd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['cg_ramd'] = float(update.message.text.replace(',', '.'))
    cid = orders[uid]['active_cargo_id']; draft = cargo_drafts[uid][cid]
    
    t_weight = sum(i['weight'] for i in draft['items']); t_vol = sum(i['vol'] for i in draft['items']); t_pieces = sum(i['pieces'] for i in draft['items'])
    pack_cost = sum(i['pieces'] * i['pack_price'] for i in draft['items'])
    unload_cost = t_pieces * 4.0
    
    client_weight_usd = t_weight * orders[uid]['cg_tcl']
    client_total_usd = client_weight_usd + pack_cost + unload_cost
    client_total_amd = int(client_total_usd * orders[uid]['cg_rcny'] * orders[uid]['cg_ramd'])
    
    cargo_weight_usd = t_weight * orders[uid]['cg_tc']
    cargo_total_usd = cargo_weight_usd + pack_cost + unload_cost
    cargo_total_cny = int(cargo_total_usd * orders[uid]['cg_rcny'])
    profit_amd = client_total_amd - int(cargo_total_cny * orders[uid]['cg_ramd'])
    
    draft.update({'t_weight': t_weight, 't_vol': t_vol, 't_pieces': t_pieces, 'density': int(t_weight/t_vol) if t_vol>0 else 0, 'tc': orders[uid]['cg_tc'], 'tcl': orders[uid]['cg_tcl'], 'rcny': orders[uid]['cg_rcny'], 'ramd': orders[uid]['cg_ramd'], 'client_amd': client_total_amd, 'cargo_cny': cargo_total_cny, 'profit_amd': profit_amd})
    
    msg_client = f"""🚛 **CARGO INVOICE: {draft['client'].upper()}**
🏷 {draft['label']}

**ПАРАМЕТРЫ ГРУЗА:**
• Вес брутто: {t_weight} кг | Мест: {t_pieces} шт

**РАСЧЕТ СТОИМОСТИ:**
• Доставка ({t_weight} кг × ${orders[uid]['cg_tcl']}): ${client_weight_usd:.1f}
• Доп. упаковка и выгрузка: ${pack_cost + unload_cost:.1f}

💵 Итого логистика: ${client_total_usd:.1f}
🔄 Конвертация: ${client_total_usd:.1f} × {orders[uid]['cg_rcny']} ¥ × {orders[uid]['cg_ramd']} AMD
✅ **К ОПЛАТЕ: {client_total_amd:,} AMD**"""
    await update.message.reply_text(msg_client, parse_mode='Markdown')
    
    msg_admin = f"""💼 **ВНУТРЕННИЙ РАСЧЕТ ({cid}):**

**1. ОТДАЕМ В КАРГО:**
• Себестоимость (${orders[uid]['cg_tc']}/кг + Услуги): **${cargo_total_usd:.1f}**
🇨🇳 **Перевести Карго: {cargo_total_cny:,} ¥** *(по курсу {orders[uid]['cg_rcny']})*

**2. ДОХОДЫ И ПРИБЫЛЬ:**
• Берем с клиента: {int(client_total_amd/orders[uid]['cg_ramd']):,} ¥ ({client_total_amd:,} AMD)
• Отдаем Карго: {cargo_total_cny:,} ¥
💰 **ЧИСТАЯ ПРИБЫЛЬ: {int(profit_amd/orders[uid]['cg_ramd']):,} ¥ ({profit_amd:,} AMD)**"""
    
    kb = [[InlineKeyboardButton("📊 Export Excel", callback_data='cg_export_ex')], [InlineKeyboardButton("📑 Export Airtable", callback_data='cg_export_air')], [InlineKeyboardButton("🗑 Завершить и удалить", callback_data='cg_delete')]]
    await update.message.reply_text(msg_admin, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

async def cg_export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    cid = orders[uid].get('active_cargo_id'); draft = cargo_drafts[uid].get(cid)
    if not draft: return
    
    if query.data == 'cg_export_air':
        export_text = f"AIRTABLE_EXPORT_START\nParty_ID: {cid}\nDate: {datetime.now().strftime('%d.%m.%Y')}\nTotal_Weight_KG: {draft['t_weight']}\nTotal_Volume_CBM: {draft['t_vol']:.2f}\nTotal_Pieces: {draft['t_pieces']}\nDensity: {draft['density']}\nPackaging_Type: Сборная\nTariff_Cargo_USD: {draft['tc']}\nTariff_Client_USD: {draft['tcl']}\nRate_USD_CNY: {draft['rcny']}\nRate_USD_AMD: {draft['ramd']}\nTotal_Client_AMD: {draft['client_amd']}\nTotal_Cargo_CNY: {draft['cargo_cny']}\nNet_Profit_AMD: {draft['profit_amd']}\nAIRTABLE_EXPORT_END"
        await query.message.reply_text(f"```text\n{export_text}\n```", parse_mode='Markdown')
    elif query.data == 'cg_export_ex':
        items_data = []
        for i, item in enumerate(draft['items'], 1):
            items_data.append({"№": i, "Название товара": item['name'], "Упаковка": item['pack_type'], "Места (шт)": item['pieces'], "Вес (кг)": item['weight'], "Объем (м³)": item['vol']})
        items_data.extend([{"№": "", "Название товара": "", "Упаковка": "", "Места (шт)": "", "Вес (кг)": "", "Объем (м³)": ""}, {"№": "", "Название товара": "ИТОГО ПО ГРУЗУ:", "Упаковка": "", "Места (шт)": draft['t_pieces'], "Вес (кг)": draft['t_weight'], "Объем (м³)": f"{draft['t_vol']:.2f}"}])
        df = pd.DataFrame(items_data); output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer: df.to_excel(writer, index=False, sheet_name='Packing_List')
        output.seek(0); await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(output, filename=f"Cargo_{draft['client']}.xlsx"))
    elif query.data == 'cg_delete':
        del cargo_drafts[uid][cid]; await query.edit_message_text(f"{query.message.text}\n\n✅ **Партия закрыта и удалена из черновиков.**", parse_mode='Markdown')

def cancel(update, context): update.message.reply_text("Отменено."); return ConversationHandler.END

# ======== MAIN ========
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', cmd_menu))
    app.add_handler(CommandHandler('menu', cmd_menu))
    app.add_handler(CommandHandler('paste', cmd_paste))
    app.add_handler(CommandHandler('calc', cmd_calc))
    
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(calc_fill_start, pattern='^calc_fill$')],
        states={C_PURCHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_get_purchase)], C_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_get_dims)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('zakaz', cmd_zakaz)],
        states={
            Z_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_name)], Z_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_qty)],
            Z_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_price)], Z_PURCHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_purchase)],
            Z_DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_delivery)], Z_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_dims)],
            Z_MORE: [CallbackQueryHandler(z_more_cb)], Z_CLIENT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_client_rate)],
            Z_REAL_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_real_rate)],
        }, fallbacks=[CommandHandler('cancel', cancel)]
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('cargo', cmd_cargo), CallbackQueryHandler(cg_create_new, pattern='^cg_create_new$'), CallbackQueryHandler(cg_open_draft, pattern='^cg_open_')],
        states={
            CG_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_get_client)], CG_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_get_label)],
            CG_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_get_item_name)], CG_PACK: [CallbackQueryHandler(cg_pack_cb, pattern='^cg_pack_')],
            CG_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_dims_input)]
        }, fallbacks=[CommandHandler('cancel', cancel)]
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(cg_start_calc, pattern='^cg_start_calc$')],
        states={
            CG_T_CARGO: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_t_cargo)], CG_T_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_t_client)],
            CG_R_CNY: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_r_cny)], CG_R_AMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_r_amd)]
        }, fallbacks=[CommandHandler('cancel', cancel)]
    ))
    
    app.add_handler(CallbackQueryHandler(export_handler, pattern='^gen_excel$|^export_airtable$|^paste_new$|^paste_update$'))
    app.add_handler(CallbackQueryHandler(cg_routing_cb, pattern='^cg_add_more$|^cg_save_draft$|^cg_calc_now$'))
    app.add_handler(CallbackQueryHandler(cg_export_handler, pattern='^cg_export_|^cg_delete$'))
    
    logger.info("Бот запущен. Версия v61 (Cargo Ultimate + Multiline Dims + Separate Calc)")
    app.run_polling()

if __name__ == '__main__': main()
