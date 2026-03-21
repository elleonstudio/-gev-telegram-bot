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
        text = pytesseract.image_to_string(img, lang='rus+eng')
        if text.strip():
            texts.append(f"--- Страница {i+1} ---\n{text.strip()}")
    
    return '\n\n'.join(texts)

# ========== ПРОВЕРКА ШТРИХ-КОДОВ ==========

def check_barcodes(text: str) -> dict:
    """Находит штрих-коды в тексте (EAN-8, EAN-13, EAN-14, ITF-14, и т.д.)"""
    
    # Убираем пробелы и переносы для поиска
    clean_text = text.replace(' ', '').replace('\n', '').replace('\t', '')
    
    # Ищем цифры длиной 8-14 символов (все типы штрих-кодов)
    # EAN-8 (8), EAN-12 (12), EAN-13 (13), EAN-14 (14), ITF-14 (14)
    barcodes = re.findall(r'\b\d{8,14}\b', clean_text)
    
    # Фильтруем: убираем короткие числа (артикулы обычно 7-9 цифр)
    # Оставляем только потенциальные штрих-коды (12, 13, 14 цифр)
    valid_lengths = [12, 13, 14]
    filtered = [b for b in barcodes if len(b) in valid_lengths]
    
    # Убираем дубликаты сохраняя порядок
    seen = set()
    unique = []
    for b in filtered:
        if b not in seen:
            seen.add(b)
            unique.append(b)
    
    return {
        'found': unique,
        'count': len(unique),
        'raw': barcodes  # для отладки
    }

def validate_ean13(barcode: str) -> bool:
    """Проверка контрольной суммы EAN-13"""
    if len(barcode) != 13:
        return None  # Не EAN-13
    
    try:
        odd = sum(int(barcode[i]) for i in range(0, 12, 2))
        even = sum(int(barcode[i]) for i in range(1, 12, 2))
        checksum = (10 - (odd + even * 3) % 10) % 10
        return checksum == int(barcode[12])
    except:
        return False

def validate_ean14(barcode: str) -> bool:
    """Проверка контрольной суммы EAN-14 (ITF-14)"""
    if len(barcode) != 14:
        return None
    
    try:
        # EAN-14: веса 3 и 1 чередуются с конца
        total = 0
        for i, digit in enumerate(reversed(barcode[:-1])):
            weight = 3 if i % 2 == 0 else 1
            total += int(digit) * weight
        
        checksum = (10 - (total % 10)) % 10
        return checksum == int(barcode[13])
    except:
        return False

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
        
        prompt = f"Кратко:\n\n{content}"
        resp = await ask_kimi(prompt)
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
        
        # Проверка штрих-кодов
        barcode_info = check_barcodes(text)
        logging.info(f"Найдены штрих-коды: {barcode_info}")
        
        # Формируем список для промпта
        if barcode_info['found']:
            barcode_str = ', '.join(barcode_info['found'])
        else:
            barcode_str = "НЕ НАЙДЕНЫ"
        
        caption = update.message.caption or "Проанализируй"
        
        prompt = f"""ЗАДАЧА: {caption}

ТЕКСТ ДОКУМЕНТА:
{text[:2000]}

НАЙДЕННЫЕ ШТРИХ-КОДЫ ({barcode_info['count']}): {barcode_str}

ВЫДАЙ:
1. Штрих-коды: список всех с длиной
2. Новое имя файла: китайский_английский_артикул_штрихкод.pdf
3. Размеры: из текста
4. Чего не хватает

Коротко."""

        await update.message.reply_text('🤖 Анализ...')
        resp = await ask_kimi(prompt)
        
        # Добавляем свою проверку
        result = resp + "\n\n📊 *Проверка штрих-кодов:*\n"
        
        if barcode_info['found']:
            for bc in barcode_info['found']:
                ean13 = validate_ean13(bc)
                ean14 = validate_ean14(bc)
                
                if ean13 is True:
                    result += f"• `{bc}` (13) ✅ EAN-13\n"
                elif ean14 is True:
                    result += f"• `{bc}` (14) ✅ EAN-14\n"
                elif ean13 is False or ean14 is False:
                    result += f"• `{bc}` ({len(bc)}) ❌ Ошибка контрольной суммы\n"
                else:
                    result += f"• `{bc}` ({len(bc)}) ⚠️ Неизвестный формат\n"
        else:
            result += "• ❌ Не найдены\n"
        
        # Чистим мусор
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
