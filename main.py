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
                {'role': 'system', 'content': 'Ты помощник для бизнеса. Отвечай коротко, только результат, без объяснений.'},
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
            logging.error(f"Kimi error: {r.status_code} - {r.text}")
            return f"Ошибка API: {r.status_code}"
            
    except Exception as e:
        logging.error(f"Kimi exception: {e}")
        return f"Ошибка: {str(e)}"

# ========== OCR ==========

async def ocr_pdf(file_bytes: BytesIO) -> str:
    from pdf2image import convert_from_bytes
    import pytesseract
    
    file_bytes.seek(0)
    images = convert_from_bytes(file_bytes.read(), first_page=1, last_page=3, dpi=200)
    
    texts = []
    for i, img in enumerate(images):
        text = pytesseract.image_to_string(img, lang='rus+eng')
        if text.strip():
            texts.append(f"--- Страница {i+1} ---\n{text.strip()}")
    
    return '\n\n'.join(texts)

# ========== ПРОВЕРКА ШТРИХ-КОДОВ ==========

def check_barcodes(text: str) -> dict:
    """Находит и проверяет штрих-коды в тексте"""
    # Ищем цифры длиной 8-13 символов (EAN-8, EAN-13, и т.д.)
    barcodes = re.findall(r'\b\d{8,13}\b', text.replace(' ', '').replace('\n', ''))
    
    # Убираем дубликаты
    unique_barcodes = list(set(barcodes))
    
    result = {
        'found': unique_barcodes,
        'count': len(unique_barcodes),
        'by_page': {}
    }
    
    # Ищем по страницам
    pages = text.split('--- Страница')
    for i, page in enumerate(pages[1:], 1):
        page_barcodes = re.findall(r'\b\d{8,13}\b', page.replace(' ', '').replace('\n', ''))
        if page_barcodes:
            result['by_page'][f'Стр {i}'] = list(set(page_barcodes))
    
    return result

def validate_barcode(barcode: str) -> bool:
    """Проверка контрольной суммы EAN-13"""
    if len(barcode) != 13:
        return len(barcode) in [8, 12, 13]  # Допустимые длины
    
    # EAN-13 checksum
    odd = sum(int(barcode[i]) for i in range(0, 12, 2))
    even = sum(int(barcode[i]) for i in range(1, 12, 2))
    checksum = (10 - (odd + even * 3) % 10) % 10
    
    return checksum == int(barcode[12])

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
            
            prompt = f"Краткое содержание:\n\n{content}\n\n3-5 пунктов:"
            resp = await ask_kimi(prompt)
            await update.message.reply_text(resp[:4000])
        except Exception as e:
            await update.message.reply_text(f'❌ Ошибка: {e}')
    else:
        prompt = f"{text}\n\nКоротко:"
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
        caption = update.message.caption or "Опиши"
        
        prompt = f"{caption}\n\nКоротко, факты:"
        resp = await ask_kimi(prompt, b64)
        
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
            # Текстовый слой
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(buf)
                text = '\n'.join([p.extract_text() or '' for p in reader.pages[:3]])
            except:
                pass
            
            # OCR
            if len(text.strip()) < 50:
                await update.message.reply_text('🔍 OCR...')
                text = await ocr_pdf(buf)
        else:
            await update.message.reply_text('❌ Только .pdf или .txt')
            return
        
        if not text.strip():
            await update.message.reply_text('⚠️ Нет текста')
            return
        
        # Проверка штрих-кодов
        barcode_info = check_barcodes(text)
        
        # Формируем промпт
        caption = update.message.caption or "Проанализируй документ"
        
        barcode_list = ', '.join(barcode_info['found']) if barcode_info['found'] else 'НЕ НАЙДЕНЫ'
        
        prompt = f"""ЗАДАЧА: {caption}

ТЕКСТ:
{text[:2500]}

НАЙДЕННЫЕ ШТРИХ-КОДЫ: {barcode_list}

ВЫДАЙ РЕЗУЛЬТАТ:
1. Штрих-коды: перечисли все с проверкой (✅ корректен / ❌ ошибка)
2. Новое имя файла на китайском-английском с артикулом и штрих-кодом
3. Размеры: укажи из текста (если есть)
4. Проверка: что не так, чего не хватает

Коротко, без лишних слов."""

        await update.message.reply_text('🤖 Анализ...')
        resp = await ask_kimi(prompt)
        
        # Добавляем свою проверку штрих-кодов
        barcode_check = "\n\n📊 *Проверка штрих-кодов:*\n"
        if barcode_info['found']:
            for bc in barcode_info['found']:
                valid = validate_barcode(bc)
                barcode_check += f"• `{bc}` {'✅' if valid else '❌'}\n"
        else:
            barcode_check += "• ❌ Не найдены\n"
        
        # Итог
        final = resp + barcode_check
        
        # Чистим
        for bad in ['ОПРЕДЕЛИ', 'ВЫБЕРИ', 'ВЫПОЛНИ', '---', 'ЗАДАЧА:', 'ФОРМАТ:', 'ЯЗЫК:']:
            final = final.replace(bad, '')
        
        await update.message.reply_text(final.strip()[:4000])
        
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
