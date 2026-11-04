# 🤖 Telegram AI Personal Assistant Agent

ผู้ช่วย AI ส่วนตัวบน Telegram ขับเคลื่อนด้วย **Google Gemini (Multimodal + Google Search)** และ **Imagen 3**

---

## ✨ ความสามารถหลัก

1. **💬 สนทนาและตอบคำถามทั่วไป:** มีความจำบริบทการคุยต่อเนื่อง จำชื่อและเรื่องที่คุยได้
2. **🌐 ค้นหาข้อมูลสดบนเน็ต (Web Search):** ค้นข่าวล่าสุด ราคาทอง หุ้น อากาศ ด้วย Google Search ในตัว
3. **📸 สแกนรูปภาพและสลิป (Vision):** ถ่ายรูปสลิป ใบเสร็จ เมนู หรือเอกสารส่งเข้าแชท AI จะอ่านและวิเคราะห์ทันที
4. **🎙️ ถอดเสียงพูด (Voice Note):** กดอัดเสียงพูดใน Telegram ส่งหาบอท AI จะฟังและตอบกลับ
5. **🎨 วาดภาพตามสั่ง (Image Generation):** พิมพ์คำสั่ง `/draw <คำอธิบาย>` หรือพิมพ์ว่า "วาดรูป..." บอทจะใช้ Imagen 3 วาดรูปสวยๆ ส่งกลับมา
6. **⏰ ตั้งเตือนความจำ (Reminders):** สั่งให้บอททักหาเราเองเมื่อถึงเวลา เช่น `/remind 30 โทรหาลูกค้า`
7. **🔒 ล็อกความปลอดภัย (Whitelist):** อนุญาตเฉพาะ Telegram User ID ของคุณคนเดียว คนอื่นแอบใช้ไม่ได้

---

## 🛠️ ขั้นตอนการตั้งค่าก่อนเริ่มรัน

### 1. รับ Telegram Bot Token
1. เปิดแอป Telegram ค้นหาบอทชื่อ **`@BotFather`**
2. พิมพ์คำสั่ง `/newbot`
3. ตั้งชื่อบอท และตั้งชื่อ username ของบอท (ต้องลงท้ายด้วย `bot` เช่น `MySmartAssistantBot`)
4. คัดลอก **HTTP API Token** ที่ได้มา

### 2. ดู Telegram User ID ของตัวเอง (เพื่อล็อกสิทธิ์)
1. ใน Telegram ค้นหาบอทชื่อ **`@userinfobot`**
2. กด `/start` บอทจะบอกตัวเลข **Id** ของคุณ (เช่น `123456789`)

### 3. รับ Google Gemini API Key
1. เข้าไปที่ [Google AI Studio](https://aistudio.google.com/)
2. กด **Get API Key** และกด Create API Key

### 4. สร้างไฟล์ `.env`
คัดลอกไฟล์ `.env.example` เป็น `.env` แล้วกรอกค่าที่ได้มา:
```env
TELEGRAM_BOT_TOKEN="วาง_TOKEN_จาก_BotFather"
ALLOWED_USER_ID="วาง_ID_ของคุณ"
GEMINI_API_KEY="วาง_GEMINI_API_KEY"
GEMINI_MODEL="gemini-3.8-flash"
IMAGEN_MODEL="imagen-3.0-generate-002"
```

---

## 🚀 วิธีการรัน

1. เปิด Terminal ในโฟลเดอร์นี้
2. ติดตั้งไลบรารีที่จำเป็น:
```bash
pip install -r requirements.txt
```
3. เริ่มต้นรัน Agent:
```bash
python main.py
```
4. เปิด Telegram ค้นหาชื่อบอทของคุณ แล้วพิมพ์ `/start` เริ่มใช้งานได้ทันที!
