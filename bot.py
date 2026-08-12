import os
import re
import time
import uuid
import asyncio
import logging
import threading
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
import yt_dlp


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

PORT = int(os.getenv("PORT", "10000"))
MAX_AUDIO_MB = int(os.getenv("MAX_AUDIO_MB", "49"))

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_ROOT = BASE_DIR / "downloads"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# Optional:
# YOUTUBE_COOKIES_FILE=/path/to/cookies.txt
COOKIE_ENV = os.getenv("YOUTUBE_COOKIES_FILE")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("resso")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")


# ============================================================
# FLASK KEEP-ALIVE
# ============================================================

flask_app = Flask(__name__)


@flask_app.get("/")
def home():
    return "Resso bot is running."


@flask_app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "resso-bot",
            "time": int(time.time()),
        }
    )


def run_web_server():
    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
        use_reloader=False,
    )


# ============================================================
# BUTTONS
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏭️ Skip", callback_data="skip"),
                InlineKeyboardButton("⏹️ Stop", callback_data="stop"),
            ],
            [
                InlineKeyboardButton("📋 Queue", callback_data="queue"),
                InlineKeyboardButton("🎵 Now", callback_data="now"),
            ],
            [
                InlineKeyboardButton("ℹ️ Help", callback_data="help"),
                InlineKeyboardButton("📊 Status", callback_data="status"),
            ],
        ]
    )


# ============================================================
# JOB MODEL
# ============================================================

@dataclass
class SongJob:
    chat_id: int
    query: str
    requested_by: str
    job_id: str


@dataclass
class CurrentJob:
    job: SongJob
    title: str
    cancel_event: threading.Event
    stop_mode: bool = False


song_queue: asyncio.Queue[SongJob] = asyncio.Queue()

current_job: Optional[CurrentJob] = None
current_lock = threading.Lock()

skip_requested = False
stop_requested = False

worker_task: Optional[asyncio.Task] = None


# ============================================================
# HELPERS
# ============================================================

def is_url(text: str) -> bool:
    try:
        p = urlparse(text.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    return title[:180] if title else "Unknown"


def human_size(num_bytes: int) -> str:
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def find_cookie_file() -> Optional[str]:
    if COOKIE_ENV:
        p = Path(COOKIE_ENV).expanduser()
        if p.is_file():
            return str(p)

    candidates = [
        BASE_DIR / "cookies.txt",
        BASE_DIR / "youtube_cookies.txt",
        BASE_DIR / "youtube.cookies.txt",
        BASE_DIR / "cookies" / "youtube.txt",
    ]

    for p in candidates:
        if p.is_file():
            return str(p)

    return None


def shutil_which(name: str) -> Optional[str]:
    import shutil
    return shutil.which(name)


def get_deno_path() -> Optional[str]:
    try:
        import deno

        path = deno.find_deno_bin()
        if path and Path(path).exists():
            return str(path)
    except Exception:
        pass

    for candidate in ("deno", "deno.exe"):
        path = shutil_which(candidate)
        if path:
            return path

    return None


def get_ffmpeg_path() -> Optional[str]:
    try:
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and Path(path).exists():
            return str(path)
    except Exception:
        pass

    for candidate in ("ffmpeg", "ffmpeg.exe"):
        path = shutil_which(candidate)
        if path:
            return path

    return None


def media_files(workdir: Path):
    ignored_exts = {
        ".part",
        ".ytdl",
        ".json",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }

    result = []

    for p in workdir.rglob("*"):
        if not p.is_file():
            continue

        if p.suffix.lower() in ignored_exts:
            continue

        if p.name.endswith(".part"):
            continue

        try:
            size = p.stat().st_size
        except OSError:
            continue

        if size < 1024:
            continue

        result.append(p)

    result.sort(
        key=lambda x: x.stat().st_size,
        reverse=True,
    )
    return result


# ============================================================
# YOUTUBE SEARCH / INFO
# ============================================================

def resolve_video(query: str):
    target = query.strip()

    if not is_url(target):
        target = f"ytsearch1:{target}"

    cookie_file = find_cookie_file()
    deno_path = get_deno_path()

    opts = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
        "retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        },
    }

    if cookie_file:
        opts["cookiefile"] = cookie_file
        logger.info("YouTube cookies enabled.")

    if deno_path:
        opts["js_runtimes"] = {"deno": deno_path}
        logger.info("Deno JS runtime: %s", deno_path)
    else:
        logger.warning(
            "Deno JS runtime not found. Full YouTube support may fail."
        )

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)

        if info is None:
            raise RuntimeError("YouTube returned no result.")

        if info.get("entries"):
            entries = [e for e in info.get("entries", []) if e]
            if not entries:
                raise RuntimeError("No YouTube result found.")

            info = entries[0]

            video_url = (
                info.get("webpage_url")
                or info.get("url")
            )

            if video_url and (
                info.get("formats") is None
                or info.get("_type") == "url"
            ):
                info = ydl.extract_info(
                    video_url,
                    download=False,
                )

    video_url = (
        info.get("webpage_url")
        or info.get("original_url")
    )

    if not video_url:
        video_id = info.get("id")
        if video_id:
            video_url = (
                f"https://www.youtube.com/watch?v={video_id}"
            )

    if not video_url:
        raise RuntimeError(
            "Could not resolve the YouTube video URL."
        )

    return {
        "url": video_url,
        "title": clean_title(info.get("title")),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": clean_title(info.get("uploader")),
        "id": info.get("id"),
    }


