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
        
        messages = [{'role': 'system', 'content': 'You are a professional data analyst. Extract, process and structure data accurately.'}]
        
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
            logging.error(f"Kimi API error: {response.status_code} - {response.text}")
            return f"Ошибка API: {response.status_code}"
            
    except Exception as e:
        logging.error(f"Kimi error: {e}")
        return "Ошибка при обращении к AI"

# ============ ПРОМПТЫ ============

def create_analysis_prompt(user_request: str, content: str, content_type: str = "текст") -> str:
    """Создаёт структурированный промпт для анализа"""
    
    base_prompt = f"""🔍 ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:
{user_request}

📋 ИСХОДНЫЕ ДАННЫЕ ({content_type}):
{content}

⚡ ИНСТРУКЦИИ:
1. Внимательно извлеки ВСЕ данные из источника
2. Выполни конкретное задание пользователя точно и полностью
3. Не просто описывай — выполняй действия (считай, группируй, распределяй, сравнивай)
4. Используй структурированный формат: таблицы, списки, итоги
5. Если есть числа — посчитай суммы, разности, проценты где уместно
6. Проверь штрих-коды на корректность (если есть)
7. Выдели ключевые показатели и выводы

📊 ТРЕБОВАНИЯ К ФОРМАТУ ОТВЕТА:
- Начни с краткого резюме выполненной работы
- Представь данные в табличном виде где возможно
- Добавь итоговые суммы/выводы
- Укажи рекомендации или следующие шаги (если уместно)

Ответь по-русски профессионально."""

    return base_prompt

def create_vision_prompt(user_caption: str) -> str:
    """Создаёт промпт для анализа изображений"""
    
    if not user_caption or user_caption.strip() == "":
        user_caption = "Проанализируй изображение и извлеки все данные"
    
    return f"""🎯 ЗАДАЧА: {user_caption}

📸 ИЗОБРАЖЕНИЕ: (смотри на фото выше)

⚡ ИНСТРУКЦИИ:
1. Распознай ВСЕ текстовые данные, таблицы, числа, названия
2. Выполни конкретное задание пользователя — не просто описывай
3. Если это таблица — перестрой её в текстовый вид
4. Если нужно распределить/сгруппировать — сделай это
5. Посчитай итоги, суммы, разности где требуется
6. Проверь данные на логику и корректность

📊 ТРЕБОВАНИЯ:
- НЕ пиши "на изображении видно" — просто выдай результат
- Используй таблицы Markdown для данных
- Добавь итоговые расчёты
- Будь точным с числами и названиями

Ответь по-русски структурированно."""

