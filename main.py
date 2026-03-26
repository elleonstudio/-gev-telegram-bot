import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode

# ЖЕСТКАЯ ПРОВЕРКА PyPDF2
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

# Инструкция для ИИ
SYSTEM_MSG_NAMING = (
    "Ты эксперт по неймингу файлов. Твоя задача — создать имя файла по тексту с этикетки.\n"
    "ОБЯЗАТЕЛЬНО переведи название товара на английский язык!\n"
    "Формат СТРОГО такой:\n"
    "Иероглифы_EnglishName_Размер_Артикул_Штрихкод.pdf\n"
    "Пример: 炒锅_CastIronPan_23x23x4_747232933_2048244245878.pdf\n"
    "Если чего-то нет, пиши 'None'. Никаких лишних слов, только имя файла."
)

def is_valid_ean13(barcode: str) -> bool:
    if not barcode or len(barcode) != 13 or not barcode.isdigit(): return False
    digits = [int(x) for x in barcode]
    checksum = digits.pop()
    return checksum == (10 - ((sum(digits[1::2]) * 3 + sum(digits[0::2])) % 10)) % 10

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

async def extract_data_from_image(image: Image.Image):
    barcode_num, text, article = "", "", ""
    try:
        codes = decode(image.convert('L'))
        if codes: barcode_num = codes[0].data.decode('utf-8')
    except: pass
    try:
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
    except: pass
    
    # Улучшенный поиск артикула
    for pattern in [r'Артикул[:\s]*(\d+)', r'Артикул.*?(\d{5,})', r'Article[:\s]*(\d+)']:
        match = re.search(pattern, text, re.IGNORECASE)
        if match: 
            article = match.group(1)
            break
            
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
    prompt = f"Разобщи данные строго по шаблону /calc:\n{data_to_process}"
    try:
        result = await ask_kimi(prompt, system_msg=system_paste)
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
            codes = re.findall(r'\b\d{4,10}\b', res)
            final_msg = f"📦 Коды:\n\n{res}\n\n🔍 База:\n"
            for code in set(codes): final_msg += f"👉 [Код {code}](https://www.alta.ru/tnved/code/{code}/)\n"
            return await update.message.reply_text(final_msg, parse_mode='Markdown', disable_web_page_preview=True)

        msg = await update.message.reply_text('⏳ Обрабатываю этикетку...')
        barcode, text, article = await extract_data_from_image(Image.open(BytesIO(buf.getvalue())))
        new_name = await ask_kimi(f"File naming. Text: {text}", image_b64=img_b64, system_msg=SYSTEM_MSG_NAMING)
        new_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()).replace('.pdf', '') + ".pdf"
        
        barcode_status = "❌ Не найден"
        if barcode:
            barcode_status = f"{barcode} (Читается + формат EAN-13 верен)" if is_valid_ean13(barcode) else f"{barcode} (ОШИБКА ФОРМАТА!)"
            
        # Формируем красивый текст для артикула с WB ссылкой
        article_text = f"{article} 👉 [Посмотреть на WB](https://www.wildberries.ru/catalog/{article}/detail.aspx)" if article else "Не найден"
            
        final_text = f"✅ Штрих-код: {barcode_status}\n✅ Артикул: {article_text}\n📄 `{new_name}`"
        await msg.edit_text(final_text, parse_mode='Markdown', disable_web_page_preview=True)
    except Exception: pass

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        if not doc.file_name.lower().endswith('.pdf'): return
        
        status_msg = await update.message.reply_text("⏳ Анализирую PDF и проверяю штрих-коды...")
        
        if not HAS_PYPDF:
            await status_msg.edit_text("❌ ОШИБКА СЕРВЕРА: Библиотека PyPDF2 не загрузилась!\nПожалуйста, убедись, что она есть в requirements.txt и ПЕРЕЗАПУСТИ проект в Railway (сделай Deploy). Без нее качество будет низким, поэтому я остановил процесс.")
            return

        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = await file.download_as_bytearray()
        
        reader = PdfReader(BytesIO(pdf_bytes))
        images = convert_from_bytes(bytes(pdf_bytes), dpi=200)
        seen_barcodes = {} 
        
        for i, img in enumerate(images):
            barcode, text, article = await extract_data_from_image(img)
            key = barcode if barcode else (article if article else f"unknown_{i}")
            
            if key not in seen_barcodes:
                new_name = await ask_kimi(f"Сгенерируй имя по тексту: {text[:500]}", system_msg=SYSTEM_MSG_NAMING)
                clean_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()).replace('.pdf', '') + ".pdf"
                seen_barcodes[key] = {
                    'page_index': i,
                    'filename': clean_name,
                    'barcode': barcode,
                    'article': article
                }

        files_summary = []
        for key, data in seen_barcodes.items():
            fname = data['filename']
            barcode = data['barcode']
            article = data['article']

            barcode_status = "❌ Не найден"
            if barcode:
                barcode_status = f"{barcode} (Читается + формат EAN-13 верен)" if is_valid_ean13(barcode) else f"{barcode} (Читается, но ошибка формата!)"

            article_text = f"{article} 👉 [Посмотреть на WB](https://www.wildberries.ru/catalog/{article}/detail.aspx)" if article else "Не найден"

            # Вырезаем оригинальную страницу
            writer = PdfWriter()
            writer.add_page(reader.pages[data['page_index']])
            
            pdf_out = BytesIO()
            writer.write(pdf_out)
            pdf_out.seek(0)
            
            await update.message.reply_document(document=InputFile(pdf_out, filename=fname))

            files_summary.append(f"✅ Штрих-код: {barcode_status}\n✅ Артикул: {article_text}\n📄 `{fname}`")

        summary_text = f"✅ **Анализ PDF завершен!**\nНайдено уникальных товаров: {len(seen_barcodes)}\n\n" + "\n\n---\n\n".join(files_summary)
        
        # Отправляем сообщение без предпросмотра ссылок, чтобы не засорять чат картинками с WB
        await status_msg.edit_text(summary_text, parse_mode='Markdown', disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Ошибка при обработке PDF: {e}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот готов!")))
    app.add_handler(MessageHandler(filters.Regex(r'^/paste'), handle_paste))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