# ============================================================
# YOUTUBE DOWNLOAD
# ============================================================

def download_audio(
    query: str,
    workdir: Path,
    cancel_event: threading.Event,
):
    """
    Download source audio with yt-dlp.

    Then convert it to MP3 with the ffmpeg binary supplied by
    imageio-ffmpeg.

    The final filename is found by scanning the directory rather
    than assuming a YouTube title-based filename. This fixes the
    old "Downloaded audio file not found" problem.
    """

    workdir.mkdir(parents=True, exist_ok=True)

    for p in workdir.rglob("*"):
        if p.is_file():
            try:
                p.unlink()
            except Exception:
                pass

    cookie_file = find_cookie_file()
    deno_path = get_deno_path()

    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 5,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        },
    }

    if cookie_file:
        opts["cookiefile"] = cookie_file

    if deno_path:
        opts["js_runtimes"] = {"deno": deno_path}

    def progress_hook(status):
        if cancel_event.is_set():
            raise yt_dlp.utils.DownloadError(
                "Download cancelled by user."
            )

        if status.get("status") == "downloading":
            downloaded = status.get("downloaded_bytes", 0)
            total = (
                status.get("total_bytes")
                or status.get("total_bytes_estimate")
                or 0
            )

            if total:
                percent = downloaded * 100 / total
                logger.info(
                    "Download %.1f%% (%s/%s)",
                    percent,
                    human_size(downloaded),
                    human_size(total),
                )

    opts["progress_hooks"] = [progress_hook]

    try:
        logger.info("Starting yt-dlp download: %s", query)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                query,
                download=True,
            )

        if cancel_event.is_set():
            raise RuntimeError("Download cancelled by user.")

        candidates = media_files(workdir)

        if not candidates:
            raise RuntimeError(
                "yt-dlp finished but no downloaded media file "
                "was found."
            )

        source = candidates[0]

        logger.info(
            "Downloaded source: %s (%s)",
            source,
            human_size(source.stat().st_size),
        )

        ffmpeg = get_ffmpeg_path()

        if not ffmpeg:
            logger.warning(
                "FFmpeg not found; returning original media file."
            )
            return source, clean_title(info.get("title"))

        if cancel_event.is_set():
            raise RuntimeError("Download cancelled by user.")

        output = workdir / "audio.mp3"

        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "192k",
            str(output),
        ]

        logger.info("Converting with FFmpeg: %s", output)

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                "FFmpeg conversion failed: "
                + (
                    proc.stderr[-1200:]
                    or "unknown FFmpeg error"
                )
            )

        if not output.is_file():
            raise RuntimeError(
                "FFmpeg completed but audio.mp3 "
                "was not created."
            )

        if output.stat().st_size < 1024:
            raise RuntimeError(
                "FFmpeg created an empty/invalid audio file."
            )

        try:
            source.unlink()
        except Exception:
            pass

        logger.info(
            "Final audio: %s (%s)",
            output,
            human_size(output.stat().st_size),
        )

        return output, clean_title(info.get("title"))

    except Exception as exc:
        logger.exception("YouTube download failed")
        raise RuntimeError(str(exc)) from exc


