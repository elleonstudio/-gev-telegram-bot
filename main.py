import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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
TABLE_DOSTAVKA = "Доставка в РФ"

SYSTEM_MSG_NAMING = (
    "Ты — эксперт по логистике в Китае. Сформируй имя файла. "
    "Формат: [Описание на китайском]_[Description in English]_[Размер]_[Артикул]_[Штрихкод]. "
    "Цвет и материал на китайском в начале! Выдай только строку."
)

# --- ЛОГИКА АУДИТА (PYTHON) ---

def clean_num(val):
    if val == int(val): return str(int(val))
    return str(round(val, 2))

def run_python_audit(text):
    # Убираем команду и лишние пробелы
    pure_text = text.replace('/audit_gs', '').strip()
    lines = pure_text.split('\n')
    
    audit_log = []
    corrected_lines = []
    total_cny = 0
    has_errors = False
    
    # По умолчанию параметры, если не найдены в тексте
    rate = 58.0
    commission = 10000.0

    # 1. Сначала считаем только строки с товарами (Цена x Кол-во + Доставка)
    for line in lines:
        if not line.strip():
            corrected_lines.append("")
            continue
            
        # Ищем паттерн классической строки закупа: [Число] × [Число] + [Число] = [Итог]
        match = re.search(r'([\d\.]+)\s*[×x*]\s*([\d\.]+)(?:\s*[\+]\s*([\d\.]+))?\s*=\s*([\d\.]+)', line.replace(',', '.'))
        
        if match:
            # Выделяем цифры: p (цена), q (кол-во), d (доставка), claimed (твой итог)
            p, q, d, claimed = map(float, [match.group(1), match.group(2), match.group(3) or 0, match.group(4)])
            real_line_sum = round(p * q + d, 2)
            
            # Накапливаем общую сумму в юанях для финала
            total_cny += real_line_sum
            
            # Проверка строки (без придирок к .0)
            if abs(real_line_sum - claimed) > 0.1:
                has_errors = True
                audit_log.append(f"Было: {line.strip()}\nПравильно: {line.replace(match.group(4), str(int(real_line_sum)) if real_line_sum.is_integer() else str(real_line_sum)).strip()}")
                corrected_lines.append(line.replace(match.group(4), str(int(real_line_sum)) if real_line_sum.is_integer() else str(real_line_sum)))
            else:
                corrected_lines.append(line)
        else:
            # Если в строке есть курс (например, ×58) или комиссия (+10000)
            # Извлекаем курс из текста пользователя, чтобы расчет был точным
            found_rate = re.search(r'×(5[0-9](?:\.\d+)?)', line)
            if found_rate: rate = float(found_rate.group(1))
            
            found_comm = re.search(r'\+(10000|[\d\.]+%|[\d\.]+)', line)
            if found_comm:
                # Если комиссия в процентах (например, +5%)
                if '%' in found_comm.group(1):
                    perc = float(found_comm.group(1).replace('%', ''))
                    commission = (total_cny * rate) * (perc / 100)
                else:
                    commission = float(found_comm.group(1))
            
            corrected_lines.append(line)

    # 2. Считаем ФИНАЛ (Чистая математика Python)
    # Формула: (Сумма CNY * Курс) + Комиссия
    real_final_amd = round((total_cny * rate) + commission)
    
    # Ищем, что написал пользователь в самом конце перед знаком ֏
    claimed_final_match = re.findall(r'=\s*(\d+)\s*֏', pure_text)
    claimed_final = float(claimed_final_match[-1]) if claimed_final_match else 0
    
    final_sum_err = None
    if abs(real_final_amd - claimed_final) > 1:
        has_errors = True
        final_sum_err = f"Было: {int(claimed_final)}֏\nПравильно: {int(real_final_amd)}֏"

    # 3. Сборка ответа по твоему дизайну
    res = f"/audit_gs\n\n{pure_text}\n\n"
    
    if not has_errors:
        res += f"✅ Ошибок нет, финальная сумма {int(real_final_amd)}֏ верна."
    else:
        res += "❌ Найдены ошибки в расчетах!\n\n"
        if audit_log:
            res += "Строка:\n" + "\n\n".join(audit_log) + "\n\n"
        if final_sum_err:
            res += f"Сумма:\n{final_sum_err}\n\n"
            res += f"Расхождение: {abs(int(real_final_amd - claimed_final))}֏\n\n"
        
        # Генерируем исправленный текст (заменяем только итог в последней строке)
        final_block = "\n".join(corrected_lines)
        final_block = re.sub(r'=\s*\d+\s*֏', f"= {int(real_final_amd)}֏", final_block)
        res += f"✅ Исправленный расчет:\n{final_block}"
    
    return res

