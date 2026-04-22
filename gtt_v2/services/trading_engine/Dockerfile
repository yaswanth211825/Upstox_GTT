FROM python:3.12-slim
WORKDIR /app
COPY shared/ ./shared/
ENV PYTHONPATH=/app
COPY services/trading_engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/trading_engine/ ./services/trading_engine/
CMD ["python", "-m", "services.trading_engine.main"]
