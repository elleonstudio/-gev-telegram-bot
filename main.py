import os
import logging
import requests
import base64
from io import BytesIO
from telegram import Update, File
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Токены
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

# ============ ВЕБ-СКРАПИНГ ============

async def scrape_website(url: str) -> str:
    """Заходит на сайт и извлекает текст"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Удаляем ненужные элементы
        for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            element.decompose()
        
        # Получаем текст
        text = soup.get_text(separator='\n', strip=True)
        
        # Очищаем от лишних пробелов и пустых строк
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = '\n'.join(lines)
        
        # Ограничиваем длину для Kimi
        return text[:6000]
        
    except Exception as e:
        logging.error(f"Scraping error: {e}")
        return None

# ============ KIMI API ============

async def ask_kimi(text: str, image_base64: str = None) -> str:
    """Отправляет запрос в Kimi API"""
    try:
        headers = {
            'Authorization': f'Bearer {KIMI_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        messages = [{'role': 'system', 'content': 'You are a professional analyst. Analyze provided content thoroughly.'}]
        
        # Если есть изображение — используем vision-модель
        if image_base64:
            messages.append({
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': text or 'Analyze this image'},
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
            'temperature': 0.3
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
            logging.error(f"Kimi API error: {response.status_code} - {response.text}")
            return f"Ошибка API: {response.status_code}"
            
    except Exception as e:
        logging.error(f"Kimi error: {e}")
        return "Ошибка при обращении к AI"

# ============ ОБРАБОТЧИКИ ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🤖 *Gev Bot Pro*\n\n'
        'Я умею:\n'
        '📝 Анализировать текст\n'
        '📷 Анализировать фото (Kimi Vision)\n'
        '📄 Читать документы (PDF, TXT)\n'
        '🌐 Заходить на сайты и анализировать их\n\n'
        'Просто отправьте мне ссылку, фото или текст!',
        parse_mode='Markdown'
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста и ссылок"""
    text = update.message.text
    
    # Проверяем, является ли текст ссылкой
    if text.startswith(('http://', 'https://')):
        await update.message.reply_text('🌐 Захожу на сайт, анализирую...')
        
        # Скрапим сайт
        content = await scrape_website(text)
        
        if content:
            # Отправляем в Kimi для анализа
            prompt = f"Проанализируй содержимое этого сайта и выдели ключевую информацию:\n\n{content}"
            analysis = await ask_kimi(prompt)
            
            # Формируем ответ
            domain = urlparse(text).netloc
            response = f"📊 *Анализ сайта:* `{domain}`\n\n{analysis[:3000]}"
            
            if len(analysis) > 3000:
                response += "\n\n_(ответ сокращён)_"
                
            await update.message.reply_text(response, parse_mode='Markdown')
        else:
            await update.message.reply_text('❌ Не удалось получить доступ к сайту. Возможно, он защищён от ботов.')
    else:
        # Обычный текст — отправляем в Kimi
        response = await ask_kimi(text)
        await update.message.reply_text(response)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото через Kimi Vision"""
    try:
        await update.message.reply_text('📷 Скачиваю и анализирую фото...')
        
        # Получаем фото (максимальное качество)
        photo = update.message.photo[-1]
        file: File = await context.bot.get_file(photo.file_id)
        
        # Скачиваем в память (не на диск — для Railway лучше так)
        photo_bytes = BytesIO()
        await file.download_to_memory(photo_bytes)
        photo_bytes.seek(0)
        
        # Конвертируем в base64
        image_base64 = base64.b64encode(photo_bytes.read()).decode('utf-8')
        
        # Отправляем в Kimi Vision
        caption = update.message.caption or "Опиши и проанализируй это изображение подробно."
        response = await ask_kimi(caption, image_base64)
        
        await update.message.reply_text(f"🖼️ *Анализ изображения:*\n\n{response}", parse_mode='Markdown')
        
    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text('❌ Ошибка при обработке фото')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка документов: TXT и PDF"""
    try:
        doc = update.message.document
        await update.message.reply_text(f'📄 Получаю файл: {doc.file_name}...')
        
        file: File = await context.bot.get_file(doc.file_id)
        file_bytes = BytesIO()
        await file.download_to_memory(file_bytes)
        file_bytes.seek(0)
        
        content = None
        
        # Обработка TXT
        if doc.mime_type == 'text/plain':
            content = file_bytes.read().decode('utf-8')
            
        # Обработка PDF
        elif doc.file_name.lower().endswith('.pdf') or doc.mime_type == 'application/pdf':
            try:
                import PyPDF2
                pdf_reader = PyPDF2.PdfReader(file_bytes)
                
                # Читаем все страницы
                text_parts = []
                for page in pdf_reader.pages:
                    text_parts.append(page.extract_text())
                
                content = '\n'.join(text_parts)
                
                if not content.strip():
                    await update.message.reply_text('⚠️ PDF получен, но текст не распознан (возможно, скан/изображение)')
                    return
                    
            except Exception as pdf_error:
                await update.message.reply_text(f'❌ Ошибка чтения PDF: {str(pdf_error)}')
                return
        
        # Если есть содержимое — отправляем в Kimi
        if content:
            # Ограничиваем длину
            content = content[:5000]
            
            # Проверяем, есть ли специальные инструкции в подписи
            caption = update.message.caption or ""
            
            if caption:
                prompt = f"""Выполни следующие задания по документу:
{caption}

Содержимое документа:
{content}

Ответь подробно по-русски."""
            else:
                prompt = f"""Проанализируй этот документ и выдели ключевую информацию:

{content}

Ответь по-русски."""

            response = await ask_kimi(prompt)
            await update.message.reply_text(response)
        else:
            await update.message.reply_text(f'✅ Файл получен ({doc.file_name})\n\nФормат не поддерживается для анализа. Отправьте .txt или .pdf')
            
    except Exception as e:
        logging.error(f"Document error: {e}")
        await update.message.reply_text('❌ Ошибка при обработке файла')

# ============ ЗАПУСК ============

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    application.run_polling()

if __name__ == '__main__':
    main()
