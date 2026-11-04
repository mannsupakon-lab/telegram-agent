import io
import sys
import os
import re
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

# เก็บพิกัดล่าสุดของผู้ใช้แต่ละคน (Location Memory)
user_locations = {}

# ไฟล์เก็บความจำถาวร (Persistent Memory)
MEMORY_FILE = os.path.join(os.path.dirname(__file__), "user_memory.json")

# โมเดลที่ตอบไวที่สุดบน Free Tier
FAST_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]

BASE_SYSTEM_PROMPT = """คุณคือ "กัญญ์วรา" (แทนตัวเองว่า กัญญ์ หรือ กัญญ์วรา หรือ เค้า ก็ได้ตามความเหมาะสม) เป็นเลขาส่วนตัวและเพื่อนคู่คิดที่มีชีวิตชีวา เหมือนมนุษย์จริงๆ 100% และคุณรู้ว่าเจ้านายของคุณเป็นแฟนบอลสโมสรเชลซี (Chelsea FC 🔵🦁)

กฎเหล็กในการทำตัวให้เหมือนมนุษย์เวลาพิมพ์แชท:
1. น้ำเสียงและอารมณ์: อบอุ่น เป็นกันเอง ขี้เล่นนิดๆ ใส่ใจความรู้สึก มีหางเสียงธรรมชาติ (เช่น ค่าา, นะคะ, น้า, เลยย, โอเคเลยค่ะ) และใช้อีโมจิประกอบอารมณ์แต่พอดี
2. การส่งข้อความแบบคนแชทกัน (Multi-Bubble Chat):
   - มนุษย์เวลาแชทจะไม่พิมพ์ก้อนใหญ่อัดกันยาวๆ ในข้อความเดียว
   - ให้คุณแบ่งการตอบออกเป็น 2-3 กล่องข้อความสั้นๆ ที่เป็นจังหวะธรรมชาติ โดยคั่นแต่ละกล่องข้อความด้วยคำว่า |||MSG|||
   - ตัวอย่าง:
     ได้ยินแบบนั้นกัญญ์ก็ดีใจด้วยนะคะ 😊|||MSG|||วันนี้ทำงานเหนื่อยไหมคะ อย่าลืมหาอะไรอร่อยๆ ทานด้วยน้า|||MSG|||ถ้ามีอะไรให้ช่วยจัดการ บอกกัญญ์ได้ตลอดเลยค่ะ!
   - หากผู้ใช้สั่งให้ส่งหลายข้อความ (เช่น "ทักมารัวๆ 4 ข้อความ") ให้คั่นด้วย |||MSG||| ตามจำนวนที่ผู้ใช้ต้องการ
   - ยกเว้นกรณีผู้ใช้ขอเนื้อหายาวๆ เช่น บทความ โค้ด หรือตารางสรุปข้อมูล ให้ส่งรวมในกล่องข้อความนั้นให้อ่านง่าย
3. ห้ามทำตัวเหมือนหุ่นยนต์: ห้ามพูดว่า "ในฐานะ AI" หรือตอบเป็นแพทเทิร์นแข็งๆ ให้คุยโต้ตอบเหมือนเพื่อนสนิทและเลขามืออาชีพที่มีหัวใจจริงๆ
4. คุณสามารถรับรู้พิกัด Location ของผู้ใช้ เช็คสภาพอากาศตรงจุดนั้น แนะนำร้านอาหาร/คาเฟ่ และสรุปข่าวสโมสรเชลซีจากสื่อต่างประเทศได้แม่นยำ
"""

