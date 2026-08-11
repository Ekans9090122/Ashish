import os
import asyncio
import logging
import tempfile
import threading
import shutil
import subprocess
from pathlib import Path
from collections import defaultdict, deque

from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

import yt_dlp


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

PORT = int(os.environ.get("PORT", "10000"))

# Render automatically provides this for web services.
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()

# Optional custom webhook secret.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

# Telegram upload safety target.
TARGET_MB = 40
MAX_MB = 45

TARGET_BYTES = TARGET_MB * 1024 * 1024
MAX_BYTES = MAX_MB * 1024 * 1024

COOKIE_SECRET_PATH = Path("/etc/secrets/youtube_cookies.txt")
COOKIE_TEMP_PATH = Path("/tmp/youtube_cookies.txt")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("Resso")


# =========================================================
# FLASK
# =========================================================

flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def home():
    return "Resso Bot is running."


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "bot": "Resso",
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "yt_dlp": True,
        }
    )


# =========================================================
# GLOBAL BOT STATE
# =========================================================

application = None
bot_loop = None

queues = defaultdict(deque)
current_tasks = {}
chat_locks = defaultdict(asyncio.Lock)


# =========================================================
# COOKIE SETUP
# =========================================================

def prepare_cookies():
    """
    Render Secret File:
    /etc/secrets/youtube_cookies.txt

    We copy it to /tmp so yt-dlp can use it safely.
    """

    try:
        if COOKIE_SECRET_PATH.exists():
            shutil.copy2(
                COOKIE_SECRET_PATH,
                COOKIE_TEMP_PATH,
            )

            logger.info(
                "YouTube cookies loaded from Render Secret File."
            )

            return str(COOKIE_TEMP_PATH)

        logger.warning(
            "YouTube cookies not found at %s",
            COOKIE_SECRET_PATH,
        )

    except Exception as e:
        logger.exception("Cookie setup failed: %s", e)

    return None


# =========================================================
# FFMPEG
# =========================================================

def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def get_duration(file_path):
    """
    Get audio duration using ffprobe.
    """

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )

        value = result.stdout.strip()

        if value:
            return float(value)

    except Exception as e:
        logger.warning("Duration detection failed: %s", e)

    return 0.0


def calculate_bitrate(duration):
    """
    Calculate an MP3 bitrate which should keep the output
    comfortably below 40 MB.

    We intentionally target ~39 MB instead of 40 MB.
    """

    if duration <= 0:
        return 128

    safe_bytes = 39 * 1024 * 1024

    # bits/sec -> kbps
    bitrate = int(
        (safe_bytes * 8) / duration / 1000
    )

    # Keep reasonable audio quality while respecting size.
    bitrate = min(bitrate, 192)
    bitrate = max(bitrate, 32)

    return bitrate


