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

def clean_response(text: str) -> str:
    garbage = [
        r'^\d+\.', r'ОПРЕДЕЛИ.*?:', r'ВЫБЕРИ.*?:', r'ВЫПОЛНИ.*?:', 
        r'АНАЛИЗИРУЮ.*?:', r'РАССУЖДАЮ.*?:', r'---', r'===', 
        r'ВЫВОД:', r'РЕЗУЛЬТАТ:', r'ОТВЕТ:', r'\*\*\*', r'•',
        r'список\s*[-–]', r'Проблемы:.*', r'Размеры:.*', r'Проверка:.*',
    ]
    for pattern in garbage:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
    
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
        text = pytesseract.image_to_string(image, lang='rus+eng+chi_sim')
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🤖 Привет! Я твой ассистент по товарам.\n\n"
        "📁 **Сгенерировать имя файла:** Отправь фото или PDF с этикеткой.\n"
        "📦 **Подобрать HS code (ЕАЭС):** Отправь фото с подписью `/hs [материал/описание]`."
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def handle_hs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /hs для поиска кода ТН ВЭД (Армения/ЕАЭС)"""
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
        
        system_msg = "Ты опытный таможенный декларант ЕАЭС (Армения)."
        prompt = f"""Внимательно изучи фото товара. 
Дополнительное описание от пользователя: "{user_desc if user_desc else 'нет описания'}".

Определи, что это за товар, из какого материала он сделан, и подбери 2-3 наиболее вероятных кода ТН ВЭД ЕАЭС.

ОТВЕТ ДАЙ СТРОГО В ТАКОМ ФОРМАТЕ для каждого варианта:
КОД: [только 10 цифр кода без пробелов и точек]
ОПИСАНИЕ: [кратко, почему этот код подходит]"""

        kimi_response = await ask_kimi(prompt, image_b64=image_b64, system_msg=system_msg)
        
        if kimi_response.startswith("Error"):
            await msg.edit_text(f'❌ Ошибка при анализе фото: {kimi_response}')
            return

        # Парсим ответ
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
    """Обработка фото для переименования"""
    try:
        msg = await update.message.reply_text('⏳ Обработка фото...')
        
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        buf = BytesIO()
        await file.download_to_memory(buf)
        image = Image.open(buf)
        
        barcode_num, text, article = await extract_image_data(image)
        
        system_msg = '''Ты создаёшь имена файлов для товаров. 
СТРУКТУРА: 中文_English_Размер_Артикул_Штрихкод.pdf
1. ТОЛЬКО имя файла
2. ВСЕГДА переводи на китайский (简体中文)'''

        prompt = f"""Создай имя файла для товара на фото.
Распознанный текст: {text[:1500]}
Штрих-код: {barcode_num}
Артикул: {article}
Только имя файла:"""

        image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        new_name = await ask_kimi(prompt, image_b64=image_b64, system_msg=system_msg)
        
        new_name = new_name.strip()
        if not new_name.endswith('.pdf'): new_name += '.pdf'
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
        new_name = re.sub(r'_{2,}', '_', new_name)
        
        if len(new_name) < 10:
            new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"
        
        response_lines = [f"📄 `{new_name}`"]
        if barcode_num: response_lines.insert(0, f"✅ Штрих-код: {barcode_num}")
        if article: response_lines.insert(1, f"✅ Артикул: {article}")
        
        await msg.edit_text('\n'.join(response_lines), parse_mode='Markdown')
        
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
        
        # Если это картинка документом
        if not original_name.lower().endswith('.pdf'):
            try:
                image = Image.open(buf)
                await msg.edit_text('⏳ Обработка изображения...')
                barcode_num, text, article = await extract_image_data(image)
                # Дальнейшая логика для картинки документом аналогична handle_photo
                system_msg = 'Ты создаёшь имена файлов для товаров. СТРУКТУРА: 中文_English_Размер_Артикул_Штрихкод.pdf'
                prompt = f"Текст: {text[:1000]}\nШтрих-код: {barcode_num}\nАртикул: {article}\nТолько имя:"
                image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                new_name = await ask_kimi(prompt, image_b64=image_b64, system_msg=system_msg)
                
                new_name = new_name.strip()
                if not new_name.endswith('.pdf'): new_name += '.pdf'
                new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
                await msg.edit_text(f"✅ Штрих-код: {barcode_num}\n📄 `{new_name}`", parse_mode='Markdown')
                return
            except Exception:
                await msg.edit_text('❌ Поддерживаются только .pdf или изображения')
                return

        # Обработка PDF
        await msg.edit_text('⏳ Обработка PDF (распознавание страниц)...')
        barcode_num, text, article = "", "", ""
        
        buf.seek(0)
        # Извлекаем первую страницу для анализа
        images = convert_from_bytes(buf.read(), dpi=200, first_page=1, last_page=1)
        if images:
            img = images[0]
            barcode_num, text, article = await extract_image_data(img)

        system_msg = 'Ты создаёшь имена файлов для товаров. СТРУКТУРА: 中文_English_Размер_Артикул_Штрихкод.pdf'
        prompt = f"Создай имя файла.\nТекст: {text[:1500]}\nШтрих-код: {barcode_num}\nАртикул: {article}\nТолько имя файла:"
        
        new_name = await ask_kimi(prompt, system_msg=system_msg)
        
        new_name = new_name.strip()
        if not new_name.endswith('.pdf'): new_name += '.pdf'
        new_name = re.sub(r'[\\/*?:"\u003c\u003e|]', '', new_name)
        new_name = re.sub(r'_{2,}', '_', new_name)
        
        if len(new_name) < 10:
            new_name = f"Товар_Unknown_{barcode_num if barcode_num else '000'}.pdf"

        await msg.delete() # Удаляем статусное сообщение
        
        response_lines = [f"📄 `{new_name}`"]
        if barcode_num: response_lines.insert(0, f"✅ Штрих-код: {barcode_num}")
        if article: response_lines.insert(1, f"✅ Артикул: {article}")
        
        await update.message.reply_text('\n'.join(response_lines), parse_mode='Markdown')
        
        buf.seek(0)
        await update.message.reply_document(
            document=InputFile(buf, filename=new_name),
            caption=new_name
        )
        
    except Exception as e:
        logger.error(f"Doc error: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:200]}')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text('⏳ Думаю...')
    resp = await ask_kimi(update.message.text)
    await msg.edit_text(resp[:4000])

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('hs', handle_hs))
    
    # Фильтр: обрабатываем фото только если это НЕ команда (чтобы не конфликтовать с /hs)
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logger.info("Бот запущен и готов к работе!")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
