import os
import re
import asyncio
import logging
import tempfile
import threading
import shutil
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict, deque
from urllib.parse import urlparse

from flask import Flask, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import yt_dlp


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

PORT = int(os.getenv("PORT", "10000"))

MAX_FILE_SIZE = 49 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("resso-bot")


# ============================================================
# FLASK HEALTH SERVER
# ============================================================

app = Flask(__name__)


@app.get("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "Resso",
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
    })


def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )


# ============================================================
# COOKIE FILE
# ============================================================

def find_cookie_file():
    """
    Render Secret File ke liye multiple possible locations check karta hai.
    """

    custom = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()

    candidates = []

    if custom:
        candidates.append(custom)

    candidates.extend([
        "/etc/secrets/youtube_cookies.txt",
        "/etc/secrets/cookies.txt",
        "youtube_cookies.txt",
        "cookies.txt",
    ])

    for item in candidates:
        path = Path(item)

        if path.exists() and path.is_file():
            try:
                size = path.stat().st_size

                if size > 100:
                    logger.info(
                        "YouTube cookies found: %s (%d bytes)",
                        path,
                        size,
                    )
                    return str(path)

            except Exception:
                pass

    logger.warning("No valid YouTube cookie file found.")

    return None


def cookie_options():
    cookie_file = find_cookie_file()

    if not cookie_file:
        return {}

    return {
        "cookiefile": cookie_file,
    }


# ============================================================
# YOUTUBE HELPERS
# ============================================================

def is_youtube_url(text):
    if not text:
        return False

    try:
        parsed = urlparse(text.strip())
        host = (parsed.netloc or "").lower()

        return (
            host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtu.be"
            or host.endswith(".youtu.be")
        )

    except Exception:
        return False


def clean_url(text):
    return text.strip().split()[0]


def safe_filename(name):
    name = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        name,
    )

    name = re.sub(
        r"\s+",
        " ",
        name,
    ).strip()

    return name[:180] or "audio"


def format_duration(seconds):
    if seconds is None:
        return "Unknown"

    try:
        seconds = int(seconds)
    except Exception:
        return "Unknown"

    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


# ============================================================
# YT-DLP
# ============================================================

def ytdlp_options():
    options = {
        "quiet": True,
        "no_warnings": False,

        "noplaylist": True,

        "format": (
            "bestaudio[ext=m4a]/"
            "bestaudio[ext=webm]/"
            "bestaudio/best"
        ),

        "retries": 3,
        "fragment_retries": 3,

        "socket_timeout": 30,

        "concurrent_fragment_downloads": 2,

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Linux; Android 13) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Mobile Safari/537.36"
            ),

            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    options.update(cookie_options())

    return options


def extract_info_sync(url):
    options = ytdlp_options()

    options["skip_download"] = True

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(
            url,
            download=False,
        )


async def extract_info(url):
    return await asyncio.to_thread(
        extract_info_sync,
        url,
    )


# ============================================================
# DOWNLOAD AUDIO
# ============================================================

def download_audio_sync(url, output_dir):

    output_template = str(
        Path(output_dir) /
        "%(title).180B [%(id)s].%(ext)s"
    )

    options = ytdlp_options()

    options.update({
        "outtmpl": output_template,

        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],

        "keepvideo": False,

        "prefer_ffmpeg": True,
    })

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            url,
            download=True,
        )

        title = info.get("title") or "Audio"

        video_id = info.get("id") or "audio"

        mp3_files = list(
            Path(output_dir).glob("*.mp3")
        )

        if not mp3_files:
            raise RuntimeError(
                "FFmpeg MP3 file create nahi kar saka."
            )

        mp3_files.sort(
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )

        final_file = mp3_files[0]

        return {
            "title": title,
            "path": str(final_file),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "id": video_id,
        }


async def download_audio(url, output_dir):
    return await asyncio.to_thread(
        download_audio_sync,
        url,
        output_dir,
    )


# ============================================================
# DATA
# ============================================================

@dataclass
class Song:
    url: str
    title: str
    requester: str


@dataclass
class ChatQueue:
    items: deque = field(
        default_factory=deque
    )

    current: Song | None = None


queues = defaultdict(ChatQueue)

queue_lock = asyncio.Lock()

workers = {}

stop_events = {}

skip_events = {}


# ============================================================
# KEYBOARD
# ============================================================

def keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "▶️ Play",
                callback_data="help",
            ),
            InlineKeyboardButton(
                "📋 Queue",
                callback_data="queue",
            ),
        ],

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
                "🎵 Now",
                callback_data="now",
            ),
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help",
            ),
        ],

        [
            InlineKeyboardButton(
                "📊 Status",
                callback_data="status",
            ),
        ],
    ])


# ============================================================
# SAFE REPLY
# ============================================================

async def reply(update, text, **kwargs):

    if update.message is not None:
        return await update.message.reply_text(
            text,
            **kwargs,
        )

    if update.callback_query is not None:

        message = update.callback_query.message

        if message is not None:
            return await message.reply_text(
                text,
                **kwargs,
            )

    return None


async def answer_callback(update):

    query = update.callback_query

    if query:

        try:
            await query.answer()
        except Exception:
            pass


# ============================================================
# START
# ============================================================

async def start(update, context):

    text = (
        "🎵 *Resso Music Bot*\n\n"
        "YouTube ka link bhejo aur main "
        "audio download karke bhej dunga.\n\n"
        "Example:\n"
        "`https://youtu.be/VIDEO_ID`\n\n"
        "Commands:\n"
        "/play\n"
        "/queue\n"
        "/now\n"
        "/skip\n"
        "/stop\n"
        "/status\n"
        "/help"
    )

    await reply(
        update,
        text,
        parse_mode="Markdown",
        reply_markup=keyboard(),
    )


# ============================================================
# HELP
# ============================================================

async def help_command(update, context):

    text = (
        "ℹ️ *Resso Help*\n\n"
        "YouTube URL directly bhejo.\n\n"
        "Ya:\n"
        "`/play https://youtu.be/VIDEO_ID`\n\n"
        "📋 Queue = waiting songs\n"
        "🎵 Now = current song\n"
        "⏭️ Skip = current song skip\n"
        "⏹️ Stop = queue clear\n"
        "📊 Status = bot status"
    )

    await reply(
        update,
        text,
        parse_mode="Markdown",
        reply_markup=keyboard(),
    )


# ============================================================
# PLAY
# ============================================================

async def play(update, context):

    if not context.args:

        await reply(
            update,
            "❌ YouTube URL do.\n\n"
            "Example:\n"
            "`/play https://youtu.be/VIDEO_ID`",
            parse_mode="Markdown",
        )

        return

    url = clean_url(
        context.args[0]
    )

    if not is_youtube_url(url):

        await reply(
            update,
            "❌ Valid YouTube URL nahi hai."
        )

        return

    await add_song(
        update,
        url,
    )


# ============================================================
# URL MESSAGE
# ============================================================

async def url_handler(update, context):

    if update.message is None:
        return

    text = (
        update.message.text or ""
    ).strip()

    if not is_youtube_url(text):
        return

    await add_song(
        update,
        clean_url(text),
    )


# ============================================================
# ADD SONG
# ============================================================

async def add_song(update, url):

    message = await reply(
        update,
        "🔎 YouTube information check kar raha hoon..."
    )

    try:

        info = await extract_info(url)

        title = (
            info.get("title")
            or "Unknown"
        )

        duration = format_duration(
            info.get("duration")
        )

        user = update.effective_user

        requester = (
            user.full_name
            if user
            else "Unknown"
        )

        chat = update.effective_chat

        if chat is None:
            return

        chat_id = chat.id

        song = Song(
            url=url,
            title=title,
            requester=requester,
        )

        async with queue_lock:

            q = queues[chat_id]

            q.items.append(song)

            position = len(q.items)

            worker_exists = (
                chat_id in workers
                and not workers[chat_id].done()
            )

        text = (
            "✅ *Added to queue*\n\n"
            f"🎵 {title}\n"
            f"⏱️ {duration}\n"
            f"👤 {requester}\n\n"
            f"📋 Position: {position}"
        )

        if message:

            try:

                await message.edit_text(
                    text,
                    parse_mode="Markdown",
                    reply_markup=keyboard(),
                )

            except Exception:
                pass

        if not worker_exists:

            task = asyncio.create_task(
                queue_worker(
                    chat_id,
                    update.get_bot(),
                )
            )

            workers[chat_id] = task

    except Exception as exc:

        logger.exception(
            "YouTube info failed"
        )

        error = str(exc)

        if "Sign in to confirm" in error:
            error_text = (
                "❌ *YouTube authentication required.*\n\n"
                "Render mein valid YouTube cookies "
                "Secret File ke roop mein set karo.\n\n"
                "Phir dobara try karo."
            )

        elif "cookies" in error.lower():
            error_text = (
                "❌ *YouTube cookies problem.*\n\n"
                "Cookies file invalid ya expired "
                "ho sakti hai."
            )

        else:
            error_text = (
                "❌ *YouTube information fetch failed.*\n\n"
                f"`{error[:1200]}`"
            )

        if message:

            try:

                await message.edit_text(
                    error_text,
                    parse_mode="Markdown",
                    reply_markup=keyboard(),
                )

            except Exception:
                await reply(
                    update,
                    error_text,
                    parse_mode="Markdown",
                )

        else:

            await reply(
                update,
                error_text,
                parse_mode="Markdown",
            )


