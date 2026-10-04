FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite lives on a local path. Mount a persistent volume at /data to keep data across restarts.
ENV DB_PATH=/data/sync.db \
    PORT=8000
RUN mkdir -p /data

EXPOSE 8000

# One worker: SQLite has a single writer, and this keeps the concurrency story simple.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]