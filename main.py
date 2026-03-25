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

# ЕДИНЫЙ, СТРОГИЙ СИСТЕМНЫЙ ПРОМПТ ДЛЯ ГЕНЕРАЦИИ ИМЕН ФАЙЛОВ
SYSTEM_MSG_NAMING = (
    "Ты полезный ИИ-ассистент, специализирующийся на создании стандартизированных имен файлов для товаров.\n\n"
    "СТРОГИЕ ПРАВИЛА И СТРУКТУРА ИМЕНИ ФАЙЛА:\n"
    "1. Твой ответ должен быть СТРОГО одним именем файла в формате: 中文_English_Размер_Артикул_Штрихкод.pdf\n"
    "2. Если ты не можешь определить какую-то часть (например, размер), пропусти её и соответствующий разделитель '_' (например, 中文_English_Артикул_Штрихкод.pdf). Не используй заглушки вроде 'None' или 'Unknown'.\n"
    "3. 中文 (Китайский): Ты ОБЯЗАН перевести описание товара на китайский язык (简体中文).\n"
    "4. English (Английский): Ты ОБЯЗАН перевести описание товара на английский язык.\n"
    "5. Не добавляй никаких других слов, тегов, объяснений или знаков препинания до или после имени файла.\n"
    "6. Не изменяй артикул и штрихкод, если они предоставлены."
)

def clean_response(text: str) -> str:
    """Удаляет лишний мусор и Markdown из ответа"""
    garbage = [
        r'^\d+\.', r'ОПРЕДЕЛИ.*?:', r'ВЫБЕРИ.*?:', r'ВЫПОЛНИ.*?:', 
        r'АНАЛИЗИРУЮ.*?:', r'РАССУЖДАЮ.*?:', r'---', r'===', 
        r'ВЫВОД:', r'РЕЗУЛЬТАТ:', r'ОТВЕТ:', r'\*\*\*', r'•',
        r'список\s*[-–]', r'Проблемы:.*', r'Размеры:.*', r'Проверка:.*',
    ]
    for pattern in garbage:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
    
    # Удаляем Markdown
    text = text.replace('`', '').replace('***', '').replace('**', '').replace('*', '')
    
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return ' '.join(lines)

def is_valid_ean13(barcode: str) -> bool:
    """Математическая проверка контрольной суммы штрих-кода EAN-13"""
    if not barcode or len(barcode) != 13 or not barcode.isdigit():
        return False
    
    digits = [int(x) for x in barcode]
    checksum = digits.pop()
    
    # Считаем сумму по алгоритму EAN-13
    sum_even_idx = sum(digits[0::2]) # Индексы 0, 2, 4... (нечетные позиции)
    sum_odd_idx = sum(digits[1::2]) * 3 # Индексы 1, 3, 5... (четные позиции) умножаем на 3
    total = sum_odd_idx + sum_even_idx
    
    expected_checksum = (10 - (total % 10)) % 10
    return checksum == expected_checksum

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
    """Единая функция извлечения данных из картинки: штрихкод, текст, артикул"""
    barcode_num = ""
    text = ""
    article = ""
    
    # 1. Штрихкод
    try:
        codes = decode(image.convert('L'))
        if codes:
            barcode_num = codes[0].data.decode('utf-8')
    except Exception as e:
        logger.warning(f"Barcode extraction warning: {e}")

    # 2. Текст (OCR)
    try:
        custom_config = r'--oem 3 --psm 6'
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim', config=custom_config)
    except Exception as e:
        logger.warning(f"OCR warning: {e}")

    # 3. Артикул из текста
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
        f"- Штрих-код (если найден): {barcode_num if barcode_num else 'не найден'}\n"
        f"- Артикул (если найден): {article if article else 'не найден'}\n"
        f"- Если предоставлено фото, проанализируй и его.\n\n"
        f"ПОРЯДОК ДЕЙСТВИЙ ДЛЯ ТЕБЯ:\n"
        f"1. Пойми, что это за товар.\n"
        f"2. ПЕРЕВЕДИ на китайский (简体中文).\n"
        f"3. ПЕРЕВЕДИ на английский.\n"
        f"4. Найди размер из текста (например, 200x100).\n"
        f"5. Сформируй итоговое имя файла: 中文_English_Размер_Артикул_Штрихкод.pdf.\n"
        f"ОТВЕТЬ ТОЛЬКО ИМЕНЕМ ФАЙЛА:"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🤖 Привет! Я твой ассистент по товарам.\n\n"
        "📁 **Сгенерировать имя файла:** Отправь фото или PDF с этикеткой.\n"
        "📦 **Подобрать HS code (ЕАЭС):** Отправь фото с подписью `/hs [описание]`."
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def handle_hs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /hs для поиска кода ТН ВЭД"""
    try:
        if not update.message.photo:
            await update.message.reply_text('❌ Отправь команду /hs вместе с фотографией товара (прикрепи фото и напиши /hs в описании).')
            return

        msg = await update.message.reply_text('⏳ Изучаю товар и подбираю коды ТН ВЭД для Армении (ЕАЭС)...')
        
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        buf = BytesIO()
        await file.download_to_memory(buf)
        image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        
        caption = update.message.caption if update.message.caption else ""
        user_desc = caption.replace('/hs', '').strip()
        
        system_msg_hs = "Ты опытный таможенный декларант ЕАЭС (Армения)."
        prompt_hs = f"""Внимательно изучи фото товара. 
