import os
import logging
import base64
import re
import aiohttp
import json
from io import BytesIO

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract
from pyzbar.pyzbar import decode

from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.graphics.barcode import createBarcodeDrawing

try:
    from PyPDF2 import PdfReader, PdfWriter
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

# Встроенный словарь цветов для 100% точности
COLORS_DICT = {
    "blue": {"cn": "蓝色", "en": "Blue"}, "синий": {"cn": "蓝色", "en": "Blue"}, "синяя": {"cn": "蓝色", "en": "Blue"},
    "голубой": {"cn": "浅蓝色", "en": "LightBlue"}, "голубая": {"cn": "浅蓝色", "en": "LightBlue"},
    "black": {"cn": "黑色", "en": "Black"}, "черный": {"cn": "黑色", "en": "Black"}, "чёрный": {"cn": "黑色", "en": "Black"}, "черная": {"cn": "黑色", "en": "Black"},
    "white": {"cn": "白色", "en": "White"}, "белый": {"cn": "白色", "en": "White"}, "белая": {"cn": "白色", "en": "White"},
    "red": {"cn": "红色", "en": "Red"}, "красный": {"cn": "红色", "en": "Red"}, "красная": {"cn": "红色", "en": "Red"},
    "pink": {"cn": "粉色", "en": "Pink"}, "розовый": {"cn": "粉色", "en": "Pink"}, "розовая": {"cn": "粉色", "en": "Pink"},
    "green": {"cn": "绿色", "en": "Green"}, "зеленый": {"cn": "绿色", "en": "Green"}, "зеленая": {"cn": "绿色", "en": "Green"},
    "yellow": {"cn": "黄色", "en": "Yellow"}, "желтый": {"cn": "黄色", "en": "Yellow"}, "желтая": {"cn": "黄色", "en": "Yellow"},
    "beige": {"cn": "米色", "en": "Beige"}, "бежевый": {"cn": "米色", "en": "Beige"}, "бежевая": {"cn": "米色", "en": "Beige"},
    "purple": {"cn": "紫色", "en": "Purple"}, "фиолетовый": {"cn": "紫色", "en": "Purple"}, "фиолетовая": {"cn": "紫色", "en": "Purple"},
    "grey": {"cn": "灰色", "en": "Grey"}, "gray": {"cn": "灰色", "en": "Grey"}, "серый": {"cn": "灰色", "en": "Grey"}, "серая": {"cn": "灰色", "en": "Grey"},
    "brown": {"cn": "棕色", "en": "Brown"}, "коричневый": {"cn": "棕色", "en": "Brown"}, "коричневая": {"cn": "棕色", "en": "Brown"},
    "orange": {"cn": "橙色", "en": "Orange"}, "оранжевый": {"cn": "橙色", "en": "Orange"}, "оранжевая": {"cn": "橙色", "en": "Orange"}
}

STOP_WORDS = ['none', 'null', 'нет', 'не указан', 'не указано', 'отсутствует', '无', '']

def is_valid_ean13(barcode: str) -> bool:
    if not barcode or len(barcode) != 13 or not barcode.isdigit(): return False
    digits = [int(x) for x in barcode]
    checksum = digits.pop()
    return checksum == (10 - ((sum(digits[1::2]) * 3 + sum(digits[0::2])) % 10)) % 10

def generate_vector_label_60x40(barcode_num, article):
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(60*mm, 40*mm))
    if barcode_num:
        try:
            if len(barcode_num) == 13 and barcode_num.isdigit():
                d = createBarcodeDrawing('EAN13', value=barcode_num, barWidth=0.35*mm, barHeight=18*mm)
            else:
                d = createBarcodeDrawing('Code128', value=barcode_num, barWidth=0.35*mm, barHeight=18*mm)
            d.drawOn(c, 7*mm, 15*mm)
        except Exception:
            c.setFont("Helvetica", 10)
            c.drawString(5*mm, 20*mm, f"BC: {barcode_num}")
    if article:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(7*mm, 6*mm, f"Art: {article[:40]}")
    c.showPage()
    c.save()
    buf.seek(0)
    return buf