# --- ФУНКЦИИ ИИ И OCR ---

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ассистент") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    content = [{'type': 'text', 'text': prompt}]
    if image_b64: content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, 
                                 json={'model': 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k', 
                                       'messages': [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}], 'temperature': 0.0}) as resp:
            return (await resp.json())['choices'][0]['message']['content'] if resp.status == 200 else f"Error_{resp.status}"

async def extract_image_data(image: Image.Image):
    barcode_num, text, article = "-", "-", "-"
    try:
        codes = decode(image.convert('L'))
        if codes: barcode_num = codes[0].data.decode('utf-8')
    except: pass
    try: text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
    except: pass
    for pattern in [r'Артикул[:\s]+(\w+)', r'Артикул[:\s]*(\w+)', r'Article[:\s]+(\w+)']:
        match = re.search(pattern, text, re.IGNORECASE)
        if match: article = match.group(1); break
    return barcode_num, text, article

# --- AIRTABLE ---

async def write_to_airtable(data: dict):
    api = Api(AIRTABLE_TOKEN)
    def fmt_date(d):
        try: return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")
        except: return datetime.now().strftime("%Y-%m-%d")

    if "Invoice_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_ORDERS)
        full_id = data.get("Invoice_ID", "")
        client_name = re.match(r'^([a-zA-Z]+)', full_id).group(1).capitalize() if re.match(r'^([a-zA-Z]+)', full_id) else ""
        record = {"Код Карго": full_id, "Клиент": client_name, "Дата": fmt_date(data.get("Date")), "Сумма (¥)": float(data.get("Sum_Client_CNY", 0)), "Реал Цена Закупки (¥)": float(data.get("Real_Purchase_CNY", 0)), "Курс Клиент": float(data.get("Client_Rate", 58)), "Курс Реал": float(data.get("Real_Rate", 55)), "Расход материалов (¥)": float(data.get("China_Logistics_CNY", 0)), "Кол-во коробок": int(data.get("FF_Boxes_Qty", 0))}
        table.create(record, typecast=True)
        return f"✅ Выкуп: Заказ {full_id} добавлен!"
    elif "Party_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_CARGO)
        record = {"Party_ID": data.get("Party_ID"), "Date": fmt_date(data.get("Date")), "Total_Weight_KG": float(data.get("Total_Weight_KG", 0)), "Total_Volume_CBM": float(data.get("Total_Volume_CBM", 0)), "Total_Pieces": int(data.get("Total_Pieces", 0)), "Density": int(data.get("Density", 0)), "Packaging_Type": data.get("Packaging_Type", "Сборная"), "Tariff_Cargo_USD": float(data.get("Tariff_Cargo_USD", 0)), "Tariff_Client_USD": float(data.get("Tariff_Client_USD", 0)), "Rate_USD_CNY": float(data.get("Rate_USD_CNY", 0)), "Rate_USD_AMD": float(data.get("Rate_USD_AMD", 0)), "Total_Client_AMD": int(data.get("Total_Client_AMD", 0)), "Total_Cargo_CNY": int(data.get("Total_Cargo_CNY", 0)), "Net_Profit_AMD": int(data.get("Net_Profit_AMD", 0)), "Logistics_Status": "Выполнен"}
        table.create(record, typecast=True)
        return f"✅ Карго: Партия {data.get('Party_ID')} добавлена!"
    elif "Client_ID" in data:
        table = api.table(AIRTABLE_BASE_ID, TABLE_DOSTAVKA)
        record = {"Клиент / Код заказа": data.get("Client_ID", ""), "Дата расчета": fmt_date(data.get("Date")), "Количество коробок": int(data.get("Total_Boxes", 0)), "Маршрут / Склады": data.get("Destinations", ""), "Себестоимость РФ (RUB)": float(data.get("Logistics_RUB", 0)), "Курс клиента (RUB/AMD)": float(data.get("Rate_RUB_AMD", 0)), "К оплате за доставку (AMD)": int(float(data.get("Total_Client_AMD", 0)))}
        table.create(record, typecast=True)
        return f"✅ Доставка РФ: Расчет для {data.get('Client_ID')} добавлен!"
    return "❌ Ошибка типа данных."

