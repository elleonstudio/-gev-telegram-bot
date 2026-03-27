import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, InputFile, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode
from pyairtable import Api

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

TABLE_ORDERS = "Закупка"
TABLE_CARGO = "Логистика Карго"

SYSTEM_MSG_NAMING = "Ты ассистент по именам файлов. Формат: 中文_English_Размер_Артикул_Штрихкод.pdf. Перевод обязателен."

# --- ФУНКЦИИ ---

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
    barcode_num, text, article = "", "", ""
    try:
        codes = decode(image.convert('L'))
        if codes: barcode_num = codes[0].data.decode('utf-8')
    except: pass
    try:
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
    except: pass
    for pattern in [r'Артикул[:\s]+(\d+)', r'Артикул[:\s]*(\d+)', r'Article[:\s]+(\d+)']:
        match = re.search(pattern, text, re.IGNORECASE)
        if match: article = match.group(1); break
    return barcode_num, text, article

async def write_to_airtable(data: dict):
    try:
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
            return f"✅ Партия {data.get('Party_ID')} добавлена!"
    except Exception as e:
        return f"❌ Ошибка Airtable: {str(e)}"
    return "❌ Неизвестный формат данных."

# --- ОБРАБОТЧИКИ ---

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "<b>📂 GS Assistant: Главное меню</b>\n\nВыбери нужную функцию или нажми на Руководство."
    keyboard = [[InlineKeyboardButton("📖 Открыть руководство", callback_data='open_guide')],
                [InlineKeyboardButton("📊 Статус Airtable", callback_data='check_airtable')]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'open_guide':
        guide = "<b>📖 Руководство:</b>\n\n1. /paste — расчеты\n2. /1688 — поставщики\n3. /hs — ТН ВЭД\n4. Фото — штрих-коды\n5. Блок START/END — Airtable"
        await query.edit_message_text(guide, parse_mode='HTML')
    elif query.data == 'check_airtable':
        await query.edit_message_text(f"📊 <b>Airtable OK</b>\nТаблицы: {TABLE_ORDERS}, {TABLE_CARGO}", parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text
    if text.strip().startswith('/calc'): return

    if text.startswith('/paste'):
        raw = text.replace('/paste', '').strip()
        msg = await update.message.reply_text("⏳...")
        res = await ask_kimi(f"Шаблон /calc для: {raw}", system_msg="Ты конвертер. Курс 58/55. Начало: /calc")
        await msg.edit_text(res)
        return

    if "AIRTABLE_EXPORT_START" in text:
        match = re.search(r'AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END', text, re.DOTALL)
        if match:
            data = {k.strip(): v.strip() for k, v in [l.split(':', 1) for l in match.group(1).strip().split('\n') if ':' in l]}
            await update.message.reply_text(await write_to_airtable(data))
        return

    await update.message.reply_text(await ask_kimi(text))

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # ПРОВЕРКА: Фото или Документ?
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id
        else: return

        caption = update.message.caption or ""
        msg = await update.message.reply_text("⏳ Обрабатываю медиа...")
        file = await context.bot.get_file(file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

        if caption.startswith('/1688'):
            res = await ask_kimi("Supplier Info", image_b64=img_b64, system_msg="1688 Expert")
            await msg.edit_text(res)
        elif caption.startswith('/hs'):
            res = await ask_kimi("HS Codes", image_b64=img_b64, system_msg="Broker")
            await msg.edit_text(res)
        else:
            # ШТРИХ-КОДЫ
            image = Image.open(buf)
            barcode, ocr, art = await extract_image_data(image)
            name = await ask_kimi(f"Naming: {ocr}", image_b64=img_b64, system_msg=SYSTEM_MSG_NAMING)
            name = re.sub(r'[\\/*?:"<>|]', '', name.strip()) + ".pdf"
            await msg.edit_text(f"📄 <code>{name}</code>\nBarcode: {barcode}\nArt: {art}", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Media error: {e}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 GS Assistant Online!")))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
