import os
import re
import asyncio
import logging
import tempfile
import threading
import shutil
import subprocess
from pathlib import Path
from dataclasses import dataclass
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

# Render Secret File:
# /etc/secrets/youtube_cookies.txt
COOKIE_FILE = os.getenv(
    "COOKIE_FILE",
    "/etc/secrets/youtube_cookies.txt"
).strip()

MAX_FILE_SIZE = 49 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("music_bot")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.get("/")
def home():
    return jsonify({
        "status": "online",
        "service": "Telegram Music Bot"
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok"
    })


def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )


# ============================================================
# DATA
# ============================================================

@dataclass
class Song:
    url: str
    title: str = "Unknown"
    requester: str = "Unknown"
    status: str = "queued"


queues = defaultdict(
    lambda: {
        "items": deque(),
        "current": None,
    }
)

queue_lock = asyncio.Lock()

active_tasks = {}

skip_events = {}
stop_events = {}


# ============================================================
# KEYBOARD
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "▶️ Play",
                callback_data="help_play"
            ),
            InlineKeyboardButton(
                "📋 Queue",
                callback_data="queue"
            ),
        ],
        [
            InlineKeyboardButton(
                "⏭️ Skip",
                callback_data="skip"
            ),
            InlineKeyboardButton(
                "⏹️ Stop",
                callback_data="stop"
            ),
        ],
        [
            InlineKeyboardButton(
                "🎵 Now",
                callback_data="now"
            ),
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help"
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 Status",
                callback_data="status"
            ),
        ],
    ])


# ============================================================
# HELPERS
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
        name
    )

    name = re.sub(
        r"\s+",
        " ",
        name
    ).strip()

    return (name or "audio")[:180]


def format_duration(seconds):
    if not seconds:
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


def find_cookie_file():
    """
    Finds Render Secret File or local cookies.txt.
    """

    possible = [
        COOKIE_FILE,
        "/etc/secrets/youtube_cookies.txt",
        "/etc/secrets/cookies.txt",
        "youtube_cookies.txt",
        "cookies.txt",
    ]

    for item in possible:
        if not item:
            continue

        path = Path(item)

        if path.exists() and path.is_file():
            logger.info(
                "YouTube cookies found: %s",
                path
            )
            return str(path)

    logger.warning(
        "No YouTube cookies file found."
    )

    return None


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def ytdlp_options():
    options = {
        "quiet": True,
        "no_warnings": True,

        "noplaylist": True,

        "retries": 3,
        "fragment_retries": 3,

        "socket_timeout": 30,

        "concurrent_fragment_downloads": 4,

        "format": (
            "bestaudio[ext=m4a]/"
            "bestaudio[ext=webm]/"
            "bestaudio/best"
        ),

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Linux; Android 13) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0 Mobile Safari/537.36"
            ),

            "Accept-Language":
                "en-US,en;q=0.9",
        },

        # yt-dlp YouTube JS challenge support
        "js_runtimes": {
            "deno": {}
        },
    }

    cookie_file = find_cookie_file()

    if cookie_file:
        options["cookiefile"] = cookie_file

    return options


# ============================================================
# YOUTUBE INFO
# ============================================================

def extract_info_sync(url):
    options = ytdlp_options()

    options["skip_download"] = True

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(
            url,
            download=False
        )


async def extract_info(url):
    return await asyncio.to_thread(
        extract_info_sync,
        url
    )


# ============================================================
# DOWNLOAD
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

        "prefer_ffmpeg": True,
        "keepvideo": False,
    })

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            url,
            download=True
        )

    title = info.get("title") or "Audio"
    video_id = info.get("id") or "audio"

    mp3_files = list(
        Path(output_dir).glob("*.mp3")
    )

    if not mp3_files:

        candidates = [
            p
            for p in Path(output_dir).iterdir()
            if p.is_file()
        ]

        if candidates:

            source = candidates[0]

            output_mp3 = (
                Path(output_dir) /
                f"{safe_filename(title)} "
                f"[{video_id}].mp3"
            )

            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    str(output_mp3),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            mp3_files = [output_mp3]

    if not mp3_files:
        raise RuntimeError(
            "MP3 file was not created."
        )

    mp3_files.sort(
        key=lambda p: p.stat().st_mtime,
        reverse=True
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
        output_dir
    )


# ============================================================
# SAFE TELEGRAM REPLY
# ============================================================