# ============================================================
# QUEUE DISPLAY
# ============================================================

async def queue_text():
    items = list(song_queue._queue)

    with current_lock:
        cj = current_job

    lines = []

    if cj:
        lines.append(f"🎵 Now: {cj.title}")
    else:
        lines.append("🎵 Now: Nothing")

    if not items:
        lines.append("\n📋 Queue is empty.")
    else:
        lines.append("\n📋 Queue:")
        for i, item in enumerate(items, 1):
            lines.append(
                f"{i}. {clean_title(item.query)[:80]}"
            )

    return "\n".join(lines)


# ============================================================
# WORKER
# ============================================================

async def process_one(
    application: Application,
    job: SongJob,
):
    global current_job
    global skip_requested
    global stop_requested

    cancel_event = threading.Event()

    await application.bot.send_chat_action(
        chat_id=job.chat_id,
        action=ChatAction.TYPING,
    )

    await application.bot.send_message(
        chat_id=job.chat_id,
        text=(
            f"⏳ Downloading...\n"
            f"🎵 {clean_title(job.query)}"
        ),
        reply_markup=main_keyboard(),
    )

    try:
        info = await asyncio.to_thread(
            resolve_video,
            job.query,
        )

        title = info["title"]

        with current_lock:
            current_job = CurrentJob(
                job=job,
                title=title,
                cancel_event=cancel_event,
            )

        await application.bot.send_message(
            chat_id=job.chat_id,
            text=f"🎵 Starting:\n{title}",
            reply_markup=main_keyboard(),
        )

        workdir = DOWNLOAD_ROOT / job.job_id

        audio_file, final_title = await asyncio.to_thread(
            download_audio,
            info["url"],
            workdir,
            cancel_event,
        )

        if cancel_event.is_set():
            raise RuntimeError("Skipped/stopped by user.")

        if stop_requested or skip_requested:
            raise RuntimeError("Skipped/stopped by user.")

        size = audio_file.stat().st_size

        if size > MAX_AUDIO_MB * 1024 * 1024:
            raise RuntimeError(
                "Audio is too large for Telegram upload "
                f"({human_size(size)})."
            )

        await application.bot.send_chat_action(
            chat_id=job.chat_id,
            action=ChatAction.UPLOAD_AUDIO,
        )

        with open(audio_file, "rb") as audio:
            await application.bot.send_audio(
                chat_id=job.chat_id,
                audio=audio,
                title=final_title[:64],
                performer="Resso Bot",
                caption=f"🎵 {final_title}",
                reply_markup=main_keyboard(),
            )

        logger.info(
            "Sent audio to chat %s: %s",
            job.chat_id,
            final_title,
        )

    except Exception as exc:
        error_text = str(exc).strip() or "Unknown error."

        await application.bot.send_message(
            chat_id=job.chat_id,
            text=(
                "❌ YouTube download failed.\n\n"
                f"🎵 {clean_title(job.query)}\n\n"
                "ℹ️ Technical error:\n"
                f"{error_text[-1400:]}"
            ),
            reply_markup=main_keyboard(),
        )

    finally:
        with current_lock:
            current_job = None

        workdir = DOWNLOAD_ROOT / job.job_id

        try:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