# ============================================================
# QUEUE WORKER
# ============================================================

async def queue_worker(chat_id, bot):

    stop_event = asyncio.Event()

    skip_event = asyncio.Event()

    stop_events[chat_id] = stop_event

    skip_events[chat_id] = skip_event

    try:

        while True:

            async with queue_lock:

                q = queues[chat_id]

                if not q.items:

                    q.current = None

                    break

                song = q.items.popleft()

                q.current = song

                skip_event.clear()

                stop_event.clear()

            temp_dir = tempfile.mkdtemp(
                prefix="resso_"
            )

            try:

                await bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.UPLOAD_DOCUMENT,
                )

                progress = await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⏳ Downloading...\n\n"
                        f"🎵 {song.title}"
                    ),
                    reply_markup=keyboard(),
                )

                result = await download_audio(
                    song.url,
                    temp_dir,
                )

                if skip_event.is_set():

                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏭️ Song skipped.",
                        reply_markup=keyboard(),
                    )

                    continue

                if stop_event.is_set():

                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏹️ Download stopped.",
                        reply_markup=keyboard(),
                    )

                    break

                path = Path(
                    result["path"]
                )

                if not path.exists():

                    raise RuntimeError(
                        "Audio file missing."
                    )

                size = path.stat().st_size

                if size > MAX_FILE_SIZE:

                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "❌ File too large.\n"
                            f"Size: {size / 1024 / 1024:.1f} MB"
                        ),
                        reply_markup=keyboard(),
                    )

                    continue

                try:

                    await progress.edit_text(
                        "📤 Uploading...\n\n"
                        f"🎵 {result['title']}"
                    )

                except Exception:
                    pass

                with open(
                    path,
                    "rb",
                ) as audio:

                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=audio,
                        title=result["title"][:64],
                        duration=(
                            int(result["duration"])
                            if result.get("duration")
                            else None
                        ),
                        caption=(
                            f"🎵 {result['title']}\n"
                            "🤖 Resso Music Bot"
                        ),
                    )

                try:
                    await progress.delete()
                except Exception:
                    pass

            except Exception as exc:

                logger.exception(
                    "Download failed"
                )

                if stop_event.is_set():

                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏹️ Stopped.",
                        reply_markup=keyboard(),
                    )

                    break

                elif skip_event.is_set():

                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏭️ Skipped.",
                        reply_markup=keyboard(),
                    )

                else:

                    error = str(exc)

                    if "Sign in to confirm" in error:

                        text = (
                            "❌ *YouTube authentication required.*\n\n"
                            "Cookies expired/invalid ho sakti hain.\n"
                            "Render Secret File mein fresh "
                            "YouTube cookies lagao."
                        )

                    else:

                        text = (
                            "❌ *Download failed.*\n\n"
                            f"🎵 {song.title}\n\n"
                            f"`{error[:1000]}`"
                        )

                    await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=keyboard(),
                    )

            finally:

                shutil.rmtree(
                    temp_dir,
                    ignore_errors=True,
                )

                async with queue_lock:
                    queues[chat_id].current = None

        await bot.send_message(
            chat_id=chat_id,
            text="✅ Queue finished.",
            reply_markup=keyboard(),
        )

    except asyncio.CancelledError:

        logger.info(
            "Worker cancelled: %s",
            chat_id,
        )

        raise

    finally:

        stop_events.pop(
            chat_id,
            None,
        )

        skip_events.pop(
            chat_id,
            None,
        )

        async with queue_lock:
            queues[chat_id].current = None


# ============================================================
# QUEUE
# ============================================================

