import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode
from pyairtable import Api

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"
AIRTABLE_TABLE_NAME = "Закупка"

SYSTEM_MSG_NAMING = "Ты ассистент по именам файлов. Формат: 中文_English_Размер_Артикул_Штрихкод.pdf"

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = None) -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64: content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    messages = [{'role': 'system', 'content': system_msg or 'Ты ИИ-ассистент.'}, {'role': 'user', 'content': content}]
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json={'model': model, 'messages': messages, 'temperature': 0.0}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
            return f"Error_{resp.status}"

async def extract_image_data(image: Image.Image):
    barcode_num, text, article = "", "", ""
    try:
        codes = decode(image.convert('L'))
        if codes: barcode_num = codes[0].data.decode('utf-8')
    except: pass
    try: text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=r'--oem 3 --psm 6')
    except: pass
    for pattern in [r'Артикул[:\s]+(\d+)', r'Артикул[:\s]*(\d+)', r'Article[:\s]+(\d+)']:
        match = re.search(pattern, text, re.IGNORECASE)
        if match: article = match.group(1); break
    return barcode_num, text, article

def build_response_lines(new_name, barcode_num, article):
    response_lines = [f"📄 `{new_name}`"]
    if barcode_num: response_lines.insert(0, f"✅ Штрих-код: {barcode_num}")
    if article: response_lines.insert(1, f"✅ Артикул: {article}")
    return response_lines

def parse_airtable_export(text: str) -> dict:
    parsed = {}
    match = re.search(r'AIRTABLE_EXPORT_START(.*?)AIRTABLE_EXPORT_END', text, re.DOTALL)
    if match:
        for line in match.group(1).strip().split('\n'):
            if ':' in line:
                key, val = line.split(':', 1)
                parsed[key.strip()] = val.strip()
    invoice_body = text.split('AIRTABLE_EXPORT_START')[0].strip()
    items = [l.strip() for l in invoice_body.split('\n') if l.strip().startswith(('•', '-'))]
    parsed["Invoice_Body"] = "\n".join(items) if items else invoice_body
    return parsed

