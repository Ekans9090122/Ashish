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

RENDER_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    ""
).strip()

WEBHOOK_SECRET = os.environ.get(
    "WEBHOOK_SECRET",
    ""
).strip()


# ---------------------------------------------------------
# TELEGRAM FILE SIZE
# ---------------------------------------------------------

TARGET_MB = 40
MAX_MB = 45

TARGET_BYTES = TARGET_MB * 1024 * 1024
MAX_BYTES = MAX_MB * 1024 * 1024


# ---------------------------------------------------------
# COOKIE FILE
# ---------------------------------------------------------

COOKIE_SECRET_PATH = Path(
    "/etc/secrets/youtube_cookies.txt"
)

COOKIE_TEMP_PATH = Path(
    "/tmp/youtube_cookies.txt"
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
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
            "ffmpeg": ffmpeg_available(),
            "yt_dlp": yt_dlp.version.__version__,
        }
    )


# =========================================================
# GLOBAL STATE
# =========================================================

application = None
bot_loop = None

queues = defaultdict(deque)

current_tasks = {}

worker_running = set()

stop_requested = set()


# =========================================================
# COOKIE SETUP
# =========================================================

def prepare_cookies():

    try:

        if not COOKIE_SECRET_PATH.exists():

            logger.warning(
                "Cookie file not found: %s",
                COOKIE_SECRET_PATH,
            )

            return None

        shutil.copy2(
            COOKIE_SECRET_PATH,
            COOKIE_TEMP_PATH,
        )

        # Make sure file is readable.
        try:
            os.chmod(
                COOKIE_TEMP_PATH,
                0o600,
            )
        except Exception:
            pass

        logger.info(
            "YouTube cookies loaded successfully."
        )

        return str(COOKIE_TEMP_PATH)

    except Exception as e:

        logger.exception(
            "Cookie setup failed: %s",
            e,
        )

        return None


# =========================================================
# FFMPEG
# =========================================================

def ffmpeg_available():

    return shutil.which("ffmpeg") is not None


def ffprobe_available():

    return shutil.which("ffprobe") is not None


def get_duration(file_path):

    if not ffprobe_available():
        return 0.0

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

        logger.warning(
            "Duration detection failed: %s",
            e,
        )

    return 0.0


# =========================================================
# AUDIO BITRATE
# =========================================================

def calculate_bitrate(duration):

    if duration <= 0:

        return 128

    # Keep output safely below 40 MB.
    safe_bytes = 38 * 1024 * 1024

    bitrate = int(
        (safe_bytes * 8)
        / duration
        / 1000
    )

    bitrate = min(
        bitrate,
        192,
    )

    bitrate = max(
        bitrate,
        32,
    )

    return bitrate


# =========================================================
# AUDIO COMPRESSION
# =========================================================

