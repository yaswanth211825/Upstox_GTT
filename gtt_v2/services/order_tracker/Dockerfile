FROM python:3.12-slim
WORKDIR /app
COPY shared/ ./shared/
ENV PYTHONPATH=/app
COPY services/order_tracker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/order_tracker/ ./services/order_tracker/
CMD ["python", "-m", "services.order_tracker.main"]
