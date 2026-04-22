FROM python:3.12-slim
WORKDIR /app
COPY shared/ ./shared/
ENV PYTHONPATH=/app
COPY services/admin_api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/admin_api/ ./services/admin_api/
EXPOSE 8080
CMD ["uvicorn", "services.admin_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