async def reply_text(
    update,
    text,
    **kwargs
):

    if update.message is not None:
        return await update.message.reply_text(
            text,
            **kwargs
        )

    query = update.callback_query

    if query and query.message:
        return await query.message.reply_text(
            text,
            **kwargs
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

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await reply_text(
        update,

        "🎵 *Resso Music Bot*\n\n"
        "YouTube link bhejo aur main "
        "audio download kar dunga.\n\n"

        "Example:\n"
        "`/play https://youtu.be/VIDEO_ID`\n\n"

        "Ya direct YouTube URL bhej do 👇",

        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update,
    context
):

    await reply_text(
        update,

        "ℹ️ *Help*\n\n"

        "🎵 `/play <YouTube URL>`\n"
        "YouTube audio download karega.\n\n"

        "📋 `/queue` — Queue\n"
        "🎵 `/now` — Current song\n"
        "⏭️ `/skip` — Skip\n"
        "⏹️ `/stop` — Stop + clear queue\n"
        "📊 `/status` — Bot status\n\n"

        "Direct YouTube URL bhi bhej sakte ho.",

        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ============================================================
# PLAY
# ============================================================

async def play_command(
    update,
    context
):

    if not context.args:

        await reply_text(
            update,

            "❌ YouTube URL do.\n\n"
            "Example:\n"
            "`/play https://youtu.be/VIDEO_ID`",

            parse_mode="Markdown"
        )

        return

    url = clean_url(
        context.args[0]
    )

    if not is_youtube_url(url):

        await reply_text(
            update,
            "❌ Valid YouTube URL nahi hai."
        )

        return

    await add_song(
        update,
        url
    )


# ============================================================
# DIRECT URL
# ============================================================

async def url_handler(
    update,
    context
):

    if not update.message:
        return

    text = (
        update.message.text or ""
    ).strip()

    if not is_youtube_url(text):
        return

    await add_song(
        update,
        clean_url(text)
    )


# ============================================================
# ADD SONG
# ============================================================

async def add_song(
    update,
    url
):

    status = await reply_text(
        update,
        "🔎 YouTube information fetch ho rahi hai..."
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

        if not chat:
            return

        chat_id = chat.id

        song = Song(
            url=url,
            title=title,
            requester=requester
        )

        async with queue_lock:

            q = queues[chat_id]

            empty = (
                q["current"] is None
                and len(q["items"]) == 0
            )

            q["items"].append(song)

            position = len(q["items"])

        text = (
            "✅ *Added to queue*\n\n"
            f"🎵 {title}\n"
            f"⏱️ {duration}\n"
            f"👤 {requester}\n"
        )

        if empty:
            text += "\n⏳ Download starting..."
        else:
            text += f"\n📋 Position: {position}"

        if status:

            try:

                await status.edit_text(
                    text,
                    parse_mode="Markdown",
                    reply_markup=main_keyboard()
                )

            except Exception:
                pass

        if chat_id not in active_tasks:

            task = asyncio.create_task(
                process_queue(
                    chat_id,
                    update.get_bot()
                )
            )

            active_tasks[chat_id] = task

            def cleanup(_):
                active_tasks.pop(
                    chat_id,
                    None
                )

            task.add_done_callback(
                cleanup
            )

    except Exception as exc:

        logger.exception(
            "YouTube information error"
        )

        error = str(exc)

        if (
            "Sign in to confirm" in error
            or "cookies" in error.lower()
        ):

            message = (
                "❌ *YouTube authentication required.*\n\n"
                "Render mein valid YouTube cookies "
                "Secret File ke roop mein set karo.\n\n"
                "Cookies file milne ke baad dobara try karo."
            )

        else:

            message = (
                "❌ *YouTube information fetch failed.*\n\n"
                f"`{error[:1200]}`"
            )

        if status:

            try:

                await status.edit_text(
                    message,
                    parse_mode="Markdown",
                    reply_markup=main_keyboard()
                )

            except Exception:
                await reply_text(
                    update,
                    message,
                    parse_mode="Markdown"
                )


# ============================================================
# QUEUE WORKER
# ============================================================

async def process_queue(
    chat_id,
    bot
):

    skip_event = asyncio.Event()
    stop_event = asyncio.Event()

    skip_events[chat_id] = skip_event
    stop_events[chat_id] = stop_event

    try:

        while True:

            async with queue_lock:

                q = queues[chat_id]

                if not q["items"]:

                    q["current"] = None
                    break

                song = q["items"].popleft()

                song.status = "downloading"

                q["current"] = song

                skip_event.clear()
                stop_event.clear()

            temp_dir = tempfile.mkdtemp(
                prefix="resso_"
            )

            progress = None

            try:

                await bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.UPLOAD_DOCUMENT
                )

                progress = await bot.send_message(
                    chat_id=chat_id,

                    text=(
                        "⏳ *Downloading...*\n\n"
                        f"🎵 {song.title}"
                    ),

                    parse_mode="Markdown",
                    reply_markup=main_keyboard()
                )

                result = await download_audio(
                    song.url,
                    temp_dir
                )

                if skip_event.is_set():

                    if progress:
                        try:
                            await progress.edit_text(
                                "⏭️ Skipped."
                            )
                        except Exception:
                            pass

                    continue

                if stop_event.is_set():

                    if progress:
                        try:
                            await progress.edit_text(
                                "⏹️ Stopped."
                            )
                        except Exception:
                            pass

                    break

                file_path = Path(
                    result["path"]
                )

                if not file_path.exists():
                    raise RuntimeError(
                        "Downloaded file not found."
                    )

                size = file_path.stat().st_size

                if size > MAX_FILE_SIZE:

                    await bot.send_message(
                        chat_id=chat_id,

                        text=(
                            "❌ File Telegram limit "
                            "se bada hai.\n\n"
                            f"Size: "
                            f"{size / 1024 / 1024:.1f} MB"
                        ),

                        reply_markup=main_keyboard()
                    )

                    continue

                if progress:

                    try:

                        await progress.edit_text(
                            (
                                "📤 *Uploading...*\n\n"
                                f"🎵 {result['title']}"
                            ),
                            parse_mode="Markdown"
                        )

                    except Exception:
                        pass

                with open(
                    file_path,
                    "rb"
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
                            f"🤖 Resso Music Bot"
                        )
                    )

                if progress:

                    try:
                        await progress.delete()
                    except Exception:
                        pass

            except Exception as exc:

                logger.exception(
                    "Download failed"
                )

                if skip_event.is_set():

                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏭️ Current song skipped.",
                        reply_markup=main_keyboard()
                    )

                elif stop_event.is_set():

                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏹️ Download stopped.",
                        reply_markup=main_keyboard()
                    )

                    break

                else:

                    error = str(exc)

                    if (
                        "Sign in to confirm" in error
                        or "cookies" in error.lower()
                    ):

                        text = (
                            "❌ *YouTube authentication failed.*\n\n"
                            "Cookies check karo.\n\n"
                            f"`{error[:1000]}`"
                        )

                    else:

                        text = (
                            "❌ *Download failed.*\n\n"
                            f"🎵 {song.title}\n\n"
                            f"`{error[:1200]}`"
                        )

                    await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=main_keyboard()
                    )

            finally:

                shutil.rmtree(
                    temp_dir,
                    ignore_errors=True
                )

                async with queue_lock:
                    queues[chat_id]["current"] = None

        await bot.send_message(
            chat_id=chat_id,
            text="✅ Queue finished.",
            reply_markup=main_keyboard()
        )

    except asyncio.CancelledError:

        logger.info(
            "Queue cancelled: %s",
            chat_id
        )

        raise

    finally:

        skip_events.pop(
            chat_id,
            None
        )

        stop_events.pop(
            chat_id,
            None
        )

        async with queue_lock:
            queues[chat_id]["current"] = None


