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

# Рекомендуется перенести токены в переменные окружения (.env)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'ВАШ_TELEGRAM_ТОКЕН')
KIMI_API_KEY = os.getenv('KIMI_API_KEY', 'ВАШ_KIMI_ТОКЕН')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

TABLE_ORDERS = "Закупка"
TABLE_CARGO = "Логистика Карго"

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

# --- ИЗВЛЕЧЕНИЕ ДАННЫХ С КАРТИНКИ ---
async def extract_image_data(image: Image.Image):
    barcode_num, text, article = "-", "-", "-"
    
    # Ищем штрихкод
    try:
        codes = decode(image.convert('L'))
        if codes: barcode_num = codes[0].data.decode('utf-8')
    except Exception as e:
        logger.error(f"Ошибка чтения штрихкода: {e}")
        
    # Читаем текст (OCR)
    try:
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
    except Exception as e:
        logger.error(f"Ошибка OCR: {e}")
        
    # Ищем артикул через регулярные выражения
    for pattern in [r'Артикул[:\s]+(\w+)', r'Артикул[:\s]*(\w+)', r'Article[:\s]+(\w+)']:
        match = re.search(pattern, text, re.IGNORECASE)
        if match: 
            article = match.group(1)
            break
            
    return barcode_num, text, article

# --- AIRTABLE ЛОГИКА ---
async def write_to_airtable(data: dict):
    api = Api(AIRTABLE_TOKEN)
    def fmt_date(d):
        try: return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")
        except: return datetime.now().strftime("%Y-%m-%d")

    # ТИП 1: ВЫКУП
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

    # ТИП 2: ЛОГИСТИКА КАРГО
    elif "Party_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_CARGO)
        record = {
            "Party_ID": data.get("Party_ID"), 
            "Date": fmt_date(data.get("Date")),
            "Total_Weight_KG": float(data.get("Total_Weight_KG", 0)), 
            "Total_Volume_CBM": float(data.get("Total_Volume_CBM", 0)),
            "Total_Pieces": int(data.get("Total_Pieces", 0)), 
            "Density": int(data.get("Density", 0)),
            "Packaging_Type": data.get("Packaging_Type", "Сборная"), 
            "Tariff_Cargo_USD": float(data.get("Tariff_Cargo_USD", 0)),
            "Tariff_Client_USD": float(data.get("Tariff_Client_USD", 0)), 
            "Rate_USD_CNY": float(data.get("Rate_USD_CNY", 0)),
            "Rate_USD_AMD": float(data.get("Rate_USD_AMD", 0)), 
            "Total_Client_AMD": int(data.get("Total_Client_AMD", 0)),
            "Total_Cargo_CNY": int(data.get("Total_Cargo_CNY", 0)), 
            "Net_Profit_AMD": int(data.get("Net_Profit_AMD", 0)),
            "Logistics_Status": "Выполнен"
        }
        table.create(record, typecast=True)
        return f"✅ Карго: Партия {data.get('Party_ID')} добавлена!"
    return "❌ Ошибка: Тип данных не определен (нет Invoice_ID или Party_ID)."

# --- ОБРАБОТЧИКИ ТЕКСТА ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    if text.strip().startswith('/calc'): return

    # Команда /paste
    if text.startswith('/paste'):
        raw_input = text.replace('/paste', '').strip()
        msg = await update.message.reply_text("⏳ Формирую шаблон...")
        system_paste = "Ты конвертер. Расставь данные в шаблон /calc. Цена - 1-е число, Кол-во - после x, Доставка - после +. Курс: 58/55. Начало ответа: /calc"
        res = await ask_kimi(f"Данные: {raw_input}", system_msg=system_paste)
        await msg.edit_text(res.strip())
        return

    # Airtable парсинг
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

    # Обычное общение с ИИ
    resp = await ask_kimi(text)
    await update.message.reply_text(resp[:4000])