def compress_audio(input_file):
    """
    Convert audio to MP3 and keep it below the target.
    Returns compressed file path.
    """

    input_file = Path(input_file)

    original_size = input_file.stat().st_size

    logger.info(
        "Original audio size: %.2f MB",
        original_size / 1024 / 1024,
    )

    # Already safely below target.
    if original_size <= TARGET_BYTES:
        logger.info("Audio already below target. No compression needed.")
        return input_file

    if not ffmpeg_available():
        raise RuntimeError("FFmpeg is not installed.")

    duration = get_duration(input_file)

    bitrate = calculate_bitrate(duration)

    logger.info(
        "Audio duration: %.2f seconds | Initial bitrate: %s kbps",
        duration,
        bitrate,
    )

    output_file = input_file.with_name(
        input_file.stem + "_compressed.mp3"
    )

    def run_ffmpeg(kbps):
        if output_file.exists():
            try:
                output_file.unlink()
            except Exception:
                pass

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_file),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            f"{kbps}k",
            "-map_metadata",
            "-1",
            str(output_file),
        ]

        logger.info(
            "Running FFmpeg at %s kbps",
            kbps,
        )

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
        )

        if result.returncode != 0:
            logger.error(
                "FFmpeg error: %s",
                result.stderr[-3000:],
            )
            raise RuntimeError(
                "FFmpeg compression failed."
            )

        return output_file

    # Try progressively lower bitrates if needed.
    attempts = [
        bitrate,
        min(bitrate, 128),
        min(bitrate, 96),
        min(bitrate, 64),
        48,
        40,
        32,
    ]

    tried = set()

    for kbps in attempts:

        if kbps in tried:
            continue

        tried.add(kbps)

        run_ffmpeg(kbps)

        if not output_file.exists():
            continue

        size = output_file.stat().st_size

        logger.info(
            "Compressed size at %s kbps: %.2f MB",
            kbps,
            size / 1024 / 1024,
        )

        if size <= TARGET_BYTES:
            logger.info(
                "Compression successful: %.2f MB",
                size / 1024 / 1024,
            )

            try:
                input_file.unlink()
            except Exception:
                pass

            return output_file

    # Last check.
    if output_file.exists():

        size = output_file.stat().st_size

        if size <= MAX_BYTES:
            logger.warning(
                "File is under Telegram maximum but above target: %.2f MB",
                size / 1024 / 1024,
            )

            try:
                input_file.unlink()
            except Exception:
                pass

            return output_file

    raise RuntimeError(
        "Audio could not be compressed below 45 MB."
    )


# =========================================================
# BUTTONS
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏭️ Skip",
                    callback_data="skip",
                ),
                InlineKeyboardButton(
                    "⏹️ Stop",
                    callback_data="stop",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Queue",
                    callback_data="queue",
                ),
                InlineKeyboardButton(
                    "🎵 Now",
                    callback_data="now",
                ),
            ],
            [
                InlineKeyboardButton(
                    "ℹ️ Help",
                    callback_data="help",
                ),
                InlineKeyboardButton(
                    "📊 Status",
                    callback_data="status",
                ),
            ],
        ]
    )


# =========================================================
# HELP
# =========================================================

HELP_TEXT = """
🎵 Resso Bot

Commands:

/play <YouTube URL or search>
/queue
/now
/skip
/stop
/remove
/clear
/status

Buttons:

⏭️ Skip - current song skip
⏹️ Stop - stop current download
📋 Queue - queue list
🎵 Now - current song
ℹ️ Help - help
📊 Status - bot status

Audio files are automatically compressed to stay below
the configured Telegram upload limit.
"""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        HELP_TEXT,
        reply_markup=main_keyboard(),
    )


# =========================================================
# STATUS
# =========================================================

async def status_text():

    ffmpeg = "FOUND" if ffmpeg_available() else "NOT FOUND"

    total_queue = sum(
        len(q)
        for q in queues.values()
    )

    running = len(current_tasks)

    return (
        "📊 Resso Status\n\n"
        "🟢 Running\n"
        f"🎬 FFmpeg: {ffmpeg}\n"
        f"💾 Target: {TARGET_MB} MB\n"
        f"🚫 Maximum: {MAX_MB} MB\n"
        f"📋 Queued: {total_queue}\n"
        f"⚙️ Active: {running}"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        await status_text(),
        reply_markup=main_keyboard(),
    )


# =========================================================
# QUEUE
# =========================================================

async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id
    queue = queues[chat_id]

    if not queue:
        await update.message.reply_text(
            "📋 Queue empty.",
            reply_markup=main_keyboard(),
        )
        return

    lines = ["📋 Queue:\n"]

    for i, item in enumerate(queue, start=1):
        title = item.get("title", "Unknown")
        lines.append(f"{i}. {title}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_keyboard(),
    )


# =========================================================
# NOW
# =========================================================

async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id

    task = current_tasks.get(chat_id)

    if not task:
        await update.message.reply_text(
            "🎵 Nothing is playing/downloading right now.",
            reply_markup=main_keyboard(),
        )
        return

    title = task.get("title", "Unknown")

    await update.message.reply_text(
        f"🎵 Now:\n{title}",
        reply_markup=main_keyboard(),
    )


