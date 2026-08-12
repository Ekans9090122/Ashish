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
from telegram.error import (
    TimedOut,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
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

DOWNLOAD_TIMEOUT = 600


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("music_bot")


# ============================================================
# FLASK / RENDER
# ============================================================

app = Flask(__name__)


@app.get("/")
def home():
    return jsonify(
        {
            "status": "online",
            "bot": "Resso Music Bot",
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
    try:
        app.run(
            host="0.0.0.0",
            port=PORT,
            debug=False,
            use_reloader=False,
        )
    except Exception:
        logger.exception("Flask server stopped")


# ============================================================
# DATA
# ============================================================

@dataclass
class Song:
    url: str
    title: str = "Unknown"
    requester: str = "Unknown"
    status: str = "queued"


@dataclass
class UserQueue:
    items: deque = field(default_factory=deque)
    current: Song | None = None


queues = defaultdict(UserQueue)

queue_lock = asyncio.Lock()

active_tasks = {}

stop_events = {}

skip_events = {}


# ============================================================
# KEYBOARD
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Play",
                    callback_data="help_play",
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
        ]
    )


# ============================================================
# HELPERS
# ============================================================

def clean_url(text: str) -> str:
    if not text:
        return ""

    return text.strip().split()[0]


def is_youtube_url(text: str) -> bool:
    if not text:
        return False

    try:
        parsed = urlparse(text.strip())

        host = (parsed.netloc or "").lower()

        if host.startswith("www."):
            host = host[4:]

        return (
            host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtu.be"
            or host.endswith(".youtu.be")
        )

    except Exception:
        return False


def safe_filename(name: str) -> str:
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

    hours, remainder = divmod(
        seconds,
        3600,
    )

    minutes, secs = divmod(
        remainder,
        60,
    )

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


# ============================================================
# COOKIE DETECTION
# ============================================================

def find_cookie_file():
    """
    Automatically checks common Render cookie locations.
    """

    candidates = []

    env_paths = [
        os.getenv("COOKIE_FILE", "").strip(),
        os.getenv("YOUTUBE_COOKIES_FILE", "").strip(),
        os.getenv("YOUTUBE_COOKIE_FILE", "").strip(),
    ]

    for value in env_paths:
        if value:
            candidates.append(Path(value))

    candidates.extend(
        [
            Path("cookies.txt"),
            Path("youtube_cookies.txt"),
            Path("youtube-cookies.txt"),
            Path("/etc/secrets/cookies.txt"),
            Path("/etc/secrets/youtube_cookies.txt"),
            Path("/etc/secrets/youtube-cookies.txt"),
        ]
    )

    # Search Render secret directory for a likely cookie file.
    secret_dir = Path("/etc/secrets")

    if secret_dir.exists():
        try:
            for item in secret_dir.iterdir():
                if not item.is_file():
                    continue

                name = item.name.lower()

                if (
                    "cookie" in name
                    and "youtube" in name
                ):
                    candidates.append(item)

        except Exception:
            pass

    checked = set()

    for path in candidates:

        try:
            path = path.resolve()
        except Exception:
            pass

        if str(path) in checked:
            continue

        checked.add(str(path))

        if path.exists() and path.is_file():

            try:
                size = path.stat().st_size

                if size > 20:
                    logger.info(
                        "YouTube cookies found: %s (%s bytes)",
                        path,
                        size,
                    )

                    return str(path)

            except Exception:
                continue

    logger.warning(
        "No YouTube cookies.txt found."
    )

    return None


def cookie_options():
    path = find_cookie_file()

    if not path:
        return {}

    return {
        "cookiefile": path,
    }


# ============================================================
# DENO
# ============================================================

def get_deno_path():
    possible = [
        os.getenv("DENO_PATH", "").strip(),
        os.path.join(
            os.getcwd(),
            ".deno",
            "bin",
            "deno",
        ),
        shutil.which("deno"),
    ]

    for path in possible:
        if not path:
            continue

        if os.path.isfile(path):
            return path

    return None


# ============================================================
# FFMPEG
# ============================================================

def get_ffmpeg_path():
    return shutil.which("ffmpeg")


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def base_ydl_options():

    options = {
        "quiet": True,
        "no_warnings": True,

        "noplaylist": True,

        "extract_flat": False,

        "ignoreerrors": False,

        "retries": 5,
        "fragment_retries": 5,

        "socket_timeout": 45,

        "concurrent_fragment_downloads": 2,

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
                "Chrome/131.0 "
                "Mobile Safari/537.36"
            ),

            "Accept-Language": (
                "en-US,en;q=0.9"
            ),
        },

        "geo_bypass": True,

        "check_formats": True,
    }

    # --------------------------------------------------------
    # COOKIES
    # --------------------------------------------------------

    options.update(
        cookie_options()
    )

    # --------------------------------------------------------
    # DENO / EJS
    # --------------------------------------------------------

    deno = get_deno_path()

    if deno:
        logger.info(
            "Deno detected: %s",
            deno,
        )

        options["js_runtimes"] = {
            "deno": {
                "path": deno,
            }
        }

    else:
        logger.warning(
            "Deno not detected."
        )

    return options


