import io
import os
import sys
import socket
import asyncio
import logging
import urllib.request
import httpx
from aiohttp import web
from telegram import Update, Bot
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

# ตั้งค่า Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

def is_authorized(user_id: int) -> bool:
    """ตรวจสอบสิทธิ์การใช้งาน (Whitelist)"""
    if not ALLOWED_USER_ID:
        return True
    return str(user_id) == str(ALLOWED_USER_ID).strip()

async def send_human_bubbles(update: Update, bubbles: list[str]):
    """ส่งข้อความทีละกล่องแบบจังหวะมนุษย์พิมพ์แชทจริงๆ (Multi-Bubble + Typing Rhythm)"""
    clean_bubbles = [b.strip() for b in bubbles if b and b.strip()]
    if not clean_bubbles:
        clean_bubbles = ["กัญญ์อยู่นี่ค่าา มีอะไรให้ช่วยบอกได้เลยนะคะ 😊"]

    for idx, bubble in enumerate(clean_bubbles):
        if idx > 0:
            try:
                await update.message.reply_chat_action(ChatAction.TYPING)
            except Exception:
                pass
            # หน่วงเวลาสั้นๆ ให้เหมือนคนกำลังพิมพ์ข้อความถัดไป (0.6 - 1.2 วินาที)
            delay = min(1.2, max(0.5, len(bubble) * 0.015))
            await asyncio.sleep(delay)

        max_len = 4000
        for i in range(0, len(bubble), max_len):
            chunk = bubble[i : i + max_len]
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await update.message.reply_text(chunk)

async def send_safe_reply(update: Update, text: str):
    """แยกข้อความด้วย |||MSG||| แล้วส่งเป็นหลายบอลลูนแบบมนุษย์"""
    if not text:
        text = "กัญญ์อยู่นี่ค่าา มีอะไรให้ช่วยบอกได้เลยนะคะ 😊"
    bubbles = text.split("|||MSG|||")
    await send_human_bubbles(update, bubbles)

def schedule_reminder_task(bot: Bot, user_id: int, seconds: int, alert_bubbles: list[str]):
    """ตั้งเวลาส่งแจ้งเตือนจริงใน Background (ส่งเป็นชุดข้อความเหมือนคนทักมาปลุก/เตือนจริงๆ)"""
    async def delayed_send():
        await asyncio.sleep(seconds)
        for idx, msg in enumerate(alert_bubbles):
            if idx > 0:
                try:
                    await bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                except Exception:
                    pass
                await asyncio.sleep(0.7)
            await bot.send_message(chat_id=user_id, text=msg)

    asyncio.create_task(delayed_send())

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
            "• 💬 ชวนคุย ปรึกษา หรือให้ค้นหาข่าว/ราคาทอง/ข้อมูลสด\n"
            "• ⏰ สั่งตั้งเตือนด้วยภาษาพูดได้เลย เช่น *'12.00 เตือนไปกินข้าวหน่อย ทักมารัวๆ เลยนะ'*\n"
            "• 📸 ส่งรูปสลิป เอกสาร หรืออาหารมาให้ช่วยดู\n"
            "• 🎙️ อัดเสียงส่งมาคุยกันได้เลย\n"
            "• 🎨 สั่ง *'วาดรูป...'* หรือ 📊 *'สร้างไฟล์ excel...'*\n"
            "• 🧠 บอกว่า *'จำไว้ว่า...'* เพื่อให้กัญญ์จำเรื่องสำคัญของคุณไว้ถาวร"
        ),
        "วันนี้มีอะไรให้กัญญ์ช่วยดูแล บอกมาได้เลยน้าา 💕"
    ]
    await send_human_bubbles(update, bubbles)

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

    # 4. ตรวจจับคำสั่งตั้งเตือนความจำด้วยภาษาพูดธรรมชาติ (รองรับหลายเวลา + ทักเตือนหลายข้อความแบบคนจริงๆ)
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

    # 5. ตอบคำถามปกติแบบมนุษย์ (ส่งเป็นหลายกล่องข้อความ Multi-Bubble)
    asyncio.create_task(update.message.reply_chat_action(ChatAction.TYPING))
    try:
        reply = await asyncio.to_thread(agent.ask_agent, user_id, text)
        await send_safe_reply(update, reply)
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
        voice_file = await voice.get_file()
        voice_bytes = await voice_file.download_as_bytearray()

        reply = await asyncio.to_thread(
            agent.process_voice_audio, user_id, bytes(voice_bytes), "audio/ogg"
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
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("memo", memo_command))
    app.add_handler(CommandHandler("draw", draw_command))
    app.add_handler(CommandHandler("excel", excel_command))
    app.add_handler(CommandHandler("remind", remind_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_message))
    app.add_error_handler(error_handler)
    return app

async def run_cloud_run_server(tg_app: Application, port: int):
    """รันโหมด Webhook สำหรับ Render / Cloud Run (ออนไลน์ 24 ชม.)"""
    await tg_app.initialize()
    await tg_app.start()

    external_url = os.getenv("RENDER_EXTERNAL_URL", "https://telegram-agent-wxg0.onrender.com").rstrip("/")

    try:
        await tg_app.bot.set_webhook(url=f"{external_url}/webhook", drop_pending_updates=True)
        logger.info(f"Auto-registered webhook: {external_url}/webhook")
    except Exception as e:
        logger.warning(f"Initial webhook set warning: {e}")

    async def handle_index(request: web.Request) -> web.Response:
        host = request.headers.get("X-Forwarded-Host") or request.host
        if "127.0.0.1" not in host and "localhost" not in host:
            webhook_url = f"https://{host}/webhook"
            try:
                await tg_app.bot.set_webhook(url=webhook_url)
            except Exception:
                pass
        else:
            webhook_url = f"{external_url}/webhook"

        html = f"""
        <html>
        <head><meta charset="utf-8"><title>Telegram AI Agent Status</title></head>
        <body style="font-family: sans-serif; padding: 40px; text-align: center; background: #f4f7f6;">
            <div style="background: white; padding: 30px; border-radius: 12px; max-width: 600px; margin: auto; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
                <h1 style="color: #2ecc71;">✅ กัญญ์วรา ออนไลน์ 24 ชั่วโมงแล้วค่ะ!</h1>
                <p>เวลาประเทศไทยบนระบบ: <b>{agent.now_th().strftime('%Y-%m-%d %H:%M:%S')}</b></p>
                <code style="background: #eee; padding: 8px 12px; border-radius: 6px; display: inline-block;">{webhook_url}</code>
            </div>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def handle_health(request: web.Request) -> web.Response:
        return web.Response(text="ALIVE")

    async def handle_webhook(request: web.Request) -> web.Response:
        try:
            data = await request.json()
            update = Update.de_json(data, tg_app.bot)
            asyncio.create_task(tg_app.process_update(update))
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
