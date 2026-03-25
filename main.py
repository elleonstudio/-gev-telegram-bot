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

# Токены (Telegram и Kimi берутся из ENV сервера)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

# Токены Airtable (Baza 2026)
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX" # Чистый ID базы
AIRTABLE_TABLE_NAME = "Закупка"

# Системные промпты
SYSTEM_MSG_NAMING = (
    "Ты ассистент по созданию имен файлов. Формат: 中文_English_Размер_Артикул_Штрихкод.pdf\n"
    "Перевод на китайский и английский ОБЯЗАТЕЛЕН."
)

def is_valid_ean13(barcode: str) -> bool:
    if not barcode or len(barcode) != 13 or not barcode.isdigit(): return False
    digits = [int(x) for x in barcode]
    checksum = digits.pop()
    sum_even = sum(digits[1::2]) * 3
    sum_odd = sum(digits[0::2])
    total = sum_even + sum_odd
    return checksum == (10 - (total % 10)) % 10

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
    data = {'model': model, 'messages': messages, 'temperature': 0.05}
    
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json=data) as resp:
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
        if is_valid_ean13(barcode_num):
            response_lines.insert(0, f"✅ Штрих-код: {barcode_num} (Читается + формат EAN-13 верен)")
        else:
            response_lines.insert(0, f"⚠️ Штрих-код: {barcode_num} (Читается, НО ОШИБКА ФОРМАТА!)")
    else:
        response_lines.insert(0, "❌ Штрих-код: НЕ НАЙДЕН НА ИЗОБРАЖЕНИИ")

    if article:
        wb_link = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
        response_lines.insert(1, f"✅ Артикул: {article} 👉 [Посмотреть на WB]({wb_link})")
    return response_lines

# --- ИНТЕГРАЦИЯ С AIRTABLE ---

def parse_airtable_export(text: str) -> dict:
    """Моментально извлекает данные из блока GS Orders без участия ИИ"""
    match = re.search(r'AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END', text, re.DOTALL)
    if not match: return {}
    
    parsed = {}
    for line in match.group(1).strip().split('\n'):
        if ':' in line:
            key, val = line.split(':', 1)
            parsed[key.strip()] = val.strip()
    return parsed

async def send_to_airtable(parsed_data: dict):
    """Отправка распарсенных данных в базу ERAZ ERP (Baza 2026)"""
    try:
        api = Api(AIRTABLE_TOKEN)
        table = api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME)
        
        # Конвертация даты DD.MM.YYYY -> YYYY-MM-DD для Airtable
        raw_date = parsed_data.get("Date", "")
        formatted_date = datetime.now().strftime("%Y-%m-%d")
        if "." in raw_date:
            d, m, y = raw_date.split(".")
            formatted_date = f"{y}-{m}-{d}"

        # Нормализация имени клиента из Инвойса (ZAVEN8291-260325 -> Zaven-8291)
        invoice = parsed_data.get("Invoice_ID", "")
        client_name = ""
        match = re.match(r'^([a-zA-Z]+)-?(\d+)', invoice)
        if match:
            client_name = f"{match.group(1).capitalize()}-{match.group(2)}"

        # Формируем запись строго по твоим колонкам
     record = {
            "Код Карго": invoice,
            "Дата": formatted_date,
            "Сумма (¥)": float(parsed_data.get("Sum_Client_CNY", 0)),
            "Реал Цена Закупки (¥)": float(parsed_data.get("Real_Purchase_CNY", 0)),
            "Курс Клиент": float(parsed_data.get("Client_Rate", 0)),
            "Курс Реал": float(parsed_data.get("Real_Rate", 0)),
            "Расход материалов (¥)": float(parsed_data.get("China_Logistics_CNY", 0)),
            "Кол-во коробок": int(parsed_data.get("FF_Boxes_Qty", 0)), # <--- ИСПОЛЬЗУЕМ ЭТО ПОЛЕ
            "Заказ": f"Количество товаров: {parsed_data.get('Total_Qty', 0)} шт.",
            "Карго Статус": "Заказано"
        }
        
        # Если удалось вытащить клиента, привязываем его
        if client_name:
            record["Клиент"] = client_name # Используем typecast=True для автоматической связи

        # typecast=True обязателен для связи Linked Records (Клиент) и дат!
        table.create(record, typecast=True)
        return True, client_name
    except Exception as e:
        logger.error(f"Airtable Error: {e}")
        return False, str(e)

# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    # 1. Ловим экспорт в Airtable
    if "AIRTABLE_EXPORT_START" in text:
        msg = await update.message.reply_text("📥 Вижу отчёт GS Orders. Записываю в базу...")
        parsed_data = parse_airtable_export(text)
        
        if not parsed_data:
            await msg.edit_text("❌ Ошибка: не удалось прочитать блок данных. Проверь формат.")
            return

        success, info = await send_to_airtable(parsed_data)
        if success:
            client_info = f" (Клиент: {info})" if info else ""
            await msg.edit_text(f"✅ Заказ **{parsed_data.get('Invoice_ID', 'N/A')}** успешно добавлен в Airtable{client_info}!\n\nВсе налоги и прибыль рассчитаются автоматически.", parse_mode="Markdown")
        else:
            await msg.edit_text(f"❌ Ошибка записи в Airtable: {info}")
        return

    # 2. Обычный чат
    msg = await update.message.reply_text('⏳ Думаю...')
    resp = await ask_kimi(text)
    await msg.edit_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        caption = update.message.caption or ""
        # Если это таможенный запрос
        if caption.lower().strip().startswith('/hs'):
            msg = await update.message.reply_text('⏳ Подбираю коды ТН ВЭД (ЕАЭС)...')
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            buf = BytesIO()
            await file.download_to_memory(buf)
            image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
            
            prompt = f"Подбери 2-3 наиболее вероятных 10-значных кода ТН ВЭД ЕАЭС для товара на фото. Описание: {caption.replace('/hs', '')}"
            res = await ask_kimi(prompt, image_b64=image_b64, system_msg="Ты таможенный декларант ЕАЭС.")
            
            codes = set(re.findall(r'(?i)(\d{4,10})', res))
            final_msg = f"📦 **Предполагаемые коды ТН ВЭД:**\n\n{res}\n\n🔍 **Проверить на Alta.ru:**\n"
            for code in codes:
                if len(code) >= 4:
                    final_msg += f"👉 [Код {code}](https://www.alta.ru/tnved/code/{code}/)\n"
            
            await msg.edit_text(final_msg, parse_mode='Markdown', disable_web_page_preview=True)
            return

        # Иначе - генерация имени файла
        msg = await update.message.reply_text('⏳ Обработка фото для создания имени файла...')
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        image = Image.open(buf)
        
        barcode_num, text, article = await extract_image_data(image)
        prompt = f"Текст: {text[:2000]}\nШтрих-код: {barcode_num}\nАртикул: {article}\nТолько имя файла в формате 中文_English_Размер_Артикул_Штрихкод.pdf:"
        image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        
        new_name = await ask_kimi(prompt, image_b64=image_b64, system_msg=SYSTEM_MSG_NAMING)
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name.strip())
        if not new_name.endswith('.pdf'): new_name += '.pdf'
        new_name = re.sub(r'_{2,}', '_', new_name)
        
        if len(new_name) < 10: new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"
        
        response_lines = build_response_lines(new_name, barcode_num, article)
        await msg.edit_text('\n'.join(response_lines), parse_mode='Markdown', disable_web_page_preview=True)
        
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка PDF документов"""
    try:
        doc = update.message.document
        if doc.file_size > 20 * 1024 * 1024:
            return await update.message.reply_text('❌ Файл слишком большой (>20MB)')
            
        msg = await update.message.reply_text('⏳ Обработка PDF...')
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        
        buf.seek(0)
        images = convert_from_bytes(buf.read(), dpi=250, first_page=1, last_page=1)
        if not images:
            return await msg.edit_text('❌ Не удалось открыть PDF')
            
        img = images[0]
        barcode_num, text, article = await extract_image_data(img)
        
        prompt = f"Текст: {text[:2000]}\nШтрих-код: {barcode_num}\nАртикул: {article}\nТолько имя файла в формате 中文_English_Размер_Артикул_Штрихкод.pdf:"
        new_name = await ask_kimi(prompt, system_msg=SYSTEM_MSG_NAMING)
        
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name.strip())
        if not new_name.endswith('.pdf'): new_name += '.pdf'
        new_name = re.sub(r'_{2,}', '_', new_name)
        
        if len(new_name) < 10: new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"

        await msg.delete()
        response_lines = build_response_lines(new_name, barcode_num, article)
        await update.message.reply_text('\n'.join(response_lines), parse_mode='Markdown', disable_web_page_preview=True)
        
        buf.seek(0)
        await update.message.reply_document(document=InputFile(buf, filename=new_name), caption=new_name)
        
    except Exception as e:
        logger.error(f"Doc error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', lambda u, c: u.message.reply_text("🤖 Бот готов! Жду фото, PDF или отчет из GS Orders.")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