# ============================================================
# YOUTUBE INFO
# ============================================================

def extract_info_sync(url: str):

    options = base_ydl_options()

    options["skip_download"] = True

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(
            url,
            download=False,
        )


async def extract_info(url: str):

    return await asyncio.wait_for(
        asyncio.to_thread(
            extract_info_sync,
            url,
        ),
        timeout=DOWNLOAD_TIMEOUT,
    )


# ============================================================
# DOWNLOAD
# ============================================================

def download_audio_sync(
    url: str,
    output_dir: str,
):

    ffmpeg = get_ffmpeg_path()

    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg is not installed on Render."
        )

    output_template = os.path.join(
        output_dir,
        "%(title).150B [%(id)s].%(ext)s",
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

            "ffmpeg_location": ffmpeg,
        }
    )

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            url,
            download=True,
        )

        if not info:
            raise RuntimeError(
                "YouTube returned no information."
            )

        title = (
            info.get("title")
            or "Audio"
        )

        video_id = (
            info.get("id")
            or "audio"
        )

        duration = info.get(
            "duration"
        )

        # ----------------------------------------------------
        # Find MP3
        # ----------------------------------------------------

        mp3_files = list(
            Path(output_dir).glob(
                "*.mp3"
            )
        )

        if mp3_files:

            mp3_files.sort(
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )

            final_file = mp3_files[0]

        else:

            # ------------------------------------------------
            # Fallback conversion
            # ------------------------------------------------

            candidates = []

            for item in Path(
                output_dir
            ).iterdir():

                if item.is_file():

                    if item.suffix.lower() not in (
                        ".part",
                        ".ytdl",
                    ):
                        candidates.append(
                            item
                        )

            if not candidates:
                raise RuntimeError(
                    "Audio file was not created."
                )

            candidates.sort(
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )

            source = candidates[0]

            final_file = (
                Path(output_dir)
                / (
                    f"{safe_filename(title)} "
                    f"[{video_id}].mp3"
                )
            )

            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    str(final_file),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=300,
            )

        if not final_file.exists():
            raise RuntimeError(
                "Final MP3 file does not exist."
            )

        return {
            "title": title,
            "path": str(final_file),
            "duration": duration,
            "id": video_id,
        }


async def download_audio(
    url: str,
    output_dir: str,
):

    return await asyncio.wait_for(
        asyncio.to_thread(
            download_audio_sync,
            url,
            output_dir,
        ),
        timeout=DOWNLOAD_TIMEOUT,
        # IMPORTANT:
        # asyncio timeout does not kill the thread,
        # but it prevents the Telegram event loop
        # from waiting forever.
    )


# ============================================================
# SAFE TELEGRAM REPLY
# ============================================================

