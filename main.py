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

async def send_safe_reply(update: Update, text: str):
    """ส่งข้อความอย่างปลอดภัย รองรับข้อความยาวเกิน 4000 ตัวอักษร"""
    if not text:
        text = "⚠️ ไม่ได้รับข้อความตอบกลับจากระบบ"
    max_len = 4000
    for i in range(0, len(text), max_len):
        chunk = text[i : i + max_len]
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(chunk)

def schedule_reminder_task(bot: Bot, user_id: int, seconds: int, reminder_text: str):
    """ตั้งเวลาส่งแจ้งเตือนจริงใน Background"""
    async def delayed_send():
        await asyncio.sleep(seconds)
        now_str = agent.now_th().strftime("%H:%M น.")
        await bot.send_message(
            chat_id=user_id,
            text=f"⏰ แจ้งเตือนความจำ! ({now_str})\n\n📌 {reminder_text}"
        )
    asyncio.create_task(delayed_send())

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งเริ่มต้น /start"""
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ ขออภัยครับ บอทนี้เปิดให้ใช้งานเฉพาะเจ้าของระบบเท่านั้น")
        return

    welcome_text = (
        f"👋 สวัสดีครับคุณ {user.first_name}!\n\n"
        "ผมคือ **AI Personal Assistant (กัญญ์วรา)** ผู้ช่วยส่วนตัวของคุณ\n\n"
        "✨ **สิ่งที่ผมช่วยคุณได้:**\n"
        "• 💬 พิมพ์คุย ถามคำถาม ค้นหาข้อมูลล่าสุดบนเน็ต (Web Search)\n"
        "• 📸 ส่งรูปภาพเข้ามาให้ดู วิเคราะห์สลิป หรืออ่านเอกสาร\n"
        "• 🎙️ ส่งข้อความเสียงมาคุยได้โดยตรง\n"
        "• 🎨 สั่งวาดรูป: `/draw <คำอธิบาย>` หรือพิมพ์ *'วาดรูป...'*\n"
        "• 📊 สร้างไฟล์ Excel: `/excel <หัวข้อ>` หรือพิมพ์ *'สร้างไฟล์ excel...'*\n"
        "• 🧠 สั่งจำข้อมูลถาวร: `/memo <ข้อมูลที่ต้องการให้จำ>`\n"
        "• ⏰ ตั้งเตือนความจำ: พิมพ์บอกเวลาได้เลย เช่น *'เตือนตอน 11.30 น. ประชุม'* หรือ `/remind <นาที> <ข้อความ>`\n"
        "• 🔄 เริ่มหัวข้อใหม่: `/reset`"
    )
    await send_safe_reply(update, welcome_text)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งล้างความจำ /reset"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    agent.reset_chat(user_id)
    await update.message.reply_text("🔄 ล้างความทรงจำบทสนทนาปัจจุบันเรียบร้อยแล้วครับ เริ่มเรื่องใหม่ได้เลย!")

async def memo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งบันทึกความจำถาวร /memo <ข้อความ>"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if not context.args:
        memories = agent.load_memories().get(str(user_id), [])
        if not memories:
            await update.message.reply_text("📝 ยังไม่มีบันทึกความจำถาวรครับ\nพิมพ์ `/memo <สิ่งที่ต้องการให้จำ>` เพื่อบันทึกได้เลย")
        else:
            mem_list = "\n".join(f"{i+1}. {m}" for i, m in enumerate(memories))
            await update.message.reply_text(f"🧠 รายการความจำถาวรของคุณ:\n\n{mem_list}")
        return

    note = " ".join(context.args)
    agent.save_memory(user_id, note)
    agent.reset_chat(user_id)
    await update.message.reply_text(f"✅ จดจำข้อมูลนี้ไว้ถาวรแล้วครับ:\n📌 \"{note}\"")

async def draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งสร้างภาพ /draw <prompt>"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if not context.args:
        await update.message.reply_text("💡 กรุณาระบุคำอธิบายภาพ เช่น `/draw แมวไซเบอร์พังก์ในเมืองอนาคต`")
        return

    prompt = " ".join(context.args)
    await handle_image_generation(update, prompt)

async def handle_image_generation(update: Update, prompt: str):
    """ฟังก์ชันช่วยสร้างรูปภาพแบบไม่บล็อก Event Loop"""
    await update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)
    status_msg = await update.message.reply_text(f"🎨 กำลังวาดภาพ: \"{prompt}\" สักครู่นะครับ...")

    image_bytes = await asyncio.to_thread(agent.generate_image, prompt)
    if image_bytes:
        try:
            await status_msg.delete()
        except Exception:
            pass
        await update.message.reply_photo(photo=io.BytesIO(image_bytes), caption=f"✨ ภาพจากคำสั่ง: {prompt}")
    else:
        await status_msg.edit_text("❌ ขออภัยครับ ไม่สามารถสร้างรูปภาพได้ในขณะนี้ กรุณาลองใหม่อีกครั้ง")

async def excel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งสร้างไฟล์ Excel /excel <หัวข้อ>"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if not context.args:
        await update.message.reply_text("💡 กรุณาระบุหัวข้อตาราง เช่น `/excel ตารางแผนออกกำลังกาย 7 วัน`")
        return

    topic = " ".join(context.args)
    await handle_excel_generation(update, topic)

async def handle_excel_generation(update: Update, topic: str):
    """ฟังก์ชันช่วยสร้างไฟล์ Excel และส่งกลับเข้าแชท"""
    await update.message.reply_chat_action(ChatAction.UPLOAD_DOCUMENT)
    status_msg = await update.message.reply_text(f"📊 กำลังสร้างไฟล์ Excel เรื่อง: \"{topic}\"...")

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
            caption=f"✅ ไฟล์ Excel: {topic}\n({summary})"
        )
    except Exception as e:
        logger.error(f"Excel generation error: {e}")
        await status_msg.edit_text(f"❌ ไม่สามารถสร้างไฟล์ Excel ได้: {str(e)}")

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """คำสั่งตั้งเตือนความจำ /remind <นาที> <ข้อความ>"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if len(context.args) < 2:
        await update.message.reply_text("💡 ใช้งานแบบนี้ครับ: `/remind 30 โทรหาลูกค้า` (ตัวเลขคือจำนวนนาที)")
        return

    try:
        minutes = float(context.args[0])
        reminder_text = " ".join(context.args[1:])
        seconds = int(minutes * 60)

        schedule_reminder_task(context.bot, user_id, seconds, reminder_text)
        target_dt = agent.now_th() + agent.timedelta(seconds=seconds)
        await update.message.reply_text(
            f"✅ ตั้งเวลาแจ้งเตือนจริงในระบบแล้วครับ!\n"
            f"📌 เรื่อง: \"{reminder_text}\"\n"
            f"🕒 เวลาที่จะเตือน: {target_dt.strftime('%H:%M น.')} (อีก {minutes:g} นาที)"
        )
    except ValueError:
        await update.message.reply_text("⚠️ กรุณาระบุจำนวนนาทีเป็นตัวเลขครับ เช่น `/remind 15 ทานยา`")

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
                await update.message.reply_text(f"🧠 จำไว้เรียบร้อยแล้วครับ:\n📌 \"{note}\"")
                return

    # 4. ตรวจจับคำสั่งตั้งเตือนความจำด้วยภาษาพูดธรรมชาติ (เช่น "เตือนตอน 11.15 น.", "อีก 10 นาทีเตือน...")
    if any(k in lower_text for k in ("เตือน", "ปลุก")):
        asyncio.create_task(update.message.reply_chat_action(ChatAction.TYPING))
        rem_info = await asyncio.to_thread(agent.extract_reminder_intent, text)
        if rem_info and rem_info.get("is_reminder"):
            seconds = int(rem_info["seconds_from_now"])
            task_name = rem_info.get("task") or text
            target_time_str = rem_info.get("target_time_str", "")
            schedule_reminder_task(context.bot, user_id, seconds, task_name)
            mins = round(seconds / 60, 1)
            await update.message.reply_text(
                f"⏰ ตั้งเวลาแจ้งเตือนในระบบเรียบร้อยแล้วครับ!\n"
                f"📌 เรื่อง: \"{task_name}\"\n"
                f"🕒 เวลาแจ้งเตือน: {target_time_str} (อีกประมาณ {mins:g} นาที)\n"
                f"(เวลาไทยปัจจุบัน: {agent.now_th().strftime('%H:%M น.')})"
            )
            return

    # 5. ตอบคำถามปกติด้วย Gemini + Search
    asyncio.create_task(update.message.reply_chat_action(ChatAction.TYPING))
    try:
        reply = await asyncio.to_thread(agent.ask_agent, user_id, text)
        await send_safe_reply(update, reply)
    except Exception as e:
        logger.error(f"Error handling text: {e}")
        await update.message.reply_text(f"⚠️ เกิดข้อผิดพลาดในการประมวลผล กรุณาลองใหม่อีกครั้งครับ ({type(e).__name__})")

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """จัดการรูปภาพที่ส่งเข้ามา"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    await update.message.reply_chat_action(ChatAction.TYPING)
    status_msg = await update.message.reply_text("🔍 กำลังดูและวิเคราะห์รูปภาพ...")

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
        await status_msg.edit_text(f"⚠️ ไม่สามารถวิเคราะห์ภาพได้: {str(e)}")

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """จัดการไฟล์เสียงที่ส่งเข้ามา"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    await update.message.reply_chat_action(ChatAction.TYPING)
    status_msg = await update.message.reply_text("🎧 กำลังฟังข้อความเสียงของคุณ...")

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
        await status_msg.edit_text(f"⚠️ ไม่สามารถประมวลผลเสียงได้: {str(e)}")

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

    # ตั้งค่า Webhook ตอนเริ่มเซิร์ฟเวอร์อัตโนมัติ
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
                <h1 style="color: #2ecc71;">✅ บอทออนไลน์ 24 ชั่วโมงแล้ว!</h1>
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

    # ระบบ Self-Ping ทุก 10 นาที เพื่อป้องกันไม่ให้ Render Free Tier หลับขณะรอแจ้งเตือน
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
