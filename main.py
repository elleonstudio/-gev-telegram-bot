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

# --- НАСТРОЙКИ ЛОГИРОВАНИЯ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ТОКЕНЫ И КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

# Названия таблиц в твоем Airtable
TABLE_ORDERS = "Закупка"
TABLE_CARGO = "Логистика Карго"

SYSTEM_MSG_NAMING = "Ты ассистент по именам файлов. Формат: 中文_English_Размер_Артикул_Штрихкод.pdf. Перевод обязателен."

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ИИ-ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64:
        content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    
    messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}]
    
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', 
                                 headers=headers, 
                                 json={'model': model, 'messages': messages, 'temperature': 0.0}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
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
        if match: article = match.group(1); break
    return barcode_num, text, article

def parse_airtable_block(text: str) -> dict:
    parsed = {}
    match = re.search(r'AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END', text, re.DOTALL)
    if match:
        for line in match.group(1).strip().split('\n'):
            if ':' in line:
                key, val = line.split(':', 1)
                parsed[key.strip()] = val.strip()
    return parsed

async def write_to_airtable(data: dict):
    api = Api(AIRTABLE_TOKEN)
    
    def fmt_date(d):
        try: return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")
        except: return datetime.now().strftime("%Y-%m-%d")

    # ТИП 1: ЗАКАЗЫ (ВЫКУП)
    if "Invoice_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_ORDERS)
        record = {
            "Код Карго": data.get("Invoice_ID"),
            "Дата": fmt_date(data.get("Date")),
            "Сумма (¥)": float(data.get("Sum_Client_CNY", 0)),
            "Реал Цена Закупки (¥)": float(data.get("Real_Purchase_CNY", 0)),
            "Курс Клиент": float(data.get("Client_Rate", 58)),
            "Курс Реал": float(data.get("Real_Rate", 55)),
            "Расход материалов (¥)": float(data.get("China_Logistics_CNY", 0)),
            "Кол-во коробок": int(data.get("FF_Boxes_Qty", 0))
        }
        table.create(record, typecast=True)
        return "✅ Данные [Выкуп] успешно добавлены в Airtable!"

    # ТИП 2: ЛОГИСТИКА КАРГО
    elif "Party_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_CARGO)
        record = {
            "Party_ID": data.get("Party_ID"),
            "Дата": fmt_date(data.get("Date")),
            "Вес (кг)": float(data.get("Total_Weight_KG", 0)),
            "Объем (м3)": float(data.get("Total_Volume_CBM", 0)),
            "Мест": int(data.get("Total_Pieces", 0)),
            "Плотность": int(data.get("Density", 0)),
            "Упаковка": data.get("Packaging_Type", "Сборная"),
            "Тариф Карго ($)": float(data.get("Tariff_Cargo_USD", 0)),
            "Тариф Клиент ($)": float(data.get("Tariff_Client_USD", 0)),
            "Курс USD/CNY": float(data.get("Rate_USD_CNY", 0)),
            "Курс USD/AMD": float(data.get("Rate_USD_AMD", 0)),
            "Итого Клиент (AMD)": int(data.get("Total_Client_AMD", 0)),
            "Итого Карго (CNY)": int(data.get("Total_Cargo_CNY", 0)),
            "Прибыль (AMD)": int(data.get("Net_Profit_AMD", 0))
        }
        table.create(record, typecast=True)
        return "✅ Данные [Логистика] успешно добавлены в Airtable!"
    
    return "❌ Ошибка: Тип данных не определен."

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return

    # Игнорируем готовые расчеты
    if text.strip().startswith('/calc'): return

    # 1. КОМАНДА /paste (Конвертация в шаблон GS Orders)
    if text.startswith('/paste'):
        raw_input = text.replace('/paste', '').strip()
        msg = await update.message.reply_text("⏳ Формирую шаблон...")
        system_paste = (
            "Ты — технический конвертер. Разбей математические строки пользователя на части.\n"
            "ПРАВИЛО: Цена - первое число, Количество - после 'x', Доставка - после '+'. Игнорируй результат после '='.\n"
            "Курс клиента всегда 58, мой курс всегда 55. Закупка всегда '-'.\n"
            "Ответ начни строго с /calc"
        )
        res = await ask_kimi(f"Заполни шаблон /calc: {raw_input}", system_msg=system_paste)
        await msg.edit_text(res.strip())
        return

    # 2. ЭКСПОРТ В AIRTABLE
    if "AIRTABLE_EXPORT_START" in text:
        data = parse_airtable_block(text)
        if data:
            status = await write_to_airtable(data)
            await update.message.reply_text(status)
        else:
            await update.message.reply_text("❌ Ошибка: Теги экспорта найдены, но данные внутри не читаются.")
        return

    # 3. ОБЫЧНЫЙ ЧАТ
    resp = await ask_kimi(text)
    await update.message.reply_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    # 1. ПАРСЕР 1688
    if caption.startswith('/1688'):
        msg = await update.message.reply_text("⏳ Читаю данные поставщика...")
        res = await ask_kimi("Extract Supplier Info: Company CN/EN, Tax ID, Address CN/EN, Phone. No emojis. Use code blocks.", image_b64=img_b64, system_msg="1688 Expert.")
        await msg.edit_text(res, parse_mode='Markdown')
        
    # 2. ТН ВЭД БРОКЕР
    elif caption.startswith('/hs'):
        msg = await update.message.reply_text("⏳ Подбираю коды ТН ВЭД...")
        res = await ask_kimi(f"Suggest 3 REAL HS Codes (4, 6 or 10 digits). If shop screenshot - read composition in Chinese. Info: {caption}", image_b64=img_b64, system_msg="Customs Broker.")
        codes = re.findall(r'\b\d{4,10}\b', res)
        links = "\n\n🔍 **Проверить на Alta.ru:**\n" + "\n".join([f"👉 [Код {c}](https://www.alta.ru/tnved/code/{c}/)" for c in set(codes)])
        await msg.edit_text(res + links, parse_mode='Markdown', disable_web_page_preview=True)
        
    # 3. ЭТИКЕТКА / НАЗВАНИЕ ФАЙЛА
    else:
        msg = await update.message.reply_text("⏳ Обрабатываю этикетку...")
        barcode, ocr_text, art = await extract_image_data(Image.open(buf))
        new_name = await ask_kimi(f"Naming for file. Text: {ocr_text}", image_b64=img_b64, system_msg=SYSTEM_MSG_NAMING)
        new_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()) + ".pdf"
        await msg.edit_text(f"📄 `{new_name}`\n\n✅ Штрих-код: {barcode}\n✅ Артикул: {art}", parse_mode='Markdown')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    msg = await update.message.reply_text("⏳ Обработка PDF...")
    buf = BytesIO()
    await (await context.bot.get_file(doc.file_id)).download_to_memory(buf)
    buf.seek(0)
    images = convert_from_bytes(buf.read(), dpi=200, first_page=1, last_page=1)
    barcode, ocr_text, art = await extract_image_data(images[0])
    new_name = await ask_kimi(f"Naming: {ocr_text}", system_msg=SYSTEM_MSG_NAMING)
    new_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()) + ".pdf"
    await msg.delete()
    await update.message.reply_document(document=InputFile(buf, filename=new_name), caption=f"Переименовано в: {new_name}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот GS Orders v3.0 запущен и готов к работе!")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
