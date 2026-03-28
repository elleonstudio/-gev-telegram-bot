import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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
TABLE_DELIVERY = "Доставка РФ"

SYSTEM_MSG_NAMING = (
    "Ты — эксперт по логистике в Китае. Создай имя файла для фулфилмента. "
    "Формат: [Описание на китайском]_[Description in English]_[Размер]_[Артикул]_[Штрихкод]. "
    "ОБЯЗАТЕЛЬНО: цвет и материал на китайском в начале! Выдай только строку имени."
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

# --- AIRTABLE ---

async def write_to_airtable(data: dict, data_type: str = "EXPORT"):
    api = Api(AIRTABLE_TOKEN)
    def fmt_date(d):
        try: return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")
        except: return datetime.now().strftime("%Y-%m-%d")

    if data_type == "DOSTAVKA":
        table = api.table(AIRTABLE_BASE_ID, TABLE_DELIVERY)
        record = {
            "Клиент / Код заказа": data.get("Client_ID", ""),
            "Дата расчета": fmt_date(data.get("Date")),
            "Количество коробок": int(data.get("Total_Boxes", 0)),
            "Маршрут / Склады": data.get("Destinations", ""),
            "Себестоимость РФ (RUB)": float(data.get("Logistics_RUB", 0)),
            "Курс клиента (RUB/AMD)": float(data.get("Rate_RUB_AMD", 0)),
            "К оплате за доставку (AMD)": int(data.get("Total_Client_AMD", 0))
        }
        table.create(record, typecast=True)
        return f"✅ Доставка для {data.get('Client_ID')} добавлена!"

    elif "Invoice_ID" in data:
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
        return f"✅ Выкуп для {client_name} добавлен!"

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
        return f"✅ Карго партия {data.get('Party_ID')} добавлена!"

# --- АУДИТ ---

async def run_audit(update: Update, text: str):
    pure_text = text.replace('/audit_gs', '').strip()
    system_audit = (
        "Ты — финансовый аудитор. Ответ ВСЕГДА начинай строго с заголовка /audit_gs.\n\n"
        "1. НЕ ПИШИ свои размышления. Считай молча.\n"
        "2. ЗАПРЕЩЕНО использовать LaTeX \\[ \\].\n"
        "3. Сначала ПОЛНОСТЬЮ выведи оригинальный текст пользователя.\n"
        "Если ВСЁ ВЕРНО: выведи текст + '✅ Ошибок нет, финальная сумма [X]֏ верна.'\n"
        "Если ОШИБКИ: выведи текст + '❌ Найдены ошибки в расчетах!', блоки 'Строка:', 'Сумма:', 'Расхождение:' и '✅ Исправленный расчет:'."
    )
    res = await ask_kimi(pure_text, system_msg=system_audit)
    await update.message.reply_text(res)

# --- МЕНЮ И ОБРАБОТЧИКИ ---

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>📂 Функции GS Orders:</b>\n\n"
        "1️⃣ <code>/audit_gs [текст]</code> — Проверка расчетов (по твоему дизайну)\n"
        "2️⃣ <code>/paste [текст]</code> — Конвертер в шаблон /calc\n"
        "3️⃣ <b>Фото этикетки</b> — Имя файла для фулфилмента (с иероглифами)\n"
        "4️⃣ <code>/1688 [фото]</code> — Поиск поставщика\n"
        "5️⃣ <code>/hs [фото]</code> — Коды ТН ВЭД\n"
        "6️⃣ <b>Airtable</b> — Авто-запись (Выкуп, Карго, Доставка РФ)\n"
        "7️⃣ <code>/cancel</code> — Отмена текущей операции"
    )
    await update.message.reply_text(text, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return

    if text.startswith('/audit_gs'):
        await run_audit(update, text)
        return
    
    if text.startswith('/paste'):
        raw = text.replace('/paste', '').strip()
        res = await ask_kimi(raw, system_msg="Конвертер в /calc. Курс 58/55.")
        await update.message.reply_text(res)
        return

    if text.lower() in ['/cancel', 'cancel', 'отмена']:
        await update.message.reply_text("⛔ Операция отменена.")
        return

    # Airtable теги
    for tag, t_type in [("AIRTABLE_EXPORT_START", "EXPORT"), ("AIRTABLE_DOSTAVKA_START", "DOSTAVKA")]:
        if tag in text:
            match = re.search(f"{tag}(.*?){tag.replace('START', 'END')}", text, re.DOTALL)
            if match:
                parsed = {l.split(':', 1)[0].strip(): l.split(':', 1)[1].strip() for l in match.group(1).strip().split('\n') if ':' in l}
                status = await write_to_airtable(parsed, t_type)
                await update.message.reply_text(status)
            return

    resp = await ask_kimi(text)
    await update.message.reply_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO(); await file.download_to_memory(buf)
    img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    if caption.startswith('/1688'):
        res = await ask_kimi("Supplier Info.", image_b64=img_b64, system_msg="1688 Expert.")
        await update.message.reply_text(res)
    elif caption.startswith('/hs'):
        res = await ask_kimi("HS Codes.", image_b64=img_b64, system_msg="Broker.")
        await update.message.reply_text(res)
    else:
        barcode, ocr, art = await extract_image_data(Image.open(buf))
        name = await ask_kimi(f"Naming: {ocr}.", image_b64=img_b64, system_msg=SYSTEM_MSG_NAMING)
        final = re.sub(r'[\\/*?:"<>|]', '', name.strip()) + ".pdf"
        await update.message.reply_text(f"✅ Для склада:\n📄 `{final}`\n\nBarcode: {barcode}\nArt: {art}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот готов! Нажми /menu")))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
