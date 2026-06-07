import os
import json
import asyncio
import logging
import secrets
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Depends, HTTPException, status, Request, Form, Header
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler
import yt_dlp

from database import init_db, get_db, SessionLocal, Profile, Video

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "admin123")
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL", "15"))
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def check_auth(password: str = Header(default="")):
    if not secrets.compare_digest(password, WEB_PASSWORD):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid password")
    return True


def migrate_json_to_db():
    db = SessionLocal()
    try:
        if os.path.exists("profiles.json"):
            usernames = json.load(open("profiles.json"))
            for u in usernames:
                if not db.query(Profile).filter(Profile.username == u).first():
                    db.add(Profile(username=u, is_active=True))
            db.commit()
            os.rename("profiles.json", "profiles.json.bak")
    except Exception as e:
        logger.warning(f"Migration skipped: {e}")
    finally:
        db.close()


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
    except Exception as e:
        logger.error(f"Error descargando {url}: {e}")
    return None, None


async def check_and_send():
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
                with yt_dlp.YoutubeDL({"extract_flat": True, "quiet": True}) as ydl:
                    playlist = ydl.extract_info(profile_url, download=False)
            except Exception as e:
                logger.error(f"Error obteniendo @{profile.username}: {e}")
                continue

            entries = (playlist.get("entries") or [])[:10]
            logger.info(f"  → {len(entries)} videos")

            new_videos = 0
            for entry in entries:
                vid_id = entry.get("id")
                if not vid_id or db.query(Video).filter(Video.video_id == vid_id).first():
                    continue

                video_url = entry.get("url") or f"https://www.tiktok.com/@{profile.username}/video/{vid_id}"
                logger.info(f"  Nuevo: {vid_id}")

                path, info = download_video(video_url)
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
                file_size = os.path.getsize(path)
                try:
                    if file_size < 50 * 1024 * 1024:
                        with open(path, "rb") as f:
                            await bot.send_video(chat_id=CHAT_ID, video=f, caption=caption)
                    else:
                        await bot.send_message(chat_id=CHAT_ID, text=f"{caption}\n⚠ +50MB: {video_url}")
                except Exception as e:
                    logger.error(f"Error enviando {vid_id}: {e}")

                os.remove(path)
                new_videos += 1
                await asyncio.sleep(1.5)

            profile.last_checked_at = datetime.utcnow()
            db.commit()
            logger.info(f"@{profile.username}: {new_videos} nuevos")
    finally:
        db.close()


async def cmd_start(update: Update, context):
    await update.message.reply_text(
        "Bot activo.\n/add @usuario | /remove @usuario | /list | /check"
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
        await update.message.reply_text(f"✅ @{username} agregado.")
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
        await update.message.reply_text(f"❌ @{username} eliminado.")
    finally:
        db.close()

async def cmd_list(update: Update, context):
    db = SessionLocal()
    try:
        profiles = db.query(Profile).all()
        if not profiles:
            await update.message.reply_text("No hay perfiles. Usa /add @usuario")
            return
        text = "Perfiles:\n" + "\n".join(f"• @{p.username} ({p.video_count} videos)" for p in profiles)
        await update.message.reply_text(text)
    finally:
        db.close()

async def cmd_check(update: Update, context):
    await update.message.reply_text("Revisando...")
    await check_and_send()
    await update.message.reply_text("✅ Listo.")


# ── FastAPI ──────────────────────────────────────────
init_db()
migrate_json_to_db()

app = FastAPI(title="TikTok Downloader Bot")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return FileResponse("templates/index.html")

@app.get("/api/profiles")
def api_profiles(db: Session = Depends(get_db)):
    profiles = db.query(Profile).order_by(Profile.added_at.desc()).all()
    return [{"username": p.username, "display_name": p.display_name, "avatar_url": p.avatar_url,
             "video_count": p.video_count, "last_checked": p.last_checked_at.isoformat() if p.last_checked_at else None,
             "is_active": p.is_active} for p in profiles]

@app.post("/api/profiles")
def api_add_profile(username: str = Form(...), password: str = Header(default=""), db: Session = Depends(get_db)):
    check_auth(password)
    username = username.lstrip("@").strip()
    if db.query(Profile).filter(Profile.username == username).first():
        raise HTTPException(400, "Profile already exists")
    p = Profile(username=username, is_active=True)
    db.add(p)
    db.commit()
    return {"ok": True, "username": username}

@app.delete("/api/profiles/{username}")
def api_remove_profile(username: str, password: str = Header(default=""), db: Session = Depends(get_db)):
    check_auth(password)
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
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_and_send, "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.start()

    if not BOT_TOKEN or CHAT_ID == 0:
        logger.warning("BOT_TOKEN o CHAT_ID no configurados. Bot no iniciará.")
        return

    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("add", cmd_add))
    bot_app.add_handler(CommandHandler("remove", cmd_remove))
    bot_app.add_handler(CommandHandler("list", cmd_list))
    bot_app.add_handler(CommandHandler("check", cmd_check))

    logger.info("Bot iniciado (polling)")
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(start_bot())
