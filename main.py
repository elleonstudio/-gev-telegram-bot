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
from pyairtable import Api

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"
AIRTABLE_TABLE_NAME = "Закупка"

SYSTEM_MSG_NAMING = (
    "Ты ассистент по созданию имен файлов. Формат: 中文_English_Размер_Артикул_Штрихкод.pdf\n"
    "Перевод на китайский и английский ОБЯЗАТЕЛЕН."
)

def is_valid_ean13(barcode: str) -> bool:
    if not barcode or len(barcode) != 13 or not barcode.isdigit(): return False
    digits = [int(x) for x in barcode]
    checksum = digits.pop()
    return checksum == (10 - ((sum(digits[1::2]) * 3 + sum(digits[0::2])) % 10)) % 10

def clean_response(text: str) -> str:
    text = re.sub(r'(`|\*+)', '', text)
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return ' '.join(lines)

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = None) -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64: content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    messages = [{'role': 'system', 'content': system_msg or 'Ты ИИ-ассистент.'}, {'role': 'user', 'content': content}]
    
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json={'model': model, 'messages': messages, 'temperature': 0.05}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return clean_response(res['choices'][0]['message']['content'])
            return f"Error_{resp.status}"

async def extract_image_data(image: Image.Image):
    barcode_num, text, article = "", "", ""
    try:
        codes = decode(image.convert('L'))
        if codes: barcode_num = codes[0].data.decode('utf-8')
    except: pass
    try:
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
    except: pass
    for pattern in [r'Артикул[:\s]+(\d+)', r'Артикул[:\s]*(\d+)', r'Article[:\s]+(\d+)']:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            article = match.group(1)
            break
    return barcode_num, text, article

def build_response_lines(new_name, barcode_num, article):
    response_lines = [f"📄 `{new_name}`"]
    if barcode_num:
        response_lines.insert(0, f"✅ Штрих-код: {barcode_num}" + (" (Читается + EAN-13 верен)" if is_valid_ean13(barcode_num) else " (ОШИБКА ФОРМАТА!)"))
    else:
        response_lines.insert(0, "❌ Штрих-код: НЕ НАЙДЕН НА ИЗОБРАЖЕНИИ")
    if article:
        response_lines.insert(1, f"✅ Артикул: {article} 👉 [На WB](https://www.wildberries.ru/catalog/{article}/detail.aspx)")
    return response_lines

