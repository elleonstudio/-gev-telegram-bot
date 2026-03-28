import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, InputFile, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode
from pyairtable import Api

# --- НАСТРОЙКИ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены (рекомендуется использовать переменные окружения)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'ВАШ_ТОКЕН_ЗДЕСЬ')
KIMI_API_KEY = os.getenv('KIMI_API_KEY', 'ВАШ_KIMI_КЛЮЧ_ЗДЕСЬ')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

TABLE_ORDERS = "Закупка"
TABLE_CARGO = "Логистика Карго"

# --- ФУНКЦИИ ИИ (KIMI) ---
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

# --- ОБРАБОТКА ИЗОБРАЖЕНИЙ ---
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
    return "❌ Ошибка данных."

# --- ОБРАБОТЧИКИ ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    if text.startswith('/paste'):
        raw_input = text.replace('/paste', '').strip()
        msg = await update.message.reply_text("⏳ Формирую шаблон...")
        res = await ask_kimi(f"Данные: {raw_input}", system_msg="Ты конвертер. Расставь данные в шаблон /calc. Цена - 1-е число, Кол-во - после x, Доставка - после +. Курс: 58/55. Начало ответа: /calc")
        await msg.edit_text(res.strip())
    elif "AIRTABLE_EXPORT_START" in text:
        data = re.search(r'AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END', text, re.DOTALL)
        if data:
            parsed = {l.split(':', 1)[0].strip(): l.split(':', 1)[1].strip() for l in data.group(1).strip().split('\n') if ':' in l}
            status = await write_to_airtable(parsed)
            await update.message.reply_text(status)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (update.message.caption or "").lower()
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO(); await file.download_to_memory(buf)
    img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    image = Image.open(buf)

    # 1. /1688 (Поставщик)
    if caption.startswith('/1688'):
        prompt = "Извлеки данные компании: Название (CN/EN), Tax ID, Адрес (CN/EN), Телефон. Формат: 📝 SUPPLIER CARD (1688)..."
        res = await ask_kimi(prompt, image_b64=img_b64, system_msg="1688 Expert.")
        await update.message.reply_text(res, parse_mode='Markdown')

    # 2. /hs (ТН ВЭД)
    elif caption.startswith('/hs'):
        prompt = "Предложи 3 кода ТН ВЭД для товара на фото. Формат: 📦 Коды ТН ВЭД... Ссылки на alta.ru/tnved/code/..."
        res = await ask_kimi(prompt, image_b64=img_b64, system_msg="Broker.")
        await update.message.reply_text(res, parse_mode='Markdown', disable_web_page_preview=True)

    # 3. ЭТИКЕТКА (Переименование файла + PDF)
    else:
        msg = await update.message.reply_text("⏳ Обработка этикетки...")
        barcode, ocr_text, art = await extract_image_data(image)
        
        prompt_label = f"""Текст: {ocr_text}. Артикул: {art}. Штрихкод: {barcode}.
        ЗАДАЧА: Создать имя для китайского склада.
        ⚠️ СТРОГОЕ ПРАВИЛО: Первая часть FILENAME ДОЛЖНА БЫТЬ НА КИТАЙСКОМ (Иероглифы). Переведи Суть+Цвет+Материал.
        Пример: [蓝色棉质套装]_[BlueCottonSet]...
        
        Шаблон ответа:
        FILENAME: [Китай_Описание]_[English_Description]_[Размер]
        ITEM_RU: [Название товара RU]
        COLOR_RU: [Цвет/Материал RU]
        ITEM_EN: [Название товара EN]
        COLOR_EN: [Цвет/Материал EN]"""

        raw_res = await ask_kimi(prompt_label, image_b64=img_b64, system_msg="Ты логист в Китае. Обязательно используй иероглифы для перевода.")
        
        # Парсинг данных
        data = {l.split(':', 1)[0].strip(): l.split(':', 1)[1].strip() for l in raw_res.split('\n') if ':' in l}
        
        # Формирование PDF и Имени
        final_name = f"{data.get('FILENAME', 'Товар')}_{art}_{barcode}.pdf"
        final_name = re.sub(r'[\\/*?:"<>|]', '', final_name)
        pdf_buf = BytesIO(); image.convert('RGB').save(pdf_buf, format='PDF'); pdf_buf.seek(0)

        wb_link = f" 👉 [Ссылка на товар](https://www.wildberries.ru/search?search={art})" if art != "-" else ""
        msg_text = (f"📦 Страниц: 1\n✅ Штрих-код: {barcode}\n✅ Артикул: {art}{wb_link}\n"
                    f"📝 Детали с этикетки:\n🔶 Товар: {data.get('ITEM_RU')}\n🔶 Цвет: {data.get('COLOR_RU')}\n"
                    f"🔶 Товар (EN): {data.get('ITEM_EN')}\n🔶 Цвет (EN): {data.get('COLOR_EN')}")

        await msg.delete()
        await context.bot.send_document(chat_id=update.effective_chat.id, document=InputFile(pdf_buf, filename=final_name), caption=msg_text, parse_mode='Markdown')

# --- ЗАПУСК ---
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("<b>📂 Меню GS Orders Bot:</b>\n\n1️⃣ /paste [данные]\n2️⃣ /1688 [фото]\n3️⃣ /hs [фото]\n4️⃣ Фото этикетки -> PDF", parse_mode='HTML')

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
