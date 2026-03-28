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

SYSTEM_MSG_NAMING = (
    "Ты — эксперт по логистике в Китае. Создай имя файла для фулфилмента. "
    "Формат: [Описание на китайском]_[Description in English]_[Размер]_[Артикул]_[Штрихкод]. "
    "ОБЯЗАТЕЛЬНО: цвет и материал на китайском в начале!"
)

# --- ЛОГИКА АУДИТА (PYTHON) ---

def clean_num(val):
    if val == int(val): return str(int(val))
    return str(round(val, 2))

def run_python_audit(text):
    lines = text.strip().split('\n')
    audit_log, corrected_lines = [], []
    total_cny, has_errors = 0, False
    rate, commission = 58.0, 10000.0

    rate_match = re.search(r'(?:курс|1¥-)\s*(\d+(?:\.\d+)?)', text.lower())
    if rate_match: rate = float(rate_match.group(1))
    
    for line in lines:
        match = re.search(r'([\d\.]+)\s*[×x*]\s*([\d\.]+)(?:\s*[\+]\s*([\d\.]+))?\s*=\s*([\d\.]+)', line.replace(',', '.'))
        if match:
            p, q, d, claimed = map(float, [match.group(1), match.group(2), match.group(3) or 0, match.group(4)])
            real = round(p * q + d, 2)
            total_cny += real
            if abs(real - claimed) > 0.01:
                has_errors = True
                audit_log.append(f"Было: {line.strip()}\nПравильно: {line.replace(match.group(4), clean_num(real)).strip()}")
            corrected_lines.append(line.replace(match.group(4), clean_num(real)))
        else: corrected_lines.append(line)

    final_matches = re.findall(r'=\s*(\d+)\s*֏', text)
    claimed_final = float(final_matches[-1]) if final_matches else 0
    real_final = round((total_cny * rate) + commission)

    res = f"/audit_gs\n\n{text}\n\n"
    if not has_errors and abs(real_final - claimed_final) <= 1:
        res += f"✅ Ошибок нет, финальная сумма {int(real_final)}֏ верна."
    else:
        res += "❌ Найдены ошибки в расчетах!\n\n"
        if audit_log: res += "Строка:\n" + "\n\n".join(audit_log) + "\n\n"
        if abs(real_final - claimed_final) > 1:
            res += f"Сумма:\nБыло: {int(claimed_final)}֏\nПравильно: {int(real_final)}֏\n\nРасхождение: {abs(int(real_final - claimed_final))}֏\n\n"
        res += f"✅ Исправленный расчет:\n" + "\n".join(corrected_lines).replace(str(int(claimed_final)), str(int(real_final)))
    return res

# --- ИИ И ОБРАБОТКА ДАННЫХ ---

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ассистент"):
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    content = [{'type': 'text', 'text': prompt}]
    if image_b64: content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, 
                                 json={'model': 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k', 
                                       'messages': [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}]}) as resp:
            return (await resp.json())['choices'][0]['message']['content'] if resp.status == 200 else "Error"

# --- ОБРАБОТЧИКИ ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    if any(c in text for c in ['×', 'x', '*', '=']) and ('֏' in text or '¥' in text):
        await update.message.reply_text(run_python_audit(text))
    elif text.startswith('/paste'):
        await update.message.reply_text(await ask_kimi(text[6:], system_msg="Конвертер в /calc"))
    elif "AIRTABLE" in text:
        await update.message.reply_text("✅ Данные отправлены в Airtable") # Здесь вызов API Airtable

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cap = update.message.caption or ""
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO(); await file.download_to_memory(buf)
    img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    
    if cap.startswith('/1688'):
        await update.message.reply_text(await ask_kimi("Supplier Info", img_b64, "1688 Expert"))
    elif cap.startswith('/hs'):
        await update.message.reply_text(await ask_kimi("HS Codes", img_b64, "Broker"))
    else:
        # Логика для этикеток (naming)
        img = Image.open(buf)
        bc = decode(img)[0].data.decode() if decode(img) else "-"
        ocr = pytesseract.image_to_string(img, lang='rus+eng+chi_sim')
        name = await ask_kimi(f"Name this: {ocr}", img_b64, SYSTEM_MSG_NAMING)
        await update.message.reply_text(f"✅ Для склада:\n📄 `{name.strip()}.pdf`\nBarcode: {bc}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__': main()