def compress_audio(input_file):

    input_file = Path(input_file)

    if not input_file.exists():
        raise RuntimeError(
            "Downloaded audio file does not exist."
        )

    original_size = input_file.stat().st_size

    logger.info(
        "Original size: %.2f MB",
        original_size / 1024 / 1024,
    )

    # Already safe.
    if original_size <= TARGET_BYTES:

        logger.info(
            "Audio already below %s MB.",
            TARGET_MB,
        )

        return input_file

    if not ffmpeg_available():

        raise RuntimeError(
            "FFmpeg is not installed."
        )

    duration = get_duration(
        input_file
    )

    bitrate = calculate_bitrate(
        duration
    )

    output_file = input_file.with_name(
        input_file.stem
        + "_resso.mp3"
    )

    attempts = [
        bitrate,
        160,
        128,
        112,
        96,
        80,
        64,
        48,
        40,
        32,
    ]

    tried = set()

    for kbps in attempts:

        if kbps in tried:
            continue

        tried.add(kbps)

        try:

            if output_file.exists():
                output_file.unlink()

        except Exception:
            pass

        logger.info(
            "FFmpeg compression: %s kbps",
            kbps,
        )

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

        try:

            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=900,
            )

        except subprocess.TimeoutExpired:

            logger.warning(
                "FFmpeg timed out at %s kbps.",
                kbps,
            )

            continue

        if result.returncode != 0:

            logger.warning(
                "FFmpeg failed at %s kbps: %s",
                kbps,
                result.stderr[-1000:],
            )

            continue

        if not output_file.exists():
            continue

        size = output_file.stat().st_size

        logger.info(
            "Output at %s kbps: %.2f MB",
            kbps,
            size / 1024 / 1024,
        )

        if size <= TARGET_BYTES:

            try:
                input_file.unlink()
            except Exception:
                pass

            return output_file

    # One final safety check.
    if output_file.exists():

        size = output_file.stat().st_size

        if size <= MAX_BYTES:

            logger.warning(
                "Output is above target but below maximum: %.2f MB",
                size / 1024 / 1024,
            )

            try:
                input_file.unlink()
            except Exception:
                pass

            return output_file

    raise RuntimeError(
        "Audio could not be reduced below "
        f"{MAX_MB} MB."
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

/play <YouTube URL>
/play <song name>

Commands:

/play   - Download song
/queue  - Show queue
/now    - Current song
/skip   - Skip queued song
/stop   - Stop queue
/clear  - Clear queue
/remove <number> - Remove item
/status - Bot status
/help   - Help

YouTube audio is automatically processed
to stay below the configured Telegram size.
"""


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(
        HELP_TEXT,
        reply_markup=main_keyboard(),
    )


# =========================================================
# STATUS
# =========================================================

async def status_text():

    ffmpeg = (
        "FOUND"
        if ffmpeg_available()
        else "NOT FOUND"
    )

    cookies = (
        "FOUND"
        if COOKIE_SECRET_PATH.exists()
        else "NOT FOUND"
    )

    queued = sum(
        len(q)
        for q in queues.values()
    )

    active = len(
        current_tasks
    )

    workers = len(
        worker_running
    )

    return (
        "📊 Resso Status\n\n"
        "🟢 Running\n"
        f"🎬 FFmpeg: {ffmpeg}\n"
        f"🍪 Cookies: {cookies}\n"
        f"📦 yt-dlp: {yt_dlp.version.__version__}\n"
        f"💾 Target: {TARGET_MB} MB\n"
        f"🚫 Maximum: {MAX_MB} MB\n"
        f"📋 Queued: {queued}\n"
        f"⚙️ Active: {active}\n"
        f"👷 Workers: {workers}"
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(
        await status_text(),
        reply_markup=main_keyboard(),
    )


# =========================================================
# QUEUE
# =========================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    chat_id = update.effective_chat.id

    queue = queues[chat_id]

    if not queue:

        await update.message.reply_text(
            "📋 Queue empty.",
            reply_markup=main_keyboard(),
        )

        return

    lines = [
        "📋 Queue:\n"
    ]

    for index, item in enumerate(
        queue,
        start=1,
    ):

        title = item.get(
            "title",
            item.get(
                "query",
                "Unknown",
            ),
        )

        lines.append(
            f"{index}. {title}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_keyboard(),
    )


# =========================================================
# NOW
# =========================================================

async def now_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    chat_id = update.effective_chat.id

    task = current_tasks.get(
        chat_id
    )

    if not task:

        await update.message.reply_text(
            "🎵 Nothing is running right now.",
            reply_markup=main_keyboard(),
        )

        return

    await update.message.reply_text(
        "🎵 Now:\n"
        f"{task.get('title', 'Unknown')}",
        reply_markup=main_keyboard(),
    )


# =========================================================
# YOUTUBE URL CHECK
# =========================================================

def is_url(query):

    query = query.strip().lower()

    return (
        query.startswith(
            "https://"
        )
        or query.startswith(
            "http://"
        )
    )


# =========================================================
# FIND MEDIA FILE
# =========================================================

def find_media_file(workdir):

    ignored = {
        ".part",
        ".ytdl",
        ".json",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".vtt",
        ".srt",
    }

    files = []

    for file in Path(workdir).iterdir():

        if not file.is_file():
            continue

        if file.suffix.lower() in ignored:
            continue

        if file.stat().st_size <= 0:
            continue

        files.append(file)

    if not files:
        return None

    # Prefer audio extensions.
    audio_extensions = {
        ".mp3",
        ".m4a",
        ".webm",
        ".opus",
        ".aac",
        ".wav",
        ".flac",
        ".ogg",
        ".mp4",
    }

    audio_files = [
        f
        for f in files
        if f.suffix.lower()
        in audio_extensions
    ]

    if audio_files:

        return max(
            audio_files,
            key=lambda x: x.stat().st_size,
        )

    return max(
        files,
        key=lambda x: x.stat().st_size,
    )


# =========================================================
# YOUTUBE DOWNLOAD
# =========================================================

def download_youtube(
    query,
    workdir,
):

    cookies = prepare_cookies()

    output_template = str(
        Path(workdir)
        / "%(title).120s.%(ext)s"
    )

    # -----------------------------------------------------
    # IMPORTANT:
    #
    # Do NOT force android client.
    #
    # YouTube currently changes available formats
    # depending on player client.
    # -----------------------------------------------------

    strategies = [
        {
            "name": "default + web_embedded",
            "extractor_args": {
                "youtube": {
                    "player_client": [
                        "default",
                        "web_embedded",
                    ]
                }
            },
        },

        {
            "name": "default",
            "extractor_args": {
                "youtube": {
                    "player_client": [
                        "default",
                    ]
                }
            },
        },

        {
            "name": "web_embedded",
            "extractor_args": {
                "youtube": {
                    "player_client": [
                        "web_embedded",
                    ]
                }
            },
        },
    ]

    if not is_url(query):

        download_query = (
            "ytsearch1:"
            + query
        )

    else:

        download_query = query

    last_error = None

    for strategy in strategies:

        logger.info(
            "YouTube strategy: %s",
            strategy["name"],
        )

        # Clean workdir before retry.
        for old_file in Path(
            workdir
        ).iterdir():

            try:

                if old_file.is_file():
                    old_file.unlink()

                elif old_file.is_dir():
                    shutil.rmtree(
                        old_file,
                        ignore_errors=True,
                    )

            except Exception:
                pass

        ydl_opts = {

            "format": (
                "bestaudio/best"
            ),

            "outtmpl": output_template,

            "noplaylist": True,

            "quiet": True,

            "no_warnings": False,

            "restrictfilenames": True,

            "socket_timeout": 45,

            "retries": 5,

            "fragment_retries": 5,

            "file_access_retries": 3,

            "extractor_retries": 3,

            "concurrent_fragment_downloads": 1,

            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(X11; Linux x86_64) "
                    AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    Chrome/131.0.0.0 "
                    "Safari/537.36"
                ),
                "Accept-Language": (
                    "en-US,en;q=0.9"
                ),
            },

            "extractor_args": strategy[
                "extractor_args"
            ],

            "noprogress": True,

            "continuedl": True,

            "overwrites": True,

            "ignoreerrors": False,
        }

        if cookies:

            ydl_opts[
                "cookiefile"
            ] = cookies

        try:

            with yt_dlp.YoutubeDL(
                ydl_opts
            ) as ydl:

                info = ydl.extract_info(
                    download_query,
                    download=True,
                )

                if not info:

                    raise RuntimeError(
                        "YouTube returned no result."
                    )

                if "entries" in info:

                    entries = (
                        info.get("entries")
                        or []
                    )

                    if not entries:

                        raise RuntimeError(
                            "YouTube search returned no result."
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

                media_file = (
                    find_media_file(
                        workdir
                    )
                )

                if not media_file:

                    raise RuntimeError(
                        "Downloaded media file "
                        "was not found."
                    )

                logger.info(
                    "Downloaded: %s",
                    media_file,
                )

                return {
                    "title": title,
                    "duration": duration,
                    "file": media_file,
                }

        except Exception as e:

            last_error = e

            logger.warning(
                "Strategy '%s' failed: %s",
                strategy["name"],
                str(e),
            )

            continue

    raise RuntimeError(
        "All YouTube download methods failed.\n\n"
        f"Last error: {str(last_error)[:2000]}"
    )


# =========================================================
# PROCESS SONG
# =========================================================

async def process_song(
    chat_id,
    item,
    bot,
):

    query = item.get(
        "query",
        "",
    )

    title = item.get(
        "title",
        query,
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

        real_title = result[
            "title"
        ]

        downloaded_file = result[
            "file"
        ]

        # -------------------------------------------------
        # CHECK STOP
        # -------------------------------------------------

        if chat_id in stop_requested:

            stop_requested.discard(
                chat_id
            )

            return

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "🎧 Download complete.\n"
                "⚙️ Checking audio size..."
            ),
        )

        compressed_file = (
            await loop.run_in_executor(
                None,
                compress_audio,
                downloaded_file,
            )
        )

        size = compressed_file.stat().st_size

        if size > MAX_BYTES:

            raise RuntimeError(
                "Final audio is "
                f"{size / 1024 / 1024:.1f} MB, "
                "which is above the limit."
            )

        # -------------------------------------------------
        # UPLOAD
        # -------------------------------------------------

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
                caption=(
                    f"🎵 {real_title}"
                ),
                read_timeout=180,
                write_timeout=180,
                connect_timeout=60,
                pool_timeout=60,
            )

        await bot.send_message(
            chat_id=chat_id,
            text="✅ Done!",
            reply_markup=main_keyboard(),
        )

    except Exception as e:

        logger.exception(
            "Song processing failed.",
        )

        error_text = str(e)

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ YouTube download failed.\n\n"
                f"🎵 {title}\n\n"
                "Possible reasons:\n"
                "• YouTube changed its player\n"
                "• Cookies expired/invalid\n"
                "• Network error\n"
                "• FFmpeg error\n"
                "• Video has no downloadable audio\n\n"
                "Technical error:\n"
                f"{error_text[:2500]}"
            ),
            reply_markup=main_keyboard(),
        )

    finally:

        current_tasks.pop(
            chat_id,
            None,
        )

        shutil.rmtree(
            workdir,
            ignore_errors=True,
        )


# =========================================================
# QUEUE WORKER
# =========================================================

async def queue_worker(
    chat_id,
    bot,
):

    if chat_id in worker_running:
        return

    worker_running.add(
        chat_id
    )

    try:

        while queues[chat_id]:

            item = queues[
                chat_id
            ].popleft()

            await process_song(
                chat_id,
                item,
                bot,
            )

    finally:

        worker_running.discard(
            chat_id
        )

        current_tasks.pop(
            chat_id,
            None,
        )


# =========================================================
# ADD QUEUE
# =========================================================

async def add_to_queue(
    chat_id,
    query,
    bot,
):

    item = {
        "query": query,
        "title": query,
    }

    queues[
        chat_id
    ].append(item)

    position = len(
        queues[chat_id]
    )

    # Start only one worker.
    if chat_id not in worker_running:

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

    if not context.args:

        await update.message.reply_text(
            "🎵 Use:\n\n"
            "/play <YouTube URL>\n\n"
            "or\n\n"
            "/play <song name>",
            reply_markup=main_keyboard(),
        )

        return

    chat_id = (
        update.effective_chat.id
    )

    query = " ".join(
        context.args
    ).strip()

    was_idle = (
        chat_id not in worker_running
        and chat_id not in current_tasks
    )

    position = await add_to_queue(
        chat_id,
        query,
        context.bot,
    )

    if was_idle:

        await update.message.reply_text(
            "🎵 Starting:\n"
            f"{query}",
            reply_markup=main_keyboard(),
        )

    else:

        await update.message.reply_text(
            "📋 Added to queue.\n"
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

    if not update.message:
        return

    chat_id = (
        update.effective_chat.id
    )

    queues[
        chat_id
    ].clear()

    stop_requested.add(
        chat_id
    )

    await update.message.reply_text(
        "⏹️ Queue cleared.\n\n"
        "Current download/upload will "
        "finish safely.",
        reply_markup=main_keyboard(),
    )


# =========================================================
# CLEAR
# =========================================================

async def clear_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    chat_id = (
        update.effective_chat.id
    )

    queues[
        chat_id
    ].clear()

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

    if not update.message:
        return

    chat_id = (
        update.effective_chat.id
    )

    if not context.args:

        await update.message.reply_text(
            "Use:\n"
            "/remove <queue number>"
        )

        return

    try:

        number = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid number."
        )

        return

    queue = queues[
        chat_id
    ]

    if number < 1 or number > len(queue):

        await update.message.reply_text(
            "❌ Invalid queue number."
        )

        return

    items = list(queue)

    removed = items.pop(
        number - 1
    )

    queue.clear()

    queue.extend(
        items
    )

    await update.message.reply_text(
        "🗑️ Removed:\n"
        f"{removed.get('title', 'Unknown')}",
        reply_markup=main_keyboard(),
    )


# =========================================================
# SKIP
# =========================================================

async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    chat_id = (
        update.effective_chat.id
    )

    queue = queues[
        chat_id
    ]

    if not queue:

        await update.message.reply_text(
            "📋 No queued song to skip.",
            reply_markup=main_keyboard(),
        )

        return

    skipped = queue.popleft()

    await update.message.reply_text(
        "⏭️ Skipped:\n"
        f"{skipped.get('title', 'Unknown')}",
        reply_markup=main_keyboard(),
    )


# =========================================================
# CALLBACK
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    chat_id = (
        query.message.chat.id
    )

    data = query.data

    # -----------------------------------------------------
    # HELP
    # -----------------------------------------------------

    if data == "help":

        await query.message.reply_text(
            HELP_TEXT,
            reply_markup=main_keyboard(),
        )

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    elif data == "status":

        await query.message.reply_text(
            await status_text(),
            reply_markup=main_keyboard(),
        )

    # -----------------------------------------------------
    # QUEUE
    # -----------------------------------------------------

    elif data == "queue":

        queue = queues[
            chat_id
        ]

        if not queue:

            text = (
                "📋 Queue empty."
            )

        else:

            lines = [
                "📋 Queue:\n"
            ]

            for i, item in enumerate(
                queue,
                start=1,
            ):

                lines.append(
                    f"{i}. "
                    f"{item.get('title', 'Unknown')}"
                )

            text = "\n".join(
                lines
            )

        await query.message.reply_text(
            text,
            reply_markup=main_keyboard(),
        )

    # -----------------------------------------------------
    # NOW
    # -----------------------------------------------------

    elif data == "now":

        task = current_tasks.get(
            chat_id
        )

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

    # -----------------------------------------------------
    # SKIP
    # -----------------------------------------------------

    elif data == "skip":

        queue = queues[
            chat_id
        ]

        if queue:

            skipped = queue.popleft()

            text = (
                "⏭️ Skipped:\n"
                f"{skipped.get('title', 'Unknown')}"
            )

        else:

            text = (
                "📋 No queued song."
            )

        await query.message.reply_text(
            text,
            reply_markup=main_keyboard(),
        )

    # -----------------------------------------------------
    # STOP
    # -----------------------------------------------------

    elif data == "stop":

        queues[
            chat_id
        ].clear()

        stop_requested.add(
            chat_id
        )

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
            "BOT_TOKEN environment variable "
            "is missing."
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
# TELEGRAM WEBHOOK
# =========================================================

@flask_app.route(
    "/telegram/webhook",
    methods=["POST"],
)
def telegram_webhook():

    global application
    global bot_loop

    if application is None:

        return (
            "Bot not ready",
            503,
        )

    # -----------------------------------------------------
    # SECRET CHECK
    # -----------------------------------------------------

    if WEBHOOK_SECRET:

        received_secret = (
            request.headers.get(
                "X-Telegram-Bot-Api-Secret-Token",
                "",
            )
        )

        if received_secret != WEBHOOK_SECRET:

            return (
                "Forbidden",
                403,
            )

    try:

        data = request.get_json(
            force=True
        )

        if not data:

            return (
                "Bad Request",
                400,
            )

        update = Update.de_json(
            data,
            application.bot,
        )

        if bot_loop is None:

            return (
                "Loop not ready",
                503,
            )

        bot_loop.call_soon_threadsafe(
            application.update_queue.put_nowait,
            update,
        )

        return (
            "OK",
            200,
        )

    except Exception as e:

        logger.exception(
            "Webhook error: %s",
            e,
        )

        return (
            "Bad Request",
            400,
        )


# =========================================================
# TELEGRAM START
# =========================================================

async def telegram_start():

    global bot_loop

    bot_loop = (
        asyncio.get_running_loop()
    )

    await application.initialize()

    await application.start()

    if not RENDER_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing."
        )

    webhook_url = (
        RENDER_URL.rstrip("/")
        + "/telegram/webhook"
    )

    logger.info(
        "Webhook URL: %s",
        webhook_url,
    )

    # Remove old webhook.
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
        "Telegram webhook ACTIVE."
    )

    # Keep loop alive.
    await asyncio.Event().wait()


# =========================================================
# TELEGRAM THREAD
# =========================================================

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
        "===================================="
    )

    logger.info(
        "Starting Resso Bot"
    )

    logger.info(
        "yt-dlp version: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "FFmpeg: %s",
        (
            "FOUND"
            if ffmpeg_available()
            else "NOT FOUND"
        ),
    )

    logger.info(
        "Cookies: %s",
        (
            "FOUND"
            if COOKIE_SECRET_PATH.exists()
            else "NOT FOUND"
        ),
    )

    logger.info(
        "===================================="
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
