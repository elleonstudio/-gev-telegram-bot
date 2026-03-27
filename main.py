import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, InputFile, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode
from pyairtable import Api

# --- НАСТРОЙКИ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

TABLE_ORDERS = "Закупка"
TABLE_CARGO = "Логистика Карго"

# Обновленная инструкция: требуем и имя файла, и детали!
SYSTEM_MSG_NAMING = (
    "Ты — эксперт по логистике. Твоя задача — извлечь данные с этикетки.\n"
    "ОТВЕТ ДОЛЖЕН БЫТЬ СТРОГО В ТАКОМ ФОРМАТЕ:\n\n"
    "FILE: [Описание на китайском]_[Description in English]_[Размер]_[Артикул]_[Штрихкод].pdf\n"
    "📝 Детали с этикетки:\n"
    "🔸 Товар: [название]\n"
    "🔸 Цвет: [цвет]\n"
    "🔸 Материал: [материал]\n\n"
    "ПРАВИЛО: В имени файла (в [Описание на китайском]) ОБЯЗАТЕЛЬНО укажи цвет и тип товара иероглифами, чтобы рабочий на складе в Китае ничего не перепутал (например: 棕色虎纹套装). Если размера нет, ставь '-'."
)

# --- ФУНКЦИИ ИИ ---

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64:
        content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}]
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', 
                                 headers=headers, json={'model': model, 'messages': messages, 'temperature': 0.0}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
            return f"Error_{resp.status}"

async def extract_image_data(image: Image.Image):
    barcode_num, text, article = "-", "-", "-"
    try:
        codes = decode(image.convert('L'))
        if codes: barcode_num = codes[0].data.decode('utf-8')
    except: pass
    try:
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
    except: pass
    for pattern in [r'Артикул[:\s]+(\w+)', r'Артикул[:\s]*(\w+)', r'Article[:\s]+(\w+)']:
        match = re.search(pattern, text, re.IGNORECASE)
        if match: article = match.group(1); break
    return barcode_num, text, article

# --- AIRTABLE ЛОГИКА ---

async def write_to_airtable(data: dict):
    api = Api(AIRTABLE_TOKEN)
    def fmt_date(d):
        try: return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")
        except: return datetime.now().strftime("%Y-%m-%d")

    if "Invoice_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_ORDERS)
        full_id = data.get("Invoice_ID", "")
        client_match = re.match(r'^([a-zA-Z]+)', full_id)
        client_name = client_match.group(1).capitalize() if client_match else ""
        record = {
            "Код Карго": full_id, "Клиент": client_name, "Дата": fmt_date(data.get("Date")),
            "Сумма (¥)": float(data.get("Sum_Client_CNY", 0)), "Реал Цена Закупки (¥)": float(data.get("Real_Purchase_CNY", 0)),
            "Курс Клиент": float(data.get("Client_Rate", 58)), "Курс Реал": float(data.get("Real_Rate", 55)),
            "Расход материалов (¥)": float(data.get("China_Logistics_CNY", 0)), "Кол-во коробок": int(data.get("FF_Boxes_Qty", 0))
        }
        table.create(record, typecast=True)
        return f"✅ Выкупы: Заказ {full_id} для {client_name} добавлен!"

    elif "Party_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_CARGO)
        record = {
            "Party_ID": data.get("Party_ID"), "Date": fmt_date(data.get("Date")),
            "Total_Weight_KG": float(data.get("Total_Weight_KG", 0)), "Total_Volume_CBM": float(data.get("Total_Volume_CBM", 0)),
            "Total_Pieces": int(data.get("Total_Pieces", 0)), "Density": int(data.get("Density", 0)),
            "Packaging_Type": data.get("Packaging_Type", "Сборная"), "Tariff_Cargo_USD": float(data.get("Tariff_Cargo_USD", 0)),
            "Tariff_Client_USD": float(data.get("Tariff_Client_USD", 0)), "Rate_USD_CNY": float(data.get("Rate_USD_CNY", 0)),
            "Rate_USD_AMD": float(data.get("Rate_USD_AMD", 0)), "Total_Client_AMD": int(data.get("Total_Client_AMD", 0)),
            "Total_Cargo_CNY": int(data.get("Total_Cargo_CNY", 0)), "Net_Profit_AMD": int(data.get("Net_Profit_AMD", 0)),
            "Logistics_Status": "Выполнен"
        }
        table.create(record, typecast=True)
        return f"✅ Карго: Партия {data.get('Party_ID')} добавлена!"
    return "❌ Ошибка: Тип данных не определен (нет Invoice_ID или Party_ID)."

