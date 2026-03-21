import os
import logging
import requests
import base64
import re
from io import BytesIO
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
from PIL import Image
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

async def check_barcodes_image(image: Image.Image) -> str:
    """Проверяет штрих-коды на изображении"""
    try:
        from pyzbar.pyzbar import decode
        codes = decode(image.convert('L'))
        if codes:
            return codes[0].data.decode('utf-8')
        return ""
    except:
        return ""

async def ocr_image(image: Image.Image) -> str:
    """OCR для изображения"""
    try:
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim')
        return text
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
    await update.message.reply_text('🤖 Отправь PDF или фото с этикеткой товара')

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото (JPG, PNG)"""
    try:
        await update.message.reply_text('⏳ Обработка фото...')
        
        # Получаем фото (самое большое)
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        buf = BytesIO()
        await file.download_to_memory(buf)
        
        # Открываем как изображение
        image = Image.open(buf)
        
        # Проверяем штрих-код
        barcode_num = await check_barcodes_image(image)
        
        # OCR
        text = await ocr_image(image)
        
        # Артикул
        article = await extract_article(text)
        
        prompt = f"""Создай имя файла для товара на фото.

Распознанный текст:
{text[:1500]}

НАЙДЕННЫЕ ДАННЫЕ:
- Штрих-код: {barcode_num}
- Артикул: {article}

СТРУКТУРА ИМЕНИ ФАЙЛА:
中文_English_Размер_Артикул_Штрихкод.pdf

ШАГИ:
1. Определи что за товар
2. ПЕРЕВЕДИ на китайский (简体中文)
3. Напиши на английском
4. Найди размер
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
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
        new_name = re.sub(r'_{2,}', '_', new_name)
        
        if len(new_name) < 10:
            new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"
        
        # Формируем ответ
        response_lines = [f"📄 {new_name}"]
        if barcode_num:
            response_lines.insert(0, f"✅ Штрих-код: {barcode_num}")
        if article:
            response_lines.insert(1, f"✅ Артикул: {article}")
        
        await update.message.reply_text('\n'.join(response_lines))
        
    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text(f'❌ Ошибка обработки фото: {str(e)[:200]}')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка документов (PDF)"""
    try:
        doc = update.message.document
        original_name = doc.file_name
        
        if doc.file_size > 20*1024*1024:
            await update.message.reply_text('❌ Файл >20MB')
            return
        
        if not original_name.lower().endswith('.pdf'):
            # Если не PDF, пробуем как изображение
            try:
                file = await context.bot.get_file(doc.file_id)
                buf = BytesIO()
                await file.download_to_memory(buf)
                image = Image.open(buf)
                
                # Обрабатываем как фото
                await update.message.reply_text('⏳ Обработка изображения...')
                
                barcode_num = await check_barcodes_image(image)
                text = await ocr_image(image)
                article = await extract_article(text)
                
                prompt = f"""Создай имя файла:

Текст: {text[:1500]}
Штрих-код: {barcode_num}
Артикул: {article}

Структура: 中文_English_Размер_Артикул_Штрихкод.pdf

Только имя:"""
                
                new_name = await ask_kimi(prompt)
                new_name = new_name.strip()
                if not new_name.endswith('.pdf'):
                    new_name += '.pdf'
                new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
                
                await update.message.reply_text(f"✅ Штрих-код: {barcode_num}\n📄 {new_name}")
                return
                
            except Exception as e:
                await update.message.reply_text('❌ Только .pdf или изображения')
                return
        
        await update.message.reply_text('⏳ Обработка PDF...')
        
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        
        # Штрих-код из PDF
        from pyzbar.pyzbar import decode
        barcode_num = ""
        for dpi in [300, 200, 150]:
            try:
                buf.seek(0)
                images = convert_from_bytes(buf.read(), dpi=dpi, first_page=1, last_page=1)
                for img in images:
                    codes = decode(img.convert('L'))
                    if codes:
                        barcode_num = codes[0].data.decode('utf-8')
                        break
                if barcode_num:
                    break
            except:
                continue
        
        # OCR
        buf.seek(0)
        images = convert_from_bytes(buf.read(), first_page=1, last_page=1, dpi=200)
        text = ""
        for img in images:
            text += pytesseract.image_to_string(img, lang='rus+eng+chi_sim')
        
        article = await extract_article(text)
        
        prompt = f"""Создай имя файла:

Текст PDF:
{text[:1500]}

Штрих-код: {barcode_num}
Артикул: {article}

Структура: 中文_English_Размер_Артикул_Штрихкод.pdf

Примеры:
汽车遮阳挡_Car_Sunshade_150x70_881532453_2049622662683.pdf
猫玩具逗猫棒_Cat_Teaser_Toy_881455116_2049621889739.pdf

Только имя файла:"""

        new_name = await ask_kimi(prompt)
        
        new_name = new_name.strip()
        if not new_name.endswith('.pdf'):
            new_name += '.pdf'
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
        new_name = re.sub(r'_{2,}', '_', new_name)
        
        if len(new_name) < 10:
            new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"
        
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
        logging.error(f"Doc error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

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
