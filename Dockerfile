FROM python:3.12-slim

# ป้องกันไม่ให้ Python สร้างไฟล์ .pyc และให้แสดง Log ทันที
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ติดตั้งไลบรารี
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# คัดลอกไฟล์โค้ดทั้งหมดเข้า Container
COPY . .

# Google Cloud Run จะส่งค่า PORT=8080 เข้ามาอัตโนมัติ
ENV PORT=8080
EXPOSE 8080

CMD ["python", "main.py"]
