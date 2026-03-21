FROM python:3.11-slim

# Установка системных зависимостей для OCR и штрих-кодов
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    tesseract-ocr-chi-sim \
    libzbar0 \
    poppler-utils \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV TELEGRAM_BOT_TOKEN=""
ENV KIMI_API_KEY=""
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