# =========================================================
# YOUTUBE SEARCH / DOWNLOAD
# =========================================================

def download_youtube(query, workdir):

    cookies = prepare_cookies()

    output_template = str(
        Path(workdir) / "%(title).120s.%(ext)s"
    )

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": True,
        "no_warnings": True,

        "restrictfilenames": True,

        "extractor_args": {
            "youtube": {
                "player_client": [
                    "android",
                ],
            }
        },

        "socket_timeout": 30,

        "retries": 3,

        "fragment_retries": 3,

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Linux; Android 13) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/120 Mobile Safari/537.36"
            )
        },
    }

    if cookies:
        ydl_opts["cookiefile"] = cookies

    # Search if user didn't give URL.
    if not (
        query.startswith("http://")
        or query.startswith("https://")
    ):
        query = f"ytsearch1:{query}"

    logger.info(
        "Downloading YouTube: %s",
        query,
    )

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        info = ydl.extract_info(
            query,
            download=True,
        )

        if "entries" in info:
            entries = info.get("entries") or []

            if not entries:
                raise RuntimeError(
                    "YouTube result not found."
                )

            info = entries[0]

        title = info.get(
            "title",
            "Unknown",
        )

        duration = info.get(
            "duration",
            0,
        )

        files = list(
            Path(workdir).glob("*")
        )

        media_files = [
            f
            for f in files
            if f.is_file()
            and f.suffix.lower()
            not in [
                ".part",
                ".ytdl",
                ".json",
            ]
        ]

        if not media_files:
            raise RuntimeError(
                "Downloaded audio file not found."
            )

        # Usually the largest media file is the actual audio.
        media_file = max(
            media_files,
            key=lambda p: p.stat().st_size,
        )

        return {
            "title": title,
            "duration": duration,
            "file": media_file,
        }


# =========================================================
# PROCESS SONG
# =========================================================

async def process_song(
    chat_id,
    item,
    bot,
):

    title = item.get(
        "title",
        "Unknown",
    )

    query = item.get(
        "query",
        "",
    )

    current_tasks[chat_id] = {
        "title": title,
        "query": query,
    }

    workdir = tempfile.mkdtemp(
        prefix="resso_"
    )

    try:

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ Downloading...\n\n"
                f"🎵 {title}"
            ),
        )

        loop = asyncio.get_running_loop()

        result = await loop.run_in_executor(
            None,
            download_youtube,
            query,
            workdir,
        )

        downloaded_file = result["file"]

        real_title = result["title"]

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "🎧 Download complete.\n"
                "⚙️ Checking audio size..."
            ),
        )

        compressed_file = await loop.run_in_executor(
            None,
            compress_audio,
            downloaded_file,
        )

        size = compressed_file.stat().st_size

        if size > MAX_BYTES:
            raise RuntimeError(
                f"Final audio is still "
                f"{size / 1024 / 1024:.1f} MB."
            )

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "📤 Uploading...\n\n"
                f"🎵 {real_title}\n"
                f"💾 {size / 1024 / 1024:.1f} MB"
            ),
        )

        with open(
            compressed_file,
            "rb",
        ) as audio:

            await bot.send_audio(
                chat_id=chat_id,
                audio=audio,
                title=real_title[:64],
                caption=f"🎵 {real_title}",
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )

        await bot.send_message(
            chat_id=chat_id,
            text="✅ Done!",
            reply_markup=main_keyboard(),
        )

    except Exception as e:

        logger.exception(
            "Song processing failed: %s",
            e,
        )

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ YouTube download failed.\n\n"
                f"🎵 {title}\n\n"
                "Possible reasons:\n"
                "• YouTube cookies expired\n"
                "• YouTube player changed\n"
                "• Network error\n"
                "• FFmpeg error\n"
                "• Audio could not be reduced below 45 MB\n\n"
                f"Technical error:\n{str(e)[:1500]}"
            ),
            reply_markup=main_keyboard(),
        )

    finally:

        current_tasks.pop(
            chat_id,
            None,
        )

        try:
            shutil.rmtree(
                workdir,
                ignore_errors=True,
            )
        except Exception:
            pass


