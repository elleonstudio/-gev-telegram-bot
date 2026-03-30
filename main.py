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

# Замените на свои токены
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'ВАШ_ТОКЕН')
KIMI_API_KEY = os.getenv('KIMI_API_KEY', 'ВАШ_КЛЮЧ')
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

# --- AIRTABLE ЛОГИКА ---
async def write_to_airtable(data: dict):
    api = Api(AIRTABLE_TOKEN)
    def fmt_date(d):
        try: return datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")
        except: return datetime.now().strftime("%Y-%m-%d")

    try:
        if "Invoice_ID" in data:
            table = api.table(AIRTABLE_BASE_ID, TABLE_ORDERS)
            full_id = data.get("Invoice_ID", "")
            client_match = re.match(r'^([a-zA-Z]+)', full_id)
            client_name = client_match.group(1).capitalize() if client_match else ""
            
            record = {
                "Код Карго": full_id,
                "Клиент": client_name,
                "Дата": fmt_date(data.get("Date")),
                "Сумма (¥)": float(data.get("Sum_Client_CNY", 0)),
                "Реал Цена Закупки (¥)": float(data.get("Real_Purchase_CNY", data.get("Sum_Client_CNY", 0))),
                "Курс Клиент": float(data.get("Client_Rate", 58)),
                "Курс Реал": float(data.get("Real_Rate", 55)),
                "Расход материалов (¥)": float(data.get("China_Logistics_CNY", 0))
            }
            table.create(record, typecast=True)
            return f"✅ Выкупы: Заказ {full_id} добавлен!"
        return "❌ Тип данных не распознан."
    except Exception as e:
        return f"❌ Ошибка Airtable: {e}"

# --- ОБРАБОТЧИК ТЕКСТА ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    print(f"DEBUG: Получено сообщение: {text}") # Для отладки в консоли
    
    if "AIRTABLE_EXPORT_START" in text:
        data_match = re.search(r'AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END', text, re.DOTALL)
        if data_match:
            lines = data_match.group(1).strip().split('\n')
            parsed = {}
            for line in lines:
                if ':' in line:
                    key, val = line.split(':', 1)
                    parsed[key.strip()] = val.strip()
            
            status = await write_to_airtable(parsed)
            await update.message.reply_text(status)
    else:
        # Ответ от Кими на обычный текст
        res = await ask_kimi(text)
        await update.message.reply_text(res)

# --- ОБРАБОТЧИК ФОТО (Штрих-коды и этикетки) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (update.message.caption or "").lower()
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO(); await file.download_to_memory(buf)
    image = Image.open(buf)
    
    # (Здесь остается ваш рабочий код для штрих-кодов, который вы просили не трогать)
    # ... логика OCR и генерации PDF ...
    await update.message.reply_text("⏳ Обрабатываю фото...")
    # (Добавьте сюда вызов вашей функции генерации PDF)

# --- ЗАПУСК ---
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # ВАЖНО: Обработчик текста должен быть зарегистрирован явно
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Бот запущен!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    print("Бот запущен и готов к работе...")
    app.run_polling()

if __name__ == '__main__':
    main()
