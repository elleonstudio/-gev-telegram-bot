import os
import logging
import requests
import base64
from io import BytesIO
from telegram import Update, File
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bs4 import BeautifulSoup

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
        
        # Формируем сообщения правильно
        if image_b64:
            # Для vision API
            messages = [
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
            # Текстовый запрос
            messages = [
                {'role': 'system', 'content': 'Ты помощник для бизнеса. Отвечай коротко, только результат, без объяснений.'},
                {'role': 'user', 'content': prompt}
            ]
            model = 'moonshot-v1-8k'
        
        data = {
            'model': model,
            'messages': messages,
            'temperature': 0.1
        }
        
        logging.info(f"Отправка в Kimi: модель {model}, длина промпта {len(prompt)}")
        
        r = requests.post(
            'https://api.moonshot.cn/v1/chat/completions',
            headers=headers,
            json=data,
            timeout=60
        )
        
        logging.info(f"Kimi ответ: статус {r.status_code}")
        
        if r.status_code == 200:
            result = r.json()
            return result['choices'][0]['message']['content']
        else:
            logging.error(f"Kimi ошибка: {r.text}")
            return f"Ошибка API: {r.status_code}"
            
    except Exception as e:
        logging.error(f"Kimi exception: {e}")
        return f"Ошибка: {str(e)}"

# ========== OCR ==========

async def ocr_pdf(file_bytes: BytesIO) -> str:
    from pdf2image import convert_from_bytes
    import pytesseract
    
    file_bytes.seek(0)
    images = convert_from_bytes(file_bytes.read(), first_page=1, last_page=2, dpi=150)
    
    texts = []
    for img in images:
        text = pytesseract.image_to_string(img, lang='rus+eng')
        if text.strip():
            texts.append(text.strip())
    
    return '\n'.join(texts)

# ========== ОБРАБОТЧИКИ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 Отправь PDF/фото с заданием')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text.startswith('http'):
        await update.message.reply_text('🌐 Загружаю...')
        try:
            soup = BeautifulSoup(requests.get(text, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10).text, 'html.parser')
            for tag in soup(['script', 'style']): tag.decompose()
            content = soup.get_text(separator='\n', strip=True)[:3000]
            
            prompt = f"Краткое содержание сайта:\n\n{content}\n\nКоротко, 3-5 пунктов:"
            resp = await ask_kimi(prompt)
            await update.message.reply_text(resp[:4000])
        except Exception as e:
            await update.message.reply_text(f'❌ Ошибка: {e}')
    else:
        prompt = f"{text}\n\nКороткий ответ (макс 200 слов):"
        resp = await ask_kimi(prompt)
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
        caption = update.message.caption or "Опиши изображение"
        
        prompt = f"{caption}\n\nКоротко, только факты:"
        resp = await ask_kimi(prompt, b64)
        
        await update.message.reply_text(resp[:4000])
    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {e}')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        
        if doc.file_size > 20*1024*1024:
            await update.message.reply_text('❌ Файл >20MB')
            return
        
        await update.message.reply_text('⏳ Загрузка...')
        
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        
        name = doc.file_name.lower()
        text = ""
        
        # TXT
        if name.endswith('.txt'):
            text = buf.read().decode('utf-8')
            
        # PDF
        elif name.endswith('.pdf'):
            # Пробуем текстовый слой
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(buf)
                text = '\n'.join([p.extract_text() or '' for p in reader.pages[:3]])
            except:
                pass
            
            # OCR если нужно
            if len(text.strip()) < 50:
                await update.message.reply_text('🔍 OCR...')
                try:
                    text = await ocr_pdf(buf)
                except Exception as e:
                    await update.message.reply_text(f'❌ OCR ошибка: {e}')
                    return
        else:
            await update.message.reply_text('❌ Только .pdf или .txt')
            return
        
        if not text.strip():
            await update.message.reply_text('⚠️ Нет текста')
            return
        
        # Анализ
        caption = update.message.caption or "Проанализируй документ"
        prompt = f"""ЗАДАЧА: {caption}

ТЕКСТ ДОКУМЕНТА:
{text[:3000]}

ДАЙ КОРОТКИЙ ОТВЕТ:
- Только результат
- Без слов "ОПРЕДЕЛИ", "ВЫБЕРИ", "ВЫПОЛНИ"
- Таблицей или списком"""
        
        await update.message.reply_text('🤖 Анализ...')
        resp = await ask_kimi(prompt)
        
        # Чистим мусор
        clean = resp
        for bad in ['ОПРЕДЕЛИ', 'ВЫБЕРИ', 'ВЫПОЛНИ', '---', 'ЗАДАЧА:', 'ФОРМАТ:', 'ЯЗЫК:']:
            clean = clean.replace(bad, '')
        
        await update.message.reply_text(clean.strip()[:4000])
        
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
