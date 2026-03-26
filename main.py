import os
import logging
import base64
import re
import aiohttp
from io import BytesIO

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode

# Библиотеки для создания векторной этикетки из фото
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.graphics.barcode import createBarcodeDrawing

# Библиотеки для работы с оригинальным качеством PDF
try:
    from PyPDF2 import PdfReader, PdfWriter
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

# БРОНЕБОЙНАЯ ИНСТРУКЦИЯ ДЛЯ ИИ
SYSTEM_MSG_NAMING = (
    "Ты — бездушный алгоритм перевода и генерации имен файлов. Ты выдаешь ТОЛЬКО имя файла и больше ничего.\n"
    "ФОРМАТ СТРОГО: КитайскийПеревод_EnglishTranslation_Размер_Артикул_Штрихкод.pdf\n"
    "ПРАВИЛА (НАРУШЕНИЕ СМЕРТЕЛЬНО):\n"
    "1. Ты ОБЯЗАН перевести название товара на КИТАЙСКИЙ (иероглифы) и АНГЛИЙСКИЙ.\n"
    "2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать русские буквы (кириллицу) в ответе!\n"
    "3. ЗАПРЕЩЕНО писать слова 'None', 'Null', 'БезРазмера'. Просто пропускай эти блоки.\n"
    "Пример: 梳子_Hairbrush_wb22tangleteezer_2049653974557.pdf\n"
    "Выведи только имя файла, оканчивающееся на .pdf"
)

def is_valid_ean13(barcode: str) -> bool:
    if not barcode or len(barcode) != 13 or not barcode.isdigit(): return False
    digits = [int(x) for x in barcode]
    checksum = digits.pop()
    return checksum == (10 - ((sum(digits[1::2]) * 3 + sum(digits[0::2])) % 10)) % 10

def generate_vector_label_60x40(barcode_num, article):
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(60*mm, 40*mm))
    if barcode_num:
        try:
            if len(barcode_num) == 13 and barcode_num.isdigit():
                d = createBarcodeDrawing('EAN13', value=barcode_num, barWidth=0.35*mm, barHeight=18*mm)
            else:
                d = createBarcodeDrawing('Code128', value=barcode_num, barWidth=0.35*mm, barHeight=18*mm)
            d.drawOn(c, 7*mm, 15*mm)
        except Exception as e:
            logger.error(f"Barcode error: {e}")
            c.setFont("Helvetica", 12)
            c.drawString(5*mm, 20*mm, f"BC: {barcode_num}")
    if article:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(7*mm, 6*mm, f"Art: {article}")
    c.showPage()
    c.save()
    buf.seek(0)
    return buf

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ИИ-ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64:
        content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}]
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json={'model': model, 'messages': messages, 'temperature': 0.0}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
            return f"Error_{resp.status}"

def find_article(text: str) -> str:
    match = re.search(r'(?:Артикул|Article|Арт\.?)[:\s]+([^\n\r]+)', text, re.IGNORECASE)
    if match: 
        return match.group(1).strip()
    return ""

async def extract_data_from_image(image: Image.Image):
    barcode_num, text, article = "", "", ""
    try:
        codes = decode(image.convert('L'))
        if codes: barcode_num = codes[0].data.decode('utf-8')
    except: pass
    try:
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
    except: pass
    
    article = find_article(text)
    return barcode_num, text, article

