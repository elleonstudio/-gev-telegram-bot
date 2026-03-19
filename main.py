import os
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Получаем токены из переменных окружения
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KIMI_API_KEY = os.getenv('KIMI_API_KEY')

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Привет! Я Gev Bot. Напиши мне что-нибудь!')

# Обработка сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    
    # Отправляем в Kimi API
    try:
        headers = {
            'Authorization': f'Bearer {KIMI_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        data = {
            'model': 'moonshot-v1-8k',
            'messages': [
                {'role': 'system', 'content': 'You are a helpful assistant.'},
                {'role': 'user', 'content': user_message}
            ]
        }
        
        response = requests.post(
            'https://api.moonshot.cn/v1/chat/completions',
            headers=headers,
            json=data
        )
        
        if response.status_code == 200:
            ai_response = response.json()['choices'][0]['message']['content']
            await update.message.reply_text(ai_response)
        else:
            await update.message.reply_text('Извини, не могу получить ответ от AI.')
            
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text('Произошла ошибка. Попробуй позже.')

# Главная функция
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    application.run_polling()

if __name__ == '__main__':
    main()