# ============ ОБРАБОТЧИКИ ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🤖 *Gev Bot Pro — Анализ данных*\n\n'
        'Отправьте мне:\n'
        '📷 Фото с таблицей/данными\n'
        '📄 PDF или TXT документ\n'
        '🌐 Ссылку на сайт\n'
        '💬 Текст для анализа\n\n'
        '*Важно:* Добавьте подпись с конкретным заданием!\n'
        'Пример: "Распредели по складам, посчитай итоги"',
        parse_mode='Markdown'
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста и ссылок"""
    text = update.message.text
    
    # Проверяем, является ли текст ссылкой
    if text.startswith(('http://', 'https://')):
        await update.message.reply_text('🌐 Захожу на сайт, извлекаю данные...')
        
        content = await scrape_website(text)
        
        if content:
            prompt = create_analysis_prompt(
                "Проанализируй содержимое сайта, выдели ключевую информацию и структурируй данные",
                content,
                "веб-страница"
            )
            analysis = await ask_kimi(prompt)
            
            domain = urlparse(text).netloc
            response = f"📊 *Анализ сайта:* `{domain}`\n\n{analysis[:3500]}"
            
            if len(analysis) > 3500:
                response += "\n\n_(часть ответа сокращена)_"
                
            await update.message.reply_text(response, parse_mode='Markdown')
        else:
            await update.message.reply_text('❌ Не удалось получить доступ к сайту')
    else:
        # Обычный текст
        prompt = create_analysis_prompt(
            "Проанализируй текст и выдели ключевую информацию",
            text,
            "текст"
        )
        response = await ask_kimi(prompt)
        await update.message.reply_text(response)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото через Kimi Vision"""
    try:
        await update.message.reply_text('📷 Сканирую изображение...')
        
        photo = update.message.photo[-1]
        file: File = await context.bot.get_file(photo.file_id)
        
        # Проверяем размер
        if photo.file_size and photo.file_size > 10 * 1024 * 1024:  # 10 MB
            await update.message.reply_text("❌ Фото слишком большое (>10MB). Отправьте меньше или обрежьте.")
            return
        
        photo_bytes = BytesIO()
        await file.download_to_memory(photo_bytes)
        photo_bytes.seek(0)
        
        image_base64 = base64.b64encode(photo_bytes.read()).decode('utf-8')
        
        # Получаем подпись и создаём структурированный промпт
        user_caption = update.message.caption or ""
        structured_prompt = create_vision_prompt(user_caption)
        
        await update.message.reply_text('🤖 Анализирую данные...')
        response = await ask_kimi(structured_prompt, image_base64)
        
        # Разбиваем длинный ответ
        if len(response) > 4000:
            parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
            await update.message.reply_text(f"📊 *Результат (часть 1/{len(parts)}):*\n\n{parts[0]}", parse_mode='Markdown')
            for i, part in enumerate(parts[1:], 2):
                await update.message.reply_text(f"📄 *Часть {i}:*\n\n{part}", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"📊 *Результат анализа:*\n\n{response}", parse_mode='Markdown')
        
    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text('❌ Ошибка при обработке фото. Попробуйте другое изображение.')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка документов: TXT и PDF"""
    try:
        doc = update.message.document
        file_name = doc.file_name or "unknown"
        mime_type = doc.mime_type or "unknown"
        
        logging.info(f"Получен файл: {file_name}, mime_type: {mime_type}")
        
        # Проверка размера (макс 20MB)
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text('❌ Файл слишком большой (>20MB)')
            return
        
        await update.message.reply_text(f'📄 Загружаю: {file_name}...')
        
        file: File = await context.bot.get_file(doc.file_id)
        file_bytes = BytesIO()
        await file.download_to_memory(file_bytes)
        file_bytes.seek(0)
        
        content = None
        file_type = "неизвестный"
        
        # Обработка TXT
        if mime_type == 'text/plain' or file_name.lower().endswith('.txt'):
            try:
                content = file_bytes.read().decode('utf-8')
                file_type = "TXT"
                logging.info(f"TXT прочитан: {len(content)} символов")
            except Exception as e:
                logging.error(f"Ошибка чтения TXT: {e}")
                await update.message.reply_text('❌ Ошибка чтения TXT файла')
                return
            
        # Обработка PDF
        elif mime_type == 'application/pdf' or file_name.lower().endswith('.pdf'):
            try:
                import PyPDF2
                logging.info("Чтение PDF...")
                
                pdf_reader = PyPDF2.PdfReader(file_bytes)
                num_pages = len(pdf_reader.pages)
                
                if num_pages > 50:
                    await update.message.reply_text(f'⚠️ PDF большой ({num_pages} стр). Буду читать первые 50 страниц.')
                    num_pages = 50
                
                text_parts = []
                for i in range(num_pages):
                    try:
                        page_text = pdf_reader.pages[i].extract_text()
                        if page_text:
                            text_parts.append(page_text)
                    except Exception as page_error:
                        logging.warning(f"Ошибка страницы {i+1}: {page_error}")
                
                content = '\n\n'.join(text_parts)
                file_type = "PDF"
                logging.info(f"PDF прочитан: {len(content)} символов")
                
                if not content.strip():
                    await update.message.reply_text('⚠️ PDF пустой или содержит только изображения (скан). Попробуйте отправить как фото.')
                    return
                    
            except Exception as pdf_error:
                logging.error(f"Ошибка PDF: {pdf_error}")
                await update.message.reply_text(f'❌ Ошибка чтения PDF: {str(pdf_error)}')
                return
        
        else:
            logging.warning(f"Неподдерживаемый формат: {mime_type}")
            await update.message.reply_text(f'❌ Формат не поддерживается: {mime_type}\nОтправьте .txt или .pdf')
            return
        
        # Анализ содержимого
        if content and content.strip():
            original_length = len(content)
            if len(content) > 6000:
                content = content[:6000]
                logging.info(f"Текст сокращён с {original_length}")
            
            # Получаем подпись с заданием
            caption = update.message.caption or "Проанализируй документ и выдели ключевую информацию"
            
            prompt = create_analysis_prompt(caption, content, file_type)
            
            await update.message.reply_text(f'🤖 Анализирую {file_type} ({len(content)} символов)...')
            response = await ask_kimi(prompt)
            
            # Разбиваем длинный ответ
            if len(response) > 4000:
                parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
                for i, part in enumerate(parts, 1):
                    header = f"📊 *Результат ({i}/{len(parts)}):*\n\n" if i == 1 else f"📄 *Продолжение {i}:*\n\n"
                    await update.message.reply_text(header + part, parse_mode='Markdown')
            else:
                await update.message.reply_text(f"📊 *Результат анализа:*\n\n{response}", parse_mode='Markdown')
        else:
            await update.message.reply_text('⚠️ Не удалось извлечь текст из файла.')
            
    except Exception as e:
        logging.error(f"Document error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)}')

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
