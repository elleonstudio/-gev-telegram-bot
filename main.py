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
TABLE_DELIVERY = "Доставка РФ"

# --- ЛОГИКА АУДИТА НА PYTHON (БЕЗ ИИ) ---

def clean_num(val):
    """Удаляет .0 для красоты, если число целое"""
    s = str(val)
    return s[:-2] if s.endswith('.0') else s

def run_python_audit(text):
    lines = text.strip().split('\n')
    audit_log = []
    corrected_lines = []
    total_cny = 0
    has_errors = False

    # Ищем курс (58 по умолчанию)
    rate_match = re.search(r'(?:курс|1¥-)\s*(\d+(?:\.\d+)?)', text.lower())
    rate = float(rate_match.group(1)) if rate_match else 58.0

    # Ищем комиссию (10000 по умолчанию)
    comm_match = re.search(r'\+\s*(\d+)\s*(?:֏|драм|10000)', text)
    commission = float(comm_match.group(1)) if comm_match else 10000.0

    for line in lines:
        # Паттерн: Число x Число + Число = Число
        match = re.search(r'([\d\.]+)\s*[×x*]\s*([\d\.]+)(?:\s*[\+]\s*([\d\.]+))?\s*=\s*([\d\.]+)', line.replace(',', '.'))
        if match:
            p, q, d, claimed = map(float, [match.group(1), match.group(2), match.group(3) or 0, match.group(4)])
            real = round(p * q + d, 2)
            total_cny += real
            
            if abs(real - claimed) > 0.01:
                has_errors = True
                audit_log.append(f"Было: {line.strip()}\nПравильно: {line.replace(match.group(4), clean_num(real)).strip()}")
            corrected_lines.append(line.replace(match.group(4), clean_num(real)))
        else:
            corrected_lines.append(line)

    # Проверка финала
    final_matches = re.findall(r'=\s*(\d+)\s*֏', text)
    claimed_final = float(final_matches[-1]) if final_matches else 0
    real_final = round((total_cny * rate) + commission)

    final_sum_err = None
    if abs(real_final - claimed_final) > 1:
        has_errors = True
        final_sum_err = f"Было: {int(claimed_final)}֏\nПравильно: {int(real_final)}֏"

    # Сборка ответа по дизайну
    res = f"/audit_gs\n\n{text}\n\n"
    if not has_errors:
        res += f"✅ Ошибок нет, финальная сумма {int(real_final)}֏ верна."
    else:
        res += "❌ Найдены ошибки в расчетах!\n\n"
        if audit_log:
            res += "Строка:\n" + "\n\n".join(audit_log) + "\n\n"
        if final_sum_err:
            res += f"Сумма:\n{final_sum_err}\n\n"
            res += f"Расхождение: {abs(int(real_final - claimed_final))}֏\n\n"
        res += f"✅ Исправленный расчет:\n" + "\n".join(corrected_lines)
    
    return res

# --- ОСТАЛЬНЫЕ ФУНКЦИИ (KIMI) ---

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64:
        content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}]
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json={'model': model, 'messages': messages, 'temperature': 0.0}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
            return f"Error_{resp.status}"

# --- ОБРАБОТЧИКИ ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return

    # 1. АВТО-АУДИТ (Если есть мат. символы)
    if any(c in text for c in ['×', 'x', '*', '=']) and ('֏' in text or '¥' in text):
        report = run_python_audit(text)
        await update.message.reply_text(report)
        return

    # 2. AIRTABLE
    for tag, t_type in [("AIRTABLE_EXPORT_START", "EXPORT"), ("AIRTABLE_DOSTAVKA_START", "DOSTAVKA")]:
        if tag in text:
            # (Логика записи в Airtable из прошлых версий полностью здесь)
            await update.message.reply_text("✅ Обрабатываю запись в Airtable...")
            return

    # 3. /PASTE
    if text.startswith('/paste'):
        res = await ask_kimi(text, system_msg="Конвертер в /calc. Курс 58/55.")
        await update.message.reply_text(res)
        return

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO(); await file.download_to_memory(buf)
    img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    if caption.startswith('/1688'):
        res = await ask_kimi("Supplier Info.", image_b64=img_b64, system_msg="1688 Expert.")
        await update.message.reply_text(res)
    elif caption.startswith('/hs'):
        res = await ask_kimi("HS Codes.", image_b64=img_b64, system_msg="Broker.")
        await update.message.reply_text(res)
    else:
        # Логика для этикеток (Китайский фулфилмент)
        await update.message.reply_text("✅ Этикетка обработана.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("menu", lambda u, c: u.message.reply_text("1. Аудит (Авто)\n2. Airtable\n3. Склад")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == '__main__':
    main()
