import os
import re
import asyncio
import logging
import tempfile
import threading
import shutil
import subprocess
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

# Optional YouTube cookies.
# If you upload cookies.txt to the Render project root,
# this will automatically use it.
COOKIE_FILE = os.getenv("COOKIE_FILE", "cookies.txt").strip()

# Maximum Telegram upload size is handled conservatively.
MAX_FILE_SIZE = 49 * 1024 * 1024

DOWNLOAD_TIMEOUT = 300

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# FLASK / RENDER HEALTH SERVER
# ============================================================

app = Flask(__name__)


@app.get("/")
def home():
    return jsonify(
        {
            "status": "online",
            "service": "Telegram Music Bot",
        }
    )


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
        }
    )


def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class Song:
    url: str
    title: str = "Unknown"
    requester: str = ""
    status: str = "queued"


@dataclass
class UserQueue:
    items: deque = field(default_factory=deque)
    current: Song | None = None


queues = defaultdict(UserQueue)

# One lock protects queue changes.
queue_lock = asyncio.Lock()

# Currently running downloads/tasks.
active_tasks = {}

# Used to stop the current download/playback.
stop_events = {}

# Used for skip requests.
skip_events = {}


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ Play", callback_data="help_play"),
                InlineKeyboardButton("📋 Queue", callback_data="queue"),
            ],
            [
                InlineKeyboardButton("⏭️ Skip", callback_data="skip"),
                InlineKeyboardButton("⏹️ Stop", callback_data="stop"),
            ],
            [
                InlineKeyboardButton("🎵 Now", callback_data="now"),
                InlineKeyboardButton("ℹ️ Help", callback_data="help"),
            ],
            [
                InlineKeyboardButton("📊 Status", callback_data="status"),
            ],
        ]
    )


# ============================================================
# BASIC HELPERS
# ============================================================

def is_youtube_url(text: str) -> bool:
    if not text:
        return False

    try:
        parsed = urlparse(text.strip())
        host = (parsed.netloc or "").lower()

        return (
            "youtube.com" in host
            or "youtu.be" in host
            or "music.youtube.com" in host
        )
    except Exception:
        return False


def clean_url(text: str) -> str:
    return text.strip().split()[0]


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        name = "audio"

    return name[:180]


def format_duration(seconds):
    if not seconds:
        return "Unknown"

    try:
        seconds = int(seconds)
    except Exception:
        return "Unknown"

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


def get_cookie_options():
    """
    Returns yt-dlp cookie options only when cookies.txt exists.
    """
    path = Path(COOKIE_FILE)

    if path.exists() and path.is_file():
        logger.info("Using cookie file: %s", path)
        return {
            "cookiefile": str(path),
        }

    logger.info("No cookies.txt found. Continuing without cookies.")
    return {}


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def base_ydl_options():
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,

        # YouTube extraction.
        "format": (
            "bestaudio[ext=m4a]/"
            "bestaudio[ext=webm]/"
            "bestaudio/best"
        ),

        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,

        "concurrent_fragment_downloads": 4,

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    options.update(get_cookie_options())

    return options


# ============================================================
# YOUTUBE INFO
# ============================================================

def extract_info_sync(url: str):
    """
    Extract YouTube metadata.
    Runs in a worker thread so Telegram event loop isn't blocked.
    """

    options = base_ydl_options()

    options.update(
        {
            "skip_download": True,
        }
    )

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


async def extract_info(url: str):
    return await asyncio.to_thread(
        extract_info_sync,
        url,
    )


# ============================================================
# AUDIO DOWNLOAD
# ============================================================

