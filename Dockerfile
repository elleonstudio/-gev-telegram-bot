import os
import logging
import requests
import base64
from io import BytesIO
from telegram import Update, File
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bs4 import BeautifulSoup
from urllib.parse import urlparse

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

# ========== КОРОТКИЙ ЖЁСТКИЙ ПРОМПТ ==========

SYSTEM_PROMPT = """Ты — ассистент для бизнеса. Отвечай КОРОТКО и ПО ДЕЛУ.
ЗАПРЕЩЕНО: объяснять как ты думаешь, писать "ОПРЕДЕЛИ ЗАДАЧУ", "ВЫБЕРИ ФОРМАТ" и т.п.
ТОЛЬКО результат: таблица, список или цифры."""

def make_prompt(user_text: str, doc_text: str = None) -> str:
    if doc_text:
        return f"""{user_text}

ДОКУМЕНТ:
{doc_text[:4000]}

ДАЙ КОРОТКИЙ ОТВЕТ:
- Таблицей или списком
- Без вступлений и объяснений
- Только факты и цифры"""
    else:
        return f"{user_text}\n\nКороткий ответ, только результат:"

# ========== KIMI ==========

async def ask_kimi(prompt: str, image_b64: str = None) -> str:
    try:
        headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
        
        messages = [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': prompt if not image_b64 else [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}}
            ]}
        ]
        
        r = requests.post('https://api.moonshot.cn/v1/chat/completions', 
                         headers=headers, 
                         json={'model': 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k', 
                               'messages': messages, 'temperature': 0.1},
                         timeout=60)
        
        return r.json()['choices'][0]['message']['content'] if r.status_code == 200 else "Ошибка"
    except Exception as e:
        return f"Ошибка: {e}"

# ========== OCR ==========

async def ocr_pdf(file_bytes: BytesIO) -> str:
    from pdf2image import convert_from_bytes
    import pytesseract
    file_bytes.seek(0)
    imgs = convert_from_bytes(file_bytes.read(), first_page=1, last_page=3, dpi=150)
    texts = [pytesseract.image_to_string(img, lang='rus+eng') for img in imgs]
    return '\n'.join([t for t in texts if t.strip()])

# ========== ОБРАБОТЧИКИ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 Отправь PDF/фото с заданием')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text.startswith('http'):
        # Сайт
        soup = BeautifulSoup(requests.get(text, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10).text, 'html.parser')
        for tag in soup(['script', 'style']): tag.decompose()
        content = soup.get_text(separator='\n', strip=True)[:4000]
        resp = await ask_kimi(make_prompt("Краткое содержание сайта:", content))
    else:
        resp = await ask_kimi(make_prompt(text))
    
    await update.message.reply_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    buf = BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    
    b64 = base64.b64encode(buf.read()).decode()
    caption = update.message.caption or "Что на изображении?"
    
    resp = await ask_kimi(make_prompt(caption), b64)
    await update.message.reply_text(resp[:4000])

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    
    if doc.file_size > 20*1024*1024:
        await update.message.reply_text('❌ Файл >20MB')
        return
    
    await update.message.reply_text('⏳ Обработка...')
    
    file = await context.bot.get_file(doc.file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    
    name = doc.file_name.lower()
    text = ""
    
    if name.endswith('.txt'):
        text = buf.read().decode('utf-8')
    elif name.endswith('.pdf'):
        # Пробуем текстовый слой
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(buf)
            text = '\n'.join([p.extract_text() or '' for p in reader.pages[:5]])
        except:
            pass
        
        # Если мало текста — OCR
        if len(text.strip()) < 100:
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
    
    caption = update.message.caption or "Проанализируй"
    resp = await ask_kimi(make_prompt(caption, text))
    
    # Чистим мусор
    clean = resp.replace('ОПРЕДЕЛИ ЗАДАЧУ:', '').replace('ВЫБЕРИ ФОРМАТ:', '').replace('ВЫПОЛНИ ЗАДАЧУ:', '')
    clean = clean.replace('---', '').strip()
    
    await update.message.reply_text(clean[:4000])

# ========== ЗАПУСК ==========

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()

if __name__ == '__main__':
    main()
