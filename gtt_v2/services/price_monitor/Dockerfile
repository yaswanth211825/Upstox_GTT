FROM python:3.12-slim
WORKDIR /app
COPY shared/ ./shared/
ENV PYTHONPATH=/app
COPY services/price_monitor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/price_monitor/ ./services/price_monitor/
COPY MarketDataFeedV3_pb2.py ./
CMD ["python", "-m", "services.price_monitor.main"]
