import io
import os
import re
import sys
import html
import hmac
import socket
import asyncio
import hashlib
import logging
import urllib.request
import httpx
from aiohttp import web
from telegram import Update, Bot, KeyboardButton, ReplyKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# บังคับใช้ IPv4 เท่านั้น ป้องกันอาการค้างจาก IPv6
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

# รองรับ UTF-8 บน Windows Console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID, validate_config
import agent
import sales_data

# ตั้งค่า Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ซ่อน Log ของ httpx ระดับ INFO เพราะ URL ที่ log ออกมามี Bot Token ติดไปด้วย
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# รหัสลับยืนยันว่า Request ที่เข้ามาทาง /webhook มาจาก Telegram จริง
# (ตั้งเองได้ผ่าน env WEBHOOK_SECRET หากไม่ตั้งจะสร้างจาก Bot Token อัตโนมัติ)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(
    f"webhook:{TELEGRAM_BOT_TOKEN}".encode("utf-8")
).hexdigest()[:48]

# เก็บ reference ของ Task เบื้องหลัง ป้องกันไม่ให้ถูก Garbage Collect ทิ้งกลางทาง
_background_tasks: set[asyncio.Task] = set()

def run_background(coro) -> asyncio.Task:
    """สร้าง Task เบื้องหลังที่ปลอดภัย (ถือ reference ไว้จนกว่าจะทำงานเสร็จ)"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

# เวลาส่งสรุปสภาพอากาศ + ข่าวเชลซีประจำวันทุกเช้า (ค่าเริ่มต้น 07:30 น. เวลาไทย)
MORNING_BRIEFING_HOUR = 7
MORNING_BRIEFING_MINUTE = 30
last_morning_sent_date = None

def is_authorized(user_id: int) -> bool:
    """ตรวจสอบสิทธิ์การใช้งาน (Whitelist) - ถ้าไม่ได้ตั้ง ALLOWED_USER_ID จะปฏิเสธทุกคน (Fail-closed)"""
    if not ALLOWED_USER_ID or not ALLOWED_USER_ID.strip():
        logger.warning("ALLOWED_USER_ID is not set - rejecting all users for safety.")
        return False
    return str(user_id) == str(ALLOWED_USER_ID).strip()

def md_to_html(text: str) -> str:
    """แปลง Markdown ที่ Gemini ตอบมา (**ตัวหนา**, *เอียง*, `code`, ### หัวข้อ, [ลิงก์](url)) เป็น HTML ที่ Telegram รองรับ"""
    placeholders = []

    def _stash(value: str) -> str:
        placeholders.append(value)
        return f"\x00{len(placeholders) - 1}\x00"

    # 1. เก็บ code block / inline code ไว้ก่อน (ไม่แปลงอะไรข้างใน)
    text = re.sub(r"```(?:\w+)?\n?(.*?)```", lambda m: _stash(f"<pre>{html.escape(m.group(1).strip())}</pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: _stash(f"<code>{html.escape(m.group(1))}</code>"), text)

    # 2. Escape อักขระพิเศษของ HTML
    text = html.escape(text, quote=False)

    # 3. ลิงก์ [ข้อความ](url)
    text = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
        lambda m: _stash(f'<a href="{m.group(2).replace(chr(34), "&quot;")}">{m.group(1)}</a>'),
        text,
    )

    # 4. หัวข้อ ### xxx -> ตัวหนา
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.M)

    # 5. bullet "* " / "- " ต้นบรรทัด -> "• "
    text = re.sub(r"^(\s*)[*\-]\s+", r"\1• ", text, flags=re.M)

    # 6. ตัวหนา / ขีดเส้นใต้ / ตัวเอียง
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)

    # 7. คืนค่า code / ลิงก์ที่เก็บไว้
    return re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)

