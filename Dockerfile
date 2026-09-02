FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    TELEMETRY_STREAM_PATH=/tmp/telemetry_stream.ndjson

WORKDIR /app

COPY requirements.txt ./
COPY simulator/requirements.txt ./simulator/
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "simulator/web_app.py"]
