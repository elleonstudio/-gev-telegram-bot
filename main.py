import os
import logging
import requests
import base64
import re
from io import BytesIO
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

def clean_response(text: str) -> str:
    garbage = [r'ОПРЕДЕЛИ.*?:', r'ВЫБЕРИ.*?:', r'ВЫПОЛНИ.*?:', r'АНАЛИЗИРУЮ.*?:',
               r'РАССУЖДАЮ.*?:', r'---', r'ВЫВОД:', r'РЕЗУЛЬТАТ:']
    for pattern in garbage:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    return '\n'.join([l.strip() for l in text.split('\n') if l.strip()])

async def ask_kimi(prompt: str, image_b64: str = None) -> str:
    try:
        headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
        system_msg = '''Ты бизнес-ассистент. ПРАВИЛА:
1. Отвечай ТОЛЬКО результатом
2. БЕЗ слов: "ОПРЕДЕЛИ", "ВЫБЕРИ", "ВЫПОЛНИ"
3. Коротко, по делу'''
        
        if image_b64:
            messages = [{'role': 'system', 'content': system_msg},
                       {'role': 'user', 'content': [{'type': 'text', 'text': prompt}, {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}}]}]
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
        from pdf2image import convert_from_bytes
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
        
        return '\n'.join(results) if results else "Штрих-коды не обнаружены"
    except Exception as e:
        return f"Ошибка: {e}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 Бот для бизнеса\nОтправь PDF/фото с заданием')

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
        if 'штрих' in caption.lower() or 'код' in caption.lower() or 'barcode' in caption.lower():
            await update.message.reply_text('🔍 Проверяю штрих-коды...')
            barcode_check = await check_barcodes(buf)
            buf.seek(0)
        
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(buf)
            text = '\n'.join([p.extract_text() or '' for p in reader.pages[:3]])
        except:
            text = ""
        
        if not text and not barcode_check:
            await update.message.reply_text('⚠️ Нет текста в PDF')
            return
        
        prompt = f"Задача: {caption}\n\nТекст:\n{text[:2500]}\n\nКороткий ответ:"
        resp = await ask_kimi(prompt)
        
        if barcode_check:
            resp = f"📊 Штрих-коды:\n{barcode_check}\n\n{resp}"
        
        await update.message.reply_text(resp[:4000])
        
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {e}')

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        b64 = base64.b64encode(buf.read()).decode()
        resp = await ask_kimi(update.message.caption or "Что на фото?", b64)
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
    app.run_polling(drop_pending_updates=True)  # Очищает очередь при старте

if __name__ == '__main__':
    main()