def strip_markdown(text: str) -> str:
    """ลบสัญลักษณ์ Markdown ออก ใช้เป็น fallback ตอนส่ง HTML ไม่ผ่าน"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)
    text = text.replace("```", "").replace("`", "")
    return text

def split_message(text: str, max_len: int = 3500) -> list[str]:
    """แบ่งข้อความยาวเป็นหลายท่อนตามบรรทัด (Telegram จำกัด 4096 ตัวอักษร)"""
    if len(text) <= max_len:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > max_len:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_len])
            line = line[max_len:]
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks

def get_location_keyboard() -> ReplyKeyboardMarkup:
    """ปุ่มลัดกดส่งพิกัด GPS ในคลิกเดียว"""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 ส่งตำแหน่งของฉันตอนนี้", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

async def send_bot_bubbles_to_chat(bot: Bot, chat_id: int, bubbles: list[str]):
    """ส่งชุดข้อความหลายกล่องไปยัง chat_id โดยตรง พร้อมจังหวะกำลังพิมพ์แบบมนุษย์"""
    for idx, msg in enumerate(bubbles):
        if idx > 0:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(0.8)
        try:
            await bot.send_message(chat_id=chat_id, text=md_to_html(msg), parse_mode=ParseMode.HTML)
        except Exception:
            await bot.send_message(chat_id=chat_id, text=strip_markdown(msg))

async def send_human_bubbles(update: Update, bubbles: list[str], reply_markup=None):
    """ส่งข้อความทีละกล่องแบบจังหวะมนุษย์พิมพ์แชทจริงๆ (Multi-Bubble + Typing Rhythm)"""
    msg_target = update.message or update.edited_message
    if not msg_target:
        return

    clean_bubbles = [b.strip() for b in bubbles if b and b.strip()]
    if not clean_bubbles:
        clean_bubbles = ["กัญญ์อยู่นี่ค่าา มีอะไรให้ช่วยบอกได้เลยนะคะ 😊"]

    for idx, bubble in enumerate(clean_bubbles):
        if idx > 0:
            try:
                await msg_target.reply_chat_action(ChatAction.TYPING)
            except Exception:
                pass
            delay = min(1.2, max(0.5, len(bubble) * 0.015))
            await asyncio.sleep(delay)

        is_last = idx == len(clean_bubbles) - 1
        markup = reply_markup if is_last else None

        chunks = split_message(bubble)
        for c_idx, chunk in enumerate(chunks):
            chunk_markup = markup if c_idx == len(chunks) - 1 else None
            try:
                await msg_target.reply_text(md_to_html(chunk), parse_mode=ParseMode.HTML, reply_markup=chunk_markup)
            except Exception:
                await msg_target.reply_text(strip_markdown(chunk), reply_markup=chunk_markup)

async def send_safe_reply(update: Update, text: str, reply_markup=None):
    """แยกข้อความด้วย |||MSG||| แล้วส่งเป็นหลายบอลลูนแบบมนุษย์"""
    if not text:
        text = "กัญญ์อยู่นี่ค่าา มีอะไรให้ช่วยบอกได้เลยนะคะ 😊"
    bubbles = text.split("|||MSG|||")
    await send_human_bubbles(update, bubbles, reply_markup=reply_markup)

def schedule_reminder_task(bot: Bot, user_id: int, seconds: int, alert_bubbles: list[str]):
    """ตั้งเวลาส่งแจ้งเตือนจริงใน Background (ส่งเป็นชุดข้อความเหมือนคนทักมาปลุก/เตือนจริงๆ)"""
    async def delayed_send():
        await asyncio.sleep(seconds)
        await send_bot_bubbles_to_chat(bot, user_id, alert_bubbles)

    run_background(delayed_send())

async def daily_morning_weather_loop(bot: Bot):
    """ลูปตรวจสอบเวลาไทยเพื่อส่งสรุปสภาพอากาศ + ข่าวเชลซีทุกเช้าเวลา 07:30 น."""
    global last_morning_sent_date
    while True:
        try:
            now = agent.now_th()
            today_str = now.strftime("%Y-%m-%d")
            if (
                ALLOWED_USER_ID
                and now.hour == MORNING_BRIEFING_HOUR
                and now.minute >= MORNING_BRIEFING_MINUTE
                and now.minute <= MORNING_BRIEFING_MINUTE + 5
                and last_morning_sent_date != today_str
            ):
                last_morning_sent_date = today_str
                uid = int(ALLOWED_USER_ID.strip())
                logger.info(f"Sending daily morning weather + Chelsea briefing to {uid}...")
                bubbles = await asyncio.to_thread(agent.generate_morning_weather_briefing, uid)
                await send_bot_bubbles_to_chat(bot, uid, bubbles)
        except Exception as e:
            logger.error(f"Morning weather loop error: {e}")
        await asyncio.sleep(30)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งเริ่มต้น /start"""
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ ขออภัยค่ะ บอทนี้เปิดให้ใช้งานเฉพาะเจ้าของเท่านั้นนะคะ")
        return

    bubbles = [
        f"สวัสดีค่าาคุณ {user.first_name}! กัญญ์วรามาแล้วค่ะ 😊✨",
        (
            "กัญญ์เป็นเลขาส่วนตัวและเพื่อนคู่คิดของคุณนะคะ คุยกับกัญญ์ได้เหมือนคุยกับคนจริงๆ เลย!\n\n"
            "• ☀️🔵 **ทุกเช้า 07:30 น.** กัญญ์จะทักมาสรุป **สภาพอากาศ + ข่าวเชลซีจาก 4 สำนักดัง** ให้ฟังอัตโนมัติ\n"
            "• 🦁 พิมพ์ `/chelsea` หรือ *'สรุปข่าวเชลซี'* เพื่อดูข่าวเชลซีล่าสุดตอนนี้ได้เลย\n"
            "• 📊 ถามข้อมูล Sales Dashboard ได้เลย เช่น *'ยอดขายเดือนนี้เท่าไหร่'* หรือ `/sales`\n"
            "• 🌤️ พิมพ์ `/weather` หรือ *'สรุปอากาศ'* เพื่อดูสภาพอากาศตรงจุดที่คุณอยู่\n"
            "• 📍 **กดปุ่ม 'ส่งตำแหน่งของฉันตอนนี้' ด้านล่าง** เพื่อให้กัญญ์จำพิกัดของคุณไว้เช็คอากาศและหาร้านอร่อยๆ\n"
            "• ⏰ สั่งตั้งเตือนด้วยภาษาพูดได้เลย เช่น *'12.00 เตือนไปกินข้าวหน่อย ทักมารัวๆ เลยนะ'*\n"
            "• 📸 ส่งรูปสลิป เอกสาร หรือ 🎙️ อัดเสียงส่งมาคุยกันได้เลยค่ะ!"
        ),
        "วันนี้มีอะไรให้กัญญ์ช่วยดูแล บอกมาได้เลยน้าา 💕"
    ]
    await send_human_bubbles(update, bubbles, reply_markup=get_location_keyboard())

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งขอดูสรุปสภาพอากาศทันที /weather (ไม่รวมข่าวเชลซี เพื่อให้ตอบไว)"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    asyncio.create_task(update.message.reply_chat_action(ChatAction.TYPING))
    bubbles = await asyncio.to_thread(agent.generate_morning_weather_briefing, user_id, False)
    await send_human_bubbles(update, bubbles)

