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
        "Ты — строгий парсер текста с этикеток.\n"
        "ГЛАВНОЕ ПРАВИЛО: Ищи цвет ТОЛЬКО в тексте! НЕ угадывай по картинке.\n"
        "ПЕРЕВОДИ СТРОГО ПО ОТДЕЛЬНЫМ ЯЧЕЙКАМ. Верни ТОЛЬКО чистый JSON:\n"
        "{\n"
        '  "cn_type": "Китайский перевод типа товара (например: 梳子)",\n'
        '  "cn_color": "Китайский перевод ЦВЕТА (например: 蓝色). Если цвета нет - пустота",\n'
        '  "en_type": "Английский перевод типа товара (например: Hairbrush)",\n'
        '  "en_color": "Английский перевод ЦВЕТА (например: Blue). Если цвета нет - пустота",\n'
        '  "size": "Размер (если есть)",\n'
        '  "article": "Основной артикул",\n'
        '  "raw_color": "Цвет товара СТРОГО ИЗ ТЕКСТА на оригинальном языке (например: blue, розовый)"\n'
        "}\n"
    )
    prompt = f"Текст: {text[:800]}"
    res_text = await ask_kimi(prompt, image_b64=image_b64, system_msg=system_msg)
    try:
        clean_res = re.sub(r'```json|```', '', res_text).strip()
        info = json.loads(clean_res)
        
        t_low = text.lower()
        if not info.get("cn_type") or info.get("cn_type").lower() in ['none', 'null', '']:
            if any(x in t_low for x in ["расческа", "梳", "tangle", "brush"]): info["cn_type"] = "梳子"
            elif any(x in t_low for x in ["маска", "mask", "面"]): info["cn_type"] = "面膜"
        return info
    except:
        return {"cn_type": "商品", "cn_color": "", "en_type": "Product", "en_color": "", "size": "", "article": "", "raw_color": ""}

def build_filename(info: dict, regex_article: str, barcode: str) -> tuple:
    # Изолируем перевод цвета и типа, жестко вычищаем русский
    cn_color = re.sub(r'[а-яА-ЯёЁ\s]', '', str(info.get("cn_color", "")).replace('None', '').replace('null', ''))
    cn_type = re.sub(r'[а-яА-ЯёЁ\s]', '', str(info.get("cn_type", "")).replace('None', '').replace('null', ''))
    # Python сам склеивает китайское: Цвет + Товар
    cn = cn_color + cn_type
    
    en_color = re.sub(r'[а-яА-ЯёЁ\s]', '', str(info.get("en_color", "")).replace('None', '').replace('null', ''))
    en_type = re.sub(r'[а-яА-ЯёЁ\s]', '', str(info.get("en_type", "")).replace('None', '').replace('null', ''))
    # Python сам склеивает английское: Цвет + Товар
    en = en_color + en_type
    
    size = re.sub(r'\s', '', str(info.get("size", "")).replace('None', '').replace('null', ''))
    
    ai_art = str(info.get("article", "")).strip()
    raw_color = str(info.get("raw_color", "")).strip()
    
    # Добавляем оригинальный цвет в артикул
    if raw_color and raw_color.lower() not in ['none', 'null', '无', 'нет', '']:
        if raw_color.lower() not in ai_art.lower():
            ai_art = f"{ai_art} {raw_color}"
            
    reg_art = str(regex_article).strip()
    full_article = ai_art if len(ai_art) > len(reg_art) else reg_art
    
    clean_article_for_filename = re.sub(r'[\\/*?:"<>|\s]', '', full_article)
    
    parts = []
    for p in [cn, en, size, clean_article_for_filename, barcode]:
        if p and str(p).lower() not in ['none', 'null', 'безразмера', '无', 'нет', '']:
            parts.append(str(p))
            
    if not parts: parts = ["Product"]
    new_name = "_".join(parts) + ".pdf"
    
    return new_name, full_article

