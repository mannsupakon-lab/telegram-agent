import io
import sys
import os
import json
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import httpx
import pandas as pd
from PIL import Image
from google import genai
from google.genai import types

# เวลาประเทศไทย (GMT+7) เสมอ แม้รันบน Cloud Server ที่เป็น UTC
TH_TZ = timezone(timedelta(hours=7))

def now_th() -> datetime:
    return datetime.now(TH_TZ)

# บังคับใช้ IPv4 เท่านั้น ป้องกันปัญหา IPv6 ค้าง (Blackhole) บนอินเทอร์เน็ตบ้าน
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from config import GEMINI_API_KEY, GEMINI_MODEL, IMAGEN_MODEL

# ตั้งค่า Client ปิดการจำ Socket ค้าง (max_keepalive_connections=0) และตั้ง Timeout 15 วินาที
client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(
        timeout=15000,
        client_args={"limits": httpx.Limits(max_keepalive_connections=0)}
    )
)

# เก็บประวัติการสนทนาในหน่วยความจำ (Memory per user)
chat_sessions = {}

# ไฟล์เก็บความจำถาวร (Persistent Memory)
MEMORY_FILE = os.path.join(os.path.dirname(__file__), "user_memory.json")

# โมเดลที่ตอบไวที่สุดบน Free Tier (0.5 วินาที)
FAST_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]

BASE_SYSTEM_PROMPT = """คุณคือ AI Personal Assistant (ชื่อ กัญญ์วรา) ผู้ช่วยส่วนตัวอัจฉริยะ ทำหน้าที่ช่วยเหลือผู้ใช้ในทุกมิติ
บุคลิก: ฉลาด กระตือรือร้น สุภาพ จริงใจ และเป็นมิตร ใช้ภาษาไทยเป็นหลัก ตอบกระชับ ชัดเจน เข้าใจง่าย
ความสามารถของคุณ:
1. ตอบคำถาม วิเคราะห์ข้อมูล ให้คำปรึกษา สรุปสาระสำคัญ
2. ค้นหาข้อมูลข่าวสารและข้อมูลปัจจุบัน
3. วิเคราะห์รูปภาพ สลิป เอกสาร เมนูอาหาร ป้ายข้อความ
4. ถอดความและทำความเข้าใจไฟล์เสียง
5. สร้างไฟล์ตาราง Excel และวาดรูปภาพตามคำสั่ง
"""

def search_live_web(query: str) -> str:
    """ค้นหาข่าวสารและข้อมูลล่าสุดจากอินเทอร์เน็ต (Google News & Web)"""
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=th&gl=TH&ceid=TH:th"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
        items = []
        for item in root.findall(".//item")[:6]:
            title = item.findtext("title", "")
            pub_date = item.findtext("pubDate", "")
            items.append(f"- {title} ({pub_date})")
        if items:
            return f"ผลการค้นหาล่าสุดสำหรับ '{query}' (วันที่ระบบ {now_th().strftime('%Y-%m-%d %H:%M')}):\n" + "\n".join(items)
        return f"ไม่พบข่าวสารล่าสุดสำหรับ '{query}'"
    except Exception as e:
        return f"ไม่สามารถดึงข้อมูลค้นหาได้: {e}"

