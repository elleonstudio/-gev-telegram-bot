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

# Логирование для отладки в Railway (смотри вкладку Logs, если замолчит)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')
AIRTABLE_TOKEN = "pati6TFqzPlZaI08o.88a1e98775f215fb08b58c2fde28b38acebc5f4556c8eb850b9ca9930dbcf607"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"
AIRTABLE_TABLE_NAME = "Закупка"

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "Ты ИИ-ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64:
        content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    
    messages = [
        {'role': 'system', 'content': system_msg},
        {'role': 'user', 'content': content}
    ]
    
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json={'model': model, 'messages': messages, 'temperature': 0.0}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
            logger.error(f"Kimi API Error: {resp.status}")
            return f"Error_{resp.status}"

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return

    # 1. Если сообщение начинается с /calc — это уже готовый расчет, ИГНОРИРУЕМ
    if text.strip().startswith('/calc'):
        return

    # 2. Обработка команды /paste (Твой ручной расчет -> Шаблон GS Orders)
    if text.startswith('/paste'):
        raw_input = text.replace('/paste', '').strip()
        msg = await update.message.reply_text("⏳ Формирую шаблон...")
        
        system_paste = (
            "Ты — робот-конвертер. Перенеси данные в шаблон /calc.\n"
            "НЕ СЧИТАЙ САМ. Просто вытащи числа.\n"
            "Пример: 7.5x200+144=1644 vase -> Название: vase, Кол-во: 200, Цена: 7.5, Доставка: 144.\n"
            "Закупка: -. Размеры: - - - -.\n"
            "Ответ начни строго с /calc"
        )
        
        res = await ask_kimi(f"Заполни шаблон: {raw_input}", system_msg=system_paste)
        await msg.edit_text(res.strip())
        return

    # 3. Запись в Airtable
    if "AIRTABLE_EXPORT_START" in text:
        await update.message.reply_text("📥 Записываю в Airtable (функция в разработке)...")
        # Здесь можно оставить твой старый парсер Airtable
        return

    # 4. Обычный чат
    resp = await ask_kimi(text)
    await update.message.reply_text(resp[:4000])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    if caption.startswith('/1688'):
        res = await ask_kimi("Extract supplier info: Company CN/EN, Tax ID, Address CN/EN. Use code blocks.", image_b64=img_b64, system_msg="1688 Expert.")
        await update.message.reply_text(res, parse_mode='Markdown')
    elif caption.startswith('/hs'):
        res = await ask_kimi(f"Suggest 3 HS Codes for: {caption}", image_b64=img_b64, system_msg="Broker.")
        await update.message.reply_text(res)
    else:
        await update.message.reply_text("📸 Фото получено. Используй /1688 или /hs в подписи.")

def main():
    if not TELEGRAM_TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN found!")
        return
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', lambda u, c: u.message.reply_text("🤖 Бот запущен!")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    logger.info("Bot started polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
