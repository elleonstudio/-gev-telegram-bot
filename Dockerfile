FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libzbar0 \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV TELEGRAM_BOT_TOKEN=""
ENV KIMI_API_KEY=""
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