def extract_reminder_intent(text: str) -> dict | None:
    """ให้ AI ตรวจสอบว่าข้อความนี้เป็นการสั่งตั้งเวลาเตือนหรือไม่ และคำนวณเวลาเป็นวินาทีเทียบกับเวลาไทยปัจจุบัน"""
    current_dt = now_th()
    current_str = current_dt.strftime("%Y-%m-%d %H:%M:%S")
    prompt = f"""เวลาประเทศไทยปัจจุบันคือ: {current_str}
ข้อความจากผู้ใช้: "{text}"

วิเคราะห์ว่าผู้ใช้กำลังสั่งให้ "ตั้งเวลาแจ้งเตือน/ปลุก" ในอนาคตหรือไม่:
- ถ้าใช่ ให้คำนวณจำนวนวินาทีนับจากเวลาปัจจุบัน ({current_str}) ไปจนถึงเวลาที่ผู้ใช้ต้องการให้เตือน
- ตอบกลับเป็น JSON เท่านั้นตามรูปแบบนี้:
{{
  "is_reminder": true หรือ false,
  "seconds_from_now": จำนวนวินาที (ตัวเลขจำนวนเต็ม เช่น 300),
  "target_time_str": "เวลาที่จะเตือน เช่น 11:15 น.",
  "task": "สิ่งที่ต้องเตือนสั้นๆ เช่น ทานยา / ประชุม"
}}
"""
    for model_name in FAST_MODELS:
        try:
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            )
            if res.text:
                data = json.loads(res.text.strip())
                if isinstance(data, dict) and data.get("is_reminder") and int(data.get("seconds_from_now", 0)) > 0:
                    return data
                return None
        except Exception as e:
            print(f"Reminder parse error on {model_name}: {e}")
            continue
    return None

def load_memories() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_memory(user_id: int, note: str):
    memories = load_memories()
    uid = str(user_id)
    if uid not in memories:
        memories[uid] = []
    memories[uid].append(note)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memories, f, ensure_ascii=False, indent=2)

def get_user_memories_text(user_id: int) -> str:
    memories = load_memories().get(str(user_id), [])
    if not memories:
        return ""
    notes_str = "\n".join(f"- {m}" for m in memories)
    return f"\n\n[ข้อมูลส่วนตัว/บันทึกความจำของผู้ใช้ที่คุณต้องจำไว้เสมอ]:\n{notes_str}"

def get_system_prompt(user_id: int) -> str:
    now_str = now_th().strftime("%Y-%m-%d %H:%M:%S (เวลาประเทศไทย)")
    return f"{BASE_SYSTEM_PROMPT}\n(วันและเวลาปัจจุบัน: {now_str})" + get_user_memories_text(user_id)

SEARCH_KEYWORDS = ("ข่าว", "ราคา", "ล่าสุด", "วันนี้", "ตอนนี้", "พยากรณ์อากาศ", "หุ้น", "ทอง", "บิตคอยน์", "ค้นหา", "เช็ค")

def should_search_web(text: str) -> bool:
    return any(k in text.lower() for k in SEARCH_KEYWORDS)

def get_or_create_chat(user_id: int):
    """สร้างหรือดึงเซสชันการคุยเดิมของผู้ใช้ (ใช้โมเดลความเร็วสูง Single-Roundtrip)"""
    if user_id in chat_sessions:
        return chat_sessions[user_id]

    sys_prompt = get_system_prompt(user_id)

    for model_name in FAST_MODELS:
        try:
            chat = client.chats.create(
                model=model_name,
                config=types.GenerateContentConfig(
                    system_instruction=sys_prompt,
                )
            )
            chat_sessions[user_id] = (chat, model_name)
            return chat_sessions[user_id]
        except Exception as e:
            print(f"Warning: Could not init chat with {model_name}: {e}")
            continue

    chat = client.chats.create(
        model="gemini-3.1-flash-lite",
        config=types.GenerateContentConfig(system_instruction=sys_prompt)
    )
    chat_sessions[user_id] = (chat, "gemini-3.1-flash-lite")
    return chat_sessions[user_id]

def reset_chat(user_id: int):
    """รีเซ็ตความทรงจำของแชท"""
    if user_id in chat_sessions:
        del chat_sessions[user_id]

def ask_agent(user_id: int, message_text: str) -> str:
    """ประมวลผลข้อความและตอบกลับอย่างรวดเร็ว (Single Round-Trip พร้อมเวลาไทยปัจจุบัน)"""
    chat, model_name = get_or_create_chat(user_id)
    now_str = now_th().strftime("%H:%M น.")
    prompt_to_send = f"[เวลาไทยตอนนี้: {now_str}] {message_text}"
    if should_search_web(message_text):
        web_context = search_live_web(message_text)
        prompt_to_send = f"{prompt_to_send}\n\n[ข้อมูลอ้างอิงสดจากอินเทอร์เน็ต]:\n{web_context}"

    try:
        response = chat.send_message(prompt_to_send)
        return response.text
    except Exception as e:
        print(f"Error with {model_name}, trying fallback: {e}")
        reset_chat(user_id)
        sys_prompt = get_system_prompt(user_id)
        for fallback_model in ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]:
            try:
                fb_chat = client.chats.create(
                    model=fallback_model,
                    config=types.GenerateContentConfig(system_instruction=sys_prompt)
                )
                chat_sessions[user_id] = (fb_chat, fallback_model)
                response = fb_chat.send_message(prompt_to_send)
                return response.text
            except Exception:
                continue
        raise e