async def ask_kimi(prompt: str, image_b64: str = None, system_msg: str = "") -> str:
    headers = {'Authorization': f'Bearer {KIMI_API_KEY}', 'Content-Type': 'application/json'}
    model = 'moonshot-v1-8k-vision-preview' if image_b64 else 'moonshot-v1-8k'
    content = [{'type': 'text', 'text': prompt}]
    if image_b64:
        content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}})
    messages = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': content}]
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.moonshot.cn/v1/chat/completions', headers=headers, json={'model': model, 'messages': messages, 'temperature': 0.0}) as resp:
            if resp.status == 200:
                res = await resp.json()
                return res['choices'][0]['message']['content']
            return "{}"

async def get_product_info(text: str, image_b64: str = None) -> dict:
    system_msg = (
        "Ты — следователь-аналитик этикеток. Внимательно сканируй текст и извлекай факты. Если явно не написано, ищи по смыслу.\n"
        "ПРАВИЛО АРТИКУЛА: Артикул ОБЯЗАТЕЛЬНО должен содержать цифры! Если после слова Артикул идут только буквы — игнорируй это. Если цифры есть — забирай строку ЦЕЛИКОМ.\n"
        "Верни ТОЛЬКО валидный JSON:\n"
        "{\n"
        '  "ru_type": "Что это за товар на русском (например: Набор аксессуаров, Расческа)",\n'
        '  "cn_type": "Перевод типа товара на китайский (например: 梳子)",\n'
        '  "en_type": "Перевод типа товара на англ без пробелов (например: Hairbrush)",\n'
        '  "color": "Цвет (ищи слова вроде blue, бежевый, черный)",\n'
        '  "size": "Размер (ищи цифры с см, ml, х, *)",\n'
        '  "article": "Артикул целиком (ТОЛЬКО ЕСЛИ ЕСТЬ ЦИФРЫ!)",\n'
        '  "material": "Материал (например: пластик, чугун)",\n'
        '  "complectation": "Комплектация (например: 1 расческа, 2 заколки)",\n'
        '  "characteristics": "Свойства (например: для всех типов волос)",\n'
        '  "date": "Дата"\n'
        "}\n"
        "Если данных нет, оставляй строку пустой: \"\""
    )
    prompt = f"Текст: {text[:800]}"
    res_text = await ask_kimi(prompt, image_b64=image_b64, system_msg=system_msg)
    try:
        clean_res = re.sub(r'```json|```', '', res_text).strip()
        info = json.loads(clean_res)
        
        t_low = text.lower()
        if not info.get("cn_type") or info.get("cn_type").lower() in STOP_WORDS:
            if any(x in t_low for x in ["расческа", "梳", "tangle", "brush"]): info["cn_type"] = "梳子"
            elif any(x in t_low for x in ["маска", "mask", "面"]): info["cn_type"] = "面膜"
        return info
    except:
        return {}

