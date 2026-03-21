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

# ========== KIMI API ==========

async def ask_kimi(prompt: str, image_b64: str = None) -> str:
    """Отправляет запрос в Kimi API"""
    try:
        headers = {
            'Authorization': f'Bearer {KIMI_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        # УЛУЧШЕННОЕ системное сообщение - запрещаем размышления
        system_msg = '''Ты бизнес-ассистент. ПРАВИЛА:
1. Отвечай ТОЛЬКО результатом
2. БЕЗ слов: "ОПРЕДЕЛИ", "ВЫБЕРИ", "ВЫПОЛНИ", "АНАЛИЗИРУЮ", "РАССУЖДАЮ"
3. БЕЗ вступлений и заключений
4. Коротко, по делу
5. Если штрих-коды - проверь и скажи работает/не работает
6. Если файлы - дай конкретные имена'''

        if image_b64:
            messages = [
                {'role': 'system', 'content': system_msg},
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': prompt},
                        {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}}
                    ]
                }
            ]
            model = 'moonshot-v1-8k-vision-preview'
        else:
            messages = [
                {'role': 'system', 'content': system_msg},
                {'role': 'user', 'content': prompt}
            ]
            model = 'moonshot-v1-8k'
        
        data = {
            'model': model,
            'messages': messages,
            'temperature': 0.1,
            'max_tokens': 1500
        }
        
        logging.info(f"Kimi request: {model}")
        
        r = requests.post(
            'https://api.moonshot.cn/v1/chat/completions',
            headers=headers,
            json=data,
            timeout=60
        )
        
        if r.status_code == 200:
            result = r.json()
            text = result['choices'][0]['message']['content']
            return clean_response(text)
        else:
            logging.error(f"Kimi error: {r.text}")
            return f"Ошибка API: {r.status_code}"
            
    except Exception as e:
        logging.error(f"Kimi exception: {e}")
        return f"Ошибка: {str(e)}"

def clean_response(text: str) -> str:
    """Удаляет ненужные слова и форматирование"""
    garbage = [
        r'ОПРЕДЕЛИ.*?:', r'ВЫБЕРИ.*?:', r'ВЫПОЛНИ.*?:', r'АНАЛИЗИРУЮ.*?:',
        r'РАССУЖДАЮ.*?:', r'ДУМАЮ.*?:', r'ПЛАНИРУЮ.*?:',
        r'---', r'===', r'\*\*\*',
        r'ВЫВОД:', r'РЕЗУЛЬТАТ:', r'ОТВЕТ:'
    ]
    
    for pattern in garbage:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    return '\n'.join(lines)

# ========== OCR ==========

async def ocr_pdf(file_bytes: BytesIO) -> str:
    from pdf2image import convert_from_bytes
    import pytesseract
    
    file_bytes.seek(0)
    images = convert_from_bytes(file_bytes.read(), first_page=1, last_page=2, dpi=150)
    
    texts = []
    for img in images:
        text = pytesseract.image_to_string(img, lang='rus+eng+chi_sim')
        if text.strip():
            texts.append(text.strip())
    
    return '\n'.join(texts)

# ========== ПРОВЕРКА ШТРИХ-КОДОВ ==========

async def check_barcodes(file_bytes: BytesIO) -> str:
    """Проверяет штрих-коды в PDF"""
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
                    barcode = code.data.decode('utf-8')
                    results.append(f"Стр {i}: {barcode} ✅")
            else:
                results.append(f"Стр {i}: не найден")
        
        return '\n'.join(results) if results else "Штрих-коды не обнаружены"
    except Exception as e:
        return f"Ошибка проверки: {e}"

# ========== ОБРАБОТЧИКИ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🤖 Бот для бизнеса\n'
        'Отправь PDF/фото с заданием\n'
        'Пример: "Переименуй файлы" + PDF'
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text.startswith('http'):
        await update.message.reply_text('🌐 Загружаю...')
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(
                requests.get(text, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10).text,
                'html.parser'
            )
            for tag in soup(['script', 'style']): tag.decompose()
            content = soup.get_text(separator='\n', strip=True)[:2000]
            
            prompt = f"Краткое содержание (3-5 пунктов):\n{content}"
            resp = await ask_kimi(prompt)
            await update.message.reply_text(resp[:4000])
        except Exception as e:
            await update.message.reply_text(f'❌ Ошибка: {e}')
    else:
        resp = await ask_kimi(text)
        await update.message.reply_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text('📷 Обработка...')
        
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        buf = BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        
        b64 = base64.b64encode(buf.read()).decode()
        caption = update.message.caption or "Что на изображении?"
        
        resp = await ask_kimi(caption, b64)
        await update.message.reply_text(resp[:4000])
    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {e}')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        caption = update.message.caption or ""
        
        if doc.file_size > 20*1024*1024:
            await update.message.reply_text('❌ Файл >20MB')
            return
        
        name = doc.file_name.lower()
        
        if not (name.endswith('.pdf') or name.endswith('.txt')):
            await update.message.reply_text('❌ Только .pdf или .txt')
            return
        
        await update.message.reply_text('⏳ Загрузка...')
        
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        
        text = ""
        barcode_check = ""
        
        if name.endswith('.txt'):
            text = buf.read().decode('utf-8')
        
        elif name.endswith('.pdf'):
            if 'штрих' in caption.lower() or 'barcode' in caption.lower() or 'код' in caption.lower():
                await update.message.reply_text('🔍 Проверяю штрих-коды...')
                barcode_check = await check_barcodes(buf)
                buf.seek(0)
            
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(buf)
                text = '\n'.join([p.extract_text() or '' for p in reader.pages[:3]])
            except:
                pass
            
            if len(text.strip()) < 50:
                await update.message.reply_text('🔍 OCR...')
                try:
                    text = await ocr_pdf(buf)
                except Exception as e:
                    await update.message.reply_text(f'❌ OCR ошибка: {e}')
                    return
        
        if not text.strip() and not barcode_check:
            await update.message.reply_text('⚠️ Нет данных')
            return
        
        prompt = f"""Задача: {caption}

Текст из документа:
{text[:2500]}

Ответь:
1. Если просили переименовать - дай только список новых имён
2. Если штрих-коды - скажи работают/не работают
3. Без вступлений, только результат"""

        await update.message.reply_text('🤖 Анализ...')
        resp = await ask_kimi(prompt)
        
        if barcode_check:
            resp = f"📊 Штрих-коды:\n{barcode_check}\n\n{resp}"
        
        await update.message.reply_text(resp[:4000])
        
    except Exception as e:
        logging.error(f"Doc error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {e}')

# ========== ЗАПУСК ==========

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logging.info("Бот запущен")
    app.run_polling()

if __name__ == '__main__':
    main()
