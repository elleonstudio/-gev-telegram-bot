import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, InputFile, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode
from pyairtable import Api

# --- НАСТРОЙКИ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

TABLE_ORDERS = "Закупка"
TABLE_CARGO = "Логистика Карго"
TABLE_DOSTAVKA = "Доставка в РФ"

SYSTEM_MSG_NAMING = (
    "Ты — эксперт по логистике в Китае. Твоя задача — создать имя файла для китайского фулфилмента. "
    "Формат СТРОГО: [Описание на китайском]_[Description in English]_[Размер]_[Артикул]_[Штрихкод]. "
    "В описании ОБЯЗАТЕЛЬНО укажи: что это за товар, его ЦВЕТ и МАТЕРИАЛ (или тип набора). "
    "Пример: 棕色虎纹套装_BrownTigerSet_M_880002359_2049595583930. "
    "Если размера нет, ставь '-'. Выдай только одну строку текста, без лишних слов, без расширения .pdf."
)

# --- ФУНКЦИИ ИИ ---

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64:
        content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}]
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', 
                                 headers=headers, json={'model': model, 'messages': messages, 'temperature': 0.0}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
            return f"Error_{resp.status}"

async def extract_image_data(image: Image.Image):
    barcode_num, text, article = "-", "-", "-"
    try:
        codes = decode(image.convert('L'))
        if codes: barcode_num = codes[0].data.decode('utf-8')
    except: pass
    try:
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
    except: pass
    for pattern in [r'Артикул[:\s]+(\w+)', r'Артикул[:\s]*(\w+)', r'Article[:\s]+(\w+)']:
        match = re.search(pattern, text, re.IGNORECASE)
        if match: article = match.group(1); break
    return barcode_num, text, article

def parse_airtable_block(text: str, start_tag: str, end_tag: str) -> dict:
    parsed = {}
    match = re.search(fr'{start_tag}(.*?){end_tag}', text, re.DOTALL)
    if match:
        for line in match.group(1).strip().split('\n'):
            if ':' in line:
                key, val = line.split(':', 1)
                parsed[key.strip()] = val.strip()
    return parsed

# --- AIRTABLE ЛОГИКА ---

async def write_to_airtable(data: dict):
    api = Api(AIRTABLE_TOKEN)
    def fmt_date(d):
        try: return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")
        except: return datetime.now().strftime("%Y-%m-%d")

    if "Invoice_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_ORDERS)
        full_id = data.get("Invoice_ID", "")
        client_match = re.match(r'^([a-zA-Z]+)', full_id)
        client_name = client_match.group(1).capitalize() if client_match else ""
        record = {
            "Код Карго": full_id, "Клиент": client_name, "Дата": fmt_date(data.get("Date")),
            "Сумма (¥)": float(data.get("Sum_Client_CNY", 0)), "Реал Цена Закупки (¥)": float(data.get("Real_Purchase_CNY", 0)),
            "Курс Клиент": float(data.get("Client_Rate", 58)), "Курс Реал": float(data.get("Real_Rate", 55)),
            "Расход материалов (¥)": float(data.get("China_Logistics_CNY", 0)), "Кол-во коробок": int(data.get("FF_Boxes_Qty", 0))
        }
        table.create(record, typecast=True)
        return f"✅ Выкупы: Заказ {full_id} для {client_name} добавлен!"

    elif "Party_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_CARGO)
        record = {
            "Party_ID": data.get("Party_ID"), "Date": fmt_date(data.get("Date")),
            "Total_Weight_KG": float(data.get("Total_Weight_KG", 0)), "Total_Volume_CBM": float(data.get("Total_Volume_CBM", 0)),
            "Total_Pieces": int(data.get("Total_Pieces", 0)), "Density": int(data.get("Density", 0)),
            "Packaging_Type": data.get("Packaging_Type", "Сборная"), "Tariff_Cargo_USD": float(data.get("Tariff_Cargo_USD", 0)),
            "Tariff_Client_USD": float(data.get("Tariff_Client_USD", 0)), "Rate_USD_CNY": float(data.get("Rate_USD_CNY", 0)),
            "Rate_USD_AMD": float(data.get("Rate_USD_AMD", 0)), "Total_Client_AMD": int(data.get("Total_Client_AMD", 0)),
            "Total_Cargo_CNY": int(data.get("Total_Cargo_CNY", 0)), "Net_Profit_AMD": int(data.get("Net_Profit_AMD", 0)),
            "Logistics_Status": "Выполнен"
        }
        table.create(record, typecast=True)
        return f"✅ Карго: Партия {data.get('Party_ID')} добавлена!"

    elif "Client_ID" in data and "Logistics_RUB" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_DOSTAVKA)
        record = {
            "Клиент / Код заказа": data.get("Client_ID", ""),
            "Дата расчета": fmt_date(data.get("Date")),
            "Количество коробок": int(data.get("Total_Boxes", 0)),
            "Маршрут / Склады": data.get("Destinations", ""),
            "Себестоимость РФ (RUB)": float(data.get("Logistics_RUB", 0)),
            "Курс клиента (RUB/AMD)": float(data.get("Rate_RUB_AMD", 0)),
            "К оплате за доставку (AMD)": int(float(data.get("Total_Client_AMD", 0)))
        }
        table.create(record, typecast=True)
        return f"✅ Доставка РФ: Расчет для {data.get('Client_ID')} добавлен!"

    return "❌ Ошибка: Тип данных не определен."