async def queue_worker(application: Application):
    global skip_requested
    global stop_requested

    logger.info("Queue worker started.")

    while True:
        job = await song_queue.get()

        try:
            skip_requested = False

            if stop_requested:
                stop_requested = False
                continue

            await process_one(
                application,
                job,
            )

        except asyncio.CancelledError:
            raise

        finally:
            song_queue.task_done()

            if stop_requested:
                while not song_queue.empty():
                    try:
                        song_queue.get_nowait()
                        song_queue.task_done()
                    except asyncio.QueueEmpty:
                        break

                stop_requested = False


# ============================================================
# COMMANDS
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🎵 Resso Bot\n\n"
        "Use:\n"
        "/play Kesariya\n"
        "/play https://youtube.com/watch?v=...\n\n"
        "Commands:\n"
        "/queue - show queue\n"
        "/now - current song\n"
        "/skip - skip current\n"
        "/stop - stop and clear queue\n"
        "/status - bot status\n"
        "/help - help",
        reply_markup=main_keyboard(),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "ℹ️ Help\n\n"
        "🎵 /play <song name or YouTube URL>\n"
        "📋 /queue - queue\n"
        "🎵 /now - current song\n"
        "⏭️ /skip - skip current\n"
        "⏹️ /stop - stop and clear queue\n"
        "📊 /status - status",
        reply_markup=main_keyboard(),
    )


async def play(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "❌ Usage:\n"
            "/play Kesariya\n\n"
            "or\n\n"
            "/play https://youtube.com/watch?v=...",
            reply_markup=main_keyboard(),
        )
        return

    user = update.effective_user

    username = (
        f"@{user.username}"
        if user and user.username
        else (
            user.full_name
            if user
            else "Unknown"
        )
    )

    job = SongJob(
        chat_id=update.effective_chat.id,
        query=text,
        requested_by=username,
        job_id=uuid.uuid4().hex,
    )

    await song_queue.put(job)

    await update.message.reply_text(
        f"✅ Added to queue:\n"
        f"🎵 {clean_title(text)}\n\n"
        f"📋 Position: {song_queue.qsize()}",
        reply_markup=main_keyboard(),
    )


async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        await queue_text(),
        reply_markup=main_keyboard(),
    )


async def now_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    with current_lock:
        cj = current_job

    await update.message.reply_text(
        (
            f"🎵 Now:\n{cj.title}"
            if cj
            else "🎵 Nothing is playing/downloading right now."
        ),
        reply_markup=main_keyboard(),
    )


async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global skip_requested

    with current_lock:
        cj = current_job

    if not cj:
        try:
            removed = song_queue.get_nowait()
            song_queue.task_done()

            await update.message.reply_text(
                "⏭️ Skipped queued item:\n"
                f"{clean_title(removed.query)}",
                reply_markup=main_keyboard(),
            )
        except asyncio.QueueEmpty:
            await update.message.reply_text(
                "ℹ️ Nothing to skip.",
                reply_markup=main_keyboard(),
            )
        return

    skip_requested = True
    cj.cancel_event.set()

    await update.message.reply_text(
        f"⏭️ Skipping:\n{cj.title}",
        reply_markup=main_keyboard(),
    )