async def chelsea_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งขอดูสรุปข่าวเชลซีจาก 4 สำนักทันที /chelsea"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    asyncio.create_task(update.message.reply_chat_action(ChatAction.TYPING))
    bubbles = await asyncio.to_thread(agent.generate_chelsea_news_briefing)
    await send_human_bubbles(update, bubbles)

async def sales_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งถามข้อมูลยอดขาย /sales <คำถาม> (ไม่ใส่คำถาม = สรุปภาพรวมเดือนนี้)"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    question = " ".join(context.args) if context.args else "สรุปยอดขายเดือนล่าสุด เทียบกับเดือนก่อนหน้า และ 5 ลูกค้าที่ยอดสูงสุดของเดือนล่าสุด"
    asyncio.create_task(update.message.reply_chat_action(ChatAction.TYPING))
    try:
        reply = await asyncio.to_thread(sales_data.answer_sales_question, user_id, question)
        await send_safe_reply(update, reply or "ถามเรื่องยอดขาย ลูกค้า สินค้า หรือออเดอร์ได้เลยนะคะ 📊")
    except Exception as e:
        logger.error(f"Sales command error: {e}")
        await update.message.reply_text("แงง กัญญ์เปิดข้อมูล Sales Dashboard ไม่สำเร็จค่ะ ลองใหม่อีกทีนะคะ 🥺")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งล้างความจำ /reset"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    agent.reset_chat(user_id)
    await send_human_bubbles(update, [
        "เคลียร์สมองพร้อมเริ่มคุยเรื่องใหม่แล้วค่าา 🔄✨",
        "วันนี้เราจะคุยเรื่องอะไรกันดีคะ บอกกัญญ์ได้เลยน้า 😊"
    ])