def process_product_data(info: dict, regex_article: str, barcode: str, raw_text: str):
    found_cn_color, found_en_color, found_raw_color = "", "", ""
    color_from_ai = str(info.get("color", "")).strip().lower()
    text_lower = raw_text.lower()
    
    for key, val in COLORS_DICT.items():
        if key in color_from_ai or re.search(r'\b' + key + r'\b', text_lower):
            found_cn_color = val["cn"]
            found_en_color = val["en"]
            found_raw_color = key
            break

    cn_type = re.sub(r'[а-яА-ЯёЁ\s]', '', str(info.get("cn_type", "")))
    en_type = re.sub(r'[а-яА-ЯёЁ\s]', '', str(info.get("en_type", "")))
    
    cn = (found_cn_color + cn_type) if found_cn_color else cn_type
    en = (found_en_color + en_type) if found_en_color else en_type
    size = re.sub(r'\s', '', str(info.get("size", "")))
    
    ai_art = str(info.get("article", "")).strip()
    reg_art = str(regex_article).strip()
    
    if not re.search(r'\d', ai_art): ai_art = ""
    if not re.search(r'\d', reg_art): reg_art = ""
    
    full_article = ai_art if len(ai_art) > len(reg_art) else reg_art
    
    display_color = info.get("color", "").strip() or found_raw_color
    if display_color and display_color.lower() not in STOP_WORDS and display_color.lower() not in full_article.lower():
        full_article = f"{full_article} {display_color}".strip()
        
    clean_article_for_filename = re.sub(r'[\\/*?:"<>|\s]', '', full_article)
    
    parts = []
    for p in [cn, en, size, clean_article_for_filename, barcode]:
        if p and str(p).lower() not in STOP_WORDS:
            parts.append(str(p))
            
    if not parts: parts = ["Product"]
    new_name = "_".join(parts) + ".pdf"
    
    def clean_val(k):
        val = str(info.get(k, "")).strip()
        val = val.replace('<', '&lt;').replace('>', '&gt;')
        return val if val and val.lower() not in STOP_WORDS else "➖"

    disp_color_safe = display_color.replace('<', '&lt;').replace('>', '&gt;') if display_color and display_color.lower() not in STOP_WORDS else '➖'
    
    details = (
        f"📝 <b>Детали с этикетки:</b>\n"
        f"🔸 <b>Товар:</b> {clean_val('ru_type')}\n"
        f"🔸 <b>Цвет:</b> {disp_color_safe}\n"
        f"🔸 <b>Размер:</b> {clean_val('size')}\n"
        f"🔸 <b>Материал:</b> {clean_val('material')}\n"
        f"🔸 <b>Комплект:</b> {clean_val('complectation')}\n"
        f"🔸 <b>Свойства:</b> {clean_val('characteristics')}\n"
        f"🔸 <b>Дата:</b> {clean_val('date')}"
    )
    
    return new_name, full_article, details

