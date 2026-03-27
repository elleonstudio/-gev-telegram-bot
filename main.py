import os
import logging
import base64
import re
import aiohttp
from io import BytesIO
from datetime import datetime

from telegram import Update, InputFile, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
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

# --- ОБРАБОТЧИКИ МЕНЮ И РУКОВОДСТВА ---

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = (
        "<b>📂 GS Assistant: Главное меню</b>\n\n"
        "Выбери нужную функцию или нажми на кнопку ниже для подробного руководства."
    )
    keyboard = [
        [InlineKeyboardButton("📖 Открыть руководство", callback_data='open_guide')],
        [InlineKeyboardButton("📊 Статус Airtable", callback_data='check_airtable')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(menu_text, reply_markup=reply_markup, parse_mode='HTML')

async def show_guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Метод может вызываться как через команду, так и через кнопку
    query = update.callback_query
    guide_text = (
        "<b>📖 Полное руководство GS Assistant:</b>\n\n"
        "🔹 <b>1. Расчеты (/paste):</b>\n"
        "Отправь <code>/paste Имя 1234, ценаxкол-во+доставка</code>. Бот создаст шаблон /calc для GS Orders. "
        "Курсы зафиксированы: 58 (клиент) и 55 (твой).\n\n"
        "🔹 <b>2. Проверка поставщика (/1688):</b>\n"
        "Прикрепи фото лицензии или карточки товара и напиши <code>/1688</code>. "
        "Бот выдаст название, Tax ID и контакты.\n\n"
        "🔹 <b>3. Поиск кодов ТН ВЭД (/hs):</b>\n"
        "Прикрепи фото товара и напиши <code>/hs</code>. Бот предложит 3 кода со ссылками на Alta.ru.\n\n"
        "🔹 <b>4. Склад и PDF:</b>\n"
        "Просто кидай фото этикетки или PDF. Бот вытащит штрих-код и переименует файл по стандарту.\n\n"
        "🔹 <b>5. Авто-учет Airtable:</b>\n"
        "Пересылай блоки <code>AIRTABLE_EXPORT</code>. "
        "Если есть <i>Invoice_ID</i> — летит в Выкупы. "
        "Если есть <i>Party_ID</i> — летит в Логистику Карго."
    )
    
    if query:
        await query.answer()
        await query.edit_message_text(guide_text, parse_mode='HTML')
    else:
        await update.message.reply_text(guide_text, parse_mode='HTML')

# --- ОСТАЛЬНАЯ ЛОГИКА (БЕЗ ИЗМЕНЕНИЙ) ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    if text.strip().startswith('/calc'): return

    if text.startswith('/paste'):
        raw_input = text.replace('/paste', '').strip()
        msg = await update.message.reply_text("⏳ Формирую шаблон...")
        system_paste = "Ты конвертер. Расставь данные в шаблон /calc. Цена - 1-е число, Кол-во - после x, Доставка - после +. Курс: 58/55. Начало ответа: /calc"
        res = await ask_kimi(f"Данные: {raw_input}", system_msg=system_paste)
        await msg.edit_text(res.strip())
        return

    if "AIRTABLE_EXPORT_START" in text:
        # Здесь должна быть твоя функция write_to_airtable (из прошлого шага)
        pass

    resp = await ask_kimi(text)
    await update.message.reply_text(resp[:4000])

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("menu", "Главное меню"),
        BotCommand("help", "Подробное руководство"),
        BotCommand("paste", "Конвертировать в /calc")
    ]
    
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🤖 GS Assistant готов! Нажми /menu.")))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("help", show_guide))
    app.add_handler(CallbackQueryHandler(show_guide, pattern='^open_guide$'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # ... добавь остальные обработчики (PHOTO, Document) как в прошлом коде
    
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
