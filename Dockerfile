FROM python:3.10-slim

# ติดตั้งไลบรารีระบบและ Tesseract OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ติดตั้งโมดูล Python ที่จำเป็น
RUN pip install --no-cache-dir \
    flask \
    flask-cors \
    pillow \
    numpy \
    pytesseract \
    gunicorn

# ก๊อปปี้ไฟล์โค้ดเข้า Container
COPY app.py .

# กำหนดพอร์ตสำหรับ Render
ENV PORT=10000

# รันเซิร์ฟเวอร์
CMD exec gunicorn --bind 0.0.0.0:$PORT --timeout 60 --workers 1 app:app
