import os
import logging
import re
import aiohttp
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pyairtable import Api

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены и ID
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

# Таблицы
TABLE_ORDERS = "Закупка"
TABLE_CARGO = "Логистика Карго"

def parse_airtable_block(text: str) -> dict:
    """Извлекает данные между тегами START и END"""
    parsed = {}
    match = re.search(r'AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END', text, re.DOTALL)
    if match:
        for line in match.group(1).strip().split('\n'):
            if ':' in line:
                key, val = line.split(':', 1)
                parsed[key.strip()] = val.strip()
    return parsed

def format_date(date_str):
    """Превращает ДД.ММ.ГГГГ в ГГГГ-ММ-ДД"""
    try:
        return datetime.strptime(date_str, "%d.%m.%Y").strftime("%Y-%m-%d")
    except:
        return datetime.now().strftime("%Y-%m-%d")

async def send_to_airtable(data: dict):
    api = Api(AIRTABLE_TOKEN)
    
    # ОПРЕДЕЛЯЕМ ТИП ДАННЫХ
    if "Invoice_ID" in data:
        # ТИП 1: ВЫКУП (ORDERS)
        table = api.table(AIRTABLE_BASE_ID, TABLE_ORDERS)
        record = {
            "Код Карго": data.get("Invoice_ID"),
            "Дата": format_date(data.get("Date")),
            "Сумма (¥)": float(data.get("Sum_Client_CNY", 0)),
            "Реал Цена Закупки (¥)": float(data.get("Real_Purchase_CNY", 0)),
            "Курс Клиент": float(data.get("Client_Rate", 0)),
            "Курс Реал": float(data.get("Real_Rate", 0)),
            "Расход материалов (¥)": float(data.get("China_Logistics_CNY", 0)),
            "Кол-во коробок": int(data.get("FF_Boxes_Qty", 0))
        }
        table.create(record, typecast=True)
        return "✅ Данные [Тип 1: Выкуп] успешно выгружены в Airtable"

    elif "Party_ID" in data:
        # ТИП 2: ЛОГИСТИКА КАРГО (CARGO)
        table = api.table(AIRTABLE_BASE_ID, TABLE_CARGO)
        record = {
            "Party_ID": data.get("Party_ID"),
            "Дата": format_date(data.get("Date")),
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
        return "✅ Данные [Тип 2: Логистика] успешно выгружены в Airtable"
    
    return "❌ Ошибка: Не удалось определить тип данных (нужен Invoice_ID или Party_ID)"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return

    # Если видим блок экспорта — обрабатываем и пишем в Airtable
    if "AIRTABLE_EXPORT_START" in text:
        data = parse_airtable_block(text)
        if data:
            result = await send_to_airtable(data)
            await update.message.reply_text(result)
        else:
            await update.message.reply_text("❌ Ошибка парсинга блока данных.")
        return

    # Остальные команды (паста, расчеты и т.д.) пропускаем через ИИ
    if text.startswith('/paste'):
        # ... (тут остается твоя логика /paste из прошлых шагов)
        pass

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот готов к приему данных Airtable!")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__':
    main()