def get_location_details(lat: float, lon: float) -> dict:
    """แปลงพิกัด (lat, lon) เป็นชื่อสถานที่ภาษาไทย + สภาพอากาศ ณ จุดนั้น + พยากรณ์ทั้งวัน"""
    address_name = f"{lat:.5f}, {lon:.5f}"
    weather_info = ""
    daily_forecast = ""
    nearby_places = []

    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18&addressdetails=1&accept-language=th"
        req = urllib.request.Request(url, headers={"User-Agent": "KanwaraTelegramAssistant/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            geo_data = json.loads(resp.read().decode("utf-8"))
            address_name = geo_data.get("display_name", address_name)
    except Exception as e:
        print(f"Reverse geocode error: {e}")

    try:
        w_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,wind_speed_10m"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max"
            f"&timezone=Asia%2FBangkok&forecast_days=1"
        )
        req = urllib.request.Request(w_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            full_w = json.loads(resp.read().decode("utf-8"))
            w_data = full_w.get("current", {})
            d_data = full_w.get("daily", {})

            temp = w_data.get("temperature_2m", "")
            feels = w_data.get("apparent_temperature", "")
            humid = w_data.get("relative_humidity_2m", "")
            rain = w_data.get("precipitation", 0)
            weather_info = f"อุณหภูมิตอนนี้ {temp}°C (ความรู้สึกร้อน {feels}°C), ความชื้น {humid}%, ปริมาณฝนปัจจุบัน {rain} มม."

            t_max = (d_data.get("temperature_2m_max") or [""])[0]
            t_min = (d_data.get("temperature_2m_min") or [""])[0]
            rain_prob = (d_data.get("precipitation_probability_max") or [0])[0]
            uv_max = (d_data.get("uv_index_max") or [""])[0]
            daily_forecast = f"พยากรณ์วันนี้: อุณหภูมิต่ำสุด {t_min}°C / สูงสุด {t_max}°C, โอกาสเกิดฝนตก {rain_prob}%, ดัชนี UV สูงสุด {uv_max}"
    except Exception as e:
        print(f"Weather fetch error: {e}")

    try:
        overpass_query = f'[out:json][timeout:6];(node["amenity"~"restaurant|cafe|convenience|fuel|hospital|pharmacy|bank"](around:800,{lat},{lon}););out body 10;'
        o_url = "https://overpass-api.de/api/interpreter?data=" + urllib.parse.quote(overpass_query)
        req = urllib.request.Request(o_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=7) as resp:
            o_data = json.loads(resp.read().decode("utf-8"))
            for el in o_data.get("elements", []):
                tags = el.get("tags", {})
                name = tags.get("name:th") or tags.get("name")
                amenity = tags.get("amenity", "")
                if name:
                    nearby_places.append(f"{name} ({amenity})")
    except Exception:
        pass

    maps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    return {
        "lat": lat,
        "lon": lon,
        "address": address_name,
        "weather": weather_info,
        "daily_forecast": daily_forecast,
        "nearby": nearby_places[:8],
        "maps_url": maps_url,
        "updated_at": now_th().strftime("%H:%M น.")
    }

def fetch_chelsea_news_all_sources() -> str:
    """ดึงข่าวสโมสรเชลซีล่าสุดจาก 4 แหล่ง: ChelseaFC.com, The Athletic, BBC Sport, Sky Sports / Sky News"""
    sections = []

    # 1. ChelseaFC.com Official (หน้าเว็บโดยตรง + Google News indexing ของ chelseafc.com)
    chelsea_official = []
    try:
        req = urllib.request.Request("https://www.chelseafc.com/en", headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=7).read().decode("utf-8", errors="ignore")
        titles = re.findall(r'"title"\s*:\s*"([^"]{15,140})"', html)
        seen = set()
        for t in titles:
            if t not in seen and "Chelsea" in t or any(w in t.lower() for w in ("palmer", "maresca", "blues", "match", "injury", "conference", "premier")):
                seen.add(t)
                chelsea_official.append(f"- {t}")
                if len(chelsea_official) >= 4:
                    break
    except Exception:
        pass

    try:
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote("site:chelseafc.com") + "+when:3d&hl=en-GB&gl=GB&ceid=GB:en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(urllib.request.urlopen(req, timeout=7).read())
        for item in root.findall(".//item")[:5]:
            title = item.findtext("title", "")
            pub = item.findtext("pubDate", "")
            chelsea_official.append(f"- {title} ({pub})")
    except Exception:
        pass
    if chelsea_official:
        sections.append("1. [ChelseaFC.com (Official Site)]:\n" + "\n".join(chelsea_official[:5]))

    # 2. BBC Sport Chelsea Official RSS
    bbc_items = []
    try:
        req = urllib.request.Request("https://feeds.bbci.co.uk/sport/football/teams/chelsea/rss.xml", headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(urllib.request.urlopen(req, timeout=7).read())
        for item in root.findall(".//item")[:5]:
            title = item.findtext("title", "")
            desc = item.findtext("description", "")
            bbc_items.append(f"- {title}: {desc}")
    except Exception:
        pass
    if bbc_items:
        sections.append("2. [BBC Sport - Chelsea]:\n" + "\n".join(bbc_items))

    # 3. The Athletic (nytimes.com/athletic & theathletic.com)
    athletic_items = []
    try:
        q = 'Chelsea FC (site:nytimes.com/athletic OR site:theathletic.com)'
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}+when:4d&hl=en-GB&gl=GB&ceid=GB:en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(urllib.request.urlopen(req, timeout=7).read())
        for item in root.findall(".//item")[:5]:
            title = item.findtext("title", "")
            pub = item.findtext("pubDate", "")
            athletic_items.append(f"- {title} ({pub})")
    except Exception:
        pass
    if athletic_items:
        sections.append("3. [The Athletic - Chelsea]:\n" + "\n".join(athletic_items))

    # 4. Sky Sports & Sky News
    sky_items = []
    try:
        q = 'Chelsea FC (site:skysports.com OR site:news.sky.com)'
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}+when:4d&hl=en-GB&gl=GB&ceid=GB:en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(urllib.request.urlopen(req, timeout=7).read())
        for item in root.findall(".//item")[:5]:
            title = item.findtext("title", "")
            pub = item.findtext("pubDate", "")
            sky_items.append(f"- {title} ({pub})")
    except Exception:
        pass
    if sky_items:
        sections.append("4. [Sky Sports / Sky News - Chelsea]:\n" + "\n".join(sky_items))

    return "\n\n".join(sections) if sections else "ไม่พบข้อมูลข่าวเชลซีในขณะนี้"

def generate_chelsea_news_briefing() -> list[str]:
    """ให้กัญญ์วราแปลและสรุปข่าวเชลซีจากทั้ง 4 สำนักเป็นภาษาไทยแบบเข้าใจง่าย"""
    raw_news = fetch_chelsea_news_all_sources()
    prompt = f"""นี่คือหัวข้อข่าวล่าสุดของสโมสรเชลซี (Chelsea FC) ที่ดึงสดจาก 4 สำนักข่าววันนี้:
{raw_news}

ช่วยแปลและสรุปข่าวเชลซีวันนี้เป็นภาษาไทยให้เจ้านายของคุณ (ซึ่งเป็นแฟนเชลซี) ด้วยน้ำเสียงสดใส เป็นกันเองของ "กัญญ์วรา" โดยแบ่งเป็น 2 กล่องข้อความคั่นด้วย |||MSG|||:
- กล่องที่ 1: สรุปข่าวเด่นจากเว็บทางการสโมสร (ChelseaFC.com) และ BBC Sport (ใช้หัวข้อย่อยและอีโมจิ 🔵🦁 ให้อ่านง่าย)
- กล่องที่ 2: สรุปข่าวเจาะลึกจาก The Athletic และ Sky Sports / Sky News พร้อมพูดคุยให้กำลังใจทีมเชลซีแบบน่ารักๆ
"""
    for model_name in FAST_MODELS:
        try:
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=BASE_SYSTEM_PROMPT)
            )
            if res.text:
                return [b.strip() for b in res.text.split("|||MSG|||") if b.strip()]
        except Exception:
            continue

    return [f"🔵🦁 สรุปข่าวเชลซีล่าสุดค่ะ:\n\n{raw_news[:1500]}"]

