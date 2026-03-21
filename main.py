import os
import logging
import requests
import base64
import re
from io import BytesIO
from telegram import Update, File
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bs4 import BeautifulSoup

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

# ========== KIMI API ==========

async def ask_kimi(prompt: str, image_b64: str = None) -> str:
    try:
        headers = {
            'Authorization': f'Bearer {KIMI_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        if image_b64:
            messages = [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}}
                ]
            }]
            model = 'moonshot-v1-8k-vision-preview'
        else:
            messages = [
                {'role': 'system', 'content': 'Ты помощник для бизнеса. Отвечай коротко, только результат.'},
                {'role': 'user', 'content': prompt}
            ]
            model = 'moonshot-v1-8k'
        
        data = {
            'model': model,
            'messages': messages,
            'temperature': 0.1
        }
        
        r = requests.post(
            'https://api.moonshot.cn/v1/chat/completions',
            headers=headers,
            json=data,
            timeout=60
        )
        
        if r.status_code == 200:
            return r.json()['choices'][0]['message']['content']
        else:
            logging.error(f"Kimi error: {r.status_code}")
            return f"Ошибка API: {r.status_code}"
            
    except Exception as e:
        logging.error(f"Kimi exception: {e}")
        return f"Ошибка: {str(e)}"

# ========== OCR ==========

async def ocr_pdf(file_bytes: BytesIO) -> str:
    from pdf2image import convert_from_bytes
    import pytesseract
    
    file_bytes.seek(0)
    images = convert_from_bytes(file_bytes.read(), first_page=1, last_page=2, dpi=200)
    
    texts = []
    for i, img in enumerate(images):
        text = pytesseract.image_to_string(img, lang='rus+eng+chi_sim')
        if text.strip():
            texts.append(f"--- Страница {i+1} ---\n{text.strip()}")
    
    return '\n\n'.join(texts)

# ========== ПРОВЕРКА ШТРИХ-КОДОВ ==========

def check_barcodes(text: str) -> list:
    """Находит штрих-коды 12-14 цифр"""
    clean = text.replace(' ', '').replace('\n', '').replace('\t', '')
    codes = re.findall(r'\b\d{12,14}\b', clean)
    
    # Уникальные, сохраняя порядок
    seen = set()
    result = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            result.append(c)
    
    return result

# ========== ОБРАБОТЧИКИ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 Отправь PDF/фото')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text.startswith('http'):
        await update.message.reply_text('🌐 Загружаю...')
        soup = BeautifulSoup(requests.get(text, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10).text, 'html.parser')
        for tag in soup(['script', 'style']): tag.decompose()
        content = soup.get_text(separator='\n', strip=True)[:3000]
        
        resp = await ask_kimi(f"Кратко:\n\n{content}")
        await update.message.reply_text(resp[:4000])
    else:
        resp = await ask_kimi(text)
        await update.message.reply_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        buf = BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        
        b64 = base64.b64encode(buf.read()).decode()
        caption = update.message.caption or "Опиши"
        
        resp = await ask_kimi(f"{caption}\n\nКоротко:", b64)
        await update.message.reply_text(resp[:4000])
    except Exception as e:
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
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(buf)
                text = '\n'.join([p.extract_text() or '' for p in reader.pages[:3]])
            except:
                pass
            
            if len(text.strip()) < 50:
                await update.message.reply_text('🔍 OCR...')
                text = await ocr_pdf(buf)
        else:
            await update.message.reply_text('❌ Только .pdf или .txt')
            return
        
        if not text.strip():
            await update.message.reply_text('⚠️ Нет текста')
            return
        
        # Ищем данные
        barcodes = check_barcodes(text)
        logging.info(f"Штрих-коды: {barcodes}")
        
        caption = update.message.caption or "Переименуй файл по правилу"
        
        # ЖЁСТКИЙ ПРОМПТ для переименования
        prompt = f"""ДАННЫЕ ИЗ ДОКУМЕНТА:
{text[:1500]}

НАЙДЕННЫЕ ШТРИХ-КОДЫ: {', '.join(barcodes) if barcodes else 'НЕТ'}

ЗАДАЧА: Переименуй файл СТРОГО по формату:
китайское_название_английское_название_размер_артикул_штрихкод.pdf

ПРИМЕРЫ:
- 修身连体衣_Slimming_bodysuit_S_887042518_2049687381208.pdf
- 汽车遮阳挡_Car_Sunshade_150x65_881521370_2049622662683.pdf

ПРАВИЛА:
1. Китайское название (если нет — переведи с русского)
2. Английское название (перевод)
3. Размер (S, M, L, 150x65 и т.д.)
4. Артикул (цифры после "Артикул:" или "Art.")
5. Штрих-код (12-14 цифр)

ВЫДАЙ ТОЛЬКО:
📊 Штрих-коды: список
📄 Новое имя: точно по формату выше
📐 Размеры: что найдено
⚠️ Проблемы: чего не хватает

Без объяснений, только результат."""

        await update.message.reply_text('🤖 Анализ...')
        resp = await ask_kimi(prompt)
        
        # Добавляем проверку штрих-кодов
        result = resp + "\n\n🔍 *Проверка:*\n"
        if barcodes:
            for bc in barcodes:
                result += f"• `{bc}` ({len(bc)} цифр)\n"
        else:
            result += "• ❌ Не найдены\n"
        
        # Чистим
        for bad in ['ОПРЕДЕЛИ', 'ВЫБЕРИ', 'ВЫПОЛНИ', 'ЗАДАЧА:', 'ФОРМАТ:']:
            result = result.replace(bad, '')
        
        await update.message.reply_text(result.strip()[:4000])
        
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
