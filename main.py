import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, InputFile
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
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

# --- AIRTABLE ЛОГИКА ---
async def write_to_airtable(data: dict):
    api = Api(AIRTABLE_TOKEN)
    table = api.table(AIRTABLE_BASE_ID, TABLE_ORDERS)
    
    def fmt_date(d):
        for fmt in ("%m.%d.%Y", "%d.%m.%Y", "%Y-%m-%d"):
            try: return datetime.strptime(d, fmt).strftime("%Y-%m-%d")
            except: continue
        return datetime.now().strftime("%Y-%m-%d")

    try:
        full_id = data.get("Invoice_ID", "Unknown")
        client_name = re.match(r'^([a-zA-Zа-яА-Я]+)', full_id).group(1) if full_id else "Client"
        
        # ВАЖНО: Проверьте названия столбцов в Airtable!
        record = {
            "Код Карго": str(full_id),
            "Клиент": str(client_name),
            "Дата": fmt_date(data.get("Date", "")),
            "Сумма (¥)": float(data.get("Sum_Client_CNY", 0)),
            "Курс Клиент": float(data.get("Client_Rate", 0)),
            "Курс Реал": float(data.get("Real_Rate", 0)),
            "Расход материалов (¥)": float(data.get("China_Logistics_CNY", 0))
        }
        
        table.create(record, typecast=True)
        return f"✅ Заказ {full_id} для {client_name} добавлен!"
    except Exception as e:
        return f"❌ Ошибка Airtable: {str(e)}"

# --- ОБРАБОТЧИК ТЕКСТА (ВКЛЮЧАЯ ПЕРЕСЛАННЫЕ) ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Берем текст из обычного или пересланного сообщения
    text = update.message.text or update.message.caption
    if not text: return

    if "AIRTABLE_EXPORT_START" in text:
        # Ищем блок данных внутри текста
        match = re.search(r'AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END', text, re.DOTALL)
        if match:
            lines = match.group(1).strip().split('\n')
            parsed = {}
            for line in lines:
                if ':' in line:
                    k, v = line.split(':', 1)
                    parsed[k.strip()] = v.strip()
            
            res = await write_to_airtable(parsed)
            await update.message.reply_text(res)
    else:
        # Если это просто текст, можно отправить в Kimi или проигнорировать
        pass

# --- ОБРАБОТЧИК ФОТО (Штрих-коды остаются без изменений) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Код обработки фото из вашего рабочего варианта...
    # (Обязательно вставьте сюда ту часть, где генерация PDF работает)
    pass

# --- ЗАПУСК ---
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Добавляем обработчик для пересланных сообщений через MessageHandler
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Бот онлайн!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT | filters.FORWARDED, handle_text))
    
    app.run_polling()

if __name__ == '__main__':
    main()