async def send_to_airtable(parsed_data: dict):
    try:
        api = Api(AIRTABLE_TOKEN)
        table = api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME)
        raw_date = parsed_data.get("Date", "")
        formatted_date = datetime.now().strftime("%Y-%m-%d")
        if "." in raw_date:
            d, m, y = raw_date.split(".")
            formatted_date = f"{y}-{m}-{d}"
        invoice = parsed_data.get("Invoice_ID", "")
        client_name = ""
        match = re.match(r'^([a-zA-Z]+)-?(\d+)', invoice)
        if match: client_name = f"{match.group(1).capitalize()}-{match.group(2)}"
        record = {
            "Код Карго": invoice, "Дата": formatted_date,
            "Сумма (¥)": float(parsed_data.get("Sum_Client_CNY", 0)),
            "Реал Цена Закупки (¥)": float(parsed_data.get("Real_Purchase_CNY", 0)),
            "Курс Клиент": float(parsed_data.get("Client_Rate", 0)),
            "Курс Реал": float(parsed_data.get("Real_Rate", 0)),
            "Расход материалов (¥)": float(parsed_data.get("China_Logistics_CNY", 0)),
            "Кол-во коробок": int(parsed_data.get("FF_Boxes_Qty", 0)),
            "Заказ": parsed_data.get("Invoice_Body", ""), "Карго Статус": "Заказано"
        }
        if client_name: record["Клиент"] = client_name 
        table.create(record, typecast=True)
        return True, client_name
    except Exception: return False, "Airtable Error"

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # /paste — СТРОГИЙ КОНВЕРТЕР БЕЗ ПРАВА НА РАСЧЕТ
    if text.startswith('/paste'):
        raw_input = text.replace('/paste', '').strip()
        msg = await update.message.reply_text("⏳ Переношу данные в шаблон...")
        
        system_paste = (
            "Ты — инструмент форматирования. Твоя единственная задача — перенести данные пользователя в шаблон.\n"
            "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО: складывать, умножать, вычислять промежуточные или итоговые суммы.\n"
            "Пиши только те цифры, которые дал пользователь. Если пользователь написал '7.5x200+144', в шаблоне должно быть именно '7.5x200+144', а не результат.\n"
            "Закупка: всегда '-'. Размеры: всегда '- - - -'.\n"
            "Ответ должен начинаться строго с /calc"
        )
        
        prompt_paste = (
            "Заполни шаблон /calc используя эти данные:\n" + raw_input + "\n\n"
            "Шаблон:\n/calc\n\nКлиент: [ID]\n\nТовар [N]:\nНазвание: [Name]\nКоличество: [Qty]\nЦена клиенту: [Price]\nЗакупка: -\nДоставка: [Logistics]\nРазмеры: - - - -\n\nКурс клиенту: [X]\nМой курс: [Y]"
        )
        
        res = await ask_kimi(prompt_paste, system_msg=system_paste)
        return await msg.edit_text(res.strip())

    if "AIRTABLE_EXPORT_START" in text:
        msg = await update.message.reply_text("📥 Записываю...")
        parsed_data = parse_airtable_export(text)
        success, info = await send_to_airtable(parsed_data)
        if success: await msg.edit_text(f"✅ Добавлено!")
        else: await msg.edit_text(f"❌ Ошибка.")
        return

    if text.startswith('/calc') or "COMMERCIAL INVOICE" in text: return 

    msg = await update.message.reply_text('⏳...')
    resp = await ask_kimi(text)
    await msg.edit_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        caption = update.message.caption or ""
        if caption.lower().strip().startswith('/1688'):
            msg = await update.message.reply_text('⏳...')
            file = await context.bot.get_file(update.message.photo[-1].file_id)
            buf = BytesIO(); await file.download_to_memory(buf)
            prompt = "Company (CN/EN), Tax ID, Address (CN/EN), Phone. Без эмодзи. Код блоки."
            res = await ask_kimi(prompt, image_b64=base64.b64encode(buf.getvalue()).decode('utf-8'), system_msg="Эксперт 1688.")
            return await msg.edit_text(res, parse_mode='Markdown')

        if caption.lower().strip().startswith('/hs'):
            msg = await update.message.reply_text('⏳...')
            file = await context.bot.get_file(update.message.photo[-1].file_id)
            buf = BytesIO(); await file.download_to_memory(buf)
            prompt = f"Подбери 3 кода ТН ВЭД. Описание: {caption.replace('/hs', '')}."
            res = await ask_kimi(prompt, image_b64=base64.b64encode(buf.getvalue()).decode('utf-8'), system_msg="Брокер ЕАЭС.")
            codes = re.findall(r'\b\d{4,10}\b', res)
            final_msg = f"📦 Результаты:\n\n{res}\n\n🔍 Alta.ru:\n"
            for code in set(codes): final_msg += f"👉 [Код {code}](https://www.alta.ru/tnved/code/{code}/)\n"
            return await msg.edit_text(final_msg, parse_mode='Markdown', disable_web_page_preview=True)

        msg = await update.message.reply_text('⏳...')
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        buf = BytesIO(); await file.download_to_memory(buf)
        barcode_num, text, article = await extract_image_data(Image.open(buf))
        new_name = await ask_kimi(f"Текст: {text[:1000]}\nШтрихкод: {barcode_num}", image_b64=base64.b64encode(buf.getvalue()).decode('utf-8'), system_msg=SYSTEM_MSG_NAMING)
        new_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()) + ".pdf"
        await msg.edit_text('\n'.join(build_response_lines(new_name, barcode_num, article)), parse_mode='Markdown', disable_web_page_preview=True)
    except Exception: pass

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        msg = await update.message.reply_text('⏳...')
        buf = BytesIO(); await (await context.bot.get_file(doc.file_id)).download_to_memory(buf)
        buf.seek(0); images = convert_from_bytes(buf.read(), dpi=200, first_page=1, last_page=1)
        barcode_num, text, article = await extract_image_data(images[0])
        new_name = await ask_kimi(f"Текст: {text[:1000]}", system_msg=SYSTEM_MSG_NAMING)
        new_name = re.sub(r'[\\/*?:"<>|]', '', new_name.strip()) + ".pdf"
        await msg.delete(); await update.message.reply_document(document=InputFile(buf, filename=new_name), caption=new_name)
    except Exception: pass

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', lambda u, c: u.message.reply_text("🤖 Бот готов!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__': main()