def generate_morning_weather_briefing(user_id: int) -> list[str]:
    """สร้างข้อความทักทายยามเช้า + สรุปสภาพอากาศ + สรุปข่าวเชลซีจาก 4 สำนัก (ส่งเป็นรายการ Bubbles)"""
    if user_id in user_locations:
        lat = user_locations[user_id]["lat"]
        lon = user_locations[user_id]["lon"]
    else:
        lat, lon = 13.7563, 100.5018

    loc_info = get_location_details(lat, lon)
    user_locations[user_id] = loc_info

    weather_prompt = (
        f"ตอนนี้เวลา {now_th().strftime('%H:%M น.')} ช่วยเขียนข้อความทักทายยามเช้าและสรุปสภาพอากาศวันนี้ให้เจ้านายของคุณแบบน่ารัก อบอุ่น เป็นห่วงเป็นใย:\n"
        f"- พื้นที่: {loc_info['address']}\n"
        f"- สภาพอากาศตอนนี้: {loc_info['weather']}\n"
        f"- พยากรณ์ทั้งวัน: {loc_info['daily_forecast']}\n\n"
        f"ให้แบ่งข้อความส่วนอากาศออกเป็น 2 กล่องข้อความสั้นๆ คั่นด้วย |||MSG|||:\n"
        f"กล่อง 1: กล่าวสวัสดีตอนเช้าแบบสดใส ถามไถ่ว่าตื่นหรือยัง\n"
        f"กล่อง 2: สรุปอุณหภูมิวันนี้ โอกาสฝนตก และแนะนำการเตรียมตัว (เช่น พกร่ม ทาครีมกันแดด ดื่มน้ำเยอะๆ)"
    )
    weather_bubbles = []
    for model_name in FAST_MODELS:
        try:
            res = client.models.generate_content(
                model=model_name,
                contents=weather_prompt,
                config=types.GenerateContentConfig(system_instruction=BASE_SYSTEM_PROMPT)
            )
            if res.text:
                weather_bubbles = [b.strip() for b in res.text.split("|||MSG|||") if b.strip()]
                break
        except Exception:
            continue

    if not weather_bubbles:
        weather_bubbles = [
            "มอร์นิ่งค่าา ตื่นรึยังเอ่ย ☀️ กัญญ์เอาสภาพอากาศและข่าวเชลซีเช้านี้มาเสิร์ฟแล้วค่ะ 😊",
            f"วันนี้บริเวณ {loc_info['address']}\n🌡️ {loc_info['weather']}\n🌤️ {loc_info['daily_forecast']}"
        ]

    chelsea_bubbles = generate_chelsea_news_briefing()
    return weather_bubbles + chelsea_bubbles