def find_article_regex(text: str) -> str:
    match = re.search(r'(?:Артикул|Article|Арт\.?)\s*[:\-\.]?\s*([^\n\r]+)', text, re.IGNORECASE)
    return match.group(1).strip() if match else ""

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text('⏳ Обрабатываю фото...')
    try:
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        buf = BytesIO(); await file.download_to_memory(buf)
        img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        img_obj = Image.open(BytesIO(buf.getvalue()))

        barcode = ""
        codes = decode(img_obj.convert('L'))
        if codes: barcode = codes[0].data.decode('utf-8')
        
        raw_text = pytesseract.image_to_string(img_obj, lang='rus+eng+chi_sim')
        regex_art = find_article_regex(raw_text)
        
        info = await get_product_info(raw_text, image_b64=img_b64)
        new_name, final_art = build_filename(info, regex_art, barcode)
        
        vector_pdf = generate_vector_label_60x40(barcode, final_art)
        vector_pdf.name = new_name 
        
        barcode_status = f"{barcode} (Читается + формат EAN-13 верен)" if is_valid_ean13(barcode) else f"{barcode if barcode else '❌ Не найден'}"
        wb_link_art = final_art.replace(' ', '')
        article_text = f"{final_art} 👉 [Посмотреть на WB](https://www.wildberries.ru/catalog/{wb_link_art}/detail.aspx)" if final_art else "❌ Не найден"
        
        status = f"✅ Штрих-код: {barcode_status}\n✅ Артикул: {article_text}\n📄 `{new_name}`"
        await update.message.reply_document(document=InputFile(vector_pdf, filename=new_name), caption=status, parse_mode='Markdown')
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document.file_name.lower().endswith('.pdf'): return
    status_msg = await update.message.reply_text("⏳ Умная сортировка PDF...")
    try:
        file = await context.bot.get_file(update.message.document.file_id)
        pdf_bytes = await file.download_as_bytearray()
        reader = PdfReader(BytesIO(pdf_bytes))
        images = convert_from_bytes(bytes(pdf_bytes), dpi=150)
        
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
                info = await get_product_info(txt)
                fname, f_art = build_filename(info, reg_art, bc)
                groups[key] = {'pages': [i], 'fname': fname, 'art': f_art, 'bc': bc}

        for k, g in groups.items():
            writer = PdfWriter()
            for p in g['pages']: writer.add_page(reader.pages[p])
            out = BytesIO(); writer.write(out); out.seek(0)
            out.name = g['fname'] 
            
            bc_stat = f"{g['bc']} (Читается + формат EAN-13 верен)" if is_valid_ean13(g['bc']) else f"{g['bc'] if g['bc'] else '❌'}"
            art_clean = g['art'].replace(' ', '')
            art_text = f"{g['art']} 👉 [Посмотреть на WB](https://www.wildberries.ru/catalog/{art_clean}/detail.aspx)" if g['art'] else "❌"
            
            cap = f"📦 Страниц: {len(g['pages'])}\n✅ Штрих-код: {bc_stat}\n✅ Артикул: {art_text}\n📄 `{g['fname']}`"
            await update.message.reply_document(document=InputFile(out, filename=g['fname']), caption=cap, parse_mode='Markdown')
        
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка PDF: {e}")

async def handle_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.message.text.replace('/paste', '').strip()
    sys = "Ты конвертер в /calc. НЕ СЧИТАЙ. Формат: Название, Кол-во, Цена, Доставка. Курс: 58/55."
    res = await ask_kimi(f"Разбери: {data}", system_msg=sys)
    await update.message.reply_text(res)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🤖 Бот запущен!")))
    app.add_handler(MessageHandler(filters.Regex(r'^/paste'), handle_paste))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: u.message.reply_text("Пришли фото этикетки или PDF.")))
    app.run_polling()

if __name__ == '__main__':
    main()