def analyze_image(user_id: int, prompt: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """วิเคราะห์รูปภาพผ่าน Gemini Multimodal"""
    part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    content_prompt = prompt if prompt else "ช่วยดูและวิเคราะห์รูปนี้อย่างละเอียดหน่อยครับ"
    sys_prompt = get_system_prompt(user_id)

    for model_name in FAST_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[content_prompt, part],
                config=types.GenerateContentConfig(
                    system_instruction=sys_prompt
                )
            )
            return response.text
        except Exception as e:
            print(f"Warning: Image analysis error on {model_name}: {e}")
            continue
    raise RuntimeError("ไม่สามารถวิเคราะห์รูปภาพได้ในขณะนี้ กรุณาลองใหม่อีกครั้ง")

def process_voice_audio(user_id: int, audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """ประมวลผลไฟล์เสียง (Audio to Text & Response)"""
    part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    sys_prompt = get_system_prompt(user_id)

    for model_name in FAST_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=["นี่คือข้อความเสียงจากผู้ใช้ ช่วยฟัง ถอดความ และตอบคำถามหรือให้คำปรึกษากลับมา:", part],
                config=types.GenerateContentConfig(
                    system_instruction=sys_prompt
                )
            )
            return response.text
        except Exception as e:
            print(f"Warning: Voice error on {model_name}: {e}")
            continue
    raise RuntimeError("ไม่สามารถประมวลผลเสียงได้ในขณะนี้ กรุณาลองใหม่อีกครั้ง")

def generate_image(prompt: str) -> bytes | None:
    """สร้างรูปภาพด้วย AI Prompt Enhancer + FLUX ความเร็วสูง"""
    try:
        en_prompt = prompt
        for model_name in FAST_MODELS:
            try:
                res = client.models.generate_content(
                    model=model_name,
                    contents=(
                        f"Translate and enhance this image description into a short, vivid English image generation prompt "
                        f"(max 35 words, output ONLY the English prompt, nothing else): {prompt}"
                    )
                )
                if res.text:
                    en_prompt = res.text.strip()
                    break
            except Exception:
                continue

        encoded = urllib.parse.quote(en_prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&model=flux"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.read()
    except Exception as e:
        print(f"Error generating image: {e}")
        return None

def generate_excel_bytes(topic_or_instruction: str) -> tuple[bytes, str]:
    """ให้ AI สร้างข้อมูลตารางตามหัวข้อ แล้วแปลงเป็นไฟล์ Excel (.xlsx)"""
    prompt = f"""สร้างข้อมูลตารางตามคำสั่งนี้: "{topic_or_instruction}"
ตอบกลับมาเป็น JSON Array ของ Objects เท่านั้น ห้ามมีข้อความอื่นปน ห้ามครอบด้วย ```json
ตัวอย่างรูปแบบ:
[
  {{"ลำดับ": 1, "รายการ": "ตัวอย่าง A", "รายละเอียด": "...", "ราคา": 100}},
  {{"ลำดับ": 2, "รายการ": "ตัวอย่าง B", "รายละเอียด": "...", "ราคา": 200}}
]
"""
    raw_json = "[]"
    for model_name in FAST_MODELS:
        try:
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            )
            if res.text:
                raw_json = res.text.strip()
                break
        except Exception:
            continue

    data = json.loads(raw_json)
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
        else:
            data = [data]

    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sheet1")
    output.seek(0)
    return output.getvalue(), f"สร้างตารางทั้งหมด {len(df)} แถวเรียบร้อยครับ"
