import os
import logging
import requests
import base64
import re
from io import BytesIO
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
import pytesseract

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

def clean_response(text: str) -> str:
    garbage = [r'^\d+\.', r'ОПРЕДЕЛИ.*?:', r'ВЫБЕРИ.*?:', r'ВЫПОЛНИ.*?:', 
               r'АНАЛИЗИРУЮ.*?:', r'РАССУЖДАЮ.*?:', r'---', r'===', 
               r'ВЫВОД:', r'РЕЗУЛЬТАТ:', r'ОТВЕТ:', r'\*\*\*', r'•']
    for pattern in garbage:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
    return '\n'.join([l.strip() for l in text.split('\n') if l.strip()])

async def ask_kimi(prompt: str, image_b64: str = None) -> str:
    try:
        headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
        system_msg = '''Ты помощник для бизнеса. ПРАВИЛА:
1. Отвечай ТОЛЬКО именем файла
2. БЕЗ слов: "ОПРЕДЕЛИ", "ВЫБЕРИ", "ВЫПОЛНИ", "1.", "2.", "3.", "•"
3. БЕЗ вступлений
4. Формат: Китайский_Английский_Артикул.pdf
5. Пример: 汽车遮阳挡_Car_Sunshade_881532453.pdf'''
        
        if image_b64:
            messages = [{'role': 'system', 'content': system_msg},
                       {'role': 'user', 'content': [{'type': 'text', 'text': prompt}, {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}}]}]
            model = 'moonshot-v1-8k-vision-preview'
        else:
            messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': prompt}]
            model = 'moonshot-v1-8k'
        
        data = {'model': model, 'messages': messages, 'temperature': 0.1, 'max_tokens': 500}
        r = requests.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json=data, timeout=60)
        
        if r.status_code == 200:
            return clean_response(r.json()['choices'][0]['message']['content'])
        return f"Error_API_{r.status_code}.pdf"
    except Exception as e:
        return f"Error_{str(e)[:20]}.pdf"

async def check_barcodes(file_bytes: BytesIO) -> tuple:
    """Проверяет штрих-коды с несколькими попытками DPI"""
    try:
        from pyzbar.pyzbar import decode
        
        # Пробуем разные DPI
        for dpi in [300, 200, 150]:
            try:
                file_bytes.seek(0)
                images = convert_from_bytes(file_bytes.read(), dpi=dpi, first_page=1, last_page=1)
                
                if not images:
                    continue
                
                for img in images:
                    # Конвертируем в grayscale для лучшего распознавания
                    img_gray = img.convert('L')
                    codes = decode(img_gray)
                    
                    if codes:
                        for code in codes:
                            barcode = code.data.decode('utf-8')
                            return f"Стр 1: {barcode} ✅", barcode
                
            except Exception as e:
                continue
        
        return "Штрих-коды не найдены", ""
    except Exception as e:
        return f"Ошибка: {e}", ""

async def ocr_pdf(file_bytes: BytesIO) -> str:
    try:
        file_bytes.seek(0)
        images = convert_from_bytes(file_bytes.read(), first_page=1, last_page=1, dpi=200)
        
        texts = []
        for img in images:
            text = pytesseract.image_to_string(img, lang='rus+eng+chi_sim')
            if text.strip():
                texts.append(text.strip())
        
        return '\n'.join(texts)
    except Exception as e:
        return ""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 Отправь PDF')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        original_name = doc.file_name
        
        if doc.file_size > 20*1024*1024:
            await update.message.reply_text('❌ Файл >20MB')
            return
        
        if not original_name.lower().endswith('.pdf'):
            await update.message.reply_text('❌ Только .pdf')
            return
        
        await update.message.reply_text('⏳ Загрузка...')
        
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        
        # Штрих-коды
        await update.message.reply_text('🔍 Проверяю штрих-коды...')
        barcode_check, barcode_num = await check_barcodes(buf)
        
        # OCR
        await update.message.reply_text('🔍 Распознаю текст...')
        text = await ocr_pdf(buf)
        
        # Генерируем имя
        await update.message.reply_text('🤖 Генерирую имя...')
        
        prompt = f"""Создай имя файла на основе текста:

Текст:
{text[:2000]}

Штрих-код: {barcode_num}

Формат: Китайский_Английский_Артикул.pdf
Пример: 汽车遮阳挡_Car_Sunshade_881532453.pdf

Только имя файла:"""

        new_name = await ask_kimi(prompt)
        
        # Очищаем имя
        new_name = new_name.strip().replace('\n', '_').replace(' ', '_')
        if not new_name.endswith('.pdf'):
            new_name += '.pdf'
        new_name = re.sub(r'[\\/*?:"<>|]', '', new_name)
        if len(new_name) > 120:
            new_name = new_name[:120] + '.pdf'
        
        # Ответ
        response = f"📄 Новое имя: `{new_name}`"
        if barcode_num:
            response = f"📊 Штрих-код: `{barcode_num}` ✅\n\n{response}"
        else:
            response = f"📊 Штрих-код: не найден\n\n{response}"
        
        await update.message.reply_text(response, parse_mode='Markdown')
        
        # Отправляем файл
        buf.seek(0)
        await update.message.reply_document(
            document=InputFile(buf, filename=new_name),
            caption=f"✅ {original_name} → {new_name}"
        )
        
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        b64 = base64.b64encode(buf.read()).decode()
        resp = await ask_kimi(update.message.caption or "Опиши", b64)
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