async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global stop_requested
    global skip_requested

    stop_requested = True
    skip_requested = False

    with current_lock:
        cj = current_job

    if cj:
        cj.stop_mode = True
        cj.cancel_event.set()

    removed = 0

    while True:
        try:
            song_queue.get_nowait()
            song_queue.task_done()
            removed += 1
        except asyncio.QueueEmpty:
            break

    await update.message.reply_text(
        f"⏹️ Stopped.\n"
        f"🗑️ Removed from queue: {removed}",
        reply_markup=main_keyboard(),
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    with current_lock:
        cj = current_job

    cookie = find_cookie_file()
    deno = get_deno_path()
    ffmpeg = get_ffmpeg_path()

    await update.message.reply_text(
        "📊 Status\n\n"
        "🤖 Bot: Online\n"
        f"🎵 Current: {cj.title if cj else 'Nothing'}\n"
        f"📋 Queue: {song_queue.qsize()}\n"
        f"🍪 Cookies: {'Yes' if cookie else 'No'}\n"
        f"🦕 Deno: {'Yes' if deno else 'No'}\n"
        f"🎞️ FFmpeg: {'Yes' if ffmpeg else 'No'}\n"
        f"🔧 yt-dlp: {yt_dlp.version.__version__}",
        reply_markup=main_keyboard(),
    )


# ============================================================
# CALLBACK BUTTONS
# ============================================================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global skip_requested
    global stop_requested

    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "skip":
        with current_lock:
            cj = current_job

        if cj:
            skip_requested = True
            cj.cancel_event.set()

            await query.message.reply_text(
                f"⏭️ Skipping:\n{cj.title}",
                reply_markup=main_keyboard(),
            )
        else:
            await query.message.reply_text(
                "ℹ️ Nothing is currently playing.",
                reply_markup=main_keyboard(),
            )

    elif data == "stop":
        stop_requested = True
        skip_requested = False

        with current_lock:
            cj = current_job

        if cj:
            cj.stop_mode = True
            cj.cancel_event.set()

        removed = 0

        while True:
            try:
                song_queue.get_nowait()
                song_queue.task_done()
                removed += 1
            except asyncio.QueueEmpty:
                break

        await query.message.reply_text(
            f"⏹️ Stopped.\n"
            f"🗑️ Queue cleared: {removed}",
            reply_markup=main_keyboard(),
        )

    elif data == "queue":
        await query.message.reply_text(
            await queue_text(),
            reply_markup=main_keyboard(),
        )

    elif data == "now":
        with current_lock:
            cj = current_job

        await query.message.reply_text(
            (
                f"🎵 Now:\n{cj.title}"
                if cj
                else "🎵 Nothing is playing/downloading."
            ),
            reply_markup=main_keyboard(),
        )

    elif data == "help":
        await query.message.reply_text(
            "ℹ️ Help\n\n"
            "/play <song name or YouTube URL>\n"
            "/queue\n"
            "/now\n"
            "/skip\n"
            "/stop\n"
            "/status",
            reply_markup=main_keyboard(),
        )

    elif data == "status":
        await status_command(update, context)


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def post_init(application: Application):
    global worker_task

    logger.info(
        "Starting Resso bot | yt-dlp=%s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "Deno=%s | FFmpeg=%s | Cookies=%s",
        get_deno_path(),
        get_ffmpeg_path(),
        find_cookie_file(),
    )

    worker_task = asyncio.create_task(
        queue_worker(application)
    )


async def post_shutdown(application: Application):
    global worker_task

    if worker_task:
        worker_task.cancel()

        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        worker_task = None


def main():
    threading.Thread(
        target=run_web_server,
        daemon=True,
        name="flask-health",
    ).start()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )
    application.add_handler(
        CommandHandler("help", help_command)
    )
    application.add_handler(
        CommandHandler("play", play)
    )
    application.add_handler(
        CommandHandler("queue", queue_command)
    )
    application.add_handler(
        CommandHandler("now", now_command)
    )
    application.add_handler(
        CommandHandler("skip", skip_command)
    )
    application.add_handler(
        CommandHandler("stop", stop_command)
    )
    application.add_handler(
        CommandHandler("status", status_command)
    )

    application.add_handler(
        CallbackQueryHandler(callbacks)
    )

    logger.info("Resso bot starting polling...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
