FROM python:3.12-slim
WORKDIR /app
COPY shared/ ./shared/
ENV PYTHONPATH=/app
COPY services/signal_ingestor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/signal_ingestor/ ./services/signal_ingestor/
COPY ParseerWithAI/ ./ParseerWithAI/
CMD ["python", "-m", "services.signal_ingestor.main"]
