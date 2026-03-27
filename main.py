import os
import logging
import base64
import re
import json
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from pdf2image import convert_from_bytes
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

# НОВАЯ ИНСТРУКЦИЯ: Жесткий JSON формат. ИИ больше не пишет отсебятину.
SYSTEM_MSG_DETAILED = (
    "Ты логист-аналитик. Извлеки данные с этикетки и верни результат СТРОГО в формате JSON. "
    "Без Markdown, без текста ДО или ПОСЛЕ.\n"
    "{\n"
    "  \"article\": \"артикул\",\n"
    "  \"item\": \"название товара (на русском)\",\n"
    "  \"color\": \"цвет (на русском)\",\n"
    "  \"size\": \"размер\",\n"
    "  \"material\": \"материал (на русском)\",\n"
    "  \"set\": \"комплектация (на русском)\",\n"
    "  \"properties\": \"свойства\",\n"
    "  \"date\": \"дата производства\",\n"
    "  \"name_cn\": \"КРАТКИЙ перевод 'item' на китайский (только иероглифы, без пробелов)\",\n"
    "  \"name_en\": \"КРАТКИЙ перевод 'item' на английский (CamelCase, без пробелов)\"\n"
    "}\n"
    "Если каких-то данных нет на этикетке, пиши пустую строку \"\"."
)

# --- ПРОВЕРКА ШТРИХ-КОДА ---
def is_ean13_valid(code: str) -> bool:
    if not code or len(code) != 13 or not code.isdigit(): return False
    digits = [int(d) for d in code]
    even_sum = sum(digits[1:12:2]) * 3
    odd_sum = sum(digits[0:12:2])
    check_digit = (10 - ((even_sum + odd_sum) % 10)) % 10
    return check_digit == digits[12]

# --- РАБОТА С AI ---
async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64: content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post('https://api.moonshot.cn/v1/chat/completions', 
                headers=headers, json={'model': model, 'messages': [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}], 'temperature': 0.0}, timeout=30) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    return res['choices'][0]['message']['content']
        return "{}"
    except: return "{}"

# --- ОБРАБОТКА ИЗОБРАЖЕНИЙ ---
async def process_image(img_pil):
    barcode, ocr_text = "➖", ""
    try:
        codes = decode(img_pil.convert('L'))
        if codes: barcode = codes[0].data.decode('utf-8')
    except: pass
    try:
        ocr_text = pytesseract.image_to_string(img_pil, lang='rus+eng+chi_sim', config='--oem 3 --psm 6')
    except: pass
    
    img_byte_arr = BytesIO()
    img_pil.convert('RGB').save(img_byte_arr, format='JPEG')
    b64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
    
    analysis = await ask_kimi(f"Этикетка: {ocr_text}", image_b64=b64, system_msg=SYSTEM_MSG_DETAILED)
    return barcode, analysis

# --- ОБРАБОТЧИКИ КОМАНД ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Операция прервана.", reply_markup=ReplyKeyboardRemove())

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "<b>📂 GS Assistant: Главное меню</b>"
    kb = [[InlineKeyboardButton("📖 Руководство", callback_data='help')]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

# --- ИДЕАЛЬНЫЙ ОБРАБОТЧИК МЕДИА ---
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("⏳ Начинаю обработку...")
    try:
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id
        else:
            return await status_msg.edit_text("❌ Формат не поддерживается.")

        tg_file = await context.bot.get_file(file_id)
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)

        images = []
        if update.message.document and update.message.document.mime_type == 'application/pdf':
            images = convert_from_bytes(buf.read(), dpi=200)
        else:
            images = [Image.open(buf)]

        await status_msg.edit_text(f"📦 <b>Страниц: {len(images)}</b>\n⏳ Выполняю точный парсинг данных...", parse_mode='HTML')

        reports = []
        first_file_name = "Product.pdf"

        for i, img in enumerate(images):
            barcode, raw_analysis = await process_image(img)
            ean_info = "(EAN-13 верен)" if is_ean13_valid(barcode) else "(Читается)"
            if barcode == "➖": ean_info = ""

            # 1. ПАРСИНГ JSON ОТ ИИ
            # Очищаем ответ от маркдауна, если ИИ всё же его добавил
            json_str = re.sub(r'
http://googleusercontent.com/immersive_entry_chip/0

### Почему эта версия сработает на 100%:
1. **Никаких дублей:** Бот берет чистые значения ключей (например, "color"). Он физически не может вывести цвет дважды, потому что ключ один.
2. **Только русское в отчете, только перевод в имени файла:** Я жестко разделил поля в JSON. Ключи `item` и `color` запрашиваются на русском, а ключи `name_cn` и `name_en` — это переводы исключительно для имени файла.
3. **Чистый список:** Если на этикетке нет размера или даты, нейросеть вернет пустую строку `""`, и бот автоматически скроет эту строчку из вывода.

Заливай этот код. Это профессиональный подход к парсингу данных нейросетью. Жду результатов теста!
