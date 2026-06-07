import os
import json
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import yt_dlp

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
PROFILES_FILE = "profiles.json"
SEEN_FILE = "seen_videos.json"
DOWNLOAD_DIR = "downloads"
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL", "15"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_profiles():
    return load_json(PROFILES_FILE, [])


def save_profiles(profiles):
    save_json(PROFILES_FILE, profiles)


def get_seen():
    return set(load_json(SEEN_FILE, []))


def save_seen(seen):
    save_json(SEEN_FILE, list(seen))


def download_video(url):
    ydl_opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "format": "best[ext=mp4]",
        "quiet": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = os.path.join(DOWNLOAD_DIR, f"{info['id']}.mp4")
            if os.path.exists(path):
                return path, info
        return None, None
    except Exception as e:
        logger.error(f"Error descargando {url}: {e}")
        return None, None


async def check_and_send():
    bot = Bot(token=BOT_TOKEN)
    seen = get_seen()
    profiles = get_profiles()

    if not profiles:
        logger.info("No hay perfiles configurados. Usa /add @usuario")
        return

    for profile in profiles:
        profile_url = f"https://www.tiktok.com/@{profile.strip()}"
        logger.info(f"Revisando @{profile}...")

        ydl_opts = {
            "extract_flat": True,
            "quiet": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                playlist = ydl.extract_info(profile_url, download=False)
        except Exception as e:
            logger.error(f"Error obteniendo videos de @{profile}: {e}")
            continue

        entries = playlist.get("entries") or []
        logger.info(f"  → {len(entries)} videos encontrados")

        for entry in entries[:5]:
            vid_id = entry.get("id")
            if not vid_id or vid_id in seen:
                continue

            video_url = entry.get("url") or f"https://www.tiktok.com/@{profile}/video/{vid_id}"
            logger.info(f"  Nuevo video: {vid_id}")

            path, info = download_video(video_url)
            if not path:
                continue

            file_size = os.path.getsize(path)
            caption = f"@{profile} - {info.get('title', '').strip() or 'Nuevo video'}"

            try:
                if file_size < 50 * 1024 * 1024:
                    with open(path, "rb") as f:
                        await bot.send_video(chat_id=CHAT_ID, video=f, caption=caption)
                else:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"{caption}\n⚠ Video demasiado grande (+50MB): {video_url}",
                    )
            except Exception as e:
                logger.error(f"Error enviando video {vid_id}: {e}")

            seen.add(vid_id)
            os.remove(path)
            await asyncio.sleep(1.5)

    save_seen(seen)
    logger.info("Revisión completada")


async def start(update: Update, context):
    await update.message.reply_text(
        "Bot TikTok → Telegram activo.\n\n"
        "Comandos:\n"
        "/add @usuario - Agregar perfil\n"
        "/remove @usuario - Quitar perfil\n"
        "/list - Ver perfiles monitoreados\n"
        "/check - Forzar revisión ahora"
    )


async def cmd_add(update: Update, context):
    if not context.args:
        await update.message.reply_text("Usa: /add @nombredeusuario")
        return
    username = context.args[0].lstrip("@")
    profiles = get_profiles()
    if username in profiles:
        await update.message.reply_text(f"@{username} ya está en la lista.")
        return
    profiles.append(username)
    save_profiles(profiles)
    await update.message.reply_text(f"✅ @{username} agregado. Se revisará cada {CHECK_INTERVAL_MINUTES} min.")


async def cmd_remove(update: Update, context):
    if not context.args:
        await update.message.reply_text("Usa: /remove @nombredeusuario")
        return
    username = context.args[0].lstrip("@")
    profiles = get_profiles()
    if username not in profiles:
        await update.message.reply_text(f"@{username} no está en la lista.")
        return
    profiles.remove(username)
    save_profiles(profiles)
    await update.message.reply_text(f"❌ @{username} eliminado.")


async def cmd_list(update: Update, context):
    profiles = get_profiles()
    if not profiles:
        await update.message.reply_text("No hay perfiles monitoreados. Usa /add @usuario")
        return
    text = "Perfiles monitoreados:\n" + "\n".join(f"• @{p}" for p in profiles)
    await update.message.reply_text(text)


async def cmd_check(update: Update, context):
    await update.message.reply_text("Revisando perfiles ahora...")
    await check_and_send()
    await update.message.reply_text("✅ Revisión completada.")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        logger.debug("Health: %s", format % args)


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server en puerto {port}")
    server.serve_forever()


async def on_start(app):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_and_send, "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.start()
    logger.info(f"Bot iniciado. Revisando cada {CHECK_INTERVAL_MINUTES} min.")


def main():
    if not BOT_TOKEN or CHAT_ID == 0:
        logger.error("Faltan variables de entorno: BOT_TOKEN y CHAT_ID")
        logger.error("Crea un archivo .env con:\nBOT_TOKEN=tu_token\nCHAT_ID=tu_chat_id")
        return

    http_thread = threading.Thread(target=run_health_server, daemon=True)
    http_thread.start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = (Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_start)
        .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))

    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
