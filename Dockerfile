FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/app/data \
    APP_LOG_DIR=/app/data/logs \
    TELEGRAM_SESSION_PATH=/app/data/session_name \
    APP_DB_PATH=/app/data/gtt_signals.db \
    APP_PYTHON_BIN=/usr/local/bin/python

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data/logs

CMD ["python", "app.py"]
