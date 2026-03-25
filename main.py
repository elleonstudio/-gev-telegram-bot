import os
import logging
import base64
import re
import aiohttp
from io import BytesIO

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

# СТРОГИЙ ПРОМПТ ДЛЯ ГЕНЕРАЦИИ ИМЕН ФАЙЛОВ
SYSTEM_MSG_NAMING = (
    "Ты полезный ИИ-ассистент, специализирующийся на создании стандартизированных имен файлов для товаров.\n\n"
    "СТРОГИЕ ПРАВИЛА И СТРУКТУРА ИМЕНИ ФАЙЛА:\n"
    "1. Твой ответ должен быть СТРОГО одним именем файла в формате: 中文_English_Размер_Артикул_Штрихкод.pdf\n"
    "2. Если ты не можешь определить какую-то часть (например, размер), пропусти её и соответствующий разделитель '_' (например: 中文_English_Артикул_Штрихкод.pdf). Не используй заглушки вроде 'None' или 'Unknown'.\n"
    "3. 中文 (Китайский): Ты ОБЯЗАН перевести описание товара на китайский язык (简体中文).\n"
    "4. English (Английский): Ты ОБЯЗАН перевести описание товара на английский язык.\n"
    "5. Не добавляй никаких других слов, тегов, объяснений или знаков препинания до или после имени файла.\n"
    "6. Не изменяй артикул и штрихкод, если они предоставлены."
)

def is_valid_ean13(barcode: str) -> bool:
    """Математическая проверка контрольной суммы штрих-кода EAN-13"""
    if not barcode or len(barcode) != 13 or not barcode.isdigit():
        return False
    
    digits = [int(x) for x in barcode]
    checksum = digits.pop()
    
    sum_even = sum(digits[1::2]) * 3
    sum_odd = sum(digits[0::2])
    total = sum_even + sum_odd
    
    expected_checksum = (10 - (total % 10)) % 10
    return checksum == expected_checksum

def clean_response(text: str) -> str:
    """Удаляет лишний мусор из ответа"""
    garbage = [
        r'^\d+\.', r'ОПРЕДЕЛИ.*?:', r'ВЫБЕРИ.*?:', r'ВЫПОЛНИ.*?:', 
        r'АНАЛИЗИРУЮ.*?:', r'РАССУЖДАЮ.*?:', r'---', r'===', 
        r'ВЫВОД:', r'РЕЗУЛЬТАТ:', r'ОТВЕТ:', r'\*\*\*', r'•',
        r'список\s*[-–]', r'Проблемы:.*', r'Размеры:.*', r'Проверка:.*',
    ]
    for pattern in garbage:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
    
    text = text.replace('`', '').replace('***', '').replace('**', '').replace('*', '')
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return ' '.join(lines)

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = None) -> str:
    """Асинхронный запрос к API Moonshot"""
    if not system_msg:
        system_msg = 'Ты полезный ИИ-ассистент.'

    try:
        headers = {
            'Authorization': f'Bearer {KIMI_API_KEY}', 
            'Content-Type': 'application/json'
        }
        
        if image_b64:
            messages = [
                {'role': 'system', 'content': system_msg},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': prompt}, 
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}}
                ]}
            ]
            model = 'moonshot-v1-8k-vision-preview'
        else:
            messages = [
                {'role': 'system', 'content': system_msg}, 
                {'role': 'user', 'content': prompt}
            ]
            model = 'moonshot-v1-8k'
        
        data = {
            'model': model, 
            'messages': messages, 
            'temperature': 0.05,
            'max_tokens': 300
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json=data, timeout=60) as response:
                if response.status == 200:
                    resp_json = await response.json()
                    return clean_response(resp_json['choices'][0]['message']['content'])
                else:
                    error_text = await response.text()
                    logger.error(f"API Error {response.status}: {error_text}")
                    return f"Error_{response.status}"
    except Exception as e:
        logger.error(f"Kimi Request failed: {e}")
        return f"Error_API_Request"