async def queue_command(update, context):

    chat = update.effective_chat

    if not chat:
        return

    async with queue_lock:

        q = queues[chat.id]

        current = q.current

        pending = list(q.items)

    lines = ["📋 Queue"]

    if current:

        lines.append(
            f"\n🎵 Now:\n{current.title}"
        )

    if pending:

        lines.append("\n⏳ Waiting:")

        for i, song in enumerate(
            pending,
            start=1,
        ):

            lines.append(
                f"{i}. {song.title}"
            )

    if not current and not pending:
        lines.append("\nQueue empty.")

    await reply(
        update,
        "\n".join(lines),
        reply_markup=keyboard(),
    )


# ============================================================
# NOW
# ============================================================

async def now_command(update, context):

    chat = update.effective_chat

    if not chat:
        return

    async with queue_lock:

        current = queues[
            chat.id
        ].current

    if not current:

        await reply(
            update,
            "🎵 Nothing downloading.",
            reply_markup=keyboard(),
        )

        return

    await reply(
        update,
        (
            "🎵 Now downloading\n\n"
            f"🎶 {current.title}\n"
            f"👤 {current.requester}"
        ),
        reply_markup=keyboard(),
    )


# ============================================================
# STATUS
# ============================================================

async def status_command(update, context):

    chat = update.effective_chat

    waiting = 0
    current = "Nothing"

    if chat:

        async with queue_lock:

            q = queues[chat.id]

            waiting = len(q.items)

            if q.current:
                current = q.current.title

    active = sum(
        1
        for task in workers.values()
        if not task.done()
    )

    text = (
        "📊 Bot Status\n\n"
        "🟢 Bot: Online\n"
        f"⚙️ Active queues: {active}\n"
        f"📋 Waiting: {waiting}\n"
        f"🎵 Current: {current}"
    )

    await reply(
        update,
        text,
        reply_markup=keyboard(),
    )


# ============================================================
# SKIP
# ============================================================

async def skip_command(update, context):

    chat = update.effective_chat

    if not chat:
        return

    async with queue_lock:

        current = queues[
            chat.id
        ].current

    if not current:

        await reply(
            update,
            "⏭️ Nothing to skip.",
            reply_markup=keyboard(),
        )

        return

    event = skip_events.get(
        chat.id
    )

    if event:
        event.set()

    await reply(
        update,
        "⏭️ Skip requested.",
        reply_markup=keyboard(),
    )


# ============================================================
# STOP
# ============================================================

async def stop_command(update, context):

    chat = update.effective_chat

    if not chat:
        return

    async with queue_lock:

        q = queues[chat.id]

        had_current = (
            q.current is not None
        )

        waiting = len(q.items)

        q.items.clear()

    event = stop_events.get(
        chat.id
    )

    if event:
        event.set()

    if not had_current and waiting == 0:

        await reply(
            update,
            "⏹️ Queue already empty.",
            reply_markup=keyboard(),
        )

        return

    await reply(
        update,
        (
            "⏹️ Stopped.\n\n"
            "Current job stop hoga aur "
            "waiting queue clear ho gayi."
        ),
        reply_markup=keyboard(),
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callbacks(update, context):

    query = update.callback_query

    if not query:
        return

    await answer_callback(update)

    data = query.data or ""

    if data == "help":
        await help_command(update, context)

    elif data == "queue":
        await queue_command(update, context)

    elif data == "now":
        await now_command(update, context)

    elif data == "status":
        await status_command(update, context)

    elif data == "skip":
        await skip_command(update, context)

    elif data == "stop":
        await stop_command(update, context)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):

    logger.error(
        "Telegram error: %s",
        context.error,
        exc_info=context.error,
    )


# ============================================================
# COMMANDS
# ============================================================

async def set_commands(application):

    await application.bot.set_my_commands([
        ("start", "Start bot"),
        ("play", "Download YouTube audio"),
        ("queue", "Show queue"),
        ("now", "Current song"),
        ("skip", "Skip song"),
        ("stop", "Stop queue"),
        ("status", "Bot status"),
        ("help", "Help"),
    ])


# ============================================================
# BUILD
# ============================================================

def build_application():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN missing in Render Environment."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(set_commands)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "play",
            play,
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
            "status",
            status_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            url_handler,
        )
    )

    application.add_error_handler(
        error_handler
    )

    return application


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "Starting Resso bot..."
    )

    logger.info(
        "PORT=%s",
        PORT,
    )

    cookie = find_cookie_file()

    if cookie:
        logger.info(
            "YouTube cookies enabled."
        )
    else:
        logger.warning(
            "YouTube cookies NOT found."
        )

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )

    flask_thread.start()

    application = build_application()

    logger.info(
        "Starting Telegram polling..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=None,
    )


if __name__ == "__main__":
    main()
