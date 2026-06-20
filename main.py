import os
import asyncio
import logging
import re
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler
import yt_dlp

try:
    from curl_cffi import requests as curl_requests
    from yt_dlp.networking.impersonate import ImpersonateTarget
    IMPERSONATE_AVAILABLE = True
except ImportError:
    IMPERSONATE_AVAILABLE = False

from database import init_db, get_db, SessionLocal, Profile, Video

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logger.info(f"IMPERSONATE_AVAILABLE={IMPERSONATE_AVAILABLE}")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID") or "0")
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL", "30"))
bot_app = None
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
_check_lock = asyncio.Lock()


def get_ydl_opts(download=True):
    opts = {
        "quiet": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    if download:
        opts["outtmpl"] = f"{DOWNLOAD_DIR}/%(id)s.%(ext)s"
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        opts["noplaylist"] = True
    else:
        opts["extract_flat"] = True
        opts["playlistend"] = 10

    if IMPERSONATE_AVAILABLE:
        opts["impersonate"] = ImpersonateTarget(client="chrome")
    else:
        opts["http_headers"] = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    return opts


def download_video(url):
    ydl_opts = get_ydl_opts(download=True)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            vid_id = info["id"]
            for ext in ("mp4", "webm", "mkv"):
                path = os.path.join(DOWNLOAD_DIR, f"{vid_id}.{ext}")
                if os.path.exists(path):
                    return path, info
            return None, info
    except Exception as e:
        logger.error(f"Error descargando {url}: {e}")
    return None, None


def _scan_profile(profile_url):
    logger.info(f"  Escaneando {profile_url}...")
    ydl_opts = get_ydl_opts(download=False)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(profile_url, download=False)
        logger.info(f"  Escaneo completado: {len(result.get('entries', []))} entries" if result else "  Escaneo retornó None")
        return result


def _download_video_sync(url):
    return download_video(url)


async def check_and_send():
    if _check_lock.locked():
        logger.info("Check ya en progreso, saltando...")
        return
    async with _check_lock:
        bot = Bot(token=BOT_TOKEN)
        db = SessionLocal()
        try:
            profiles = db.query(Profile).filter(Profile.is_active == True).all()
            if not profiles:
                logger.info("No hay perfiles activos.")
                return

            for profile in profiles:
                profile_url = f"https://www.tiktok.com/@{profile.username}"
                logger.info(f"Revisando @{profile.username}...")

                try:
                    playlist = await asyncio.wait_for(
                        asyncio.to_thread(_scan_profile, profile_url),
                        timeout=180,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout revisando @{profile.username}")
                    try:
                        await bot.send_message(
                            chat_id=CHAT_ID,
                            text=f"Error revisando @{profile.username}: timeout (180s)",
                        )
                    except Exception:
                        pass
                    continue
                except Exception as e:
                    logger.error(f"Error obteniendo @{profile.username}: {type(e).__name__}: {e}")
                    try:
                        await bot.send_message(
                            chat_id=CHAT_ID,
                            text=f"Error revisando @{profile.username}: {type(e).__name__}: {e}",
                        )
                    except Exception:
                        pass
                    continue

                if playlist is None:
                    continue

                entries = (playlist.get("entries") or [])[:10]
                logger.info(f"  -> {len(entries)} videos")

                new_videos = 0
                for entry in entries:
                    vid_id = entry.get("id")
                    if not vid_id or db.query(Video).filter(Video.video_id == vid_id).first():
                        continue

                    video_url = entry.get("url") or f"https://www.tiktok.com/@{profile.username}/video/{vid_id}"
                    logger.info(f"  Nuevo: {vid_id}")

                    path, info = await asyncio.to_thread(_download_video_sync, video_url)
                    if not path:
                        continue

                    video = Video(
                        video_id=vid_id,
                        username=profile.username,
                        title=(info.get("title") or "Nuevo video").strip()[:500],
                        thumbnail_url=info.get("thumbnail"),
                        duration=info.get("duration"),
                        view_count=info.get("view_count"),
                        like_count=info.get("like_count"),
                        tiktok_url=video_url,
                    )
                    db.add(video)
                    profile.video_count += 1
                    db.commit()

                caption = f"@{profile.username} - {video.title}"
                sent_ok = False

                for attempt in range(3):
                    try:
                        file_size = os.path.getsize(path)
                        if file_size < 50 * 1024 * 1024:
                            with open(path, "rb") as f:
                                await bot.send_video(chat_id=CHAT_ID, video=f, caption=caption)
                        else:
                            await bot.send_message(chat_id=CHAT_ID, text=f"{caption}\n+50MB: {video_url}")
                        sent_ok = True
                        break
                    except Exception as e:
                        logger.warning(f"Intento {attempt + 1} fallido enviando {vid_id}: {e}")
                        if attempt < 2:
                            await asyncio.sleep(2)

                    if sent_ok:
                        os.remove(path)
                        new_videos += 1
                    else:
                        logger.warning(f"No se pudo enviar {vid_id} tras 3 intentos. Archivo conservado.")

                    await asyncio.sleep(1.5)

                profile.last_checked_at = datetime.utcnow()
                db.commit()
                logger.info(f"@{profile.username}: {new_videos} nuevos")
        finally:
            db.close()


async def cmd_start(update: Update, context):
    await update.message.reply_text(
        "Bot activo.\n/add @usuario | /remove @usuario | /list | /check\n/export | /import @user1 @user2"
    )

async def cmd_add(update: Update, context):
    if not context.args:
        await update.message.reply_text("Usa: /add @nombre")
        return
    username = context.args[0].lstrip("@").strip()
    db = SessionLocal()
    try:
        if db.query(Profile).filter(Profile.username == username).first():
            await update.message.reply_text(f"@{username} ya existe.")
            return
        db.add(Profile(username=username, is_active=True))
        db.commit()
        await update.message.reply_text(f"@{username} agregado.")
    finally:
        db.close()

async def cmd_remove(update: Update, context):
    if not context.args:
        await update.message.reply_text("Usa: /remove @nombre")
        return
    username = context.args[0].lstrip("@").strip()
    db = SessionLocal()
    try:
        p = db.query(Profile).filter(Profile.username == username).first()
        if not p:
            await update.message.reply_text(f"@{username} no existe.")
            return
        db.delete(p)
        db.commit()
        await update.message.reply_text(f"@{username} eliminado.")
    finally:
        db.close()

async def cmd_list(update: Update, context):
    db = SessionLocal()
    try:
        profiles = db.query(Profile).all()
        if not profiles:
            await update.message.reply_text("No hay perfiles. Usa /add @usuario")
            return
        text = "Perfiles:\n" + "\n".join(f"@{p.username} ({p.video_count} videos)" for p in profiles)
        await update.message.reply_text(text)
    finally:
        db.close()

async def cmd_check(update: Update, context):
    await update.message.reply_text("Revisando...")
    await check_and_send()
    await update.message.reply_text("Listo.")

async def cmd_export(update: Update, context):
    db = SessionLocal()
    try:
        profiles = db.query(Profile).all()
        if not profiles:
            await update.message.reply_text("No hay perfiles.")
            return
        text = "\n".join(f"@{p.username}" for p in profiles)
        await update.message.reply_text(text)
    finally:
        db.close()

async def cmd_import(update: Update, context):
    if context.args:
        raw = " ".join(context.args)
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        raw = update.message.reply_to_message.text
    else:
        await update.message.reply_text("Usa: /import @user1 @user2\nO responde a un mensaje con usernames.")
        return

    usernames = re.split(r"[\s,]+", raw)
    usernames = [u.lstrip("@").strip() for u in usernames if u.strip()]

    added = 0
    existing = 0
    db = SessionLocal()
    try:
        for uname in usernames:
            if not uname:
                continue
            if db.query(Profile).filter(Profile.username == uname).first():
                existing += 1
            else:
                db.add(Profile(username=uname, is_active=True))
                added += 1
        db.commit()
    finally:
        db.close()

    await update.message.reply_text(f"{added} agregados, {existing} ya existian")


# ── FastAPI ──────────────────────────────────────────
init_db()

app = FastAPI(title="TikTok Downloader Bot")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse("<h1>TikTok Downloader Bot</h1><p>API: /api/profiles /api/videos /api/stats /health</p>")

@app.get("/api/profiles")
def api_profiles(db: Session = Depends(get_db)):
    profiles = db.query(Profile).order_by(Profile.added_at.desc()).all()
    return [{"username": p.username, "display_name": p.display_name, "avatar_url": p.avatar_url,
             "video_count": p.video_count, "last_checked": p.last_checked_at.isoformat() if p.last_checked_at else None,
             "is_active": p.is_active} for p in profiles]

@app.post("/api/profiles")
def api_add_profile(username: str = Form(...), db: Session = Depends(get_db)):
    username = username.lstrip("@").strip()
    if db.query(Profile).filter(Profile.username == username).first():
        raise HTTPException(400, "Profile already exists")
    p = Profile(username=username, is_active=True)
    db.add(p)
    db.commit()
    return {"ok": True, "username": username}

@app.delete("/api/profiles/{username}")
def api_remove_profile(username: str, db: Session = Depends(get_db)):
    p = db.query(Profile).filter(Profile.username == username).first()
    if not p:
        raise HTTPException(404, "Profile not found")
    db.delete(p)
    db.commit()
    return {"ok": True}

@app.get("/api/videos")
def api_videos(username: str = None, limit: int = 50, db: Session = Depends(get_db)):
    query = db.query(Video).order_by(Video.sent_at.desc())
    if username:
        query = query.filter(Video.username == username)
    videos = query.limit(limit).all()
    return [{"video_id": v.video_id, "username": v.username, "title": v.title,
             "thumbnail_url": v.thumbnail_url, "duration": v.duration,
             "view_count": v.view_count, "like_count": v.like_count,
             "tiktok_url": v.tiktok_url, "sent_at": v.sent_at.isoformat() if v.sent_at else None} for v in videos]

@app.get("/api/stats")
def api_stats(db: Session = Depends(get_db)):
    total_profiles = db.query(Profile).count()
    active_profiles = db.query(Profile).filter(Profile.is_active == True).count()
    total_videos = db.query(Video).count()
    latest_video = db.query(Video).order_by(Video.sent_at.desc()).first()
    return {
        "total_profiles": total_profiles,
        "active_profiles": active_profiles,
        "total_videos": total_videos,
        "latest_video": latest_video.sent_at.isoformat() if latest_video else None,
    }

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Startup ──────────────────────────────────────────
async def start_bot():
    global bot_app

    if not BOT_TOKEN or CHAT_ID == 0:
        logger.warning("BOT_TOKEN o CHAT_ID no configurados. Bot no iniciara.")
        return

    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("add", cmd_add))
    bot_app.add_handler(CommandHandler("remove", cmd_remove))
    bot_app.add_handler(CommandHandler("list", cmd_list))
    bot_app.add_handler(CommandHandler("check", cmd_check))
    bot_app.add_handler(CommandHandler("export", cmd_export))
    bot_app.add_handler(CommandHandler("import", cmd_import))

    logger.info("Bot iniciado (polling)")
    await bot_app.initialize()
    await bot_app.start()

    webhook_url = os.environ.get("RENDER_EXTERNAL_URL")
    webhook_path = f"/webhook/{BOT_TOKEN}"
    if webhook_url:
        full_url = f"{webhook_url.rstrip('/')}{webhook_path}"
        await bot_app.bot.set_webhook(url=full_url, drop_pending_updates=True)
        logger.info(f"Webhook configurado: {full_url}")
    else:
        await bot_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Polling iniciado (sin RENDER_EXTERNAL_URL)")

    await check_and_send()
    while True:
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
        await check_and_send()


@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    if bot_app is None:
        raise HTTPException(status_code=503, detail="Bot not initialized yet")
    update = Update.de_json(await request.json(), bot_app.bot)
    await bot_app.process_update(update)
    return JSONResponse(content={"ok": True})


@app.on_event("startup")
async def startup_event():
    global bot_app
    asyncio.create_task(start_bot())