Дополнительное описание от пользователя: "{user_desc if user_desc else 'нет описания'}".

Определи, что это за товар, из какого материала он сделан, и подбери 2-3 наиболее вероятных кода ТН ВЭД ЕАЭС.

ОТВЕТ ДАЙ СТРОГО В ТАКОМ ФОРМАТЕ для каждого варианта:
КОД: [только 10 цифр кода без пробелов и точек]
ОПИСАНИЕ: [кратко, почему этот код подходит]"""

        kimi_response = await ask_kimi(prompt_hs, image_b64=image_b64, system_msg=system_msg_hs)
        
        if kimi_response.startswith("Error"):
            await msg.edit_text(f'❌ Ошибка при анализе фото: {kimi_response}')
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

async def build_response_message(new_name: str, barcode_num: str, article: str) -> str:
    """Формирует итоговое текстовое сообщение со ссылками и проверками"""
    response_lines = [f"📄 `{new_name}`"]
    
    if barcode_num:
        if is_valid_ean13(barcode_num):
            response_lines.insert(0, f"✅ Штрих-код: `{barcode_num}` (Формат EAN-13 верен)")
        else:
            response_lines.insert(0, f"⚠️ Штрих-код: `{barcode_num}` (ОШИБКА ФОРМАТА! Возможно, не прочитается на складе)")
    else:
        response_lines.insert(0, "❌ Штрих-код: НЕ НАЙДЕН НА ИЗОБРАЖЕНИИ")

    if article:
        wb_link = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
        response_lines.insert(1, f"✅ Артикул: `{article}` 👉 [Открыть карточку WB]({wb_link})")
        
    return '\n'.join(response_lines)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото для переименования"""
    try:
        msg = await update.message.reply_text('⏳ Обработка фото...')
        
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        buf = BytesIO()
        await file.download_to_memory(buf)
        image = Image.open(buf)
        
        barcode_num, text, article = await extract_image_data(image)
        prompt = await get_naming_prompt(text, barcode_num, article)
        
        image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        new_name = await ask_kimi(prompt, image_b64=image_b64, system_msg=SYSTEM_MSG_NAMING)
        
        new_name = new_name.strip()
        if not new_name.endswith('.pdf'): new_name += '.pdf'
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
        new_name = re.sub(r'_{2,}', '_', new_name)
        
        if len(new_name) < 10:
            new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"
        
        final_msg = await build_response_message(new_name, barcode_num, article)
        await msg.edit_text(final_msg, parse_mode='Markdown', disable_web_page_preview=True)
        
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка документов (PDF)"""
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
                await msg.edit_text('⏳ Обработка изображения...')
                barcode_num, text, article = await extract_image_data(image)
                
                prompt = await get_naming_prompt(text, barcode_num, article)
                image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                new_name = await ask_kimi(prompt, image_b64=image_b64, system_msg=SYSTEM_MSG_NAMING)
                
                new_name = new_name.strip()
                if not new_name.endswith('.pdf'): new_name += '.pdf'
                new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
                new_name = re.sub(r'_{2,}', '_', new_name)
                
                final_msg = await build_response_message(new_name, barcode_num, article)
                await msg.edit_text(final_msg, parse_mode='Markdown', disable_web_page_preview=True)
                return
            except Exception:
                await msg.edit_text('❌ Поддерживаются только .pdf или изображения')