# --- ОБРАБОТЧИКИ ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    if text.strip().startswith('/calc'): return

    if text.startswith('/paste'):
        raw_input = text.replace('/paste', '').strip()
        msg = await update.message.reply_text("⏳ Формирую шаблон...")
        system_paste = "Ты конвертер. Расставь данные в шаблон /calc. Цена - 1-е число, Кол-во - после x, Доставка - после +. Курс: 58/55. Начало ответа: /calc"
        res = await ask_kimi(f"Данные: {raw_input}", system_msg=system_paste)
        await msg.edit_text(res.strip())
        return

    if "AIRTABLE_EXPORT_START" in text:
        data = re.search(r'AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END', text, re.DOTALL)
        if data:
            parsed = {}
            for line in data.group(1).strip().split('\n'):
                if ':' in line:
                    key, val = line.split(':', 1)
                    parsed[key.strip()] = val.strip()
            status = await write_to_airtable(parsed)
            await update.message.reply_text(status)
        return

    resp = await ask_kimi(text)
    await update.message.reply_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO(); await file.download_to_memory(buf)
    img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    if caption.startswith('/1688'):
        res = await ask_kimi("Supplier Info CN/EN.", image_b64=img_b64, system_msg="1688 Expert.")
        await update.message.reply_text(res)
    elif caption.startswith('/hs'):
        res = await ask_kimi("Suggest 3 HS Codes.", image_b64=img_b64, system_msg="Broker.")
        await update.message.reply_text(res)
    else:
        msg = await update.message.reply_text("⏳ Читаю фото...")
        barcode, ocr_text, art = await extract_image_data(Image.open(buf))
        prompt = f"Текст с этикетки: {ocr_text}. Артикул: {art}. Штрихкод: {barcode}."
        
        res_raw = await ask_kimi(prompt, image_b64=img_b64, system_msg=SYSTEM_MSG_NAMING)
        
        # Разделяем ответ: достаем имя файла и оставляем детали
        file_match = re.search(r'FILE:\s*([^\n]+\.pdf)', res_raw, re.IGNORECASE)
        if file_match:
            final_name = re.sub(r'[\\/*?:"<>|]', '', file_match.group(1).strip())
            details = res_raw.replace(file_match.group(0), '').strip()
        else:
            final_name = "label_converted.pdf"
            details = res_raw.strip()

        caption_text = f"✅ Штрих-код: {barcode}\n✅ Артикул: {art}\n{details}\n\n📄 Имя файла: `{final_name}`"
        await msg.edit_text(caption_text)

# ОБРАБОТКА PDF
async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith('.pdf'):
        return

    msg = await update.message.reply_text("⏳ Читаю PDF и переименовываю...")
    try:
        buf = BytesIO()
        file = await context.bot.get_file(doc.file_id)
        await file.download_to_memory(buf)
        buf.seek(0)
        
        images = convert_from_bytes(buf.read(), dpi=200, first_page=1, last_page=1)
        image = images[0]
        barcode, ocr_text, art = await extract_image_data(image)
        
        img_byte_arr = BytesIO()
        image.save(img_byte_arr, format='JPEG')
        img_b64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')

        prompt = f"Текст с этикетки: {ocr_text}. Артикул: {art}. Штрихкод: {barcode}."
        res_raw = await ask_kimi(prompt, image_b64=img_b64, system_msg=SYSTEM_MSG_NAMING)
        
        # Разделяем ответ на имя файла и красивые детали
        file_match = re.search(r'FILE:\s*([^\n]+\.pdf)', res_raw, re.IGNORECASE)
        if file_match:
            final_name = re.sub(r'[\\/*?:"<>|]', '', file_match.group(1).strip())
            details = res_raw.replace(file_match.group(0), '').strip()
        else:
            final_name = "label_converted.pdf"
            details = res_raw.strip()
        
        buf.seek(0)
        await msg.delete()
        caption_text = f"📦 Страниц: 1\n✅ Штрих-код: {barcode}\n✅ Артикул: {art}\n{details}"
        
        await update.message.reply_document(
            document=InputFile(buf, filename=final_name), 
            caption=caption_text
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка при чтении PDF: {e}")

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = (
        "<b>📂 Меню GS Orders Bot:</b>\n\n"
        "1️⃣ <b>/paste [данные]</b> - перенос расчета в шаблон /calc\n"
        "2️⃣ <b>/1688 [фото]</b> - инфо о поставщике с картинки\n"
        "3️⃣ <b>/hs [фото]</b> - подбор кодов ТН ВЭД\n"
        "4️⃣ <b>Этикетки (Фото или PDF)</b> - формирует китайское имя файла для склада\n"
        "5️⃣ <b>AIRTABLE_EXPORT</b> - авто-запись данных в базу"
    )
    await update.message.reply_text(menu_text, parse_mode='HTML')

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    commands = [
        BotCommand("start", "Запустить"),
        BotCommand("menu", "Показать все функции"),
        BotCommand("paste", "Конвертер /calc")
    ]
    
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот готов! Нажми /menu")))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    
    async def set_commands(application):
        await application.bot.set_my_commands(commands)
    
    app.post_init = set_commands
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