async def handle_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text
    if not raw_text: return
    data_to_process = raw_text.replace('/paste', '').strip()
    system_paste = (
        "Ты — технический конвертер. Разбери математическую строку.\n"
        "ПРАВИЛО РАЗБОРА '7.5x200+144=1644 vase':\n"
        "1. Первое число (7.5) -> 'Цена клиенту'.\n"
        "2. Второе число (200) -> 'Количество'.\n"
        "3. Число после + (144) -> 'Доставка'.\n"
        "4. ИГНОРИРУЙ всё после = (1644, 674, 152 — это не курсы!).\n"
        "5. Текст (vase) -> 'Название'.\n\n"
        "ФОРМАТ ОТВЕТА:\n/calc\n\nКлиент: [Имя]\n\nТовар [N]:\nНазвание: [Name]\nКоличество: [Qty]\nЦена клиенту: [Price]\nЗакупка: -\nДоставка: [Logistics]\nРазмеры: - - - -\n\nКурс клиенту: 58\nМой курс: 55"
    )
    try:
        result = await ask_kimi(f"Разобщи данные строго по шаблону /calc:\n{data_to_process}", system_msg=system_paste)
        await update.message.reply_text(result.strip())
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    if text.strip().startswith('/calc'): return
    if text.startswith('/paste'):
        await handle_paste(update, context)
        return
    resp = await ask_kimi(text)
    await update.message.reply_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        caption = update.message.caption or ""
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

        if caption.lower().strip().startswith('/1688'):
            res = await ask_kimi("Supplier Info CN/EN. Code blocks.", image_b64=img_b64, system_msg="1688 Expert.")
            return await update.message.reply_text(res, parse_mode='Markdown')

        if caption.lower().strip().startswith('/hs'):
            res = await ask_kimi(f"HS Code for: {caption}", image_b64=img_b64, system_msg="Customs Broker.")
            return await update.message.reply_text(f"📦 Коды:\n\n{res}", parse_mode='Markdown')

        msg = await update.message.reply_text('⏳ Читаю картинку и генерирую векторный PDF 60x40...')
        
        img_obj = Image.open(BytesIO(buf.getvalue()))
        barcode, text, article = await extract_data_from_image(img_obj)

        # ЖЕСТКИЙ ПРОМПТ ДЛЯ ПЕРЕВОДА ФОТО
        naming_prompt = (
            f"Текст с этикетки: {text[:800]}\n\n"
            "ШАГ 1: Пойми, что это за товар.\n"
            "ШАГ 2: ПЕРЕВЕДИ это слово на КИТАЙСКИЙ язык.\n"
            "ШАГ 3: ПЕРЕВЕДИ это слово на АНГЛИЙСКИЙ язык.\n"
            "ШАГ 4: Собери имя файла по формату. НИКАКОГО РУССКОГО ЯЗЫКА в ответе!"
        )

        new_name = await ask_kimi(naming_prompt, image_b64=img_b64, system_msg=SYSTEM_MSG_NAMING)
        new_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()).replace('.pdf', '') + ".pdf"
        
        vector_pdf = generate_vector_label_60x40(barcode, article)
        
        barcode_status = "❌ Не найден"
        if barcode:
            barcode_status = f"{barcode} (Читается + формат EAN-13 верен)" if is_valid_ean13(barcode) else f"{barcode} (ОШИБКА ФОРМАТА!)"
            
        wb_link_art = article.replace(' ', '')
        article_text = f"{article} 👉 [Посмотреть на WB](https://www.wildberries.ru/catalog/{wb_link_art}/detail.aspx)" if article else "Не найден"
            
        final_text = f"✅ Штрих-код: {barcode_status}\n✅ Артикул: {article_text}\n✨ Сгенерирована векторная этикетка (60x40мм)\n📄 `{new_name}`"
        
        await update.message.reply_document(document=InputFile(vector_pdf, filename=new_name), caption=final_text, parse_mode='Markdown')
        await msg.delete()
        
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text(f"❌ Ошибка обработки фото: {e}")

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        if not doc.file_name.lower().endswith('.pdf'): return
        
        status_msg = await update.message.reply_text("⏳ Анализирую PDF и группирую товары...")
        
        if not HAS_PYPDF:
            await status_msg.edit_text("❌ ОШИБКА: Нет библиотеки PyPDF2. Убедись, что она есть в requirements.txt!")
            return

        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = await file.download_as_bytearray()
        
        reader = PdfReader(BytesIO(pdf_bytes))
        images = convert_from_bytes(bytes(pdf_bytes), dpi=150)
        
        products = {}
        
        for i, img in enumerate(images):
            barcode = ""
            codes = decode(img.convert('L'))
            if codes: barcode = codes[0].data.decode('utf-8')
            
            key = barcode
            if key and key in products:
                products[key]['pages'].append(i)
                continue
                
            text, article = "", ""
            try:
                text = pytesseract.image_to_string(img, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
                article = find_article(text)
            except: pass
            
            if not key:
                key = article if article else f"unknown_item_{i}"
                
            if key in products:
                products[key]['pages'].append(i)
            else:
                products[key] = {
                    'pages': [i],
                    'text': text,
                    'barcode': barcode,
                    'article': article
                }

        await status_msg.edit_text(f"✅ Найдено товаров: {len(products)}. Генерирую файлы...")

        for key, data in products.items():
            barcode = data['barcode']
            article = data['article']
            text_for_name = data['text'] if data['text'] else "Unknown Product"

            # ЖЕСТКИЙ ПРОМПТ ДЛЯ ПЕРЕВОДА ИЗ PDF
            naming_prompt = (
                f"Текст с этикетки: {text_for_name[:800]}\n\n"
                "ШАГ 1: Пойми, что это за товар.\n"
                "ШАГ 2: ПЕРЕВЕДИ это слово на КИТАЙСКИЙ язык.\n"
                "ШАГ 3: ПЕРЕВЕДИ это слово на АНГЛИЙСКИЙ язык.\n"
                "ШАГ 4: Собери имя файла по формату. НИКАКОГО РУССКОГО ЯЗЫКА в ответе!"
            )

            new_name = await ask_kimi(naming_prompt, system_msg=SYSTEM_MSG_NAMING)
            clean_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()).replace('.pdf', '') + ".pdf"
            
            writer = PdfWriter()
            for p_idx in data['pages']:
                writer.add_page(reader.pages[p_idx])
                
            pdf_out = BytesIO()
            writer.write(pdf_out)
            pdf_out.seek(0)
            
            barcode_status = "❌ Не найден"
            if barcode:
                barcode_status = f"{barcode} (Читается + формат EAN-13 верен)" if is_valid_ean13(barcode) else f"{barcode} (Читается, но ошибка формата!)"

            wb_link_art = article.replace(' ', '')
            article_text = f"{article} 👉 [Посмотреть на WB](https://www.wildberries.ru/catalog/{wb_link_art}/detail.aspx)" if article else "Не найден"

            caption_text = f"📦 СТРАНИЦ В ФАЙЛЕ: {len(data['pages'])}\n✅ Штрих-код: {barcode_status}\n✅ Артикул: {article_text}\n📄 `{clean_name}`"
            
            await update.message.reply_document(document=InputFile(pdf_out, filename=clean_name), caption=caption_text, parse_mode='Markdown')

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Ошибка при обработке PDF: {e}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот готов к работе!")))
    app.add_handler(MessageHandler(filters.Regex(r'^/paste'), handle_paste))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
