import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "900"))  # حداکثر ۱۵ دقیقه
MAX_FILE_MB = 49  # محدودیت آپلود Bot API حدود ۵۰ مگابایت است
CONCURRENT_DOWNLOADS = int(os.getenv("CONCURRENT_DOWNLOADS", "3"))
USER_COOLDOWN = int(os.getenv("USER_COOLDOWN_SECONDS", "5"))
DB_PATH = os.getenv("DB_PATH", "cache.db")
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "192")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("musicbot")

download_sem = asyncio.Semaphore(CONCURRENT_DOWNLOADS)
last_request: dict[int, float] = {}
URL_RE = re.compile(r"https?://\S+")


# ---------- کش (file_id تلگرام) ----------
def db_init() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                video_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                duration INTEGER,
                plays INTEGER DEFAULT 1
            )"""
        )


def cache_get(video_id: str):
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT file_id, title, artist, duration FROM cache WHERE video_id=?",
            (video_id,),
        ).fetchone()
        if row:
            db.execute("UPDATE cache SET plays = plays + 1 WHERE video_id=?", (video_id,))
        return row


def cache_put(video_id: str, file_id: str, title: str, artist: str, duration: int):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO cache (video_id, file_id, title, artist, duration, plays)"
            " VALUES (?, ?, ?, ?, ?, 1)"
            " ON CONFLICT(video_id) DO UPDATE SET"
            " file_id=excluded.file_id, title=excluded.title,"
            " artist=excluded.artist, duration=excluded.duration,"
            " plays = plays + 1",
            (video_id, file_id, title, artist, duration),
        )


def top_tracks(limit: int = 10):
    with sqlite3.connect(DB_PATH) as db:
        return db.execute(
            "SELECT title, artist, plays FROM cache ORDER BY plays DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ---------- yt-dlp ----------
def _search_sync(query: str, n: int = 5):
    opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    results = []
    for e in info.get("entries", []):
        if not e:
            continue
        results.append(
            {
                "id": e["id"],
                "title": e.get("title") or "Unknown",
                "duration": int(e.get("duration") or 0),
                "uploader": e.get("uploader") or e.get("channel") or "",
            }
        )
    return results


def _info_sync(url: str):
    opts = {"quiet": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _download_sync(video_id: str, workdir: str):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "writethumbnail": True,
        "max_filesize": MAX_FILE_MB * 1024 * 1024 * 3,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": AUDIO_BITRATE,
            },
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    return info


def fmt_duration(sec: int) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


# ---------- هندلرها ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 سلام! من ربات موزیکم.\n\n"
        "• /play اسم آهنگ یا لینک — جستجو و ارسال آهنگ\n"
        "• /top — پرطرفدارترین آهنگ‌ها\n"
        "• تو چت خصوصی کافیه فقط اسم آهنگ رو بفرستی\n\n"
        "منو تو گروهت اضافه کن و از /play استفاده کن."
    )


async def top_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = top_tracks()
    if not rows:
        await update.message.reply_text("هنوز آهنگی پخش نشده.")
        return
    lines = [f"{i}. {t} — {a} ({p}×)" for i, (t, a, p) in enumerate(rows, 1)]
    await update.message.reply_text("🔥 پرطرفدارترین‌ها:\n\n" + "\n".join(lines))


def rate_limited(user_id: int) -> bool:
    now = time.time()
    if now - last_request.get(user_id, 0) < USER_COOLDOWN:
        return True
    last_request[user_id] = now
    return False


async def handle_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    msg = update.effective_message
    user = update.effective_user
    if rate_limited(user.id):
        await msg.reply_text(f"⏳ لطفاً {USER_COOLDOWN} ثانیه صبر کن.")
        return

    await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    loop = asyncio.get_running_loop()

    m = URL_RE.search(query)
    try:
        if m:
            info = await loop.run_in_executor(None, _info_sync, m.group(0))
            await send_track(msg, ctx, info["id"], info)
            return
        results = await loop.run_in_executor(None, _search_sync, query)
    except Exception:
        log.exception("search failed")
        await msg.reply_text("❌ نتونستم پیداش کنم. یه اسم یا لینک دیگه امتحان کن.")
        return

    if not results:
        await msg.reply_text("❌ چیزی پیدا نشد.")
        return

    buttons = [
        [
            InlineKeyboardButton(
                f"{r['title'][:45]} ({fmt_duration(r['duration'])})",
                callback_data=f"dl:{r['id']}:{user.id}",
            )
        ]
        for r in results
    ]
    await msg.reply_text(
        f"🔎 نتایج «{query[:40]}»:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def play(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args).strip()
    if not query and update.message.reply_to_message:
        query = (update.message.reply_to_message.text or "").strip()
    if not query:
        await update.message.reply_text("استفاده: /play اسم آهنگ یا لینک")
        return
    await handle_query(update, ctx, query)


async def private_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_query(update, ctx, update.message.text.strip())


async def on_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, video_id, owner = q.data.split(":")
    if q.from_user.id != int(owner):
        await q.answer("این نتایج برای کس دیگه‌ست، خودت /play بزن 😉", show_alert=True)
        return
    await q.answer("در حال دانلود...")
    await q.edit_message_text("⬇️ در حال دانلود و آپلود...")
    await send_track(q.message, ctx, video_id, None, edit=True)


async def send_track(msg, ctx, video_id: str, info, edit: bool = False):
    chat_id = msg.chat_id

    async def finish(text: str | None = None):
        if edit or text is None:
            try:
                await msg.delete()
            except Exception:
                pass
        if text:
            await ctx.bot.send_message(chat_id, text)

    cached = cache_get(video_id)
    if cached:
        file_id, title, artist, duration = cached
        await ctx.bot.send_audio(
            chat_id, file_id, title=title, performer=artist, duration=duration,
            caption="🎧 @" + ctx.bot.username,
        )
        await finish()
        return

    loop = asyncio.get_running_loop()
    if info and (info.get("duration") or 0) > MAX_DURATION:
        await finish(f"⛔ آهنگ خیلی طولانیه (حداکثر {MAX_DURATION // 60} دقیقه).")
        return

    async with download_sem:
        workdir = tempfile.mkdtemp(prefix="music_")
        try:
            await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
            info = await loop.run_in_executor(None, _download_sync, video_id, workdir)
            if (info.get("duration") or 0) > MAX_DURATION:
                await finish(f"⛔ آهنگ خیلی طولانیه (حداکثر {MAX_DURATION // 60} دقیقه).")
                return

            mp3 = next(Path(workdir).glob("*.mp3"), None)
            if not mp3:
                await finish("❌ دانلود ناموفق بود.")
                return
            if mp3.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                await finish("⛔ حجم فایل از محدودیت تلگرام بیشتره.")
                return
            thumb = next(Path(workdir).glob("*.jpg"), None)

            title = info.get("track") or info.get("title") or "Unknown"
            artist = info.get("artist") or info.get("uploader") or ""
            duration = int(info.get("duration") or 0)

            await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
            with open(mp3, "rb") as f, (open(thumb, "rb") if thumb else open(os.devnull, "rb")) as t:
                sent = await ctx.bot.send_audio(
                    chat_id, f, title=title, performer=artist, duration=duration,
                    thumbnail=t if thumb else None,
                    caption="🎧 @" + ctx.bot.username,
                    read_timeout=120, write_timeout=120,
                )
            cache_put(video_id, sent.audio.file_id, title, artist, duration)
            await finish()
        except Exception:
            log.exception("download failed")
            await finish("❌ خطا در دانلود. دوباره امتحان کن.")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def main():
    db_init()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler(["play", "p", "music"], play))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^dl:"))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_text)
    )
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
