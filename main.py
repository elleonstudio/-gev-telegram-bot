import os
import re
import logging
import base64
import aiohttp
from io import BytesIO
from datetime import datetime
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image

# --- НАСТРОЙКИ ЛОГИРОВАНИЯ ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Чтение переменных
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

# --- МАТЕМАТИЧЕСКИЙ ДВИЖОК (PYTHON) ---
def precise_audit(text):
    lines = text.replace('/audit_gs', '').strip().split('\n')
    errors = []
    total_cny = 0.0
    
    # Ищем курс (по умолчанию 58)
    rate_match = re.search(r'(?:курс|rate|1¥-)\s*(\d+[\.,]?\d*)', text.lower())
    rate = float(rate_match.group(1).replace(',', '.')) if rate_match else 58.0
    
    # Ищем комиссию (по умолчанию 10000)
    comm_match = re.search(r'\+(\d+)\s*(?:֏|драм|amd)', text.lower())
    commission = float(comm_match.group(1)) if comm_match else 10000.0

    for line in lines:
        if '=' in line and any(c in line for c in ['×', 'x', '*']):
            try:
                parts = line.split('=')
                expr = parts[0].replace('×', '*').replace('x', '*').strip()
                claimed = float(re.sub(r'[^\d\.]', '', parts[1].replace(',', '.')).strip())
                # Чистый расчет Python
                actual = round(eval(re.sub(r'[^\d\.\*\+\-\/]', '', expr)), 2)
                
                if abs(actual - claimed) > 0.01:
                    errors.append(f"Было: {line.strip()}\nПравильно: {parts[0].strip()} = {actual}")
                    total_cny += actual
                else:
                    total_cny += actual
            except Exception as e:
                logger.error(f"Ошибка парсинга строки: {e}")
        elif re.search(r'^\d+[\.,]?\d*$', line.strip()): # Если просто число
            total_cny += float(line.strip().replace(',', '.'))

    final_real = round((total_cny * rate) + commission, 2)
    
    # Ищем итоговую сумму пользователя
    claimed_final = 0
    final_match = re.search(r'=(\d+)\s*֏', text)
    if final_match: claimed_final = float(final_match.group(1))

    header = f"/audit_gs\n\n{text.replace('/audit_gs', '').strip()}\n\n"
    
    if not errors and abs(final_real - claimed_final) < 1:
        return header + f"✅ Ошибок нет, финальная сумма {int(final_real)}֏ верна."
    else:
        res = header + "❌ Найдены ошибки в расчетах!\n\n"
        if errors:
            res += "Строка:\n" + "\n\n".join(errors) + "\n\n"
        res += f"Сумма:\nБыло: {int(claimed_final)}֏\nПравильно: {final_real}֏\n\n"
        res += f"Расхождение: {round(abs(final_real - claimed_final), 2)}֏"
        return res

# --- ОСНОВНЫЕ ОБРАБОТЧИКИ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return

    try:
        if text.startswith('/audit_gs'):
            result = precise_audit(text)
            await update.message.reply_text(result)
        elif text.startswith('/menu'):
            await update.message.reply_text("📂 Функции:\n1. /audit_gs - Точный аудит\n2. /paste - Шаблон\n3. Фото - Склад")
        elif text.startswith('/start'):
            await update.message.reply_text("🤖 Бот GS Orders готов к работе!")
    except Exception as e:
        logger.error(f"Ошибка в handle_message: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке.")

def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не найден!")
        return
        
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("start", handle_message))
    app.add_handler(CommandHandler("menu", handle_message))
    
    logger.info("Бот запускается...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