# --- ОБРАБОТЧИКИ ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    
    if any(c in text for c in ['×', 'x', '*', '=']) and ('֏' in text or '¥' in text):
        await update.message.reply_text(run_python_audit(text))
        return

    if text.startswith('/paste'):
        raw = text.replace('/paste', '').strip()
        res = await ask_kimi(f"Данные: {raw}", system_msg="Конвертер в /calc. Курс 58/55.")
        await update.message.reply_text(res.strip())
        return

    if "AIRTABLE_EXPORT_START" in text:
        match = re.search(r"AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END", text, re.DOTALL)
        if match:
            data = {l.split(':', 1)[0].strip(): l.split(':', 1)[1].strip() for l in match.group(1).strip().split('\n') if ':' in l}
            await update.message.reply_text(await write_to_airtable(data))
        return

    if "AIRTABLE_DOSTAVKA_START" in text:
        match = re.search(r"AIRTABLE_DOSTAVKA_START(.*?)AIRTABLE_DOSTAVKA_END", text, re.DOTALL)
        if match:
            data = {l.split(':', 1)[0].strip(): l.split(':', 1)[1].strip() for l in match.group(1).strip().split('\n') if ':' in l}
            await update.message.reply_text(await write_to_airtable(data))
        return

    await update.message.reply_text(await ask_kimi(text))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cap = update.message.caption or ""
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO(); await file.download_to_memory(buf)
    img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    if cap.startswith('/1688'):
        await update.message.reply_text(await ask_kimi("Supplier Info", img_b64, "1688 Expert"))
    elif cap.startswith('/hs'):
        await update.message.reply_text(await ask_kimi("Suggest 3 HS Codes", img_b64, "Broker"))
    else:
        barcode, ocr, art = await extract_image_data(Image.open(buf))
        name = await ask_kimi(f"OCR: {ocr}. Art: {art}. Barcode: {barcode}.", img_b64, SYSTEM_MSG_NAMING)
        final = re.sub(r'[\\/*?:"<>|]', '', name.strip()) + ".pdf"
        await update.message.reply_text(f"✅ **Готово для склада!**\n📄 `{final}`\nBarcode: {barcode}\nArt: {art}")

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = "<b>📂 Функции GS Orders:</b>\n\n1️⃣ <b>/paste [данные]</b> - расчет в /calc\n2️⃣ <b>/1688 [фото]</b> - инфо о поставщике\n3️⃣ <b>/hs [фото]</b> - коды ТН ВЭД\n4️⃣ <b>Просто фото</b> - имя файла (Naming)\n5️⃣ <b>Airtable</b> - запись Выкупа, Карго и Доставки РФ."
    await update.message.reply_text(menu_text, parse_mode='HTML')

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот готов! Нажми /menu")))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__': main()
