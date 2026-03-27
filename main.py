import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
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

# ОБНОВЛЕННАЯ ЖЕСТКАЯ ИНСТРУКЦИЯ ДЛЯ НЕЙРОСЕТИ (Без скобок, с требованием перевода)
SYSTEM_MSG_DETAILED = (
    "Ты эксперт по логистике. Разбери этикетку строго по шаблону ниже.\n"
    "ВНИМАНИЕ: Не используй скобки! В строке ФАЙЛ ты ОБЯЗАН сделать реальный перевод названия товара на китайский (иероглифы) и английский язык.\n\n"
    "✅ Артикул: значение\n"
    "📝 Детали с этикетки:\n"
    "🔸 Товар: значение\n"
    "🔸 Цвет: значение или ➖\n"
    "🔸 Размер: значение или ➖\n"
    "🔸 Материал: значение или ➖\n"
    "🔸 Комплект: значение или ➖\n"
    "🔸 Свойства: значение или ➖\n"
    "🔸 Дата: значение или ➖\n\n"
    "ФАЙЛ: ПереводНаКитайский_ПереводНаАнглийский_Артикул"
)

# --- ПРОВЕРКА ШТРИХ-КОДА ---
def is_ean13_valid(code: str) -> bool:
    if not code or len(code) != 13 or not code.isdigit(): return False
    digits = [int(d) for d in code]
    even_sum = sum(digits[1:12:2]) * 3
    odd_sum = sum(digits[0:12:2])
    check_digit = (10 - ((even_sum + odd_sum) % 10)) % 10
    return check_digit == digits[12]

# --- РАБОТА С AI ---
async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64: content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post('https://api.moonshot.cn/v1/chat/completions', 
                headers=headers, json={'model': model, 'messages': [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}], 'temperature': 0.0}, timeout=30) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    return res['choices'][0]['message']['content']
        return "Error_API"
    except: return "Error_Timeout"

# --- ОБРАБОТКА ИЗОБРАЖЕНИЙ ---
async def process_image(img_pil):
    barcode, ocr_text = "➖", ""
    try:
        codes = decode(img_pil.convert('L'))
        if codes: barcode = codes[0].data.decode('utf-8')
    except: pass
    try:
        ocr_text = pytesseract.image_to_string(img_pil, lang='rus+eng+chi_sim', config='--oem 3 --psm 6')
    except: pass
    
    img_byte_arr = BytesIO()
    img_pil.convert('RGB').save(img_byte_arr, format='JPEG')
    b64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
    
    analysis = await ask_kimi(f"Этикетка: {ocr_text}", image_b64=b64, system_msg=SYSTEM_MSG_DETAILED)
    return barcode, analysis

# --- ОБРАБОТЧИКИ КОМАНД ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Операция прервана.", reply_markup=ReplyKeyboardRemove())

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "<b>📂 GS Assistant: Главное меню</b>"
    kb = [[InlineKeyboardButton("📖 Руководство", callback_data='help')]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

# --- ОБРАБОТЧИК МЕДИА ---
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("⏳ Начинаю обработку...")
    try:
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id
        else:
            return await status_msg.edit_text("❌ Формат не поддерживается.")

        tg_file = await context.bot.get_file(file_id)
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)

        images = []
        if update.message.document and update.message.document.mime_type == 'application/pdf':
            images = convert_from_bytes(buf.read(), dpi=200)
        else:
            images = [Image.open(buf)]

        await status_msg.edit_text(f"📦 <b>Страниц: {len(images)}</b>\n⏳ Нейросеть делает перевод и изучает этикетки...", parse_mode='HTML')

        reports = []
        first_file_name = "Product.pdf"

        for i, img in enumerate(images):
            barcode, analysis = await process_image(img)
            ean_info = "(EAN-13 верен)" if is_ean13_valid(barcode) else "(Читается)"
            if barcode == "➖": ean_info = ""

            file_prefix = "Product"
            new_analysis_lines = []
            
            for line in analysis.split('\n'):
                line = line.strip()
                if not line:
                    new_analysis_lines.append("")
                    continue
                    
                if line.startswith('ФАЙЛ:'):
                    file_prefix = line.replace('ФАЙЛ:', '').strip()
                    continue
                    
                if line.startswith('✅ Артикул:'):
                    art_val = line.replace('✅ Артикул:', '').strip()
                    digits = re.sub(r'\D', '', art_val)
                    wb_link = f" 👉 <a href='https://www.wildberries.ru/catalog/{digits}/detail.aspx'>Посмотреть на WB</a>" if digits else ""
                    new_analysis_lines.append(f"✅ Артикул: {art_val}{wb_link}")
                else:
                    new_analysis_lines.append(line)

            # Чистим имя файла от запрещенных символов
            file_prefix = re.sub(r'[\\/*?:"<>|]', '', file_prefix).replace(' ', '')
            if not file_prefix or file_prefix == "➖": file_prefix = "Product"
            
            current_file_name = f"{file_prefix}_{barcode}.pdf"
            if i == 0: first_file_name = current_file_name
            
            clean_text = "\n".join(new_analysis_lines).strip()
            page_header = "" if len(images) == 1 else f"📑 <b>Страница {i+1}:</b>\n"
            
            report = f"{page_header}✅ Штрих-код: {barcode} {ean_info}\n{clean_text}\n\n📄 <code>{current_file_name}</code>"
            reports.append(report)

        # Вывод текста
        final_text = f"📦 <b>Страниц: {len(images)}</b>\n\n" + "\n\n---\n\n".join(reports)
        await update.message.reply_text(final_text, parse_mode='HTML', disable_web_page_preview=True)

        # Вывод переименованного PDF файла
        buf.seek(0)
        if not (update.message.document and update.message.document.mime_type == 'application/pdf'):
            pdf_buf = BytesIO()
            images[0].convert('RGB').save(pdf_buf, format='PDF')
            pdf_buf.seek(0)
            await update.message.reply_document(document=pdf_buf, filename=first_file_name, caption="💾 Файл переименован и готов!")
        else:
            await update.message.reply_document(document=buf, filename=first_file_name, caption="💾 Файл переименован и готов!")
        
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Media Error: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🤖 GS Assistant Online!")))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("cancel", cancel))
    
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
