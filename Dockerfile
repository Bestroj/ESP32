FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    flask \
    flask-cors \
    pillow \
    numpy \
    pytesseract \
    gunicorn \
    pytz

COPY app.py .

ENV PORT=10000

CMD exec gunicorn --bind 0.0.0.0:$PORT --timeout 120 --workers 1 app:app
