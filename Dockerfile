import os
import logging
import requests
import base64
from io import BytesIO
from telegram import Update, File
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bs4 import BeautifulSoup
from urllib.parse import urlparse

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

# ============ ВЕБ-СКРАПИНГ ============

async def scrape_website(url: str) -> str:
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0'}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            element.decompose()
        
        text = soup.get_text(separator='\n', strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return '\n'.join(lines)[:8000]
    except Exception as e:
        logging.error(f"Scraping error: {e}")
        return None

# ============ KIMI API ============

async def ask_kimi(text: str, image_base64: str = None) -> str:
    try:
        headers = {
            'Authorization': f'Bearer {KIMI_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        messages = [{'role': 'system', 'content': 'You are a helpful assistant. Give short, clear, structured answers without explaining your thinking process.'}]
        
        if image_base64:
            messages.append({
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': text},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_base64}'}}
                ]
            })
            model = 'moonshot-v1-8k-vision-preview'
        else:
            messages.append({'role': 'user', 'content': text})
            model = 'moonshot-v1-8k'
        
        data = {
            'model': model,
            'messages': messages,
            'temperature': 0.2
        }
        
        response = requests.post(
            'https://api.moonshot.cn/v1/chat/completions',
            headers=headers,
            json=data,
            timeout=60
        )
        
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        else:
            logging.error(f"Kimi API error: {response.status_code}")
            return f"Ошибка API: {response.status_code}"
            
    except Exception as e:
        logging.error(f"Kimi error: {e}")
        return "Ошибка при обращении к AI"

# ============ КОРОТКИЙ ПРОМПТ ============

def create_prompt(user_request: str, content: str = None, content_type: str = None) -> str:
    """Короткий промпт без рассуждений"""
    
    if content:
        return f"""ЗАДАЧА: {user_request}

ДАННЫЕ ({content_type}):
{content}

ДАЙ КОРОТКИЙ ЧЁТКИЙ ОТВЕТ:
- Без объяснений как ты думаешь
- Только результат
- Таблицей или списком
- По-русски"""
    else:
        return f"""ЗАДАЧА: {user_request}

ДАЙ КОРОТКИЙ ЧЁТКИЙ ОТВЕТ БЕЗ ЛИШНИХ СЛОВ. По-русски."""

# ============ OCR ============

async def extract_pdf_with_ocr(file_bytes: BytesIO, update: Update) -> str:
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
        
        file_bytes.seek(0)
        images = convert_from_bytes(file_bytes.read(), first_page=1, last_page=3, dpi=150)
        
        if not images:
            return None
        
        ocr_texts = []
        for i, image in enumerate(images):
            text = pytesseract.image_to_string(image, lang='rus+eng')
            if text.strip():
                ocr_texts.append(text.strip())
        
        return '\n'.join(ocr_texts)
    except Exception as e:
        logging.error(f"OCR error: {e}")
        raise

# ============ ОБРАБОТЧИКИ ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🤖 *AI Бот*\n\n'
        'Отправьте PDF, фото или текст.\n'
        'Добавьте подпись что нужно сделать.',
        parse_mode='Markdown'
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text.startswith(('http://', 'https://')):
        await update.message.reply_text('🌐 Загружаю...')
        content = await scrape_website(text)
        
        if content:
            prompt = create_prompt(f"Проанализируй сайт {urlparse(text).netloc}", content, "сайт")
            response = await ask_kimi(prompt)
            await send_short(update, response)
        else:
            await update.message.reply_text('❌ Не удалось открыть сайт')
    else:
        prompt = create_prompt(text)
        response = await ask_kimi(prompt)
        await send_short(update, response)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text('📷 Сканирую...')
        
        photo = update.message.photo[-1]
        file: File = await context.bot.get_file(photo.file_id)
        
        if photo.file_size and photo.file_size > 10 * 1024 * 1024:
            await update.message.reply_text("❌ Слишком большое фото")
            return
        
        photo_bytes = BytesIO()
        await file.download_to_memory(photo_bytes)
        photo_bytes.seek(0)
        
        image_base64 = base64.b64encode(photo_bytes.read()).decode('utf-8')
        
        caption = update.message.caption or "Опиши что на изображении"
        prompt = create_prompt(caption)
        
        response = await ask_kimi(prompt, image_base64)
        await send_short(update, response)
        
    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text('❌ Ошибка')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        file_name = doc.file_name or "unknown"
        mime_type = doc.mime_type or "unknown"
        
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text('❌ Файл слишком большой')
            return
        
        await update.message.reply_text(f'📄 {file_name}...')
        
        file: File = await context.bot.get_file(doc.file_id)
        file_bytes = BytesIO()
        await file.download_to_memory(file_bytes)
        file_bytes.seek(0)
        
        content = None
        
        # TXT
        if mime_type == 'text/plain' or file_name.lower().endswith('.txt'):
            content = file_bytes.read().decode('utf-8')
            
        # PDF
        elif mime_type == 'application/pdf' or file_name.lower().endswith('.pdf'):
            # Пробуем текстовый слой
            try:
                import PyPDF2
                pdf_reader = PyPDF2.PdfReader(file_bytes)
                text_parts = []
                for page in pdf_reader.pages[:10]:
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)
                content = '\n'.join(text_parts)
                
                # Если мало текста — OCR
                if not content or len(content.strip()) < 50:
                    await update.message.reply_text('🔍 Распознаю скан...')
                    content = await extract_pdf_with_ocr(file_bytes, update)
                    
            except Exception as e:
                await update.message.reply_text('🔍 Распознаю скан...')
                content = await extract_pdf_with_ocr(file_bytes, update)
        
        else:
            await update.message.reply_text('❌ Только .pdf или .txt')
            return
        
        if content and content.strip():
            caption = update.message.caption or "Проанализируй документ"
            prompt = create_prompt(caption, content[:5000], "документ")
            
            await update.message.reply_text('🤖 Думаю...')
            response = await ask_kimi(prompt)
            await send_short(update, response)
        else:
            await update.message.reply_text('⚠️ Не удалось прочитать')
            
    except Exception as e:
        logging.error(f"Document error: {e}")
        await update.message.reply_text('❌ Ошибка')

# ============ ОТПРАВКА ============

async def send_short(update: Update, text: str):
    """Отправляет без лишних заголовков"""
    # Убираем мусор из ответа
    cleaned = text.replace('ОПРЕДЕЛИ ЗАДАЧУ:', '').replace('ВЫБЕРИ ФОРМАТ ОТВЕТА:', '').replace('ВЫПОЛНИ ЗАДАЧУ:', '')
    cleaned = cleaned.replace('---', '').strip()
    
    if len(cleaned) > 4000:
        parts = [cleaned[i:i+4000] for i in range(0, len(cleaned), 4000)]
        for part in parts:
            await update.message.reply_text(part)
    else:
        await update.message.reply_text(cleaned)

# ============ ЗАПУСК ============

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logging.info("Бот запущен")
    application.run_polling()

if __name__ == '__main__':
    main()