# ============================================================
# QUEUE
# ============================================================

async def queue_command(
    update,
    context
):

    chat = update.effective_chat

    if not chat:
        return

    async with queue_lock:

        q = queues[chat.id]

        current = q["current"]

        pending = list(
            q["items"]
        )

    lines = ["📋 *Queue*\n"]

    if current:

        lines.append(
            f"🎵 *Now:*\n"
            f"{current.title}\n"
        )

    if pending:

        lines.append(
            "\n⏳ *Waiting:*\n"
        )

        for i, song in enumerate(
            pending,
            1
        ):

            lines.append(
                f"{i}. {song.title}"
            )

    elif not current:

        lines.append(
            "Queue empty."
        )

    await reply_text(
        update,
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ============================================================
# NOW
# ============================================================

async def now_command(
    update,
    context
):

    chat = update.effective_chat

    if not chat:
        return

    async with queue_lock:

        current = queues[
            chat.id
        ]["current"]

    if not current:

        await reply_text(
            update,
            "🎵 Nothing is downloading.",
            reply_markup=main_keyboard()
        )

        return

    await reply_text(
        update,

        (
            "🎵 *Now Downloading*\n\n"
            f"🎶 {current.title}\n"
            f"👤 {current.requester}\n"
            f"📌 {current.status}"
        ),

        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ============================================================
# STATUS
# ============================================================

async def status_command(
    update,
    context
):

    chat = update.effective_chat

    waiting = 0
    current = "Nothing"

    if chat:

        async with queue_lock:

            q = queues[chat.id]

            waiting = len(
                q["items"]
            )

            if q["current"]:
                current = q[
                    "current"
                ].title

    text = (
        "📊 *Bot Status*\n\n"
        "🟢 Bot: Online\n"
        f"⚙️ Active queues: "
        f"{len(active_tasks)}\n"
        f"📋 Waiting: {waiting}\n"
        f"🎵 Current: {current}"
    )

    await reply_text(
        update,
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ============================================================
# SKIP
# ============================================================

async def skip_command(
    update,
    context
):

    chat = update.effective_chat

    if not chat:
        return

    async with queue_lock:

        current = queues[
            chat.id
        ]["current"]

    if not current:

        await reply_text(
            update,
            "⏭️ Nothing is downloading.",
            reply_markup=main_keyboard()
        )

        return

    event = skip_events.get(
        chat.id
    )

    if event:
        event.set()

    await reply_text(
        update,

        (
            "⏭️ *Skip requested*\n\n"
            f"🎵 {current.title}"
        ),

        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ============================================================
# STOP
# ============================================================

async def stop_command(
    update,
    context
):

    chat = update.effective_chat

    if not chat:
        return

    async with queue_lock:

        q = queues[chat.id]

        had_current = (
            q["current"] is not None
        )

        waiting = len(
            q["items"]
        )

        q["items"].clear()

    event = stop_events.get(
        chat.id
    )

    if event:
        event.set()

    if not had_current and waiting == 0:

        await reply_text(
            update,
            "⏹️ Queue already empty.",
            reply_markup=main_keyboard()
        )

        return

    await reply_text(
        update,

        "⏹️ *Stopped.*\n\n"
        "Waiting queue cleared.",

        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callbacks(
    update,
    context
):

    query = update.callback_query

    if not query:
        return

    await answer_callback(
        update
    )

    data = query.data

    if data in (
        "help",
        "help_play"
    ):

        await help_command(
            update,
            context
        )

    elif data == "queue":

        await queue_command(
            update,
            context
        )

    elif data == "now":

        await now_command(
            update,
            context
        )

    elif data == "status":

        await status_command(
            update,
            context
        )

    elif data == "skip":

        await skip_command(
            update,
            context
        )

    elif data == "stop":

        await stop_command(
            update,
            context
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    logger.error(
        "Unhandled Telegram error",
        exc_info=context.error
    )


# ============================================================
# POST INIT
# ============================================================

async def post_init(
    application
):

    try:

        await application.bot.set_my_commands([
            ("start", "Start bot"),
            ("play", "Download YouTube audio"),
            ("queue", "Show queue"),
            ("now", "Current download"),
            ("skip", "Skip current"),
            ("stop", "Stop queue"),
            ("status", "Bot status"),
            ("help", "Help"),
        ])

        logger.info(
            "Bot commands registered."
        )

    except Exception:

        logger.exception(
            "Command registration failed."
        )


# ============================================================
# BUILD BOT
# ============================================================

def build_application():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CommandHandler(
            "play",
            play_command
        )
    )

    application.add_handler(
        CommandHandler(
            "queue",
            queue_command
        )
    )

    application.add_handler(
        CommandHandler(
            "now",
            now_command
        )
    )

    application.add_handler(
        CommandHandler(
            "skip",
            skip_command
        )
    )

    application.add_handler(
        CommandHandler(
            "stop",
            stop_command
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    # Buttons

    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    # YouTube URL

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            url_handler
        )
    )

    # Errors

    application.add_error_handler(
        error_handler
    )

    return application


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "================================"
    )

    logger.info(
        "Starting Resso Music Bot"
    )

    logger.info(
        "================================"
    )

    # Health server

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    logger.info(
        "Flask health server started on port %s",
        PORT
    )

    # Check cookies

    cookie = find_cookie_file()

    if cookie:
        logger.info(
            "YouTube authentication: ENABLED"
        )
    else:
        logger.warning(
            "YouTube authentication: NO COOKIES"
        )

    # Telegram

    application = build_application()

    logger.info(
        "Telegram polling starting..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()