async def memo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งบันทึกความจำถาวร /memo <ข้อความ>"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if not context.args:
        memories = agent.load_memories().get(str(user_id), [])
        if not memories:
            await update.message.reply_text("ตอนนี้ยังไม่มีเรื่องที่สั่งให้กัญญ์จดไว้เลยค่ะ 📝 บอกกัญญ์ว่า 'จำไว้ว่า...' ได้เลยน้า")
        else:
            mem_list = "\n".join(f"{i+1}. {m}" for i, m in enumerate(memories))
            await update.message.reply_text(f"🧠 เรื่องสำคัญของคุณที่กัญญ์จำไว้ขึ้นใจค่ะ:\n\n{mem_list}")
        return

    note = " ".join(context.args)
    agent.save_memory(user_id, note)
    agent.reset_chat(user_id)
    await send_human_bubbles(update, [
        f"โอเคเลยค่า! กัญญ์จดจำเรื่อง \"{note}\" ไว้เรียบร้อยแล้วค่ะ 📝✨",
        "ถามเมื่อไหร่กัญญ์ก็ไม่ลืมแน่นอนค่ะ 😊"
    ])

async def draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งสร้างภาพ /draw <prompt>"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if not context.args:
        await update.message.reply_text("บอกกัญญ์ได้เลยนะคะว่าอยากให้วาดรูปอะไร เช่น 'วาดรูป แมวส้มใส่ชุดสูท' 🎨")
        return

    prompt = " ".join(context.args)
    await handle_image_generation(update, prompt)

async def handle_image_generation(update: Update, prompt: str):
    """ฟังก์ชันช่วยสร้างรูปภาพแบบไม่บล็อก Event Loop"""
    await update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)
    status_msg = await update.message.reply_text(f"แป๊บนึงนะคะ เดี๋ยวกัญญ์วาดรูป \"{prompt}\" ให้เลยค่าา 🎨✨")

    image_bytes = await asyncio.to_thread(agent.generate_image, prompt)
    if image_bytes:
        try:
            await status_msg.delete()
        except Exception:
            pass
        await update.message.reply_photo(photo=io.BytesIO(image_bytes), caption=f"เสร็จแล้วค่าา! รูป \"{prompt}\" สวยถูกใจไหมคะ 😊✨")
    else:
        await status_msg.edit_text("แงง ขอโทษด้วยนะคะ ตอนนี้ระบบวาดรูปขัดข้องชั่วคราว ลองสั่งกัญญ์ใหม่อีกรอบน้า 🥺")

async def excel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งสร้างไฟล์ Excel /excel <หัวข้อ>"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if not context.args:
        await update.message.reply_text("บอกหัวข้อตารางที่อยากให้กัญญ์ทำได้เลยนะคะ เช่น `/excel ตารางแผนออกกำลังกาย 7 วัน` 📊")
        return

    topic = " ".join(context.args)
    await handle_excel_generation(update, topic)