def download_audio_sync(url: str, output_dir: str):
    """
    Download best available audio and convert to MP3 using ffmpeg.
    """

    output_template = os.path.join(
        output_dir,
        "%(title).180B [%(id)s].%(ext)s",
    )

    options = base_ydl_options()

    options.update(
        {
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
        }
    )

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

        title = info.get("title") or "Audio"
        video_id = info.get("id") or "audio"

        # Find resulting MP3.
        files = list(Path(output_dir).glob("*.mp3"))

        if not files:
            # Fallback: search any downloaded media.
            candidates = [
                p
                for p in Path(output_dir).iterdir()
                if p.is_file()
            ]

            if candidates:
                source = candidates[0]

                mp3_path = Path(output_dir) / (
                    f"{safe_filename(title)} [{video_id}].mp3"
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
                        str(mp3_path),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                files = [mp3_path]

        if not files:
            raise RuntimeError(
                "Audio file was not created by yt-dlp/ffmpeg."
            )

        # Pick newest file.
        files.sort(
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        final_file = files[0]

        return {
            "title": title,
            "path": str(final_file),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "id": video_id,
        }


async def download_audio(url: str, output_dir: str):
    return await asyncio.to_thread(
        download_audio_sync,
        url,
        output_dir,
    )


# ============================================================
# TELEGRAM SAFE REPLY HELPERS
# ============================================================

async def reply_text(update: Update, text: str, **kwargs):
    """
    IMPORTANT:
    Works for BOTH normal messages and callback queries.

    This fixes:
        AttributeError:
        'NoneType' object has no attribute 'reply_text'
    """

    if update.message is not None:
        return await update.message.reply_text(
            text,
            **kwargs,
        )

    if update.callback_query is not None:
        query = update.callback_query

        if query.message is not None:
            return await query.message.reply_text(
                text,
                **kwargs,
            )

    return None


async def edit_text(update: Update, text: str, **kwargs):
    """
    Safely edit callback message.
    """

    query = update.callback_query

    if query is None:
        return await reply_text(
            update,
            text,
            **kwargs,
        )

    try:
        await query.edit_message_text(
            text,
            **kwargs,
        )
    except Exception:
        if query.message is not None:
            return await query.message.reply_text(
                text,
                **kwargs,
            )


async def answer_callback(update: Update):
    query = update.callback_query

    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass


# ============================================================
# HELP COMMAND
# ============================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎵 *Music Bot Help*\n\n"
        "▶️ *Play*\n"
        "Send a YouTube URL or use:\n"
        "`/play <YouTube URL>`\n\n"
        "📋 *Queue* — queued songs\n"
        "🎵 *Now* — currently downloading song\n"
        "⏭️ *Skip* — skip current song\n"
        "⏹️ *Stop* — stop current job and clear queue\n"
        "📊 *Status* — bot status\n\n"
        "Example:\n"
        "`/play https://youtu.be/VIDEO_ID`"
    )

    await reply_text(
        update,
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# START COMMAND
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎵 *Welcome!*\n\n"
        "YouTube se audio download karne ke liye "
        "YouTube link bhejo.\n\n"
        "Example:\n"
        "`/play https://youtu.be/VIDEO_ID`\n\n"
        "Neeche buttons bhi available hain 👇"
    )

    await reply_text(
        update,
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# PLAY COMMAND
# ============================================================

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await reply_text(
            update,
            "❌ YouTube URL do.\n\n"
            "Example:\n"
            "`/play https://youtu.be/VIDEO_ID`",
            parse_mode="Markdown",
        )
        return

    url = clean_url(context.args[0])

    if not is_youtube_url(url):
        await reply_text(
            update,
            "❌ Ye valid YouTube URL nahi lag raha."
        )
        return

    await add_song_to_queue(
        update,
        url,
    )


# ============================================================
# URL MESSAGE HANDLER
# ============================================================

async def url_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.message is None:
        return

    text = (update.message.text or "").strip()

    if not text:
        return

    if not is_youtube_url(text):
        return

    url = clean_url(text)

    await add_song_to_queue(
        update,
        url,
    )


# ============================================================
# ADD SONG TO QUEUE
# ============================================================

async def add_song_to_queue(
    update: Update,
    url: str,
):
    user = update.effective_user

    requester = (
        user.full_name
        if user is not None
        else "Unknown"
    )

    status_message = await reply_text(
        update,
        "🔎 Getting YouTube information..."
    )

    try:
        info = await extract_info(url)

        title = info.get("title") or "Unknown"

        duration = format_duration(
            info.get("duration")
        )

        song = Song(
            url=url,
            title=title,
            requester=requester,
            status="queued",
        )

        chat = update.effective_chat

        if chat is None:
            return

        chat_id = chat.id

        async with queue_lock:
            user_queue = queues[chat_id]

            was_empty = (
                user_queue.current is None
                and len(user_queue.items) == 0
            )

            user_queue.items.append(song)

        text = (
            "✅ *Added to queue*\n\n"
            f"🎵 *{title}*\n"
            f"⏱️ {duration}\n"
            f"👤 {requester}\n"
        )

        if was_empty:
            text += "\n⏳ Starting download..."
        else:
            text += (
                f"\n📋 Position: "
                f"{len(queues[chat_id].items)}"
            )

        if status_message is not None:
            try:
                await status_message.edit_text(
                    text,
                    parse_mode="Markdown",
                    reply_markup=main_keyboard(),
                )
            except Exception:
                pass

        # Start queue worker if needed.
        if chat_id not in active_tasks:
            task = asyncio.create_task(
                process_queue(
                    chat_id,
                    update.get_bot(),
                )
            )

            active_tasks[chat_id] = task

            def cleanup(_task):
                active_tasks.pop(chat_id, None)

            task.add_done_callback(cleanup)

    except Exception as exc:
        logger.exception(
            "Failed to add song: %s",
            exc,
        )

        error_text = (
            "❌ *YouTube information fetch failed.*\n\n"
            f"Technical error:\n`{str(exc)[:1000]}`"
        )

        if status_message is not None:
            try:
                await status_message.edit_text(
                    error_text,
                    parse_mode="Markdown",
                    reply_markup=main_keyboard(),
                )
            except Exception:
                await reply_text(
                    update,
                    error_text,
                    parse_mode="Markdown",
                )
        else:
            await reply_text(
                update,
                error_text,
                parse_mode="Markdown",
            )


# ============================================================
# QUEUE WORKER
# ============================================================

async def process_queue(chat_id: int, bot):
    """
    Processes one chat's queue at a time.
    """

    stop_event = asyncio.Event()
    skip_event = asyncio.Event()

    stop_events[chat_id] = stop_event
    skip_events[chat_id] = skip_event

    try:
        while True:

            async with queue_lock:
                user_queue = queues[chat_id]

                if not user_queue.items:
                    user_queue.current = None
                    break

                song = user_queue.items.popleft()
                song.status = "downloading"
                user_queue.current = song

                # Reset skip state for this song.
                skip_event.clear()
                stop_event.clear()

            temp_dir = tempfile.mkdtemp(
                prefix=f"yt_{chat_id}_"
            )

            try:
                await bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.UPLOAD_DOCUMENT,
                )

                progress_message = await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⏳ *Downloading...*\n\n"
                        f"🎵 {song.title}"
                    ),
                    parse_mode="Markdown",
                    reply_markup=main_keyboard(),
                )

                result = await download_audio(
                    song.url,
                    temp_dir,
                )

                if skip_event.is_set():
                    try:
                        await progress_message.edit_text(
                            "⏭️ Skipped."
                        )
                    except Exception:
                        pass

                    continue

                if stop_event.is_set():
                    try:
                        await progress_message.edit_text(
                            "⏹️ Stopped."
                        )
                    except Exception:
                        pass

                    break

                file_path = Path(
                    result["path"]
                )

                if not file_path.exists():
                    raise FileNotFoundError(
                        "Downloaded audio file not found."
                    )

                file_size = file_path.stat().st_size

                if file_size > MAX_FILE_SIZE:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "❌ File is too large for Telegram.\n\n"
                            f"Size: "
                            f"{file_size / 1024 / 1024:.1f} MB"
                        ),
                    )
                    continue

                await progress_message.edit_text(
                    (
                        "📤 *Uploading...*\n\n"
                        f"🎵 {result['title']}"
                    ),
                    parse_mode="Markdown",
                )

                with open(file_path, "rb") as audio_file:
                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_file,
                        title=result["title"][:64],
                        duration=(
                            int(result["duration"])
                            if result.get("duration")
                            else None
                        ),
                        caption=(
                            f"🎵 {result['title']}\n"
                            f"🤖 Downloaded by Music Bot"
                        ),
                    )

                try:
                    await progress_message.delete()
                except Exception:
                    pass

            except Exception as exc:
                logger.exception(
                    "Queue download failed for %s: %s",
                    chat_id,
                    exc,
                )

                if skip_event.is_set():
                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏭️ Current song skipped.",
                        reply_markup=main_keyboard(),
                    )

                elif stop_event.is_set():
                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏹️ Download stopped.",
                        reply_markup=main_keyboard(),
                    )
                    break

                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "❌ *YouTube download failed.*\n\n"
                            f"🎵 {song.title}\n\n"
                            "ℹ️ Technical error:\n"
                            f"`{str(exc)[:1200]}`"
                        ),
                        parse_mode="Markdown",
                        reply_markup=main_keyboard(),
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
            reply_markup=main_keyboard(),
        )

    except asyncio.CancelledError:
        logger.info(
            "Queue task cancelled: %s",
            chat_id,
        )
        raise

    finally:
        stop_events.pop(chat_id, None)
        skip_events.pop(chat_id, None)

        async with queue_lock:
            queues[chat_id].current = None# ============================================================
