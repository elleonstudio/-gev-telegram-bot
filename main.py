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
        
        for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            element.decompose()
        
        text = soup.get_text(separator='\n', strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = '\n'.join(lines)
        
        return text[:8000]
        
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
        
        messages = [{'role': 'system', 'content': 'You are a universal AI assistant. Understand user intent and provide best possible help.'}]
        
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

# ============ OCR ДЛЯ PDF ============

async def extract_pdf_with_ocr(file_bytes: BytesIO, update: Update) -> str:
    """Извлекает текст из PDF с помощью OCR (для сканов)"""
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
        
        file_bytes.seek(0)
        
        # Конвертируем PDF в изображения (макс 5 страниц для скорости)
        await update.message.reply_text('📸 Конвертирую PDF в изображения...')
        images = convert_from_bytes(file_bytes.read(), first_page=1, last_page=5, dpi=200)
        
        if not images:
            return None
        
        await update.message.reply_text(f'🔍 Распознаю {len(images)} страниц...')
        
        ocr_texts = []
        for i, image in enumerate(images):
            await update.message.reply_text(f'⏳ OCR страница {i+1}/{len(images)}...')
            
            # Распознаем текст (русский + английский)
            text = pytesseract.image_to_string(image, lang='rus+eng')
            
            if text.strip():
                ocr_texts.append(f"--- Страница {i+1} ---\n{text.strip()}")
        
        return '\n\n'.join(ocr_texts)
        
    except Exception as e:
        logging.error(f"OCR error: {e}")
        raise e

# ============ УНИВЕРСАЛЬНЫЙ ПРОМПТ ============

def create_universal_prompt(user_request: str, content: str = None, content_type: str = None) -> str:
    """Универсальный промпт, который адаптируется под любую задачу"""
    
    base = f"""ЗАПРОС ПОЛЬЗОВАТЕЛЯ: {user_request}"""

    if content:
        base += f"""

ИСТОЧНИК ДАННЫХ ({content_type}):
{content}"""

    base += """

ИНСТРУКЦИИ ДЛЯ AI:
1. ОПРЕДЕЛИ ЗАДАЧУ: Что именно хочет пользователь?
   - Анализ данных / Расчёты / Перевод / Проверка / Создание текста / Программирование / Другое

2. ВЫБЕРИ ФОРМАТ ОТВЕТА под задачу:
   - Таблица — для сравнения, распределения, статистики
   - Список — для инструкций, планов
   - Код — для программирования
   - Текст — для объяснений, анализа
   - Структурированный ответ — для логистики, отчётов

3. ВЫПОЛНИ ЗАДАЧУ:
   - Если данные — проанализируй, посчитай, сгруппируй
   - Если текст — проверь, перепиши, переведи
   - Если код — напиши, исправь, объясни
   - Если вопрос — ответь развёрнуто

4. ДОПОЛНИТЕЛЬНО:
   - Если есть числа — посчитай итоги, разности, проценты
   - Если есть ошибки — укажи и исправь
   - Если неясно — задай уточняющий вопрос, но попробуй догадаться

5. ЯЗЫК: Отвечай на языке запроса пользователя (русский, английский, китайский и т.д.)

Ответь максимально полезно и структурированно."""

    return base

# ============ ОБРАБОТЧИКИ ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🤖 *Универсальный AI Бот с OCR*\n\n'
        'Отправьте мне:\n\n'
        '📷 *Фото* — любой текст, таблица, документ\n'
        '📄 *PDF* — текстовый или скан (OCR автоматически)\n'
        '📝 *TXT* — любой текстовый файл\n'
        '🌐 *Ссылка* — анализ сайта\n'
        '💬 *Сообщение* — любой вопрос\n\n'
        'Я сам пойму задачу и выдам лучший результат!',
        parse_mode='Markdown'
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста и ссылок"""
    text = update.message.text
    
    if text.startswith(('http://', 'https://')):
        await update.message.reply_text('🌐 Загружаю сайт...')
        
        content = await scrape_website(text)
        
        if content:
            prompt = create_universal_prompt(
                f"Проанализируй содержимое сайта {urlparse(text).netloc}",
                content,
                "веб-страница"
            )
            response = await ask_kimi(prompt)
            await send_long_message(update, response, "📊 Анализ сайта")
        else:
            await update.message.reply_text('❌ Не удалось получить доступ к сайту')
    else:
        prompt = create_universal_prompt(text)
        response = await ask_kimi(prompt)
        await send_long_message(update, response, "🤖 Ответ")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото"""
    try:
        await update.message.reply_text('📷 Анализирую изображение...')
        
        photo = update.message.photo[-1]
        file: File = await context.bot.get_file(photo.file_id)
        
        if photo.file_size and photo.file_size > 10 * 1024 * 1024:
            await update.message.reply_text("❌ Фото слишком большое (>10MB)")
            return
        
        photo_bytes = BytesIO()
        await file.download_to_memory(photo_bytes)
        photo_bytes.seek(0)
        
        image_base64 = base64.b64encode(photo_bytes.read()).decode('utf-8')
        
        user_caption = update.message.caption or "Опиши и проанализируй это изображение. Если таблица — извлеки данные, если документ — прочитай текст."
        
        prompt = create_universal_prompt(user_caption)
        
        await update.message.reply_text('🤖 Думаю...')
        response = await ask_kimi(prompt, image_base64)
        
        await send_long_message(update, response, "📊 Результат")
        
    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text('❌ Ошибка при обработке фото')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка документов: TXT и PDF (с OCR)"""
    try:
        doc = update.message.document
        file_name = doc.file_name or "unknown"
        mime_type = doc.mime_type or "unknown"
        
        logging.info(f"Файл: {file_name}, тип: {mime_type}")
        
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text('❌ Файл слишком большой (>20MB)')
            return
        
        await update.message.reply_text(f'📄 Загружаю: {file_name}...')
        
        file: File = await context.bot.get_file(doc.file_id)
        file_bytes = BytesIO()
        await file.download_to_memory(file_bytes)
        file_bytes.seek(0)
        
        content = None
        extraction_method = ""
        
        # ========== ОБРАБОТКА TXT ==========
        if mime_type == 'text/plain' or file_name.lower().endswith('.txt'):
            try:
                content = file_bytes.read().decode('utf-8')
                extraction_method = "TXT"
                logging.info(f"TXT: {len(content)} символов")
            except Exception as e:
                await update.message.reply_text('❌ Ошибка чтения TXT')
                return
            
        # ========== ОБРАБОТКА PDF ==========
        elif mime_type == 'application/pdf' or file_name.lower().endswith('.pdf'):
            await update.message.reply_text('🔍 Анализирую PDF...')
            
            # Пробуем как текстовый PDF
            try:
                import PyPDF2
                pdf_reader = PyPDF2.PdfReader(file_bytes)
                num_pages = min(len(pdf_reader.pages), 30)
                
                text_parts = []
                for i in range(num_pages):
                    try:
                        page_text = pdf_reader.pages[i].extract_text()
                        if page_text:
                            text_parts.append(page_text)
                    except:
                        pass
                
                content = '\n\n'.join(text_parts)
                
                # Если текст найден — отлично
                if content and len(content.strip()) > 100:
                    extraction_method = "PDF текстовый"
                    logging.info(f"PDF текст: {len(content)} символов")
                    await update.message.reply_text(f'✅ Найден текстовый слой: {len(content)} символов')
                else:
                    # Если нет текста — запускаем OCR
                    logging.info("PDF без текста, запускаю OCR...")
                    await update.message.reply_text('📄 PDF — скан/изображение. Запускаю OCR...')
                    
                    try:
                        content = await extract_pdf_with_ocr(file_bytes, update)
                        extraction_method = "PDF OCR"
                        
                        if not content or not content.strip():
                            await update.message.reply_text('⚠️ OCR не распознал текст. Возможно, качество слишком низкое или язык не поддерживается.')
                            return
                            
                        await update.message.reply_text(f'✅ OCR завершён: {len(content)} символов')
                        
                    except Exception as ocr_error:
                        logging.error(f"OCR failed: {ocr_error}")
                        await update.message.reply_text(
                            f'⚠️ Не удалось распознать PDF.\n\n'
                            f'Ошибка: {str(ocr_error)[:200]}\n\n'
                            f'💡 Решение: Отправьте скриншоты PDF как фото — я распознаю через Vision API!'
                        )
                        return
                    
            except Exception as pdf_error:
                await update.message.reply_text(f'❌ Ошибка чтения PDF: {str(pdf_error)[:200]}')
                return
        
        else:
            await update.message.reply_text(f'❌ Формат не поддерживается: {mime_type}\nОтправьте .txt или .pdf')
            return
        
        # ========== АНАЛИЗ ==========
        if content and content.strip():
            if len(content) > 8000:
                content = content[:8000]
                await update.message.reply_text('⚠️ Текст длинный, взял первые 8000 символов')
            
            # Получаем задание из подписи
            caption = update.message.caption or "Проанализируй этот документ и выполни соответствующие задачи. Определи тип документа и обработай соответственно."
            
            prompt = create_universal_prompt(caption, content, f"документ ({extraction_method})")
            
            await update.message.reply_text('🤖 Анализирую содержимое...')
            response = await ask_kimi(prompt)
            
            await send_long_message(update, response, "📋 Результат")
        else:
            await update.message.reply_text('⚠️ Не удалось извлечь текст из файла')
            
    except Exception as e:
        logging.error(f"Document error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)}')

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

async def send_long_message(update: Update, text: str, header: str):
    """Отправляет длинные сообщения частями"""
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for i, part in enumerate(parts, 1):
            prefix = f"*{header} (часть {i}/{len(parts)}):*\n\n" if i == 1 else f"*(продолжение {i})*\n\n"
            await update.message.reply_text(prefix + part, parse_mode='Markdown')
    else:
        await update.message.reply_text(f"*{header}:*\n\n{text}", parse_mode='Markdown')

# ============ ЗАПУСК ============

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logging.info("Универсальный бот с OCR запущен")
    application.run_polling()

if __name__ == '__main__':
    main()
