import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
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

SYSTEM_MSG_DETAILED = (
    "Ты эксперт по складской логистике. Твоя задача — разобрать текст с этикетки товара.\n"
    "Выдай ответ строго по шаблону:\n"
    "✅ Артикул: [артикул]\n"
    "📝 Детали с этикетки:\n"
    "🔸 Товар: [что это]\n"
    "🔸 Цвет: [цвет]\n"
    "🔸 Размер: [размер или ➖]\n"
    "🔸 Материал: [материал или ➖]\n"
    "🔸 Дата: [дата или ➖]\n\n"
    "ФАЙЛ: [中文_English_Артикул]"
)

# --- ФУНКЦИИ ---

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64: content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json={'model': model, 'messages': messages, 'temperature': 0.0}) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    return res['choices'][0]['message']['content']
                return "Error"
    except: return "Error"

async def extract_image_data(image: Image.Image):
    barcode, text, art = "➖", "", "➖"
    try:
        codes = decode(image.convert('L'))
        if codes: barcode = codes[0].data.decode('utf-8')
    except: pass
    try: text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config='--oem 3 --psm 6')
    except: pass
    match = re.search(r'Артикул[:\s]*(\S+)', text, re.IGNORECASE)
    if match: art = match.group(1)
    return barcode, text, art

# --- ОБРАБОТЧИК МЕДИА ---

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        is_pdf = False
        if update.message.photo: file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id
            if update.message.document.mime_type == 'application/pdf': is_pdf = True
        else: return

        msg = await update.message.reply_text("⏳ Анализирую этикетку...")
        file = await context.bot.get_file(file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        
        if is_pdf:
            buf.seek(0)
            images = convert_from_bytes(buf.read(), dpi=200, first_page=1, last_page=1)
            img = images[0]
        else: img = Image.open(buf)

        img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        barcode, ocr_raw, art_simple = await extract_image_data(img)

        # Запрос к ИИ для красивого разбора
        analysis = await ask_kimi(f"Разбери детали этикетки: {ocr_raw}", image_b64=img_b64, system_msg=SYSTEM_MSG_DETAILED)
        
        # Формируем ссылку на WB если артикул найден
        wb_link = ""
        art_match = re.search(r'Артикул:\s*(\S+)', analysis)
        if art_match:
            art_val = art_match.group(1).lower().replace('➖', '')
            if art_val and any(c.isdigit() for c in art_val):
                wb_digits = re.sub(r'\D', '', art_val)
                if wb_digits: wb_link = f" 👉 <a href='https://www.wildberries.ru/catalog/{wb_digits}/detail.aspx'>Посмотреть на WB</a>"

        # Формируем финальное имя файла
        file_name_part = "Product"
        name_match = re.search(r'ФАЙЛ:\s*(\S+)', analysis)
        if name_match: file_name_part = name_match.group(1)
        final_file_name = f"{file_name_part}_{barcode}.pdf"

        # Собираем красивый ответ
        clean_analysis = analysis.split('ФАЙЛ:')[0].strip()
        final_text = (
            f"✅ <b>Штрих-код:</b> <code>{barcode}</code> (Читается)\n"
            f"{clean_analysis}{wb_link}\n\n"
            f"📄 <code>{final_file_name}</code>"
        )

        await msg.edit_text(final_text, parse_mode='HTML', disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error: {e}")

# --- ОСТАЛЬНЫЕ ОБРАБОТЧИКИ ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text or text.startswith('/calc'): return
    if text.startswith('/paste'):
        msg = await update.message.reply_text("⏳ Формирую шаблон...")
        res = await ask_kimi(text, system_msg="Ты конвертер заказов в шаблон /calc. Курс 58/55.")
        await msg.edit_text(res)
    else: await update.message.reply_text(await ask_kimi(text))

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 GS Assistant Online!")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
