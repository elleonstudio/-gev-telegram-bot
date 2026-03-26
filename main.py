import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

SYSTEM_MSG_NAMING = "Ты ассистент по именам файлов. Напиши ТОЛЬКО название в формате: 中文_English_Размер_Артикул_Штрихкод. Без лишних слов."

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ИИ-ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64:
        content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}]
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json={'model': model, 'messages': messages, 'temperature': 0.0}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
            return f"Error_{resp.status}"

async def extract_data_from_image(image: Image.Image):
    barcode_num, text, article = "", "", ""
    try:
        codes = decode(image.convert('L'))
        if codes: barcode_num = codes[0].data.decode('utf-8')
    except: pass
    try:
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
    except: pass
    match = re.search(r'Артикул[:\s]*(\d+)', text, re.IGNORECASE)
    if match: article = match.group(1)
    return barcode_num, text, article

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        if not doc.file_name.lower().endswith('.pdf'): return
        
        status_msg = await update.message.reply_text("⏳ Обрабатываю PDF...")
        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = await file.download_as_bytearray()
        
        images = convert_from_bytes(bytes(pdf_bytes), dpi=150)
        seen_barcodes = {} # barcode: [image, filename]
        
        for i, img in enumerate(images):
            barcode, text, article = await extract_data_from_image(img)
            # Используем штрихкод или артикул как уникальный ключ
            key = barcode if barcode else (article if article else f"unknown_{i}")
            
            if key not in seen_barcodes:
                # Генерируем имя через ИИ
                new_name = await ask_kimi(f"Придумай короткое имя (формат: Китай_English_Размер_Артикул_Штрихкод) для текста: {text[:500]}", system_msg=SYSTEM_MSG_NAMING)
                clean_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()).replace('.pdf', '') + ".pdf"
                seen_barcodes[key] = [img, clean_name]

        # 1. Отправляем PDF файлы
        files_summary = []
        for key, data in seen_barcodes.items():
            img, fname = data[0], data[1]
            pdf_out = BytesIO()
            img.convert('RGB').save(pdf_out, format='PDF')
            pdf_out.seek(0)
            
            await update.message.reply_document(document=InputFile(pdf_out, filename=fname))
            files_summary.append(fname)

        # 2. Отправляем отчет вторым письмом
        summary_text = f"✅ Готово! Найдено уникальных товаров: {len(files_summary)}\n\nСписок файлов:\n" + "\n".join([f"- {n}" for n in files_summary])
        await status_msg.edit_text(summary_text)

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ Ошибка при обработке PDF")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.PDF, handle_doc))
    # Добавь сюда handle_text и handle_photo из прошлых версий
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
