import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

async def ask_kimi(prompt: str, system_msg: str = "Ты ассистент.") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    messages = [
        {'role': 'system', 'content': system_msg},
        {'role': 'user', 'content': prompt}
    ]
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', 
                                 headers=headers, 
                                 json={'model': 'moonshot-v1-8k', 'messages': messages, 'temperature': 0.0}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
            return f"Ошибка API: {resp.status}"

async def handle_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text
    if not raw_text: return
    
    data_to_process = raw_text.replace('/paste', '').strip()
    if not data_to_process:
        await update.message.reply_text("Отправь данные после команды /paste")
        return

    status_msg = await update.message.reply_text("🏗 Переупаковываю в шаблон GS Orders...")
    
    system_paste = (
        "Ты — технический редактор заказов. Твоя задача: взять расчеты пользователя и расставить их СТРОГО по шаблону.\n"
        "НЕ ИЗМЕНЯЙ ЦИФРЫ И НЕ СЧИТАЙ СУММЫ. Если написано 7.5x200+144, так и пиши в поле Цена или Доставка.\n\n"
        "ФОРМАТ ОТВЕТА (СТРОГО):\n"
        "/calc\n\n"
        "Клиент: [Имя клиента]\n\n"
        "Товар 1:\nНазвание: [Name]\nКоличество: [Qty]\nЦена клиенту: [Price]\nЗакупка: -\nДоставка: [Logistics]\nРазмеры: - - - -\n\n"
        "(повтори для всех товаров)\n\n"
        "Курс клиенту: [Число из текста]\n"
        "Мой курс: [Число из текста]"
    )
    
    prompt = f"Преврати этот текст в шаблон /calc, распределив данные по полям: {data_to_process}"
    
    try:
        result = await ask_kimi(prompt, system_msg=system_paste)
        await status_msg.edit_text(result.strip())
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    if text.strip().startswith('/calc'): return
    if text.startswith('/paste'):
        await handle_paste(update, context)
        return
    resp = await ask_kimi(text)
    await update.message.reply_text(resp[:4000])

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 Бот готов к работе!")))
    app.add_handler(MessageHandler(filters.Regex(r'^/paste'), handle_paste))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