# QUEUE COMMAND
# ============================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat = update.effective_chat

    if chat is None:
        return

    chat_id = chat.id

    async with queue_lock:
        user_queue = queues[chat_id]

        current = user_queue.current
        pending = list(user_queue.items)

    lines = ["📋 *Queue*\n"]

    if current is not None:
        lines.append(
            f"🎵 *Now downloading:*\n"
            f"{current.title}\n"
        )

    if pending:
        lines.append("\n⏳ *Waiting:*\n")

        for index, song in enumerate(pending, start=1):
            lines.append(
                f"{index}. {song.title}"
            )
    elif current is None:
        lines.append("Queue empty.")

    await reply_text(
        update,
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# NOW COMMAND
# ============================================================

async def now_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat = update.effective_chat

    if chat is None:
        return

    chat_id = chat.id

    async with queue_lock:
        current = queues[chat_id].current

    if current is None:
        await reply_text(
            update,
            "🎵 Nothing is downloading right now.",
            reply_markup=main_keyboard(),
        )
        return

    await reply_text(
        update,
        (
            "🎵 *Now downloading*\n\n"
            f"🎶 {current.title}\n"
            f"👤 {current.requester}\n"
            f"📌 Status: {current.status}"
        ),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# STATUS COMMAND
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    IMPORTANT:
    This function never directly assumes update.message exists.

    Callback buttons use update.callback_query, so reply_text()
    above safely handles both cases.
    """

    chat = update.effective_chat

    total_queued = 0
    current_title = "Nothing"

    if chat is not None:
        async with queue_lock:
            user_queue = queues[chat.id]

            total_queued = len(user_queue.items)

            if user_queue.current is not None:
                current_title = (
                    user_queue.current.title
                )

    active_count = len(active_tasks)

    text = (
        "📊 *Bot Status*\n\n"
        "🟢 Bot: Online\n"
        f"⚙️ Active queues: {active_count}\n"
        f"📋 Waiting songs: {total_queued}\n"
        f"🎵 Current: {current_title}"
    )

    await reply_text(
        update,
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# SKIP COMMAND
# ============================================================

async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat = update.effective_chat

    if chat is None:
        return

    chat_id = chat.id

    async with queue_lock:
        current = queues[chat_id].current

    if current is None:
        await reply_text(
            update,
            "⏭️ Nothing is currently downloading.",
            reply_markup=main_keyboard(),
        )
        return

    event = skip_events.get(chat_id)

    if event is not None:
        event.set()

    await reply_text(
        update,
        (
            "⏭️ *Skip requested.*\n\n"
            f"🎵 {current.title}"
        ),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# STOP COMMAND
# ============================================================

async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat = update.effective_chat

    if chat is None:
        return

    chat_id = chat.id

    async with queue_lock:
        user_queue = queues[chat_id]

        had_current = user_queue.current is not None
        waiting_count = len(user_queue.items)

        user_queue.items.clear()

    event = stop_events.get(chat_id)

    if event is not None:
        event.set()

    if not had_current and waiting_count == 0:
        await reply_text(
            update,
            "⏹️ Nothing to stop. Queue is already empty.",
            reply_markup=main_keyboard(),
        )
        return

    await reply_text(
        update,
        (
            "⏹️ *Stopped.*\n\n"
            "Current download will stop and "
            "the waiting queue has been cleared."
        ),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# CALLBACK BUTTON HANDLER
# ============================================================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query is None:
        return

    await answer_callback(update)

    data = query.data or ""

    logger.info(
        "Callback received: %s",
        data,
    )

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    if data in ("help", "help_play"):
        await help_command(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # QUEUE
    # --------------------------------------------------------

    if data == "queue":
        await queue_command(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # NOW
    # --------------------------------------------------------

    if data == "now":
        await now_command(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    if data == "status":
        await status_command(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # SKIP
    # --------------------------------------------------------

    if data == "skip":
        await skip_command(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    if data == "stop":
        await stop_command(
            update,
            context,
        )
        return

    logger.warning(
        "Unknown callback: %s",
        data,
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Global Telegram error handler.

    This also prevents the:
        No error handlers are registered
    message.
    """

    logger.error(
        "Unhandled Telegram exception",
        exc_info=context.error,
    )

    error = context.error

    # Don't crash the bot because a message/callback failed.
    try:
        if isinstance(update, Update):
            if update.callback_query is not None:
                try:
                    await update.callback_query.answer(
                        "⚠️ Something went wrong.",
                        show_alert=False,
                    )
                except Exception:
                    pass

    except Exception:
        logger.exception(
            "Error while handling error callback"
        )


# ============================================================
# COMMAND LIST
# ============================================================

async def set_commands(
    application: Application,
):
    await application.bot.set_my_commands(
        [
            ("start", "Start the bot"),
            ("play", "Download YouTube audio"),
            ("queue", "Show queue"),
            ("now", "Show current download"),
            ("skip", "Skip current song"),
            ("stop", "Stop and clear queue"),
            ("status", "Show bot status"),
            ("help", "Show help"),
        ]
    )


# ============================================================
# APPLICATION STARTUP
# ============================================================

def build_application():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
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

    # --------------------------------------------------------
    # INLINE BUTTONS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    # --------------------------------------------------------
    # YOUTUBE URL MESSAGES
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            url_message_handler,
        )
    )

    # --------------------------------------------------------
    # GLOBAL ERROR HANDLER
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    return application


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info(
        "Starting Telegram bot..."
    )

    # Start Flask health server for Render.
    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
        name="flask-health-server",
    )

    flask_thread.start()

    logger.info(
        "Flask health server started on port %s",
        PORT,
    )

    application = build_application()

    logger.info(
        "Telegram application starting..."
    )

    # post_init is used for setting bot commands.
    async def post_init(
        app_instance: Application,
    ):
        try:
            await set_commands(
                app_instance
            )
            logger.info(
                "Bot commands registered."
            )
        except Exception:
            logger.exception(
                "Could not register bot commands."
            )

    application.post_init = post_init

    logger.info(
        "Bot is now polling."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