async def handle_excel_generation(update: Update, topic: str):
    """ฟังก์ชันช่วยสร้างไฟล์ Excel และส่งกลับเข้าแชท"""
    await update.message.reply_chat_action(ChatAction.UPLOAD_DOCUMENT)
    status_msg = await update.message.reply_text(f"ได้ค่าา เดี๋ยวกัญญ์ทำไฟล์ Excel เรื่อง \"{topic}\" ให้แป๊บนึงนะคะ 📊...")

    try:
        excel_bytes, summary = await asyncio.to_thread(agent.generate_excel_bytes, topic)
        try:
            await status_msg.delete()
        except Exception:
            pass
        file_obj = io.BytesIO(excel_bytes)
        file_obj.name = "output_table.xlsx"
        await update.message.reply_document(
            document=file_obj,
            filename="data_export.xlsx",
            caption=f"เสร็จเรียบร้อยแล้วค่าา! ไฟล์ Excel เรื่อง: {topic}\n({summary})"
        )
    except Exception as e:
        logger.error(f"Excel generation error: {e}")
        await status_msg.edit_text(f"แงง กัญญ์สร้างไฟล์ Excel ไม่สำเร็จค่ะ: {str(e)}")

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งตั้งเตือนความจำ /remind <นาที> <ข้อความ>"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if len(context.args) < 2:
        await update.message.reply_text("พิมพ์บอกกัญญ์เป็นภาษาพูดได้เลยนะคะ เช่น 'อีก 10 นาทีเตือนกินยาหน่อย' หรือ `/remind 10 กินยา` ค่ะ ⏰")
        return

    try:
        minutes = float(context.args[0])
        reminder_text = " ".join(context.args[1:])
        seconds = int(minutes * 60)
        target_dt = agent.now_th() + agent.timedelta(seconds=seconds)
        t_str = target_dt.strftime('%H:%M น.')

        alert_bubbles = [
            f"ก๊อกๆๆ ถึงเวลา {t_str} แล้วน้าา ⏰",
            f"อย่าลืมเรื่อง \"{reminder_text}\" ที่สั่งกัญญ์ไว้นะคะ!",
            "รีบไปทำเลยน้าา เป็นห่วงค่ะ 😊💕"
        ]
        schedule_reminder_task(context.bot, user_id, seconds, alert_bubbles)
        await send_human_bubbles(update, [
            f"รับทราบค่าา! กัญญ์ตั้งนาฬิกาไว้ให้ตอน {t_str} (อีก {minutes:g} นาที) เรียบร้อยแล้วนะคะ ⏰",
            f"ถึงเวลาปุ๊บ เดี๋ยวกัญญ์รีบทักมาเตือนเรื่อง \"{reminder_text}\" เลยค่ะ!"
        ])
    except ValueError:
        await update.message.reply_text("ระบุจำนวนนาทีเป็นตัวเลขนะคะ เช่น `/remind 15 ทานยา` ค่ะ 😊")