# --- ОБРАБОТЧИКИ ---

async def perform_audit(update: Update, raw_input: str):
    msg = await update.message.reply_text("⏳ Выполняю точный математический аудит...")
    try:
        # ПРОГРАММНЫЙ КАЛЬКУЛЯТОР (Без нейросети)
        lines = raw_input.strip().split('\n')
        item_pattern = re.compile(r'([\d\.]+)\s*[x×X\*]\s*([\d\.]+)\s*\+\s*([\d\.]+)\s*=\s*([\d\.]+)(.*)')
        
        client_name = lines[0] if lines else ""
        items = []
        user_yuan_sum = 0
        rate = 58
        user_final_amd = 0
        extra = 10000
        
        for line in lines:
            line = line.strip()
            if not line: continue
            
            # Ищем товары
            m = item_pattern.search(line)
            if m:
                items.append({
                    'raw': line,
                    'p': float(m.group(1)),
                    'q': float(m.group(2)),
                    's': float(m.group(3)),
                    'total': float(m.group(4)),
                    'name': m.group(5).strip()
                })
                continue
            
            # Ищем общую сумму и курс
            m_sum = re.search(r'=\s*([\d\.]+)\s*[x×X\*]\s*([\d\.]+)', line)
            if m_sum:
                user_yuan_sum = float(m_sum.group(1))
                rate = float(m_sum.group(2))
                
            # Ищем финальную сумму
            m_final = re.search(r'=\s*([\d\.]+)\s*\+\s*([\d\.]+)\s*=\s*([\d\.]+)', line)
            if m_final:
                extra = float(m_final.group(2))
                user_final_amd = float(m_final.group(3))
        
        if not items:
            await msg.edit_text("❌ Не удалось распознать формат расчета.")
            return

        # Функция для красивого вывода чисел (без .0 в конце)
        def fmt(n): return int(n) if n == int(n) else round(n, 2)
        
        errors_str = ""
        corrected_lines = []
        correct_items_sum = 0
        
        for item in items:
            # ИДЕАЛЬНАЯ МАТЕМАТИКА PYTHON
            c_total = round(item['p'] * item['q'] + item['s'], 2)
            correct_items_sum += c_total
            
            correct_str = f"{fmt(item['p'])}×{fmt(item['q'])}+{fmt(item['s'])}={fmt(c_total)} {item['name']}".strip()
            corrected_lines.append(correct_str)
            
            # Если ошибка больше копейки — фиксируем
            if abs(c_total - item['total']) > 0.01:
                errors_str += f"Было: {item['raw']}\nПравильно: {correct_str}\n\n"
                
        c_pre_amd = round(correct_items_sum * rate, 2)
        c_final_amd = round(c_pre_amd + extra, 2)
        
        if abs(c_final_amd - user_final_amd) > 0.01 or abs(correct_items_sum - user_yuan_sum) > 0.01:
            errors_str += f"Сумма:\nБыло: {fmt(user_final_amd)}֏\nПравильно: {fmt(c_final_amd)}֏\n\n"
            
        diff = abs(c_final_amd - user_final_amd)
        
        # ФОРМИРОВАНИЕ ОТВЕТА
        out = f"/audit-gs\n{raw_input}\n\n"
        if errors_str:
            out += f"❌ Найдены ошибки в расчетах!\n\nСтрока:\n{errors_str.strip()}\n\n💸 Расхождение: {fmt(diff)} ֏\n\n"
        else:
            out += f"✅ Ошибок нет, финальная сумма {fmt(c_final_amd)}֏ верна.\n\n"
            
        # Восстанавливаем строчки с итогами
        sum_parts = [str(fmt(round(i['p']*i['q']+i['s'], 2))) for i in items]
        sum_line1 = f"{'+'.join(sum_parts)}={fmt(correct_items_sum)}×{fmt(rate)}="
        sum_line2 = f"={fmt(c_pre_amd)}+{fmt(extra)}={fmt(c_final_amd)}֏"
        
        out += "✅ Исправленный расчет:\n"
        out += f"{client_name}\n" + "\n".join(corrected_lines) + "\n\n"
        out += f"{sum_line1}\n{sum_line2}"
        
        await msg.edit_text(out)
    except Exception as e:
        await msg.edit_text(f"❌ Системная ошибка проверки: {e}")