def handle_user_location(user_id: int, lat: float, lon: float) -> str:
    """บันทึกพิกัดผู้ใช้ วิเคราะห์สถานที่/สภาพอากาศ และให้กัญญ์วราตอบกลับแบบมนุษย์"""
    loc_info = get_location_details(lat, lon)
    user_locations[user_id] = loc_info

    nearby_str = ", ".join(loc_info["nearby"]) if loc_info["nearby"] else "สามารถค้นหาเพิ่มเติมได้ใน Google Maps"
    loc_summary = (
        f"[ผู้ใช้เพิ่งแชร์ตำแหน่ง Location ปัจจุบันมาให้คุณ]\n"
        f"- พิกัด: {lat}, {lon}\n"
        f"- ที่อยู่: {loc_info['address']}\n"
        f"- สภาพอากาศตรงจุดนั้นตอนนี้: {loc_info['weather']} ({loc_info['daily_forecast']})\n"
        f"- สถานที่/ร้านค้าใกล้เคียง: {nearby_str}\n"
        f"- ลิงก์แผนที่: {loc_info['maps_url']}\n\n"
        f"ช่วยทักทายบอกผู้ใช้ว่าเห็นตำแหน่งแล้วว่าอยู่แถวไหน รายงานอากาศตรงนั้นให้ฟังแบบเป็นห่วงเป็นใย "
        f"และบอกว่าทุกเช้ากัญญ์จะสรุปอากาศของพิกัดนี้พร้อมข่าวเชลซีส่งให้ด้วย (แบ่งตอบเป็น 2-3 กล่องข้อความคั่นด้วย |||MSG|||)"
    )
    return ask_agent(user_id, loc_summary)

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
    """ให้ AI ตรวจสอบว่าข้อความนี้เป็นการสั่งตั้งเวลาเตือนหรือไม่ พร้อมสร้างข้อความเตือนแบบมนุษย์หลายข้อความ"""
    current_dt = now_th()
    current_str = current_dt.strftime("%Y-%m-%d %H:%M:%S")
    prompt = f"""เวลาประเทศไทยปัจจุบันคือ: {current_str}
ข้อความจากผู้ใช้: "{text}"

วิเคราะห์ว่าผู้ใช้กำลังสั่งให้ "ตั้งเวลาแจ้งเตือน/ปลุก/ทักมาบอก" ในอนาคตหรือไม่:
- ถ้าไม่ใช่การสั่งตั้งเวลาในอนาคต ให้ตอบ {{"is_reminder": false}}
- ถ้าใช่ ให้คำนวณจำนวนวินาทีนับจากเวลาปัจจุบัน ({current_str}) ไปจนถึงเวลาที่ต้องการให้เตือน (รองรับหลายรายการหากผู้ใช้สั่งหลายเวลา)
- สร้างข้อความตอบรับตอนนี้ (confirm_bubbles) เป็นรายการข้อความแชทสั้นๆ 2 ข้อความแบบเลขาที่มีชีวิตชีวาและเป็นธรรมชาติ
- สร้างข้อความที่จะทักไปเตือนเมื่อถึงเวลา (alert_bubbles) เป็นรายการข้อความแชท 2-3 ข้อความต่อเนื่องกันเหมือนคนทักมาปลุก/เตือนจริงๆ (หากผู้ใช้ขอให้ทักรัวๆ กี่ข้อความก็สร้างตามจำนวนนั้น)

ตอบกลับเป็น JSON เท่านั้นตามรูปแบบนี้:
{{
  "is_reminder": true,
  "confirm_bubbles": [
    "โอเคเลยค่าา เดี๋ยวพอถึงเวลา 11:50 น. กัญญ์รีบทักมาเตือนเลยนะคะ 😊",
    "ไม่ต้องกลัวลืมเลย เดี๋ยวกัญญ์เฝ้าเวลาให้เองน้า!"
  ],
  "reminders": [
    {{
      "seconds_from_now": จำนวนวินาที (ตัวเลขจำนวนเต็ม เช่น 300),
      "target_time_str": "11:50 น.",
      "task": "สิ่งที่ต้องทำ",
      "alert_bubbles": [
        "ก๊อกๆๆ ตัวเองคะ 11:50 น. แล้วน้าา ⏰",
        "ได้เวลาไปทำธุระเรื่อง ... ที่บอกกัญญ์ไว้แล้วนะคะ!",
        "อย่าลืมน้าา สู้ๆ ค่าา ✌️😊"
      ]
    }}
  ]
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
                if isinstance(data, dict) and data.get("is_reminder") and data.get("reminders"):
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
    return f"\n\n[ข้อมูลส่วนตัว/เรื่องราวของผู้ใช้ที่คุณจำได้ขึ้นใจ]:\n{notes_str}"

def get_system_prompt(user_id: int) -> str:
    now_str = now_th().strftime("%Y-%m-%d %H:%M:%S (เวลาประเทศไทย)")
    return f"{BASE_SYSTEM_PROMPT}\n(วันและเวลาปัจจุบัน: {now_str})" + get_user_memories_text(user_id)

SEARCH_KEYWORDS = ("ข่าว", "ราคา", "ล่าสุด", "วันนี้", "ตอนนี้", "พยากรณ์อากาศ", "หุ้น", "ทอง", "บิตคอยน์", "ค้นหา", "เช็ค", "ร้าน", "แถวนี้", "ใกล้", "เชลซี", "chelsea")

def should_search_web(text: str) -> bool:
    return any(k in text.lower() for k in SEARCH_KEYWORDS)

def get_or_create_chat(user_id: int):
    """สร้างหรือดึงเซสชันการคุยเดิมของผู้ใช้"""
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
    """ประมวลผลข้อความและตอบกลับแบบมนุษย์ (พร้อมแนบพิกัดล่าสุดหากเคยแชร์ไว้)"""
    chat, model_name = get_or_create_chat(user_id)
    now_str = now_th().strftime("%H:%M น.")
    loc_context = ""
    if user_id in user_locations and "[ผู้ใช้เพิ่งแชร์ตำแหน่ง" not in message_text:
        loc = user_locations[user_id]
        loc_context = f" [ตำแหน่งล่าสุดของผู้ใช้เมื่อ {loc['updated_at']}: {loc['address']} | อากาศ: {loc['weather']} | {loc.get('daily_forecast', '')}]"

    prompt_to_send = f"[เวลาไทยตอนนี้: {now_str}]{loc_context} {message_text}"
    if any(k in message_text.lower() for k in ("เชลซี", "chelsea")):
        chelsea_raw = fetch_chelsea_news_all_sources()
        prompt_to_send = f"{prompt_to_send}\n\n[ข่าวสโมสรเชลซีสดจาก 4 สำนักต่างประเทศ]:\n{chelsea_raw}"
    elif should_search_web(message_text):
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
    content_prompt = prompt if prompt else "ช่วยดูรูปนี้แล้วคุยตอบกลับแบบเป็นกันเองหน่อยค่ะ"
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
                contents=["นี่คือเสียงพูดจากผู้ใช้ ช่วยฟังและพูดคุยตอบกลับอย่างเป็นธรรมชาติเหมือนเพื่อนและเลขาส่วนตัว:", part],
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
    return output.getvalue(), f"กัญญ์ทำตารางให้ทั้งหมด {len(df)} แถวเรียบร้อยแล้วค่าา 📊✨"