async def handle_location_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """จัดการเมื่อผู้ใช้แชร์ Location หรือ Live Location เข้ามา"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    msg = update.message or update.edited_message
    if not msg or not msg.location:
        return

    lat = msg.location.latitude
    lon = msg.location.longitude

    if update.message:
        asyncio.create_task(msg.reply_chat_action(ChatAction.TYPING))
        try:
            reply = await asyncio.to_thread(agent.handle_user_location, user_id, lat, lon)
            await send_safe_reply(update, reply)
        except Exception as e:
            logger.error(f"Location error: {e}")
            await msg.reply_text(f"กัญญ์เห็นพิกัด ({lat:.4f}, {lon:.4f}) แล้วค่าา แต่ดึงข้อมูลแผนที่สะดุดนิดนึงค่ะ 🥺")
    else:
        loc_info = await asyncio.to_thread(agent.get_location_details, lat, lon)
        agent.user_locations[user_id] = loc_info

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """จัดการข้อความแชททั่วไป"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    text = update.message.text.strip()
    lower_text = text.lower()

    # 1. ตรวจจับคำสั่งวาดรูปธรรมชาติ
    for prefix in ("วาดรูป", "สร้างรูป", "gen รูป", "วาดภาพ"):
        if lower_text.startswith(prefix):
            prompt = text[len(prefix):].strip()
            if prompt:
                await handle_image_generation(update, prompt)
                return

    # 2. ตรวจจับคำสั่งสร้างไฟล์ Excel ธรรมชาติ
    for prefix in ("สร้างไฟล์ excel", "ทำไฟล์ excel", "ขอไฟล์ excel", "สร้างตาราง excel"):
        if lower_text.startswith(prefix):
            topic = text[len(prefix):].strip()
            if topic:
                await handle_excel_generation(update, topic)
                return

    # 3. ตรวจจับคำสั่งให้จำข้อมูลธรรมชาติ
    for prefix in ("จำไว้ว่า", "ช่วยจำว่า", "บันทึกความจำว่า"):
        if lower_text.startswith(prefix):
            note = text[len(prefix):].strip()
            if note:
                agent.save_memory(user_id, note)
                agent.reset_chat(user_id)
                await send_human_bubbles(update, [
                    f"โอเคเลยค่า! กัญญ์จำไว้แล้วว่า \"{note}\" 🧠✨",
                    "คราวหน้าถามกัญญ์ได้ตลอดเลยนะคะ ไม่ลืมแน่นอน!"
                ])
                return

    # 4. ตรวจจับคำสั่งขอดูสรุปข่าวเชลซีทันที
    if any(k in lower_text for k in ("สรุปข่าวเชลซี", "ข่าวเชลซีวันนี้", "อัพเดทข่าวเชลซี", "อัปเดตข่าวเชลซี")):
        await chelsea_command(update, context)
        return

    # 5. ตรวจจับคำสั่งขอดูสรุปสภาพอากาศทันที
    if any(k in lower_text for k in ("สรุปอากาศ", "สรุปสภาพอากาศ", "อากาศเช้านี้", "พยากรณ์อากาศวันนี้")):
        await weather_command(update, context)
        return

    # 6. ถ้าผู้ใช้ถามเรื่องสถานที่/แถวนี้ แต่ยังไม่เคยแชร์ Location เลย ให้ส่งปุ่มกดแชร์ Location ให้ด้วย
    need_loc_btn = (
        user_id not in agent.user_locations
        and any(k in lower_text for k in ("อยู่ที่ไหน", "แถวนี้", "ใกล้ฉัน", "โลเคชัน", "location", "พิกัด"))
    )

    # 7. ตรวจจับคำสั่งตั้งเตือนความจำด้วยภาษาพูดธรรมชาติ
    if any(k in lower_text for k in ("เตือน", "ปลุก", "ทักมาตอน", "ทักตอน")):
        asyncio.create_task(update.message.reply_chat_action(ChatAction.TYPING))
        rem_info = await asyncio.to_thread(agent.extract_reminder_intent, text)
        if rem_info and rem_info.get("is_reminder") and rem_info.get("reminders"):
            for r in rem_info["reminders"]:
                seconds = int(r.get("seconds_from_now", 0))
                if seconds > 0:
                    task_name = r.get("task") or text
                    t_str = r.get("target_time_str", "")
                    alert_bubbles = r.get("alert_bubbles") or [
                        f"ก๊อกๆๆ ถึงเวลา {t_str} แล้วน้าา ⏰",
                        f"อย่าลืมเรื่อง \"{task_name}\" ที่บอกกัญญ์ไว้นะคะ 😊"
                    ]
                    schedule_reminder_task(context.bot, user_id, seconds, alert_bubbles)

            confirm_bubbles = rem_info.get("confirm_bubbles") or [
                "โอเคเลยค่าา กัญญ์ตั้งเวลาเตือนไว้ให้เรียบร้อยแล้วนะคะ ⏰✨",
                "พอถึงเวลาเดี๋ยวกัญญ์รีบทักมาเรียกเลยค่ะ ไม่ต้องกลัวลืมน้า 😊"
            ]
            await send_human_bubbles(update, confirm_bubbles)
            return

    # 8. ตอบคำถามเรื่องยอดขาย/ลูกค้า/ออเดอร์ จาก Sales Dashboard (Google Sheet)
    if sales_data.is_sales_question(user_id, text):
        asyncio.create_task(update.message.reply_chat_action(ChatAction.TYPING))
        try:
            sales_reply = await asyncio.to_thread(sales_data.answer_sales_question, user_id, text)
            if sales_reply:
                await send_safe_reply(update, sales_reply)
                return
        except Exception as e:
            logger.error(f"Sales question error: {e}")
            await update.message.reply_text("แงง กัญญ์เปิดข้อมูล Sales Dashboard ไม่สำเร็จค่ะ ลองถามใหม่อีกทีนะคะ 🥺")
            return

    # 9. ตอบคำถามปกติแบบมนุษย์ (ส่งเป็นหลายกล่องข้อความ Multi-Bubble)
    asyncio.create_task(update.message.reply_chat_action(ChatAction.TYPING))
    try:
        reply = await asyncio.to_thread(agent.ask_agent, user_id, text)
        markup = get_location_keyboard() if need_loc_btn else None
        await send_safe_reply(update, reply, reply_markup=markup)
    except Exception as e:
        logger.error(f"Error handling text: {e}")
        await update.message.reply_text("แงง เมื่อกี้สัญญาณสะดุดนิดนึงค่ะ พิมพ์บอกกัญญ์อีกทีได้ไหมคะ 🥺")

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """จัดการรูปภาพที่ส่งเข้ามา"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    await update.message.reply_chat_action(ChatAction.TYPING)
    status_msg = await update.message.reply_text("ไหนดูซิ... แป๊บนึงนะคะ กัญญ์กำลังดูรูปให้อยู่ค่ะ 🔍✨")

    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        caption = update.message.caption or ""

        analysis = await asyncio.to_thread(
            agent.analyze_image, user_id, caption, bytes(photo_bytes), "image/jpeg"
        )
        try:
            await status_msg.delete()
        except Exception:
            pass
        await send_safe_reply(update, analysis)
    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await status_msg.edit_text(f"แงง กัญญ์เปิดดูรูปนี้ไม่ได้ค่ะ: {str(e)}")

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """จัดการไฟล์เสียงที่ส่งเข้ามา"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    await update.message.reply_chat_action(ChatAction.TYPING)
    status_msg = await update.message.reply_text("กำลังตั้งใจฟังเสียงของคุณอยู่นะคะ 🎧✨...")

    try:
        voice = update.message.voice or update.message.audio
        mime_type = getattr(voice, "mime_type", None) or "audio/ogg"
        voice_file = await voice.get_file()
        voice_bytes = await voice_file.download_as_bytearray()

        reply = await asyncio.to_thread(
            agent.process_voice_audio, user_id, bytes(voice_bytes), mime_type
        )
        try:
            await status_msg.delete()
        except Exception:
            pass
        await send_safe_reply(update, reply)
    except Exception as e:
        logger.error(f"Error handling voice: {e}")
        await status_msg.edit_text(f"กัญญ์ฟังเสียงไม่ชัดเลยค่ะ ลองส่งใหม่อีกทีนะคะ: {str(e)}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """จัดการ Error ระดับ Application ไม่ให้บอทค้าง"""
    logger.warning(f"Update {update} caused error: {context.error}")

def build_telegram_app() -> Application:
    request_config = HTTPXRequest(
        connect_timeout=8.0,
        read_timeout=15.0,
        write_timeout=15.0,
        httpx_kwargs={"limits": httpx.Limits(max_keepalive_connections=0)},
    )
    get_updates_config = HTTPXRequest(
        connect_timeout=8.0,
        read_timeout=15.0,
        write_timeout=15.0,
        httpx_kwargs={"limits": httpx.Limits(max_keepalive_connections=0)},
    )
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request_config)
        .get_updates_request(get_updates_config)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("chelsea", chelsea_command))
    app.add_handler(CommandHandler("sales", sales_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("memo", memo_command))
    app.add_handler(CommandHandler("draw", draw_command))
    app.add_handler(CommandHandler("excel", excel_command))
    app.add_handler(CommandHandler("remind", remind_command))

    app.add_handler(MessageHandler(filters.LOCATION, handle_location_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_message))
    app.add_error_handler(error_handler)
    return app

async def run_cloud_run_server(tg_app: Application, port: int):
    """รันโหมด Webhook สำหรับ Render / Cloud Run (ออนไลน์ 24 ชม.)"""
    # ตอนเครื่อง Render เพิ่งบูต เครือข่ายอาจยังไม่พร้อม -> ลองเชื่อมต่อ Telegram ซ้ำแทนการล่มทันที
    for attempt in range(1, 6):
        try:
            await tg_app.initialize()
            break
        except Exception as e:
            if attempt == 5:
                raise
            wait = attempt * 3
            logger.warning(f"Telegram initialize failed ({e!r}), retrying in {wait}s... [{attempt}/5]")
            await asyncio.sleep(wait)
    await tg_app.start()

    # เริ่มต้นลูปส่งพยากรณ์อากาศ + ข่าวเชลซีทุกเช้า 07:30 น.
    run_background(daily_morning_weather_loop(tg_app.bot))

    external_url = os.getenv("RENDER_EXTERNAL_URL", "https://telegram-agent-wxg0.onrender.com").rstrip("/")
    webhook_url = f"{external_url}/webhook"

    try:
        await tg_app.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
        logger.info(f"Auto-registered webhook (with secret token): {webhook_url}")
    except Exception as e:
        logger.warning(f"Initial webhook set warning: {e}")

    async def handle_index(request: web.Request) -> web.Response:
        # หมายเหตุ: ห้ามตั้ง webhook จาก Host header ของผู้เยี่ยมชม (เสี่ยงถูกดึง webhook ไปเซิร์ฟเวอร์อื่น)
        page = f"""
        <html>
        <head><meta charset="utf-8"><title>Telegram AI Agent Status</title></head>
        <body style="font-family: sans-serif; padding: 40px; text-align: center; background: #f4f7f6;">
            <div style="background: white; padding: 30px; border-radius: 12px; max-width: 600px; margin: auto; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
                <h1 style="color: #2ecc71;">✅ กัญญ์วรา ออนไลน์ 24 ชั่วโมงแล้วค่ะ!</h1>
                <p>เวลาประเทศไทยบนระบบ: <b>{agent.now_th().strftime('%Y-%m-%d %H:%M:%S')}</b></p>
                <p>☀️🔵 ตั้งเวลาส่งสรุปสภาพอากาศ + ข่าวเชลซีทุกเช้าเวลา: <b>07:30 น.</b></p>
            </div>
        </body>
        </html>
        """
        return web.Response(text=page, content_type="text/html")

    async def handle_health(request: web.Request) -> web.Response:
        return web.Response(text="ALIVE")

    async def handle_webhook(request: web.Request) -> web.Response:
        # ตรวจรหัสลับว่าเป็น Request จาก Telegram จริงเท่านั้น
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(received, WEBHOOK_SECRET):
            logger.warning(f"Rejected webhook request with invalid secret from {request.remote}")
            return web.Response(text="Forbidden", status=403)
        try:
            data = await request.json()
            update = Update.de_json(data, tg_app.bot)
            run_background(tg_app.process_update(update))
            return web.Response(text="OK")
        except Exception as e:
            logger.error(f"Webhook processing error: {e}")
            return web.Response(text="ERROR", status=200)

    web_app = web.Application()
    web_app.router.add_get("/", handle_index)
    web_app.router.add_get("/health", handle_health)
    web_app.router.add_post("/webhook", handle_webhook)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    logger.info(f"Webhook server listening on port {port}...")
    await site.start()

    while True:
        await asyncio.sleep(600)
        try:
            await asyncio.to_thread(
                lambda: urllib.request.urlopen(f"{external_url}/health", timeout=10).read()
            )
            logger.info("Keep-alive ping sent.")
        except Exception:
            pass

def main():
    missing = validate_config()
    if missing:
        print(f"Missing config in .env: {', '.join(missing)}")
        return

    tg_app = build_telegram_app()

    cloud_port = os.getenv("PORT")
    if os.getenv("K_SERVICE") or cloud_port:
        port = int(cloud_port or 8080)
        asyncio.run(run_cloud_run_server(tg_app, port))
    else:
        print("Telegram AI Agent is running in Local Polling mode...")
        tg_app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
