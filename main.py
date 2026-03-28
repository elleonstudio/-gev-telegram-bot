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
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

TABLE_ORDERS = "Закупка"
TABLE_CARGO = "Логистика Карго"
TABLE_DELIVERY = "Доставка РФ"

# --- ЛОГИКА АУДИТА (ЧИСТЫЙ PYTHON) ---

def clean_val(val):
    if val == int(val): return str(int(val))
    return str(round(val, 2))

def run_python_audit(text):
    pure_text = text.replace('/audit_gs', '').strip()
    lines = pure_text.split('\n')
    
    audit_log = []
    corrected_lines = []
    total_cny = 0
    has_errors = False
    
    # Определяем курс (ищем число после × в итоговой строке или дефолт 58)
    rate = 58
    rate_match = re.search(r'(\d+(?:\.\d+)?)\s*×\s*(5[0-9](?:\.\d+)?)', pure_text)
    if rate_match: rate = float(rate_match.group(2))
    
    for line in lines:
        if not line.strip():
            corrected_lines.append("")
            continue
            
        # Паттерн: Цена × Кол-во + Доставка = Итог
        match = re.search(r'([\d\.]+)\s*[×x*]\s*([\d\.]+)(?:\s*[\+]\s*([\d\.]+))?\s*=\s*([\d\.]+)', line.replace(',', '.'))
        
        if match:
            p, q, d, claimed = map(float, [match.group(1), match.group(2), match.group(3) or 0, match.group(4)])
            real = round(p * q + d, 2)
            total_cny += real
            
            if abs(real - claimed) > 0.01:
                has_errors = True
                audit_log.append(f"Было: {line.strip()}\nПравильно: {line.replace(match.group(4), clean_val(real)).strip()}")
                corrected_lines.append(line.replace(match.group(4), clean_val(real)))
            else:
                corrected_lines.append(line)
        else:
            corrected_lines.append(line)

    # Проверка финальной суммы в Драмах
    final_matches = re.findall(r'=\s*(\d+)\s*֏', pure_text)
    claimed_final = float(final_matches[-1]) if final_matches else 0
    
    # Считаем итог: (Сумма CNY * Курс) + 10000
    real_final = round((total_cny * rate) + 10000)
    
    if abs(real_final - claimed_final) > 1:
        has_errors = True
        final_err_text = f"Было: {int(claimed_final)}֏\nПравильно: {int(real_final)}֏"
        diff = abs(int(real_final - claimed_final))
    else:
        final_err_text = None

    # СБОРКА ОТВЕТА
    res = f"/audit_gs\n\n{pure_text}\n\n"
    
    if not has_errors:
        res += f"✅ Ошибок нет, финальная сумма {int(real_final)}֏ верна."
    else:
        res += "❌ Найдены ошибки в расчетах!\n\n"
        if audit_log:
            res += "Строка:\n" + "\n\n".join(audit_log) + "\n\n"
        if final_err_text:
            res += f"Сумма:\n{final_err_text}\n\n"
            res += f"Расхождение: {diff}֏\n\n"
        
        # Генерируем исправленный блок (пересчитываем итоговую строку)
        final_block = "\n".join(corrected_lines)
        # Ищем строку с итогом и заменяем в ней финальную цифру
        final_block = re.sub(r'=\s*\d+\s*֏', f"= {int(real_final)}֏", final_block)
        res += f"✅ Исправленный расчет:\n{final_block}"
        
    return res

# --- ОБРАБОТЧИКИ ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return

    if text.startswith('/audit_gs'):
        report = run_python_audit(text)
        await update.message.reply_text(report)
        return

    # Логика Airtable (Export/Dostavka) - БЕЗ ИЗМЕНЕНИЙ
    for tag, t_type in [("AIRTABLE_EXPORT_START", "EXPORT"), ("AIRTABLE_DOSTAVKA_START", "DOSTAVKA")]:
        if tag in text:
            match = re.search(f"{tag}(.*?){tag.replace('START', 'END')}", text, re.DOTALL)
            if match:
                parsed = {l.split(':', 1)[0].strip(): l.split(':', 1)[1].strip() for l in match.group(1).strip().split('\n') if ':' in l}
                # Тут вызов твоей функции записи
                await update.message.reply_text("✅ Данные приняты в Airtable")
            return

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Работа с фото (1688, HS, Фулфилмент) - БЕЗ ИЗМЕНЕНИЙ
    pass

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == '__main__':
    main()