async def extract_image_data(image: Image.Image):
    """Единая функция извлечения данных из картинки"""
    barcode_num = ""
    text = ""
    article = ""
    
    try:
        codes = decode(image.convert('L'))
        if codes:
            barcode_num = codes[0].data.decode('utf-8')
    except Exception as e:
        logger.warning(f"Barcode extraction warning: {e}")

    try:
        custom_config = r'--oem 3 --psm 6'
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=custom_config)
    except Exception as e:
        logger.warning(f"OCR warning: {e}")

    patterns = [
        r'Артикул[:\s]+(\d+)',
        r'Артикул[:\s]*(\d+)',
        r'Article[:\s]+(\d+)',
        r'арт\.?[:\s]*(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            article = match.group(1)
            break

    return barcode_num, text, article

async def get_naming_prompt(text: str, barcode_num: str, article: str) -> str:
    """Генерирует структурированный пользовательский промпт для переименования"""
    return (
        f"Создай строго структурированное имя файла для товара на основе предоставленных данных.\n\n"
        f"ВХОДНЫЕ ДАННЫЕ:\n"
        f"- Распознанный текст этикетки:\n\"{text[:2000]}\"\n"
        f"- Штрих-код: {barcode_num if barcode_num else 'не найден'}\n"
        f"- Артикул: {article if article else 'не найден'}\n\n"
        f"ПОРЯДОК ДЕЙСТВИЙ ДЛЯ ТЕБЯ:\n"
        f"1. Пойми, что это за товар.\n"
        f"2. ПЕРЕВЕДИ на китайский (简体中文).\n"
        f"3. ПЕРЕВЕДИ на английский.\n"
        f"4. Найди размер из текста (например, 200x100).\n"
        f"5. Сформируй итоговое имя файла: 中文_English_Размер_Артикул_Штрихкод.pdf.\n"
        f"ОТВЕТЬ ТОЛЬКО ИМЕНЕМ ФАЙЛА:"
    )

def build_response_lines(new_name, barcode_num, article):
    """Формирует текстовый ответ с проверками штрих-кода и ссылками WB"""
    response_lines = [f"📄 `{new_name}`"]
    
    if barcode_num:
        if is_valid_ean13(barcode_num):
            response_lines.insert(0, f"✅ Штрих-код: {barcode_num} (Читается + формат EAN-13 верен)")
        else:
            response_lines.insert(0, f"⚠️ Штрих-код: {barcode_num} (Читается, НО ОШИБКА ФОРМАТА! Возможно, сгенерирован неверно)")
    else:
        response_lines.insert(0, "❌ Штрих-код: НЕ НАЙДЕН НА ИЗОБРАЖЕНИИ")

    if article:
        wb_link = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
        response_lines.insert(1, f"✅ Артикул: {article} 👉 [Посмотреть на WB]({wb_link})")
        
    return response_lines

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🤖 Привет! Я твой ассистент по товарам.\n\n"
        "📁 **Сгенерировать имя файла:** Отправь фото или PDF с этикеткой.\n"
        "📦 **Подобрать HS code (ЕАЭС):** Отправь фото с подписью `/hs [описание]`."
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def handle_hs_text_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Если пользователь отправил команду /hs без фото"""
    await update.message.reply_text('❌ Отправь команду /hs вместе с фотографией товара (прикрепи фото и напиши /hs в описании).')

async def process_hs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Основная логика для обработки HS кодов"""
    try:
        msg = await update.message.reply_text('⏳ Изучаю товар и подбираю коды ТН ВЭД (ЕАЭС)...')
        
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        buf = BytesIO()
        await file.download_to_memory(buf)
        image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        
        caption = update.message.caption if update.message.caption else ""
        # Убираем команду из описания, чтобы передать нейросети чистый текст
        user_desc = re.sub(r'(?i)/hs', '', caption).strip()
        
        system_msg_hs = "Ты опытный таможенный декларант ЕАЭС (Армения)."
        prompt_hs = f"""Внимательно изучи фото товара. 
Дополнительное описание: "{user_desc if user_desc else 'нет описания'}".

Определи, что это за товар, из какого материала он сделан, и подбери 2-3 наиболее вероятных кода ТН ВЭД ЕАЭС.

ОТВЕТ ДАЙ СТРОГО В ТАКОМ ФОРМАТЕ для каждого варианта:
КОД: [только 10 цифр кода без пробелов и точек]
ОПИСАНИЕ: [кратко, почему этот код подходит]"""

        kimi_response = await ask_kimi(prompt_hs, image_b64=image_b64, system_msg=system_msg_hs)
        
        # Перехват ошибок API (включая лимит 429)
        if kimi_response.startswith("Error"):
            if "429" in kimi_response:
                await msg.edit_text("❌ Ошибка 429: Лимит запросов к нейросети Kimi. Подожди пару минут и попробуй снова.")
            else:
                await msg.edit_text(f"❌ Ошибка API при анализе фото: {kimi_response.replace('Error_', '')}")
            return

        codes = set(re.findall(r'(?i)КОД:\s*(\d{4,10})', kimi_response))
        
        final_message = "📦 **Предполагаемые коды ТН ВЭД (ЕАЭС/Армения):**\n\n"
        final_message += kimi_response + "\n\n"
        
        if codes:
            final_message += "🔍 **Проверить коды в базе (Alta.ru):**\n"
            for code in codes:
                final_message += f"👉 [Проверить код {code}](https://www.alta.ru/tnved/code/{code}/)\n"
        else:
            final_message += "⚠️ *Не удалось извлечь точные коды для создания ссылок. Проверь базу вручную.*"

        await msg.edit_text(final_message, parse_mode='Markdown', disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"HS error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик всех фото. Распределяет логику."""
    try:
        # 1. ПРОВЕРКА МАРШРУТИЗАЦИИ: если есть /hs в подписи - уходим в таможню
        caption = update.message.caption or ""
        if caption.lower().strip().startswith('/hs'):
            return await process_hs(update, context)

        # 2. Иначе - стандартная обработка имени файла
        msg = await update.message.reply_text('⏳ Обработка фото для создания имени файла...')
        
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        buf = BytesIO()
        await file.download_to_memory(buf)
        image = Image.open(buf)
        
        barcode_num, text, article = await extract_image_data(image)
        prompt = await get_naming_prompt(text, barcode_num, article)

        image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        new_name = await ask_kimi(prompt, image_b64=image_b64, system_msg=SYSTEM_MSG_NAMING)
        
        # Перехват ошибок API (чтобы не создавать Error_429.pdf)
        if new_name.startswith("Error"):
            if "429" in new_name:
                await msg.edit_text("❌ Ошибка 429: Лимит запросов к нейросети. Подожди пару минут.")
            else:
                await msg.edit_text(f"❌ Ошибка генерации имени: {new_name.replace('Error_', '')}")
            return
        
        new_name = new_name.strip()
        if not new_name.endswith('.pdf'): new_name += '.pdf'
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
        new_name = re.sub(r'_{2,}', '_', new_name)
        
        if len(new_name) < 10:
            new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"
        
        response_lines = build_response_lines(new_name, barcode_num, article)
        await msg.edit_text('\n'.join(response_lines), parse_mode='Markdown', disable_web_page_preview=True)
        
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка документов (PDF и фото документами)"""
    try:
        doc = update.message.document
        original_name = doc.file_name
        
        if doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text('❌ Файл слишком большой (>20MB)')
            return
            
        msg = await update.message.reply_text('⏳ Загрузка файла...')
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        
        if not original_name.lower().endswith('.pdf'):
            try:
                image = Image.open(buf)
                await msg.edit_text('⏳ Обработка изображения из документа...')
                barcode_num, text, article = await extract_image_data(image)
                
                prompt = await get_naming_prompt(text, barcode_num, article)
                image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                new_name = await ask_kimi(prompt, image_b64=image_b64, system_msg=SYSTEM_MSG_NAMING)
                
                # Перехват ошибок
                if new_name.startswith("Error"):
                    if "429" in new_name:
                        await msg.edit_text("❌ Ошибка 429: Лимит запросов к нейросети.")
                    else:
                        await msg.edit_text(f"❌ Ошибка API: {new_name.replace('Error_', '')}")
                    return

                new_name = new_name.strip()
                if not new_name.endswith('.pdf'): new_name += '.pdf'
                new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
                new_name = re.sub(r'_{2,}', '_', new_name)
                
                response_lines = build_response_lines(new_name, barcode_num, article)
                await msg.edit_text('\n'.join(response_lines), parse_mode='Markdown', disable_web_page_preview=True)
                return
            except Exception:
                await msg.edit_text('❌ Поддерживаются только .pdf или изображения')
                return

        await msg.edit_text('⏳ Обработка PDF (распознавание первой страницы)...')
        
        buf.seek(0)
        images = convert_from_bytes(buf.read(), dpi=250, first_page=1, last_page=1)
        if images:
            img = images[0]
            barcode_num, text, article = await extract_image_data(img)
        else:
            await msg.edit_text('❌ Не удалось открыть PDF')
            return

        prompt = await get_naming_prompt(text, barcode_num, article)
        
        new_name = await ask_kimi(prompt, system_msg=SYSTEM_MSG_NAMING)
        
        # Перехват ошибок
        if new_name.startswith("Error"):
            if "429" in new_name:
                await msg.edit_text("❌ Ошибка 429: Лимит запросов к нейросети. Подожди немного.")
            else:
                await msg.edit_text(f"❌ Ошибка API: {new_name.replace('Error_', '')}")
            return

        new_name = new_name.strip()
        if not new_name.endswith('.pdf'): new_name += '.pdf'
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
        new_name = re.sub(r'_{2,}', '_', new_name)
        
        if len(new_name) < 10:
            new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"

        await msg.delete()
        
        response_lines = build_response_lines(new_name, barcode_num, article)
        await update.message.reply_text('\n'.join(response_lines), parse_mode='Markdown', disable_web_page_preview=True)
        
        buf.seek(0)
        await update.message.reply_document(
            document=InputFile(buf, filename=new_name),
            caption=new_name
        )
        
    except Exception as e:
        logger.error(f"Doc error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка простого текста"""
    msg = await update.message.reply_text('⏳ Думаю...')
    resp = await ask_kimi(update.message.text)
    await msg.edit_text(resp[:4000])

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler('start', start))
    
    # Перехватчик: если отправили /hs просто текстом, без картинки
    app.add_handler(CommandHandler('hs', handle_hs_text_error)) 
    
    # Обработчик фото теперь ловит ВСЕ картинки и сам решает, куда их направить
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logger.info("Бот запущен и готов к работе!")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