# --- ОБРАБОТЧИКИ ФОТО ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    # Получаем фото с максимальным разрешением
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    
    img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    image = Image.open(buf)

    # 1. АНАЛИЗ ПОСТАВЩИКА (/1688)
    if caption.lower().startswith('/1688'):
        system_1688 = "Ты бизнес-ассистент по закупкам в Китае. Твоя задача — извлекать данные с китайских сайтов."
        prompt_1688 = """Извлеки информацию о компании с прикрепленного скриншота/фото. 
Выведи ответ СТРОГО в следующем формате (без отсебятины, используй Markdown):

📝 SUPPLIER CARD (1688)

🏢 **Company (CN):**
`[Название на китайском]`

🏢 **Company (EN):**
`[Название на английском]`

📋 **Tax ID:**
`[Единый код / Tax ID]`

📍 **Address (CN):**
`[Адрес на китайском]`

📍 **Address (EN):**
`[Адрес на английском]`

📞 **Phone:**
`[Телефон, если нет, напиши '未知']`"""
        
        msg = await update.message.reply_text("⏳ Анализирую поставщика...")
        res = await ask_kimi(prompt_1688, image_b64=img_b64, system_msg=system_1688)
        await msg.edit_text(res, parse_mode='Markdown')

    # 2. ПОДБОР КОДОВ ТН ВЭД (/hs)
    elif caption.lower().startswith('/hs'):
        system_hs = "Ты профессиональный таможенный брокер."
        prompt_hs = """Посмотри на товар на фото, определи, что это, и предложи 3 наиболее подходящих кода ТН ВЭД (10 знаков). 
Выведи ответ СТРОГО в следующем формате Markdown:

📦 **Коды ТН ВЭД:**

Для товара на фотографии, который является [краткое название товара], можно предложить следующие коды:

1. [Код 1] - [Краткое описание, почему подходит]
2. [Код 2] - [Краткое описание, почему подходит]
3. [Код 3] - [Краткое описание, почему подходит]

🔍 **Проверить в базе Alta:**
👉 [Код [Код 1]](https://www.alta.ru/tnved/code/[Код 1]/)
👉 [Код [Код 2]](https://www.alta.ru/tnved/code/[Код 2]/)
👉 [Код [Код 3]](https://www.alta.ru/tnved/code/[Код 3]/)"""
        
        msg = await update.message.reply_text("⏳ Подбираю коды ТН ВЭД...")
        res = await ask_kimi(prompt_hs, image_b64=img_b64, system_msg=system_hs)
        await msg.edit_text(res, parse_mode='Markdown', disable_web_page_preview=True)

    # 3. ЭТИКЕТКИ, ШТРИХКОДЫ И PDF (Для склада)
    else:
        msg = await update.message.reply_text("⏳ Читаю штрихкод и генерирую файл...")
        
        barcode, ocr_text, art = await extract_image_data(image)
        
        prompt_label = f"""Текст с этикетки: {ocr_text}. Артикул: {art}. Штрихкод: {barcode}.
Внимательно изучи текст и выдели ГЛАВНОЕ. Твоя важнейшая задача — создать правильное имя файла для китайского фулфилмента.

⚠️ ПРАВИЛО: Китайская часть имени ОБЯЗАТЕЛЬНО должна содержать: Суть товара + Цвет + Материал (или название набора). 
Пример: 蓝色棉质睡衣套装 (Синяя хлопковая пижама набор).

Сформируй ответ СТРОГО по шаблону ниже (каждый пункт с новой строки, без лишних слов, без расширения .pdf):

FILENAME: [Китай_ТоварЦветМатериалНабор]_[Англ_ТоварЦветМатериалНабор]_[Размер]
ITEM_RU: [Название товара на русском]
COLOR_RU: [Цвет и материал/набор на русском]
ITEM_EN: [Название товара на английском]
COLOR_EN: [Цвет и материал/набор на английском]

Если размера нет, ставь '-' в FILENAME. Если цвета/материала нет на этикетке, постарайся определить их по фото (если можешь), иначе не пиши."""

        raw_res = await ask_kimi(prompt_label, image_b64=img_b64, system_msg="Ты логист китайского склада. Отвечай только по шаблону.")

        # Парсинг ответа
        filename_base, item_ru, color_ru, item_en, color_en = "Товар", "Не определен", "[не указан]", "Not specified", "[not specified]"
        for line in raw_res.split('\n'):
            line = line.strip()
            if line.startswith('FILENAME:'): filename_base = line.replace('FILENAME:', '').strip()
            elif line.startswith('ITEM_RU:'): item_ru = line.replace('ITEM_RU:', '').strip()
            elif line.startswith('COLOR_RU:'): color_ru = line.replace('COLOR_RU:', '').strip()
            elif line.startswith('ITEM_EN:'): item_en = line.replace('ITEM_EN:', '').strip()
            elif line.startswith('COLOR_EN:'): color_en = line.replace('COLOR_EN:', '').strip()

        # Создание имени файла
        final_name = f"{filename_base}_{art}_{barcode}.pdf"
        final_name = re.sub(r'[\\/*?:"<>|]', '', final_name) 

        # Конвертация в PDF
        pdf_buf = BytesIO()
        image.convert('RGB').save(pdf_buf, format='PDF', resolution=100.0)
        pdf_buf.seek(0)

        wb_link = f" 👉 [https://www.wildberries.ru/search?search={art}](https://www.wildberries.ru/search?search={art})" if art != "-" else ""

        # Финальное сообщение
        msg_text = (
            f"📦 Страниц: 1\n"
            f"✅ Штрих-код: {barcode}\n"
            f"✅ Артикул: {art}{wb_link}\n"
            f"📝 Детали с этикетки:\n"
            f"🔶 Товар: {item_ru}\n"
            f"🔶 Цвет/Материал: {color_ru}\n"
            f"🔶 Товар (EN): {item_en}\n"
            f"🔶 Цвет (EN): {color_en}"
        )

        await msg.delete()
        
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(pdf_buf, filename=final_name),
            caption=msg_text,
            parse_mode='Markdown'
        )

# --- МЕНЮ И ЗАПУСК ---
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = (
        "<b>📂 Меню GS Orders Bot:</b>\n\n"
        "1️⃣ <b>/paste [данные]</b> - перенос расчета в шаблон /calc\n"
        "2️⃣ <b>/1688 [в подписи к фото]</b> - инфо о поставщике с картинки\n"
        "3️⃣ <b>/hs [в подписи к фото]</b> - подбор 3 кодов ТН ВЭД\n"
        "4️⃣ <b>Просто фото этикетки</b> - создает PDF для склада (Китай_Англ_Размер_Арт_Штрихкод.pdf)\n"
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
    
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 GS Assistant готов! Нажми /menu")))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    async def set_commands(application):
        await application.bot.set_my_commands(commands)
    
    app.post_init = set_commands
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
