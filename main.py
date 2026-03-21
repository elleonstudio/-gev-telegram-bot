import os
import logging
import requests
import base64
import re
from io import BytesIO
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
import pytesseract

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

def clean_response(text: str) -> str:
    """Удаляет мусорные слова из ответа"""
    garbage = [
        r'^\d+\.', r'ОПРЕДЕЛИ.*?:', r'ВЫБЕРИ.*?:', r'ВЫПОЛНИ.*?:', 
        r'АНАЛИЗИРУЮ.*?:', r'РАССУЖДАЮ.*?:', r'---', r'===', 
        r'ВЫВОД:', r'РЕЗУЛЬТАТ:', r'ОТВЕТ:', r'\*\*\*'
    ]
    for pattern in garbage:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
    return '\n'.join([l.strip() for l in text.split('\n') if l.strip()])

async def ask_kimi(prompt: str, image_b64: str = None) -> str:
    """Отправляет запрос в Kimi API"""
    try:
        headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
        system_msg = '''Ты помощник для бизнеса. ПРАВИЛА:
1. Отвечай ТОЛЬКО новым именем файла
2. БЕЗ слов: "ОПРЕДЕЛИ", "ВЫБЕРИ", "ВЫПОЛНИ", "1.", "2.", "3."
3. БЕЗ вступлений и заключений
4. Только имя файла, например: "猫玩具_逗猫棒_Cat_Teaser_Toy.pdf"
5. Формат: [китайский]_[английский]_[артикул].pdf'''
        
        if image_b64:
            messages = [
                {'role': 'system', 'content': system_msg},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}}
                ]}
            ]
            model = 'moonshot-v1-8k-vision-preview'
        else:
            messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': prompt}]
            model = 'moonshot-v1-8k'
        
        data = {'model': model, 'messages': messages, 'temperature': 0.1, 'max_tokens': 500}
        r = requests.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json=data, timeout=60)
        
        if r.status_code == 200:
            return clean_response(r.json()['choices'][0]['message']['content'])
        return f"Ошибка_API_{r.status_code}.pdf"
    except Exception as e:
        return f"Ошибка_{str(e)[:20]}.pdf"

async def check_barcodes(file_bytes: BytesIO) -> tuple:
    """Проверяет штрих-коды в PDF, возвращает (штрихкоды_текст, первый_штрихкод)"""
    try:
        from pyzbar.pyzbar import decode
        
        file_bytes.seek(0)
        images = convert_from_bytes(file_bytes.read(), dpi=200)
        
        results = []
        first_barcode = ""
        for i, img in enumerate(images[:3], 1):
            codes = decode(img)
            if codes:
                for code in codes:
                    barcode = code.data.decode('utf-8')
                    if not first_barcode:
                        first_barcode = barcode
                    results.append(f"Стр {i}: {barcode} ✅")
            else:
                results.append(f"Стр {i}: не найден")
        
        return '\n'.join(results) if results else "", first_barcode
    except Exception as e:
        return f"Ошибка: {e}", ""

async def ocr_pdf(file_bytes: BytesIO) -> str:
    """OCR через tesseract"""
    try:
        file_bytes.seek(0)
        images = convert_from_bytes(file_bytes.read(), first_page=1, last_page=1, dpi=200)
        
        texts = []
        for img in images:
            text = pytesseract.image_to_string(img, lang='rus+eng+chi_sim')
            if text.strip():
                texts.append(text.strip())
        
        return '\n'.join(texts)
    except Exception as e:
        return ""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 Отправь PDF с подписью "переименуй"')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        caption = update.message.caption or ""
        original_name = doc.file_name
        
        if doc.file_size > 20*1024*1024:
            await update.message.reply_text('❌ Файл >20MB')
            return
        
        if not original_name.lower().endswith('.pdf'):
            await update.message.reply_text('❌ Только .pdf')
            return
        
        await update.message.reply_text('⏳ Загрузка...')
        
        # Скачиваем файл
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        
        # Проверка штрих-кодов
        barcode_check, barcode_num = "", ""
        if any(word in caption.lower() for word in ['штрих', 'код', 'barcode', 'проверь']):
            await update.message.reply_text('🔍 Проверяю штрих-коды...')
            barcode_check, barcode_num = await check_barcodes(buf)
            buf.seek(0)
        
        # Извлекаем текст (текстовый слой или OCR)
        text = ""
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(buf)
            text = '\n'.join([p.extract_text() or '' for p in reader.pages[:2]])
        except:
            pass
        
        if len(text.strip()) < 30:
            await update.message.reply_text('🔍 Распознаю текст...')
            text = await ocr_pdf(buf)
            buf.seek(0)
        
        # Генерируем новое имя через Kimi
        await update.message.reply_text('🤖 Генерирую имя файла...')
        
        prompt = f"""На основе текста создай новое имя файла.

Текст документа:
{text[:1500]}

Штрих-код: {barcode_num}

Правила для имени:
1. Формат: [Китайский]_[Английский]_[Артикул].pdf
2. Без пробелов, используй _
3. Китайский: короткое название товара
4. Английский: короткое название товара
5. Артикул: из текста или штрих-кода

Примеры:
- 猫玩具_逗猫棒_Cat_Teaser_Toy_881455116.pdf
- 汽车遮阳挡_Car_Sunshade_150x70_881532453.pdf

Только имя файла, без пояснений:"""

        new_name = await ask_kimi(prompt)
        
        # Очищаем имя файла
        new_name = new_name.strip().replace('\n', '_').replace(' ', '_')
        if not new_name.endswith('.pdf'):
            new_name += '.pdf'
        new_name = re.sub(r'[\\/*?:"<>|]', '', new_name)
        
        # Формируем ответ
        response = f"📄 Новое имя: `{new_name}`"
        if barcode_check:
            response = f"📊 Штрих-коды:\n{barcode_check}\n\n{response}"
        
        await update.message.reply_text(response, parse_mode='Markdown')
        
        # Отправляем файл с новым именем
        buf.seek(0)
        await update.message.reply_document(
            document=InputFile(buf, filename=new_name),
            caption=f"✅ Переименовано"
        )
        
    except Exception as e:
        logging.error(f"Doc error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {e}')

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        b64 = base64.b64encode(buf.read()).decode()
        resp = await ask_kimi(update.message.caption or "Опиши", b64)
        await update.message.reply_text(resp[:4000])
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {e}')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    resp = await ask_kimi(update.message.text)
    await update.message.reply_text(resp[:4000])

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logging.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