# =========================================================
# QUEUE WORKER
# =========================================================

async def queue_worker(
    chat_id,
    bot,
):

    if chat_id in current_tasks:
        return

    while queues[chat_id]:

        item = queues[chat_id].popleft()

        await process_song(
            chat_id,
            item,
            bot,
        )

    current_tasks.pop(
        chat_id,
        None,
    )


async def add_to_queue(
    chat_id,
    query,
    bot,
):

    item = {
        "query": query,
        "title": query,
    }

    queues[chat_id].append(item)

    position = len(
        queues[chat_id]
    )

    if chat_id in current_tasks:

        return position

    asyncio.create_task(
        queue_worker(
            chat_id,
            bot,
        )
    )

    return position


# =========================================================
# PLAY
# =========================================================

async def play_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    chat_id = update.effective_chat.id

    if not context.args:

        await update.message.reply_text(
            "🎵 Use:\n\n"
            "/play <YouTube URL>\n\n"
            "or\n\n"
            "/play <song name>",
            reply_markup=main_keyboard(),
        )

        return

    query = " ".join(
        context.args
    ).strip()

    position = await add_to_queue(
        chat_id,
        query,
        context.bot,
    )

    if position == 1 and chat_id not in current_tasks:

        await update.message.reply_text(
            f"🎵 Starting:\n{query}",
            reply_markup=main_keyboard(),
        )

    else:

        await update.message.reply_text(
            f"📋 Added to queue.\n"
            f"Position: {position}\n\n"
            f"🎵 {query}",
            reply_markup=main_keyboard(),
        )


# =========================================================
# STOP
# =========================================================

async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    queues[chat_id].clear()

    await update.message.reply_text(
        "⏹️ Queue stopped and cleared.\n\n"
        "Current download will finish safely.",
        reply_markup=main_keyboard(),
    )


# =========================================================
# CLEAR
# =========================================================

async def clear_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    queues[chat_id].clear()

    await update.message.reply_text(
        "🗑️ Queue cleared.",
        reply_markup=main_keyboard(),
    )


# =========================================================
# REMOVE
# =========================================================

async def remove_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    if not context.args:

        await update.message.reply_text(
            "Use:\n/remove <queue number>"
        )

        return

    try:
        number = int(
            context.args[0]
        )

        if number < 1:
            raise ValueError

        queue = queues[chat_id]

        if number > len(queue):
            await update.message.reply_text(
                "❌ Invalid queue number."
            )
            return

        queue.rotate(
            -(number - 1)
        )

        removed = queue.popleft()

        queue.rotate(
            number - 1
        )

        await update.message.reply_text(
            f"🗑️ Removed:\n"
            f"{removed.get('title', 'Unknown')}",
            reply_markup=main_keyboard(),
        )

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid number."
        )


# =========================================================
# SKIP
# =========================================================

async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    if queues[chat_id]:

        skipped = queues[chat_id].popleft()

        await update.message.reply_text(
            f"⏭️ Skipped:\n"
            f"{skipped.get('title', 'Unknown')}",
            reply_markup=main_keyboard(),
        )

    else:

        await update.message.reply_text(
            "📋 Queue is empty.",
            reply_markup=main_keyboard(),
        )


