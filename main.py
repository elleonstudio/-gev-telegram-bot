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
TABLE_DELIVERY = "Доставка РФ"

SYSTEM_MSG_NAMING = (
    "Ты — эксперт по логистике в Китае. Создай имя файла для фулфилмента. "
    "Формат СТРОГО: [Описание на китайском]_[Description in English]_[Размер]_[Артикул]_[Штрихкод]. "
    "ОБЯЗАТЕЛЬНО укажи цвет и материал на китайском в начале. Выдай только одну строку."
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

# --- ЛОГИКА АУДИТА (4 СЦЕНАРИЯ) ---

async def run_audit(update: Update, text: str):
    msg = await update.message.reply_text("🔍 Проверяю расчеты (Аудит)...")
    system_audit = (
        "Ты — идеальный финансовый аудитор. Найди ошибки в расчете пользователя.\n"
        "1. Пересчитай каждую строку (Цена x Кол-во + Доставка). Округление до 2 знаков.\n"
        "2. Проверь общую сумму юаней (сложение итогов строк).\n"
        "3. Найди курс в тексте (например 1¥-56֏ или курс 58). Если нет - используй 58.\n"
        "4. Проверь комиссию: либо фиксированные +10000֏, либо проценты (+3%, +5%).\n"
        "Выдай ответ: ❌ Найдены ошибки! -> Строка -> Сумма -> Расхождение -> ✅ Исправленный расчет."
    )
    res = await ask_kimi(text, system_msg=system_audit)
    await msg.edit_text(res)

# --- AIRTABLE ЛОГИКА ---

async def write_to_airtable(data: dict, data_type: str):
    api = Api(AIRTABLE_TOKEN)
    def fmt_date(d):
        try: return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")
        except: return datetime.now().strftime("%Y-%m-%d")

    if data_type == "DOSTAVKA":
        table = api.table(AIRTABLE_BASE_ID, TABLE_DELIVERY)
        record = {
            "Клиент / Код заказа": data.get("Client_ID", ""),
            "Дата расчета": fmt_date(data.get("Date")),
            "Количество коробок": int(data.get("Total_Boxes", 0)),
            "Маршрут / Склады": data.get("Destinations", ""),
            "Себестоимость РФ (RUB)": float(data.get("Logistics_RUB", 0)),
            "Курс клиента (RUB/AMD)": float(data.get("Rate_RUB_AMD", 0)),
            "К оплате за доставку (AMD)": int(data.get("Total_Client_AMD", 0))
        }
        table.create(record, typecast=True)
        return f"✅ Доставка для {data.get('Client_ID')} добавлена!"

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
        return f"✅ Карго {data.get('Party_ID')} добавлено!"

    elif "Invoice_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_ORDERS)
        full_id = data.get("Invoice_ID", "")
        client_name = re.match(r'^([a-zA-Z]+)', full_id).group(1).capitalize() if re.match(r'^([a-zA-Z]+)', full_id) else ""
        record = {
            "Код Карго": full_id, "Клиент": client_name, "Дата": fmt_date(data.get("Date")),
            "Сумма (¥)": float(data.get("Sum_Client_CNY", 0)), "Реал Цена Закупки (¥)": float(data.get("Real_Purchase_CNY", 0)),
            "Курс Клиент": float(data.get("Client_Rate", 58)), "Курс Реал": float(data.get("Real_Rate", 55)),
            "Расход материалов (¥)": float(data.get("China_Logistics_CNY", 0)), "Кол-во коробок": int(data.get("FF_Boxes_Qty", 0))
        }
        table.create(record, typecast=True)
        return f"✅ Выкуп {client_name} добавлен!"
    return "❌ Ошибка типа данных."

# --- ОБРАБОТЧИКИ ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    if text.strip().startswith('/calc'): return

    # Авто-аудит (если видим расчет)
    if any(char in text for char in ['×', 'x', '+', '=']) and ('֏' in text or '¥' in text):
        await run_audit(update, text)
        return

    # Команда /paste
    if text.startswith('/paste'):
        raw_input = text.replace('/paste', '').strip()
        msg = await update.message.reply_text("⏳ Формирую шаблон...")
        system_paste = "Ты конвертер. Расставь данные в шаблон /calc. Цена - 1-е число, Кол-во - после x, Доставка - после +. Курс: 58/55. Начало: /calc"
        res = await ask_kimi(f"Данные: {raw_input}", system_msg=system_paste)
        await msg.edit_text(res.strip())
        return

    # Парсинг Airtable
    for tag, d_type in [("AIRTABLE_EXPORT_START", "EXPORT"), ("AIRTABLE_DOSTAVKA_START", "DOSTAVKA")]:
        if tag in text:
            match = re.search(f"{tag}(.*?){tag.replace('START', 'END')}", text, re.DOTALL)
            if match:
                parsed = {line.split(':', 1)[0].strip(): line.split(':', 1)[1].strip() for line in match.group(1).strip().split('\n') if ':' in line}
                status = await write_to_airtable(parsed, d_type)
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
        res = await ask_kimi(f"HS Code. Info: {caption}", image_b64=img_b64, system_msg="Broker.")
        codes = re.findall(r'\b\d{4,10}\b', res)
        links = "\n\n🔍 Alta.ru:\n" + "\n".join([f"👉 [Код {c}](https://www.alta.ru/tnved/code/{c}/)" for c in set(codes)])
        await update.message.reply_text(res + links, parse_mode='Markdown', disable_web_page_preview=True)
    else:
        # Этикетка для склада
        barcode, text, art = "-", "-", "-"
        try:
            codes = decode(Image.open(buf).convert('L'))
            if codes: barcode = codes[0].data.decode('utf-8')
            text = pytesseract.image_to_string(Image.open(buf), lang='rus+eng+chi_sim')
        except: pass
        new_name = await ask_kimi(f"Naming: {text}. Art: {art}. Barcode: {barcode}.", image_b64=img_b64, system_msg=SYSTEM_MSG_NAMING)
        final_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()) + ".pdf"
        await update.message.reply_text(f"✅ Для склада:\n📄 `{final_name}`\n\nBarcode: {barcode}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот GS Orders v4.0 готов!")))
    app.add_handler(CommandHandler("menu", lambda u, c: u.message.reply_text("1. /paste\n2. /1688\n3. /hs\n4. Аудит (авто)\n5. Airtable (авто)")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