def find_article_regex(text: str) -> str:
    match = re.search(r'(?:Артикул|Article|Арт\.?)\s*[:\-\.]?\s*([^\n\r]+)', text, re.IGNORECASE)
    return match.group(1).strip() if match else ""

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text('⏳ Обрабатываю фото (Глубокий анализ)...')
    try:
        caption = update.message.caption or ""
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        buf = BytesIO(); await file.download_to_memory(buf)
        img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

        # ВОТ ОНИ! ВЕРНУЛ НА МЕСТО ФУНКЦИИ /1688 и /HS
        if caption.lower().strip().startswith('/1688'):
            res = await ask_kimi("Supplier Info CN/EN. Code blocks.", image_b64=img_b64, system_msg="1688 Expert.")
            await msg.delete()
            return await update.message.reply_text(res, parse_mode='Markdown')

        if caption.lower().strip().startswith('/hs'):
            res = await ask_kimi(f"Подбери коды ТН ВЭД (HS Code) для товара: {caption}", image_b64=img_b64, system_msg="Ты таможенный брокер. Выдай коды ТН ВЭД.")
            codes = re.findall(r'\b\d{4,10}\b', res)
            final_msg = f"📦 *Коды ТН ВЭД:*\n\n{res}\n\n🔍 *Проверить в базе Alta:*\n"
            for code in set(codes): final_msg += f"👉 [Код {code}](https://www.alta.ru/tnved/code/{code}/)\n"
            await msg.delete()
            return await update.message.reply_text(final_msg, parse_mode='Markdown', disable_web_page_preview=True)

        img_obj = Image.open(BytesIO(buf.getvalue()))

        barcode = ""
        codes = decode(img_obj.convert('L'))
        if codes: barcode = codes[0].data.decode('utf-8')
        
        raw_text = pytesseract.image_to_string(img_obj, lang='rus+eng+chi_sim')
        regex_art = find_article_regex(raw_text)
        
        info = await get_product_info(raw_text, image_b64=img_b64)
        new_name, final_art, details_text = process_product_data(info, regex_art, barcode, raw_text)
        
        vector_pdf = generate_vector_label_60x40(barcode, final_art)
        vector_pdf.name = new_name 
        
        barcode_status = f"{barcode} (Читается + EAN-13 верен)" if is_valid_ean13(barcode) else f"{barcode if barcode else '❌ Не найден'}"
        
        if final_art:
            wb_link_art = final_art.replace(' ', '')
            article_text = f"{final_art} 👉 <a href='https://www.wildberries.ru/catalog/{wb_link_art}/detail.aspx'>Посмотреть на WB</a>"
        else:
            article_text = "❌ Не найден (или нет цифр)"
        
        status = f"✅ <b>Штрих-код:</b> {barcode_status}\n✅ <b>Артикул:</b> {article_text}\n\n{details_text}\n\n📄 <code>{new_name}</code>"
        
        await update.message.reply_document(document=InputFile(vector_pdf, filename=new_name), caption=status, parse_mode='HTML')
        await msg.delete()
    except Exception as e:
        logger.error(f"Error in handle_photo: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document.file_name.lower().endswith('.pdf'): return
    status_msg = await update.message.reply_text("⏳ Умная сортировка PDF (Глубокий анализ)...")
    try:
        file = await context.bot.get_file(update.message.document.file_id)
        pdf_bytes = await file.download_as_bytearray()
        reader = PdfReader(BytesIO(pdf_bytes))
        
        images = convert_from_bytes(bytes(pdf_bytes), dpi=200)
        
        groups = {}
        for i, img in enumerate(images):
            codes = decode(img.convert('L'))
            bc = codes[0].data.decode('utf-8') if codes else ""
            
            if bc and bc in groups:
                groups[bc]['pages'].append(i)
                continue
            
            txt = pytesseract.image_to_string(img, lang='rus+eng+chi_sim')
            reg_art = find_article_regex(txt)
            key = bc if bc else (reg_art if reg_art else f"p{i}")
            
            if key in groups:
                groups[key]['pages'].append(i)
            else:
                img_buffer = BytesIO()
                img.save(img_buffer, format='JPEG')
                img_b64 = base64.b64encode(img_buffer.getvalue()).decode('utf-8')
                
                info = await get_product_info(txt, image_b64=img_b64)
                fname, f_art, details = process_product_data(info, reg_art, bc, txt)
                groups[key] = {'pages': [i], 'fname': fname, 'art': f_art, 'bc': bc, 'details': details}

        for k, g in groups.items():
            writer = PdfWriter()
            for p in g['pages']: writer.add_page(reader.pages[p])
            out = BytesIO(); writer.write(out); out.seek(0)
            out.name = g['fname'] 
            
            bc_stat = f"{g['bc']} (EAN-13 верен)" if is_valid_ean13(g['bc']) else f"{g['bc'] if g['bc'] else '❌'}"
            
            if g['art']:
                art_clean = g['art'].replace(' ', '')
                art_text = f"{g['art']} 👉 <a href='https://www.wildberries.ru/catalog/{art_clean}/detail.aspx'>Посмотреть на WB</a>"
            else:
                art_text = "❌"
            
            cap = f"📦 <b>Страниц:</b> {len(g['pages'])}\n✅ <b>Штрих-код:</b> {bc_stat}\n✅ <b>Артикул:</b> {art_text}\n\n{g['details']}\n\n📄 <code>{g['fname']}</code>"
            await update.message.reply_document(document=InputFile(out, filename=g['fname']), caption=cap, parse_mode='HTML')
        
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Error in handle_doc: {e}")
        await status_msg.edit_text(f"❌ Ошибка PDF: {e}")

async def handle_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.message.text.replace('/paste', '').strip()
    sys = "Ты конвертер в /calc. НЕ СЧИТАЙ. Формат: Название, Кол-во, Цена, Доставка. Курс: 58/55."
    res = await ask_kimi(f"Разбери: {data}", system_msg=sys)
    await update.message.reply_text(res)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🤖 Бот запущен (Режим Аналитика)!")))
    app.add_handler(MessageHandler(filters.Regex(r'^/paste'), handle_paste))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: u.message.reply_text("Пришли фото этикетки или PDF.")))
    app.run_polling()

if __name__ == '__main__':
    main()