# =========================================================
# CALLBACK BUTTONS
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    chat_id = query.message.chat.id

    if query.data == "help":

        await query.message.reply_text(
            HELP_TEXT,
            reply_markup=main_keyboard(),
        )

    elif query.data == "status":

        await query.message.reply_text(
            await status_text(),
            reply_markup=main_keyboard(),
        )

    elif query.data == "queue":

        queue = queues[chat_id]

        if not queue:

            text = "📋 Queue empty."

        else:

            lines = ["📋 Queue:\n"]

            for i, item in enumerate(
                queue,
                start=1,
            ):
                lines.append(
                    f"{i}. {item.get('title', 'Unknown')}"
                )

            text = "\n".join(lines)

        await query.message.reply_text(
            text,
            reply_markup=main_keyboard(),
        )

    elif query.data == "now":

        task = current_tasks.get(chat_id)

        if task:

            text = (
                "🎵 Now:\n"
                f"{task.get('title', 'Unknown')}"
            )

        else:

            text = (
                "🎵 Nothing is "
                "running right now."
            )

        await query.message.reply_text(
            text,
            reply_markup=main_keyboard(),
        )

    elif query.data == "skip":

        if queues[chat_id]:

            skipped = queues[chat_id].popleft()

            text = (
                "⏭️ Skipped:\n"
                f"{skipped.get('title', 'Unknown')}"
            )

        else:

            text = "📋 Queue is empty."

        await query.message.reply_text(
            text,
            reply_markup=main_keyboard(),
        )

    elif query.data == "stop":

        queues[chat_id].clear()

        await query.message.reply_text(
            "⏹️ Queue stopped and cleared.",
            reply_markup=main_keyboard(),
        )


# =========================================================
# TELEGRAM APPLICATION
# =========================================================

def create_application():

    global application

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "play",
            play_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "queue",
            queue_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "now",
            now_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "skip",
            skip_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "stop",
            stop_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "clear",
            clear_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "remove",
            remove_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    return application


# =========================================================
# WEBHOOK
# =========================================================

@flask_app.route(
    "/telegram/webhook",
    methods=["POST"],
)
def telegram_webhook():

    global application
    global bot_loop

    if application is None:
        return "Bot not ready", 503

    if WEBHOOK_SECRET:

        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

        if received_secret != WEBHOOK_SECRET:
            return "Forbidden", 403

    try:

        data = request.get_json(
            force=True
        )

        update = Update.de_json(
            data,
            application.bot,
        )

        if bot_loop is None:
            return "Loop not ready", 503

        bot_loop.call_soon_threadsafe(
            application.update_queue.put_nowait,
            update,
        )

        return "OK", 200

    except Exception as e:

        logger.exception(
            "Webhook error: %s",
            e,
        )

        return "Bad Request", 400


# =========================================================
# ASYNC TELEGRAM STARTUP
# =========================================================

async def telegram_start():

    global bot_loop

    bot_loop = asyncio.get_running_loop()

    await application.initialize()

    await application.start()

    webhook_base = RENDER_URL

    if not webhook_base:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing."
        )

    webhook_url = (
        webhook_base.rstrip("/")
        + "/telegram/webhook"
    )

    logger.info(
        "Setting Telegram webhook: %s",
        webhook_url,
    )

    # Remove any previous webhook first.
    await application.bot.delete_webhook(
        drop_pending_updates=True
    )

    if WEBHOOK_SECRET:

        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
        )

    else:

        await application.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
        )

    logger.info(
        "Telegram webhook is ACTIVE."
    )

    # Keep asyncio loop alive.
    await asyncio.Event().wait()


def telegram_thread():

    try:

        asyncio.run(
            telegram_start()
        )

    except Exception:

        logger.exception(
            "Telegram thread stopped."
        )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    if not BOT_TOKEN:

        raise SystemExit(
            "ERROR: BOT_TOKEN is not configured."
        )

    logger.info(
        "Starting Resso Bot..."
    )

    logger.info(
        "FFmpeg: %s",
        "FOUND"
        if ffmpeg_available()
        else "NOT FOUND",
    )

    create_application()

    thread = threading.Thread(
        target=telegram_thread,
        daemon=True,
    )

    thread.start()

    logger.info(
        "Starting Flask on port %s",
        PORT,
    )

    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
)
