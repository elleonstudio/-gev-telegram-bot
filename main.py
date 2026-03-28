import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode
from pyairtable import Api

# --- НАСТРОЙКИ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ТВОЙ ТОКЕН
TELEGRAM_TOKEN = "8745665017:AAGwLlf20_uiI1g2vdntwfHFkWsb26CKmmg"
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

# --- ЛОГИКА АУДИТА (ЧИСТЫЙ PYTHON) ---

def run_python_audit(text):
    pure_text = text.replace('/audit_gs', '').strip()
    lines = pure_text.split('\n')
    audit_log, corrected_lines = [], []
    total_cny, has_errors = 0, False
    rate, commission = 58.0, 10000.0

    for line in lines:
        if not line.strip():
            corrected_lines.append(""); continue
        
        match = re.search(r'([\d\.]+)\s*[×x*]\s*([\d\.]+)(?:\s*[\+]\s*([\d\.]+))?\s*=\s*([\d\.]+)', line.replace(',', '.'))
        if match:
            p, q, d, claimed = map(float, [match.group(1), match.group(2), match.group(3) or 0, match.group(4)])
            real = round(p * q + d, 2)
            total_cny += real
            if abs(real - claimed) > 0.1:
                has_errors = True
                val_str = str(int(real)) if real.is_integer() else str(real)
                audit_log.append(f"Было: {line.strip()}\nПравильно: {line.replace(match.group(4), val_str).strip()}")
                corrected_lines.append(line.replace(match.group(4), val_str))
            else: corrected_lines.append(line)
        else:
            r_m = re.search(r'×(5[0-9](?:\.\d+)?)', line)
            if r_m: rate = float(r_m.group(1))
            c_m = re.search(r'\+(10000|[\d\.]+%|[\d\.]+)', line)
            if c_m:
                if '%' in c_m.group(1): commission = (total_cny * rate) * (float(c_m.group(1).replace('%', '')) / 100)
                else: commission = float(c_m.group(1))
            corrected_lines.append(line)

    real_final = round((total_cny * rate) + commission)
    claimed_f_match = re.findall(r'=\s*(\d+)\s*֏', pure_text)
    claimed_final = float(claimed_f_match[-1]) if claimed_f_match else 0
    
    final_err = None
    if abs(real_final - claimed_final) > 1:
        has_errors = True
        final_err = f"Было: {int(claimed_final)}֏\nПравильно: {int(real_final)}֏"

    res = f"/audit_gs\n\n{pure_text}\n\n"
    if not has_errors:
        res += f"✅ Ошибок нет, финальная сумма {int(real_final)}֏ верна."
    else:
        res += "❌ Найдены ошибки в расчетах!\n\n"
        if audit_log: res += "Строка:\n" + "\n\n".join(audit_log) + "\n\n"
        if final_err: res += f"Сумма:\n{final_err}\n\nРасхождение: {abs(int(real_final - claimed_final))}֏\n\n"
        final_block = "\n".join(corrected_lines)
        final_block = re.sub(r'=\s*\d+\s*֏', f"= {int(real_final)}֏", final_block)
        res += f"✅ Исправленный расчет:\n{final_block}"
    return res

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ассистент") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    payload = {"model": "moonshot-v1-8k-vision-preview" if image_b64 else "moonshot-v1-8k",
               "messages": [{"role": "system", "content": system_msg},
                            {"role": "user", "content": [{"type": "text", "text": prompt}]}]}
    if image_b64: payload["messages"][1]["content"].append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json=payload) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
            return f"Error_{resp.status}"

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

# --- ОБРАБОТЧИКИ ---

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = (
        "<b>📂 Функции GS Orders:</b>\n\n"
        "1️⃣ <b>/audit_gs [текст]</b> — Проверка математики (строгий дизайн)\n"
        "2️⃣ <b>/paste [текст]</b> — Конвертер в шаблон /calc\n"
        "3️⃣ <b>Фото или PDF</b> — Имя файла для склада и штрихкод\n"
        "4️⃣ <b>/1688 [фото]</b> — Поиск поставщика\n"
        "5️⃣ <b>/hs [фото]</b> — Коды ТН ВЭД"
    )
    await update.message.reply_text(menu_text, parse_mode='HTML')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await context.bot.get_file(update.message.document.file_id)
    if not update.message.document.file_name.lower().endswith('.pdf'): return
    buf = BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    try:
        images = convert_from_bytes(buf.read(), dpi=300)
        if images:
            img = images[0]
            barcode, ocr, art = await extract_image_data(img)
            await update.message.reply_text(f"✅ PDF обработан.\nШтрихкод: <code>{barcode}</code>\nАртикул: <code>{art}</code>", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка PDF: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    if text.startswith('/audit_gs'):
        await update.message.reply_text(run_python_audit(text))
    elif text.startswith('/paste'):
        await update.message.reply_text(await ask_kimi(text, system_msg="Конвертер в /calc"))
    else:
        await update.message.reply_text(await ask_kimi(text))

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот готов!")))
    app.add_handler(CommandHandler("menu", show_menu)) # ТЕПЕРЬ ФУНКЦИЯ ОПРЕДЕЛЕНА ВЫШЕ
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__':
    main()
