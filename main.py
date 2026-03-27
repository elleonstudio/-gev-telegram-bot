import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
    "Ты эксперт по складской логистике. Разори текст этикетки.\n"
    "Ответ строго по шаблону:\n"
    "✅ Артикул: [артикул]\n"
    "📝 Детали с этикетки:\n"
    "🔸 Товар: [что это]\n"
    "🔸 Цвет: [цвет или ➖]\n"
    "🔸 Размер: [размер или ➖]\n"
    "🔸 Материал: [материал или ➖]\n"
    "🔸 Комплект: [комплект или ➖]\n"
    "🔸 Свойства: [свойства или ➖]\n"
    "🔸 Дата: [дата или ➖]\n\n"
    "ФАЙЛ: [中文_English_Артикул]"
)

# --- УТИЛИТЫ ---

def is_ean13_valid(code: str) -> bool:
    if not code or len(code) != 13 or not code.isdigit(): return False
    digits = [int(d) for d in code]
    even_sum = sum(digits[1:12:2]) * 3
    odd_sum = sum(digits[0:12:2])
    check_digit = (10 - ((even_sum + odd_sum) % 10)) % 10
    return check_digit == digits[12]

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64: content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post('https://api.moonshot.cn/v1/chat/completions', 
                                     headers=headers, json={'model': model, 'messages': [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}], 'temperature': 0.0}) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    return res['choices'][0]['message']['content']
        return "Error"
    except: return "Error"

async def process_single_image(img_pil):
    barcode, ocr_text = "➖", ""
    try:
        codes = decode(img_pil.convert('L'))
        if codes: barcode = codes[0].data.decode('utf-8')
    except: pass
    try:
        ocr_text = pytesseract.image_to_string(img_pil, lang='rus+eng+chi_sim', config='--oem 3 --psm 6')
    except: pass
    
    # Конвертация для Kimi
    img_byte_arr = BytesIO()
    img_pil.convert('RGB').save(img_byte_arr, format='JPEG')
    b64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
    
    analysis = await ask_kimi(f"Этикетка: {ocr_text}", image_b64=b64, system_msg=SYSTEM_MSG_DETAILED)
    return barcode, analysis

# --- ОБРАБОТЧИКИ ---

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "<b>📂 GS Assistant: Главное меню</b>\n\nВыбери нужную функцию:"
    kb = [[InlineKeyboardButton("📖 Руководство", callback_data='help')],
          [InlineKeyboardButton("📊 Статус Airtable", callback_data='status')]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = await update.message.reply_text("⏳ Начинаю обработку...")
        file_id = update.message.photo[-1].file_id if update.message.photo else update.message.document.file_id
        tg_file = await context.bot.get_file(file_id)
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)

        images = []
        if update.message.document and update.message.document.mime_type == 'application/pdf':
            images = convert_from_bytes(buf.read(), dpi=200)
        else:
            images = [Image.open(buf)]

        await msg.edit_text(f"📦 <b>Страниц в файле: {len(images)}</b>\n⏳ Обрабатываю данные...", parse_mode='HTML')

        all_reports = []
        final_file_name = "document.pdf"

        for i, img in enumerate(images):
            barcode, analysis = await process_single_image(img)
            
            # Валидация EAN
            ean_status = "(EAN-13 верен)" if is_ean13_valid(barcode) else "(Читается)"
            if barcode == "➖": ean_status = ""

            # Ссылка WB
            wb_link = ""
            art_match = re.search(r'Артикул:\s*([^\n🔸]+)', analysis)
            if art_match:
                art_val = art_match.group(1).strip().replace('➖', '')
                digits = re.sub(r'\D', '', art_val)
                if digits: wb_link = f" 👉 <a href='https://www.wildberries.ru/catalog/{digits}/detail.aspx'>Посмотреть на WB</a>"

            # Имя файла из первой страницы
            if i == 0:
                name_match = re.search(r'ФАЙЛ:\s*(\S+)', analysis)
                prefix = name_match.group(1) if name_match else "Product"
                final_file_name = f"{prefix}_{barcode}.pdf"

            clean_analysis = analysis.split('ФАЙЛ:')[0].strip()
            report = f"📄 <b>Страница {i+1}:</b>\n✅ Штрих-код: <code>{barcode}</code> {ean_status}\n{clean_analysis}{wb_link}"
            all_reports.append(report)

        # Отправка отчета
        await update.message.reply_text("\n\n---\n\n".join(all_reports), parse_mode='HTML', disable_web_page_preview=True)

        # Отправка файла
        buf.seek(0)
        if not (update.message.document and update.message.document.mime_type == 'application/pdf'):
            pdf_buf = BytesIO()
            images[0].convert('RGB').save(pdf_buf, format='PDF')
            pdf_buf.seek(0)
            await update.message.reply_document(document=pdf_buf, filename=final_file_name)
        else:
            await update.message.reply_document(document=buf, filename=final_file_name)
            
        await msg.delete()

    except Exception as e:
        logger.error(f"Media Error: {e}")
        await update.message.reply_text("❌ Ошибка при обработке файла.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text.startswith('/paste'):
        raw = text.replace('/paste', '').strip()
        res = await ask_kimi(raw, system_msg="Ты конвертер заказов в /calc. Курс 58/55.")
        await update.message.reply_text(res)
    else:
        await update.message.reply_text(await ask_kimi(text))

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # КОМАНДЫ (важно - ПЕРЕД MessageHandler)
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🤖 GS Assistant запущен!")))
    app.add_handler(CommandHandler("menu", show_menu))
    
    # МЕДИА (Фото и PDF)
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))
    
    # ТЕКСТ
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
