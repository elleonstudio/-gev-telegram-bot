import os
import re
import logging
from datetime import datetime
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- НАСТРОЙКИ ---
logging.basicConfig(level=logging.INFO)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
# Airtable данные остаются прежними

# --- МОТОР МАТЕМАТИКИ (ЧИСТЫЙ PYTHON) ---

def fast_audit(text):
    lines = text.replace('/audit_gs', '').strip().split('\n')
    audit_results = []
    total_cny = 0.0
    
    # Регулярка для поиска курса и комиссии
    found_rate = re.search(r'(?:курс|rate|1¥-)\s*(\d+[\.,]?\d*)', text.lower())
    rate = float(found_rate.group(1).replace(',', '.')) if found_rate else 58.0
    
    found_comm = re.search(r'\+(\d+)\s*(?:֏|драм|amd)', text.lower())
    commission = float(found_comm.group(1)) if found_comm else 10000.0

    errors = []
    processed_lines = []

    for line in lines:
        if not line.strip() or '֏' in line or '×' not in line:
            processed_lines.append(line)
            continue
            
        try:
            # Парсим строку: 6.99x125+35=909
            parts = re.split(r'=', line)
            if len(parts) < 2: 
                processed_lines.append(line)
                continue
                
            expr = parts[0].replace('×', '*').replace('x', '*').strip()
            claimed = float(re.sub(r'[^\d\.]', '', parts[1].replace(',', '.')).strip())
            
            # Считаем на Python
            actual = round(eval(re.sub(r'[^\d\.\*\+\-\/]', '', expr)), 2)
            
            if abs(actual - claimed) > 0.001:
                errors.append(f"Строка:\nБыло: {line.strip()}\nПравильно: {expr.replace('*', '×')} = {actual}")
                total_cny += actual
            else:
                total_cny += actual
            processed_lines.append(line)
        except:
            processed_lines.append(line)

    # Финальный расчет
    final_amd_actual = round((total_cny * rate) + commission, 2)
    
    # Проверка финальной суммы в тексте
    claimed_final_match = re.search(r'=(\d+)\s*֏', text)
    claimed_final = float(claimed_final_match.group(1)) if claimed_final_match else 0
    
    header = "/audit_gs\n\n" + "\n".join(processed_lines) + "\n\n"
    
    if not errors and abs(final_amd_actual - claimed_final) < 1:
        return header + f"✅ Ошибок нет, финальная сумма {int(final_amd_actual)}֏ верна."
    else:
        res = header + "❌ Найдены ошибки в расчетах!\n\n"
        if errors:
            res += "\n\n".join(errors) + "\n\n"
        
        res += f"Сумма:\nБыло: {int(claimed_final)}֏\nПравильно: {final_amd_actual}֏\n\n"
        res += f"Расхождение: {round(abs(final_amd_actual - claimed_final), 2)}֏\n\n"
        res += "✅ Исправленный расчет:\n[Здесь будет чистый блок]"
        return res

# --- ОБРАБОТЧИКИ ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return

    if text.startswith('/audit_gs'):
        report = fast_audit(text)
        await update.message.reply_text(report)
        return

    if text.startswith('/menu'):
        await update.message.reply_text("1️⃣ /audit_gs - Точный расчет (Python)\n2️⃣ /paste - Шаблон\n3️⃣ Фото - Склад")

# ОСТАЛЬНЫЕ ФУНКЦИИ (Airtable, Фото) копируются из прошлой версии...

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__':
    main()