async def reply_text(
    update: Update,
    text: str,
    **kwargs,
):

    if update.message is not None:

        return await update.message.reply_text(
            text,
            **kwargs,
        )

    query = update.callback_query

    if query is not None:

        if query.message is not None:

            return await query.message.reply_text(
                text,
                **kwargs,
            )

    return None


async def answer_callback(
    update: Update,
    text=None,
):

    query = update.callback_query

    if query is None:
        return

    try:

        await query.answer(
            text=text,
            show_alert=False,
        )

    except Exception:
        pass


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "🎵 *Resso Music Bot*\n\n"

        "▶️ *Play*\n"
        "YouTube link bhejo ya:\n"
        "`/play <YouTube URL>`\n\n"

        "📋 *Queue* — waiting songs\n"
        "🎵 *Now* — current download\n"
        "⏭️ *Skip* — current song skip\n"
        "⏹️ *Stop* — current + queue stop\n"
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
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await reply_text(
        update,
        (
            "🎵 *Welcome to Resso Music Bot!*\n\n"
            "YouTube ka link bhejo.\n\n"
            "Example:\n"
            "`/play https://youtu.be/VIDEO_ID`\n\n"
            "👇 Buttons use karo."
        ),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# PLAY
# ============================================================

async def play_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not context.args:

        await reply_text(
            update,
            (
                "❌ YouTube URL do.\n\n"
                "Example:\n"
                "`/play https://youtu.be/VIDEO_ID`"
            ),
            parse_mode="Markdown",
        )

        return

    url = clean_url(
        context.args[0]
    )

    if not is_youtube_url(url):

        await reply_text(
            update,
            "❌ Valid YouTube URL nahi hai.",
        )

        return

    await add_song(
        update,
        url,
    )


# ============================================================
# DIRECT URL
# ============================================================

async def url_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.message is None:
        return

    text = (
        update.message.text
        or ""
    ).strip()

    if not text:
        return

    if not is_youtube_url(text):
        return

    await add_song(
        update,
        clean_url(text),
    )


# ============================================================
# ADD SONG
# ============================================================

async def add_song(
    update: Update,
    url: str,
):

    user = update.effective_user

    requester = (
        user.full_name
        if user is not None
        else "Unknown"
    )

    message = await reply_text(
        update,
        "🔎 YouTube information check kar raha hoon...",
    )

    try:

        info = await extract_info(
            url
        )

        title = (
            info.get("title")
            or "Unknown"
        )

        duration = format_duration(
            info.get("duration")
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

            was_empty = (
                q.current is None
                and len(q.items) == 0
            )

            q.items.append(song)

            position = len(q.items)

        text = (
            "✅ *Added to queue*\n\n"
            f"🎵 *{title}*\n"
            f"⏱️ {duration}\n"
            f"👤 {requester}\n"
        )

        if was_empty:
            text += "\n⏳ Download starting..."

        else:
            text += (
                f"\n📋 Position: {position}"
            )

        if message is not None:

            try:

                await message.edit_text(
                    text,
                    parse_mode="Markdown",
                    reply_markup=main_keyboard(),
                )

            except Exception:
                pass

        # ----------------------------------------------------
        # Start worker
        # ----------------------------------------------------

        existing = active_tasks.get(
            chat_id
        )

        if (
            existing is None
            or existing.done()
        ):

            task = asyncio.create_task(
                process_queue(
                    chat_id,
                    update.get_bot(),
                )
            )

            active_tasks[chat_id] = task

            def cleanup(
                finished_task,
                cid=chat_id,
            ):

                current_task = (
                    active_tasks.get(cid)
                )

                if current_task is finished_task:
                    active_tasks.pop(
                        cid,
                        None,
                    )

            task.add_done_callback(
                cleanup
            )

    except Exception as exc:

        logger.exception(
            "YouTube info failed"
        )

        error = str(exc)

        lower = error.lower()

        if (
            "sign in" in lower
            or "cookies" in lower
            or "authentication" in lower
            or "not a bot" in lower
        ):

            text = (
                "❌ *YouTube authentication required.*\n\n"
                "Render mein valid YouTube "
                "cookies.txt Secret File set karo.\n\n"
                "Phir redeploy karke dobara try karo."
            )

        elif isinstance(
            exc,
            (asyncio.TimeoutError, TimeoutError),
        ):

            text = (
                "⏱️ *YouTube request timed out.*\n\n"
                "Thodi der baad dobara try karo."
            )

        else:

            text = (
                "❌ *YouTube information fetch failed.*\n\n"
                f"`{error[:1200]}`"
            )

        if message is not None:

            try:

                await message.edit_text(
                    text,
                    parse_mode="Markdown",
                    reply_markup=main_keyboard(),
                )

            except Exception:
                await reply_text(
                    update,
                    text,
                    parse_mode="Markdown",
                )


# ============================================================
# QUEUE WORKER
# ============================================================

async def process_queue(
    chat_id: int,
    bot,
):

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

                song.status = "downloading"

                q.current = song

                skip_event.clear()
                stop_event.clear()

            temp_dir = tempfile.mkdtemp(
                prefix="resso_"
            )

            progress_message = None

            try:

                await safe_chat_action(
                    bot,
                    chat_id,
                )

                progress_message = (
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "⏳ *Downloading...*\n\n"
                            f"🎵 {song.title}"
                        ),
                        parse_mode="Markdown",
                        reply_markup=main_keyboard(),
                    )
                )

                result = await download_audio(
                    song.url,
                    temp_dir,
                )

                if stop_event.is_set():

                    await safe_edit(
                        progress_message,
                        "⏹️ Stopped.",
                    )

                    break

                if skip_event.is_set():

                    await safe_edit(
                        progress_message,
                        "⏭️ Skipped.",
                    )

                    continue

                file_path = Path(
                    result["path"]
                )

                if not file_path.exists():

                    raise RuntimeError(
                        "Downloaded MP3 not found."
                    )

                file_size = (
                    file_path.stat().st_size
                )

                if file_size <= 0:

                    raise RuntimeError(
                        "Downloaded file is empty."
                    )

                if file_size > MAX_FILE_SIZE:

                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "❌ File Telegram limit "
                            "se bada hai.\n\n"
                            f"Size: "
                            f"{file_size / 1024 / 1024:.1f} MB"
                        ),
                        reply_markup=main_keyboard(),
                    )

                    continue

                await safe_edit(
                    progress_message,
                    (
                        "📤 *Uploading...*\n\n"
                        f"🎵 {result['title']}"
                    ),
                    parse_mode="Markdown",
                )

                with open(
                    file_path,
                    "rb",
                ) as audio:

                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=audio,
                        title=(
                            result["title"][:64]
                        ),
                        duration=(
                            int(result["duration"])
                            if result.get("duration")
                            else None
                        ),
                        caption=(
                            f"🎵 {result['title']}\n"
                            f"🤖 Resso Music Bot"
                        ),
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

                await safe_delete(
                    progress_message
                )

            except Exception as exc:

                logger.exception(
                    "Download error for %s",
                    chat_id,
                )

                if stop_event.is_set():

                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏹️ Current download stopped.",
                        reply_markup=main_keyboard(),
                    )

                    break

                if skip_event.is_set():

                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏭️ Current song skipped.",
                        reply_markup=main_keyboard(),
                    )

                    continue

                error = str(exc)

                lower = error.lower()

                if (
                    "sign in" in lower
                    or "cookies" in lower
                    or "authentication" in lower
                    or "not a bot" in lower
                ):

                    text = (
                        "❌ *YouTube authentication required.*\n\n"
                        "Render Secret Files mein "
                        "valid YouTube cookies.txt check karo."
                    )

                elif isinstance(
                    exc,
                    (asyncio.TimeoutError, TimeoutError),
                ):

                    text = (
                        "⏱️ Download timeout.\n\n"
                        "Song bahut slow/long ho sakta hai."
                    )

                elif isinstance(
                    exc,
                    (TimedOut, NetworkError),
                ):

                    text = (
                        "🌐 Telegram network timeout.\n\n"
                        "Bot automatically continue karega."
                    )

                else:

                    text = (
                        "❌ *Download failed.*\n\n"
                        f"🎵 {song.title}\n\n"
                        f"`{error[:1200]}`"
                    )

                try:

                    await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=main_keyboard(),
                    )

                except Exception:
                    logger.exception(
                        "Could not send error message"
                    )

            finally:

                shutil.rmtree(
                    temp_dir,
                    ignore_errors=True,
                )

                async with queue_lock:

                    queues[chat_id].current = None

        # ----------------------------------------------------
        # Queue completed
        # ----------------------------------------------------

        try:

            await bot.send_message(
                chat_id=chat_id,
                text="✅ *Queue finished.*",
                parse_mode="Markdown",
                reply_markup=main_keyboard(),
            )

        except Exception:
            pass

    except asyncio.CancelledError:

        logger.info(
            "Queue cancelled: %s",
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
# TELEGRAM SAFE FUNCTIONS
# ============================================================

async def safe_chat_action(
    bot,
    chat_id,
):

    try:

        await bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.UPLOAD_DOCUMENT,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=20,
            pool_timeout=20,
        )

    except Exception:
        pass