def parse_airtable_export(text: str) -> dict:
    parsed = {}
    
    # 1. Извлекаем AIRTABLE_EXPORT
    match = re.search(r'AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END', text, re.DOTALL)
    if match:
        for line in match.group(1).strip().split('\n'):
            if ':' in line:
                key, val = line.split(':', 1)
                parsed[key.strip()] = val.strip()

    # 2. Вытаскиваем товары
    invoice_body = text.split('AIRTABLE_EXPORT_START')[0]
    
    compact_items = []
    # Вариант 1: Ищем старый длинный формат и сжимаем его
    pattern_old = r'•\s*(.*?)\s*[—\-].*?\n\s*([\d\.]+)\s*[×xX]\s*([\d\.]+)\s*(?:\+\s*([\d\.]+))?\s*=\s*([\d\.]+)[¥Y]?'
    matches_old = re.findall(pattern_old, invoice_body)
    
    if matches_old:
        for match_item in matches_old:
            name, qty, price, log, total = match_item
            log_str = f"+{log}" if log else ""
            compact_items.append(f"• {name.strip()}: {qty}x{price}{log_str} = {total} ¥")
    else:
        # Вариант 2: Формат уже новый компактный (одна строка)
        for line in invoice_body.split('\n'):
            line = line.strip()
            if line.startswith('•') and '=' in line:
                compact_items.append(line)
    
    # Собираем их в единый список для поля "Заказ"
    if compact_items:
        parsed["Invoice_Body"] = "\n".join(compact_items)
    else:
        # Резервный вариант: чистим текст от мусора
        clean_text = re.sub(r'COMMERCIAL INVOICE:[^\n]*\n?', '', invoice_body, flags=re.IGNORECASE)
        clean_text = re.sub(r'📅?\s*Date:[^\n]*\n?', '', clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(r'[✅📅💰💼⚠️📦📊💾📑]', '', clean_text)
        parsed["Invoice_Body"] = clean_text.strip()
        
    return parsed

async def send_to_airtable(parsed_data: dict):
    try:
        api = Api(AIRTABLE_TOKEN)
        table = api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME)
        
        raw_date = parsed_data.get("Date", "")
        formatted_date = datetime.now().strftime("%Y-%m-%d")
        if "." in raw_date:
            d, m, y = raw_date.split(".")
            formatted_date = f"{y}-{m}-{d}"

        invoice = parsed_data.get("Invoice_ID", "")
        client_name = ""
        match = re.match(r'^([a-zA-Z]+)-?(\d+)', invoice)
        if match:
            client_name = f"{match.group(1).capitalize()}-{match.group(2)}"

        record = {
            "Код Карго": invoice,
            "Дата": formatted_date,
            "Сумма (¥)": float(parsed_data.get("Sum_Client_CNY", 0)),
            "Реал Цена Закупки (¥)": float(parsed_data.get("Real_Purchase_CNY", 0)),
            "Курс Клиент": float(parsed_data.get("Client_Rate", 0)),
            "Курс Реал": float(parsed_data.get("Real_Rate", 0)),
            "Расход материалов (¥)": float(parsed_data.get("China_Logistics_CNY", 0)),
            "Кол-во коробок": int(parsed_data.get("FF_Boxes_Qty", 0)),
            "Заказ": parsed_data.get("Invoice_Body", ""),
            "Карго Статус": "Заказано"
        }
        
        if client_name:
            record["Клиент"] = client_name 

        table.create(record, typecast=True)
        return True, client_name
    except Exception as e:
        logger.error(f"Airtable Error: {e}")
        return False, str(e)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if "AIRTABLE_EXPORT_START" in text:
        msg = await update.message.reply_text("📥 Вижу отчёт GS Orders. Записываю в базу...")
        parsed_data = parse_airtable_export(text)
        
        if not parsed_data:
            return await msg.edit_text("❌ Ошибка: не удалось прочитать блок данных. Проверь формат.")

        success, info = await send_to_airtable(parsed_data)
        if success:
            client_info = f" (Клиент: {info})" if info else ""
            await msg.edit_text(f"✅ Заказ **{parsed_data.get('Invoice_ID', 'N/A')}** успешно добавлен в Airtable{client_info}!\n\nТекст заказа загружен в новом компактном формате.", parse_mode="Markdown")
        else:
            await msg.edit_text(f"❌ Ошибка записи в Airtable: {info}")
        return

    msg = await update.message.reply_text('⏳ Думаю...')
    resp = await ask_kimi(text)
    await msg.edit_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        caption = update.message.caption or ""
        if caption.lower().strip().startswith('/hs'):
            msg = await update.message.reply_text('⏳ Подбираю коды ТН ВЭД (ЕАЭС)...')
            file = await context.bot.get_file(update.message.photo[-1].file_id)
            buf = BytesIO()
            await file.download_to_memory(buf)
            res = await ask_kimi(f"Подбери 2-3 наиболее вероятных 10-значных кода ТН ВЭД ЕАЭС для товара на фото. Описание: {caption.replace('/hs', '')}", image_b64=base64.b64encode(buf.getvalue()).decode('utf-8'), system_msg="Ты таможенный декларант ЕАЭС.")
            codes = set(re.findall(r'(?i)(\d{4,10})', res))
            final_msg = f"📦 **Предполагаемые коды ТН ВЭД:**\n\n{res}\n\n🔍 **Проверить на Alta.ru:**\n"
            for code in codes:
                if len(code) >= 4: final_msg += f"👉 [Код {code}](https://www.alta.ru/tnved/code/{code}/)\n"
            return await msg.edit_text(final_msg, parse_mode='Markdown', disable_web_page_preview=True)

        msg = await update.message.reply_text('⏳ Обработка фото для создания имени файла...')
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        barcode_num, text, article = await extract_image_data(Image.open(buf))
        new_name = await ask_kimi(f"Текст: {text[:2000]}\nШтрих-код: {barcode_num}\nАртикул: {article}\nТолько имя файла в формате 中文_English_Размер_Артикул_Штрихкод.pdf:", image_b64=base64.b64encode(buf.getvalue()).decode('utf-8'), system_msg=SYSTEM_MSG_NAMING)
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name.strip())
        if not new_name.endswith('.pdf'): new_name += '.pdf'
        if len(new_name) < 10: new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"
        await msg.edit_text('\n'.join(build_response_lines(re.sub(r'_{2,}', '_', new_name), barcode_num, article)), parse_mode='Markdown', disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        if doc.file_size > 20 * 1024 * 1024: return await update.message.reply_text('❌ Файл слишком большой (>20MB)')
        msg = await update.message.reply_text('⏳ Обработка PDF...')
        buf = BytesIO()
        await (await context.bot.get_file(doc.file_id)).download_to_memory(buf)
        buf.seek(0)
        images = convert_from_bytes(buf.read(), dpi=250, first_page=1, last_page=1)
        if not images: return await msg.edit_text('❌ Не удалось открыть PDF')
        barcode_num, text, article = await extract_image_data(images[0])
        new_name = await ask_kimi(f"Текст: {text[:2000]}\nШтрих-код: {barcode_num}\nАртикул: {article}\nТолько имя файла в формате 中文_English_Размер_Артикул_Штрихкод.pdf:", system_msg=SYSTEM_MSG_NAMING)
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name.strip())
        if not new_name.endswith('.pdf'): new_name += '.pdf'
        if len(new_name) < 10: new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"
        await msg.delete()
        await update.message.reply_text('\n'.join(build_response_lines(re.sub(r'_{2,}', '_', new_name), barcode_num, article)), parse_mode='Markdown', disable_web_page_preview=True)
        buf.seek(0)
        await update.message.reply_document(document=InputFile(buf, filename=new_name), caption=new_name)
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', lambda u, c: u.message.reply_text("🤖 Бот готов! Жду фото, PDF или отчет из GS Orders.")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