async def handle_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    raw_input = text.replace('/paste', '', 1).strip()
    
    if not raw_input:
        await update.message.reply_text("Отправь данные после команды /paste")
        return

    msg = await update.message.reply_text("⏳ Формирую шаблон...")
    
    system_paste = (
        "Ты — технический конвертер данных. Твоя задача: ПЕРЕУПАКОВАТЬ расчет пользователя СТРОГО по шаблону.\n\n"
        "ЛОГИКА РАЗБОРА строки (например '7.5x200+144=1644 vase'):\n"
        "1. Название: vase (текст в конце строки)\n"
        "2. Количество: 200 (число после знака 'x')\n"
        "3. Цена клиенту: 7.5 (самое первое число)\n"
        "4. Доставка: 144 (число после знака '+')\n"
        "ИГНОРИРУЙ любые итоги.\n\n"
        "ФОРМАТ ОТВЕТА СТРОГО ТАКОЙ:\n"
        "/calc\n\n"
        "Клиент: [Имя клиента из первой строки]\n\n"
        "Товар [N]:\n"
        "Название: [Name]\n"
        "Количество: [Qty]\n"
        "Цена клиенту: [Price]\n"
        "Закупка: -\n"
        "Доставка: [Logistics]\n"
        "Размеры: - - - -\n\n"
        "(повтори для всех товаров)\n\n"
        "Курс клиенту: 58\n"
        "Мой курс: 55"
    )
    
    try:
        res = await ask_kimi(f"Оформи это:\n{raw_input}", system_msg=system_paste)
        res = res.replace("(calc", "/calc").replace("(/calc", "/calc")
        if res.endswith(")"):
            res = res[:-1]
        await msg.edit_text(res.strip())
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка ИИ: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    
    # Игнорируем команды и вывод самого бота
    if text.strip().startswith('/calc'): return
    if text.strip().startswith('/audit-gs'): return

    if "AIRTABLE_EXPORT_START" in text:
        data = parse_airtable_block(text, "AIRTABLE_EXPORT_START", "AIRTABLE_EXPORT_END")
        if data:
            status = await write_to_airtable(data)
            await update.message.reply_text(status)
        return

    if "AIRTABLE_DOSTAVKA_START" in text:
        data = parse_airtable_block(text, "AIRTABLE_DOSTAVKA_START", "AIRTABLE_DOSTAVKA_END")
        if data:
            status = await write_to_airtable(data)
            await update.message.reply_text(status)
        return

    # АВТОМАТИЧЕСКИЙ АУДИТ (теперь использует чистую математику Python)
    if re.search(r'\d+(\.\d+)?\s*[x×X]\s*\d+', text) and '=' in text and '+' in text and '\n' in text:
        await perform_audit(update, text)
        return

    # Обычное общение с ИИ
    resp = await ask_kimi(text)
    await update.message.reply_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO(); await file.download_to_memory(buf)
    img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    if caption.startswith('/1688'):
        res = await ask_kimi("Supplier Info CN/EN.", image_b64=img_b64, system_msg="1688 Expert.")
        await update.message.reply_text(res)
    elif caption.startswith('/hs'):
        res = await ask_kimi("Suggest 3 HS Codes.", image_b64=img_b64, system_msg="Broker.")
        await update.message.reply_text(res)
    else:
        barcode, ocr_text, art = await extract_image_data(Image.open(buf))
        prompt = (
            f"Текст с этикетки: {ocr_text}. Артикул: {art}. Штрихкод: {barcode}. "
            f"Внимательно изучи текст и выдели ГЛАВНОЕ для китайского рабочего: что за товар, какой цвет и материал/набор. "
            f"Сформируй имя файла строго по шаблону."
        )
        new_name_raw = await ask_kimi(prompt, image_b64=img_b64, system_msg=SYSTEM_MSG_NAMING)
        final_name = re.sub(r'[\\/*?:"<>|]', '', new_name_raw.strip()) + ".pdf"
        await update.message.reply_text(f"✅ **Готово для склада!**\n📄 `{final_name}`\nBarcode: {barcode}\nArt: {art}")

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = (
        "<b>📂 Меню GS Orders Bot:</b>\n\n"
        "1️⃣ <b>Авто-Аудит</b> - просто перешли любой расчет с математикой, бот сам его проверит!\n"
        "2️⃣ <b>/paste [данные]</b> - перенос расчета в подробный шаблон /calc\n"
        "3️⃣ <b>/1688 [фото]</b> - инфо о поставщике с картинки\n"
        "4️⃣ <b>/hs [фото]</b> - подбор кодов ТН ВЭД\n"
        "5️⃣ <b>Просто фото этикетки</b> - формирует китайское имя файла\n"
        "6️⃣ <b>Экспорт (Airtable)</b>: перешли блок AIRTABLE_EXPORT"
    )
    await update.message.reply_text(menu_text, parse_mode='HTML')

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    commands = [
        BotCommand("start", "Запустить"),
        BotCommand("menu", "Показать все функции"),
        BotCommand("paste", "Конвертер /calc")
    ]
    
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот готов! Нажми /menu")))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(MessageHandler(filters.Regex(r'^/paste(\s|$)'), handle_paste))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    async def set_commands(application):
        await application.bot.set_my_commands(commands)
    
    app.post_init = set_commands
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
