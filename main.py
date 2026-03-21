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
    garbage = [
        r'^\d+\.', r'ОПРЕДЕЛИ.*?:', r'ВЫБЕРИ.*?:', r'ВЫПОЛНИ.*?:', 
        r'АНАЛИЗИРУЮ.*?:', r'РАССУЖДАЮ.*?:', r'---', r'===', 
        r'ВЫВОД:', r'РЕЗУЛЬТАТ:', r'ОТВЕТ:', r'\*\*\*', r'•',
        r'список\s*[-–]', r'Проблемы:.*', r'Размеры:.*', r'Проверка:.*',
    ]
    for pattern in garbage:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
    
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return ' '.join(lines)

async def ask_kimi(prompt: str, image_b64: str = None) -> str:
    try:
        headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
        system_msg = '''Ты создаёшь имена файлов для товаров. 

СТРУКТУРА имени файла:
中文_English_Размер_Артикул_Штрихкод.pdf

Примеры:
- 汽车遮阳挡_Car_Sunshade_150x70_881532453_2049622662683.pdf
- 猫玩具逗猫棒_Cat_Teaser_Toy_881455116_2049621889739.pdf
- 狗玩具套装_Dog_Toy_Set_8in1_881463309_2049621987510.pdf

ПРАВИЛА:
1. ТОЛЬКО имя файла
2. ВСЕГДА переводи на китайский (简体中文)
3. Размер бери из текста (150x70, 200x100 и т.д.)
4. Артикул обычно 6-9 цифр
5. Штрих-код обычно 13 цифр начинается с 20'''
        
        if image_b64:
            messages = [{'role': 'system', 'content': system_msg},
                       {'role': 'user', 'content': [{'type': 'text', 'text': prompt}, {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}}]}]
            model = 'moonshot-v1-8k-vision-preview'
        else:
            messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': prompt}]
            model = 'moonshot-v1-8k'
        
        data = {'model': model, 'messages': messages, 'temperature': 0.05, 'max_tokens': 200}
        r = requests.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json=data, timeout=60)
        
        if r.status_code == 200:
            return clean_response(r.json()['choices'][0]['message']['content'])
        return f"Error_{r.status_code}.pdf"
    except Exception as e:
        return f"Error_{str(e)[:20]}.pdf"

async def check_barcodes(file_bytes: BytesIO) -> tuple:
    try:
        from pyzbar.pyzbar import decode
        
        for dpi in [300, 200, 150]:
            try:
                file_bytes.seek(0)
                images = convert_from_bytes(file_bytes.read(), dpi=dpi, first_page=1, last_page=1)
                
                if not images:
                    continue
                
                for img in images:
                    img_gray = img.convert('L')
                    codes = decode(img_gray)
                    
                    if codes:
                        barcode = codes[0].data.decode('utf-8')
                        return barcode, barcode
            except:
                continue
        
        return "", ""
    except:
        return "", ""

async def ocr_pdf(file_bytes: BytesIO) -> str:
    try:
        file_bytes.seek(0)
        images = convert_from_bytes(file_bytes.read(), first_page=1, last_page=1, dpi=200)
        
        texts = []
        for img in images:
            text = pytesseract.image_to_string(img, lang='rus+eng+chi_sim')
            if text.strip():
                texts.append(text.strip())
        
        return '\n'.join(texts)
    except:
        return ""

async def extract_article(text: str) -> str:
    """Извлекает артикул из текста"""
    patterns = [
        r'Артикул[:\s]+(\d+)',
        r'Артикул[:\s]*(\d+)',
        r'Article[:\s]+(\d+)',
        r'арт\.?[:\s]*(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 Отправь PDF с этикеткой товара')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        original_name = doc.file_name
        
        if doc.file_size > 20*1024*1024:
            await update.message.reply_text('❌ Файл >20MB')
            return
        
        if not original_name.lower().endswith('.pdf'):
            await update.message.reply_text('❌ Только .pdf')
            return
        
        await update.message.reply_text('⏳ Обработка...')
        
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        
        # Штрих-код
        barcode_num, _ = await check_barcodes(buf)
        
        # OCR
        text = await ocr_pdf(buf)
        
        # Артикул
        article = await extract_article(text)
        
        prompt = f"""Создай имя файла по структуре:

РАСПОЗНАННЫЙ ТЕКСТ:
{text[:1500]}

НАЙДЕННЫЕ ДАННЫЕ:
- Штрих-код: {barcode_num}
- Артикул: {article}

СТРУКТУРА ИМЕНИ ФАЙЛА:
中文_English_Размер_Артикул_Штрихкод.pdf

ШАГИ:
1. Найди название товара на русском
2. ПЕРЕВЕДИ на китайский (简体中文)
3. Напиши на английском
4. Найди размер (например: 150x70, 200x100)
5. Добавь артикул: {article if article else 'из текста'}
6. Добавь штрих-код: {barcode_num if barcode_num else 'из текста'}

Примеры имён:
汽车遮阳挡_Car_Sunshade_150x70_881532453_2049622662683.pdf
猫玩具逗猫棒_Cat_Teaser_Toy_881455116_2049621889739.pdf

Только имя файла:"""

        new_name = await ask_kimi(prompt)
        
        # Очистка
        new_name = new_name.strip()
        if not new_name.endswith('.pdf'):
            new_name += '.pdf'
        new_name = re.sub(r'[\\/*?:"<>|]', '', new_name)
        new_name = re.sub(r'_{2,}', '_', new_name)
        
        # Проверяем что имя не пустое
        if len(new_name) < 10:
            new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"
        
        # Формируем ответ
        response_lines = [f"📄 {new_name}"]
        if barcode_num:
            response_lines.insert(0, f"✅ Штрих-код: {barcode_num}")
        if article:
            response_lines.insert(1, f"✅ Артикул: {article}")
        
        await update.message.reply_text('\n'.join(response_lines))
        
        # Отправляем файл
        buf.seek(0)
        await update.message.reply_document(
            document=InputFile(buf, filename=new_name),
            caption=new_name
        )
        
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

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