async def safe_edit(
    message,
    text,
    **kwargs,
):

    if message is None:
        return

    try:

        await message.edit_text(
            text,
            **kwargs,
        )

    except Exception:
        pass


async def safe_delete(
    message,
):

    if message is None:
        return

    try:

        await message.delete()

    except Exception:
        pass


# ============================================================
# QUEUE COMMAND
# ============================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if chat is None:
        return

    async with queue_lock:

        q = queues[chat.id]

        current = q.current

        pending = list(
            q.items
        )

    lines = [
        "📋 *Queue*",
        "",
    ]

    if current is not None:

        lines.extend(
            [
                "🎵 *Current:*",
                current.title,
                "",
            ]
        )

    if pending:

        lines.append(
            "⏳ *Waiting:*"
        )

        for i, song in enumerate(
            pending,
            start=1,
        ):

            lines.append(
                f"{i}. {song.title}"
            )

    elif current is None:

        lines.append(
            "Queue empty."
        )

    await reply_text(
        update,
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# NOW
# ============================================================

async def now_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if chat is None:
        return

    async with queue_lock:

        current = (
            queues[chat.id].current
        )

    if current is None:

        await reply_text(
            update,
            "🎵 Abhi kuch download nahi ho raha.",
            reply_markup=main_keyboard(),
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
        reply_markup=main_keyboard(),
    )


# ============================================================
# STATUS
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    waiting = 0
    current = "Nothing"

    if chat is not None:

        async with queue_lock:

            q = queues[chat.id]

            waiting = len(
                q.items
            )

            if q.current is not None:

                current = (
                    q.current.title
                )

    active = len(
        active_tasks
    )

    cookies = find_cookie_file()

    deno = get_deno_path()

    ffmpeg = get_ffmpeg_path()

    text = (
        "📊 *Bot Status*\n\n"

        "🟢 Bot: Online\n"
        f"⚙️ Active queues: {active}\n"
        f"📋 Waiting: {waiting}\n"
        f"🎵 Current: {current}\n\n"

        f"🍪 Cookies: "
        f"{'OK' if cookies else 'Missing'}\n"

        f"🦕 Deno: "
        f"{'OK' if deno else 'Missing'}\n"

        f"🎞️ FFmpeg: "
        f"{'OK' if ffmpeg else 'Missing'}"
    )

    await reply_text(
        update,
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# SKIP
# ============================================================

async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if chat is None:
        return

    async with queue_lock:

        current = (
            queues[chat.id].current
        )

    if current is None:

        await reply_text(
            update,
            "⏭️ Abhi koi song download nahi ho raha.",
            reply_markup=main_keyboard(),
        )

        return

    event = skip_events.get(
        chat.id
    )

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
# STOP
# ============================================================

async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if chat is None:
        return

    async with queue_lock:

        q = queues[chat.id]

        had_current = (
            q.current is not None
        )

        waiting = len(
            q.items
        )

        q.items.clear()

    event = stop_events.get(
        chat.id
    )

    if event is not None:

        event.set()

    if (
        not had_current
        and waiting == 0
    ):

        await reply_text(
            update,
            "⏹️ Queue already empty.",
            reply_markup=main_keyboard(),
        )

        return

    await reply_text(
        update,
        (
            "⏹️ *Stopped.*\n\n"
            "Current download stop request "
            "bhej diya gaya hai.\n"
            "Waiting queue clear kar di."
        ),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if query is None:
        return

    data = query.data or ""

    logger.info(
        "Callback: %s",
        data,
    )

    await answer_callback(
        update
    )

    if data in (
        "help",
        "help_play",
    ):

        await help_command(
            update,
            context,
        )

    elif data == "queue":

        await queue_command(
            update,
            context,
        )

    elif data == "now":

        await now_command(
            update,
            context,
        )

    elif data == "status":

        await status_command(
            update,
            context,
        )

    elif data == "skip":

        await skip_command(
            update,
            context,
        )

    elif data == "stop":

        await stop_command(
            update,
            context,
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    error = context.error

    if isinstance(
        error,
        RetryAfter,
    ):

        logger.warning(
            "Telegram rate limit: %s",
            error,
        )

        return

    if isinstance(
        error,
        (TimedOut, NetworkError),
    ):

        logger.warning(
            "Telegram network timeout: %s",
            error,
        )

        return

    logger.error(
        "Unhandled Telegram error",
        exc_info=error,
    )


# ============================================================
# COMMANDS
# ============================================================

async def set_commands(
    application: Application,
):

    await application.bot.set_my_commands(
        [
            (
                "start",
                "Start bot",
            ),
            (
                "play",
                "Download YouTube audio",
            ),
            (
                "queue",
                "Show queue",
            ),
            (
                "now",
                "Current download",
            ),
            (
                "skip",
                "Skip current",
            ),
            (
                "stop",
                "Stop queue",
            ),
            (
                "status",
                "Bot status",
            ),
            (
                "help",
                "Show help",
            ),
        ]
    )


# ============================================================
# POST INIT
# ============================================================

async def post_init(
    application: Application,
):

    try:

        await set_commands(
            application
        )

        logger.info(
            "Bot commands registered."
        )

    except Exception:

        logger.exception(
            "Could not register commands."
        )


# ============================================================
# BUILD APPLICATION
# ============================================================

def build_application():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)

        # Normal Telegram requests
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(120)
        .pool_timeout(30)

        # getUpdates polling
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(60)
        .get_updates_write_timeout(60)
        .get_updates_pool_timeout(30)

        .post_init(post_init)
    )

    application = builder.build()

    # --------------------------------------------------------
    # Commands
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
    # Buttons
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    # --------------------------------------------------------
    # YouTube URL
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            url_message_handler,
        )
    )

    # --------------------------------------------------------
    # Error handler
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
        "===================================="
    )

    logger.info(
        "Starting Resso Music Bot"
    )

    logger.info(
        "Python: %s",
        os.sys.version,
    )

    logger.info(
        "Port: %s",
        PORT,
    )

    logger.info(
        "Cookies: %s",
        find_cookie_file() or "MISSING",
    )

    logger.info(
        "Deno: %s",
        get_deno_path() or "MISSING",
    )

    logger.info(
        "FFmpeg: %s",
        get_ffmpeg_path() or "MISSING",
    )

    logger.info(
        "===================================="
    )

    # --------------------------------------------------------
    # Flask
    # --------------------------------------------------------

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
        name="render-health",
    )

    flask_thread.start()

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    application = build_application()

    logger.info(
        "Telegram polling starting..."
    )

    application.run_polling(
        poll_interval=2,
        timeout=30,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        close_loop=True,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
