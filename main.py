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

SYSTEM_MSG_NAMING = "Ты ассистент по именам файлов. Формат: 中文_English_Размер_Артикул_Штрихкод.pdf"

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
    """Извлекает штрих-код, текст и артикул из PIL Image"""
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

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text or text.startswith('/calc'): return

    if text.startswith('/paste'):
        raw_input = text.replace('/paste', '').strip()
        msg = await update.message.reply_text("⏳ Формирую шаблон...")
        system_paste = "Ты робот-конвертер. Перенеси данные в шаблон /calc. НЕ СЧИТАЙ. Закупка: -. Размеры: - - - -. Ответ начни с /calc"
        res = await ask_kimi(f"Заполни шаблон: {raw_input}", system_msg=system_paste)
        await msg.edit_text(res.strip())
        return

    resp = await ask_kimi(text)
    await update.message.reply_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        caption = update.message.caption or ""
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        img_bytes = buf.getvalue()
        img_b64 = base64.b64encode(img_bytes).decode('utf-8')

        if caption.startswith('/1688'):
            res = await ask_kimi("Extract supplier info. Use code blocks.", image_b64=img_b64, system_msg="1688 Expert.")
            return await update.message.reply_text(res, parse_mode='Markdown')
        
        if caption.startswith('/hs'):
            res = await ask_kimi(f"Suggest 3 HS Codes for: {caption}", image_b64=img_b64, system_msg="Broker.")
            return await update.message.reply_text(res)

        # ОБЫЧНАЯ ОБРАБОТКА ЭТИКЕТКИ (Если нет команд)
        msg = await update.message.reply_text("⏳ Читаю штрих-код на фото...")
        barcode, text, article = await extract_data_from_image(Image.open(BytesIO(img_bytes)))
        
        new_name = await ask_kimi(f"Придумай имя файла. Текст с фото: {text}", image_b64=img_b64, system_msg=SYSTEM_MSG_NAMING)
        new_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()) + ".pdf"
        
        res_text = f"📄 `{new_name}`\n\n✅ Штрих-код: {barcode if barcode else 'Не найден'}\n✅ Артикул: {article if article else 'Не найден'}"
        await msg.edit_text(res_text, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error in handle_photo: {e}")

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        if not doc.file_name.lower().endswith('.pdf'): return
        
        msg = await update.message.reply_text("⏳ Анализирую PDF (проверяю все страницы)...")
        
        file = await context.bot.get_file(doc.file_id)
        pdf_buf = BytesIO()
        await file.download_to_memory(pdf_buf)
        
        # Конвертируем все страницы
        images = convert_from_bytes(pdf_buf.getvalue(), dpi=200)
        
        unique_results = []
        seen_barcodes = set()

        for img in images:
            barcode, text, article = await extract_data_from_image(img)
            identifier = barcode if barcode else article # Используем штрихкод или артикул как ID
            
            if identifier and identifier not in seen_barcodes:
                new_name = await ask_kimi(f"Имя файла для: {text[:500]}", system_msg=SYSTEM_MSG_NAMING)
                new_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()) + ".pdf"
                unique_results.append(f"📄 `{new_name}`\nШтрих-код: {barcode}\nАртикул: {article}")
                seen_barcodes.add(identifier)

        if not unique_results:
            await msg.edit_text("❌ В PDF не найдено штрих-кодов или артикулов.")
        else:
            final_text = "✅ **Результаты анализа PDF:**\n\n" + "\n\n---\n\n".join(unique_results)
            await msg.edit_text(final_text, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error in handle_doc: {e}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот готов!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
