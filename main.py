import os
import logging
import requests
import base64
import re
from io import BytesIO
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
import pytesseract

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

def clean_response(text: str) -> str:
    garbage = [r'ОПРЕДЕЛИ.*?:', r'ВЫБЕРИ.*?:', r'ВЫПОЛНИ.*?:', r'АНАЛИЗИРУЮ.*?:',
               r'РАССУЖДАЮ.*?:', r'---', r'===', r'ВЫВОД:', r'РЕЗУЛЬТАТ:', r'ОТВЕТ:']
    for pattern in garbage:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    return '\n'.join([l.strip() for l in text.split('\n') if l.strip()])

async def ask_kimi(prompt: str, image_b64: str = None) -> str:
    try:
        headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
        system_msg = '''Ты бизнес-ассистент. ПРАВИЛА:
1. Отвечай ТОЛЬКО результатом
2. БЕЗ слов: "ОПРЕДЕЛИ", "ВЫБЕРИ", "ВЫПОЛНИ"
3. БЕЗ вступлений
4. Коротко, по делу'''
        
        if image_b64:
            messages = [
                {'role': 'system', 'content': system_msg},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}}
                ]}
            ]
            model = 'moonshot-v1-8k-vision-preview'
        else:
            messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': prompt}]
            model = 'moonshot-v1-8k'
        
        data = {'model': model, 'messages': messages, 'temperature': 0.1, 'max_tokens': 1500}
        r = requests.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json=data, timeout=60)
        
        if r.status_code == 200:
            return clean_response(r.json()['choices'][0]['message']['content'])
        return f"Ошибка API: {r.status_code}"
    except Exception as e:
        return f"Ошибка: {str(e)}"

async def check_barcodes(file_bytes: BytesIO) -> str:
    try:
        from pyzbar.pyzbar import decode
        
        file_bytes.seek(0)
        images = convert_from_bytes(file_bytes.read(), dpi=200)
        
        results = []
        for i, img in enumerate(images[:3], 1):
            codes = decode(img)
            if codes:
                for code in codes:
                    results.append(f"Стр {i}: {code.data.decode('utf-8')} ✅")
            else:
                results.append(f"Стр {i}: не найден")
        
        return '\n'.join(results) if results else ""
    except Exception as e:
        return f"Ошибка: {e}"

async def ocr_pdf(file_bytes: BytesIO) -> str:
    """OCR через tesseract"""
    try:
        file_bytes.seek(0)
        images = convert_from_bytes(file_bytes.read(), first_page=1, last_page=2, dpi=200)
        
        texts = []
        for img in images:
            text = pytesseract.image_to_string(img, lang='rus+eng')
            if text.strip():
                texts.append(text.strip())
        
        return '\n'.join(texts)
    except Exception as e:
        return f"OCR ошибка: {e}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 Бот для бизнеса\nОтправь PDF с заданием')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        caption = update.message.caption or ""
        
        if doc.file_size > 20*1024*1024:
            await update.message.reply_text('❌ Файл >20MB')
            return
        
        if not doc.file_name.lower().endswith('.pdf'):
            await update.message.reply_text('❌ Только .pdf')
            return
        
        await update.message.reply_text('⏳ Загрузка...')
        
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        
        barcode_check = ""
        text = ""
        
        # Проверка штрих-кодов
        if any(word in caption.lower() for word in ['штрих', 'код', 'barcode']):
            await update.message.reply_text('🔍 Проверяю штрих-коды...')
            barcode_check = await check_barcodes(buf)
            buf.seek(0)
        
        # Пробуем текстовый слой
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(buf)
            text = '\n'.join([p.extract_text() or '' for p in reader.pages[:3]])
        except:
            text = ""
        
        # Если мало текста — OCR
        if len(text.strip()) < 50:
            await update.message.reply_text('🔍 Распознаю текст...')
            ocr_text = await ocr_pdf(buf)
            if not ocr_text.startswith("OCR ошибка"):
                text = ocr_text
            buf.seek(0)
        
        if not text.strip() and not barcode_check:
            await update.message.reply_text('⚠️ Нет данных в PDF')
            return
        
        prompt = f"""Задача: {caption}

Текст документа:
{text[:2500]}

Штрих-коды:
{barcode_check}

Дай ответ:
1. Название товара на русском
2. Новое имя файла (китайский-английский)
3. Штрих-код и статус (работает/не работает)

Только результат, без размышлений."""
        
        await update.message.reply_text('🤖 Анализ...')
        resp = await ask_kimi(prompt)
        
        if barcode_check:
            resp = f"📊 Штрих-коды:\n{barcode_check}\n\n📝 {resp}"
        
        await update.message.reply_text(resp[:4000])
        
    except Exception as e:
        logging.error(f"Doc error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {e}')

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        b64 = base64.b64encode(buf.read()).decode()
        resp = await ask_kimi(update.message.caption or "Опиши изображение", b64)
        await update.message.reply_text(resp[:4000])
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {e}')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    resp = await ask_kimi(update.message.text)
    await update.message.reply_text(resp[:4000])

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logging.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
