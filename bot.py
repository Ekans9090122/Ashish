import os
import asyncio
import logging
import tempfile
import threading
import shutil
from pathlib import Path

from flask import Flask

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


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

PORT = int(os.getenv("PORT", "10000"))

COOKIE_SOURCE = os.getenv(
    "YT_COOKIES_FILE",
    "/etc/secrets/youtube_cookies.txt"
)

COOKIE_FILE = "/tmp/youtube_cookies.txt"

MAX_QUEUE = 20
MAX_AUDIO_MB = 45


# ============================================================
# BASIC CHECK
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is missing."
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("resso")


# ============================================================
# DENO SETUP
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

PROJECT_DENO_BIN = BASE_DIR / ".deno" / "bin"
HOME_DENO_BIN = Path.home() / ".deno" / "bin"

current_path = os.environ.get("PATH", "")

# Prefer project Deno
if PROJECT_DENO_BIN.is_dir():
    os.environ["PATH"] = (
        str(PROJECT_DENO_BIN)
        + os.pathsep
        + current_path
    )

# Also allow Home Deno
elif HOME_DENO_BIN.is_dir():
    os.environ["PATH"] = (
        str(HOME_DENO_BIN)
        + os.pathsep
        + current_path
    )

logger.info("Project Deno directory: %s", PROJECT_DENO_BIN)
logger.info("Home Deno directory: %s", HOME_DENO_BIN)

DENO_PATH = shutil.which("deno")

if DENO_PATH:
    logger.info("Deno FOUND: %s", DENO_PATH)

    try:
        deno_version = os.popen(
            f'"{DENO_PATH}" --version'
        ).read().strip()

        logger.info(
            "Deno version: %s",
            deno_version
        )

    except Exception as error:
        logger.warning(
            "Could not read Deno version: %s",
            error
        )

else:
    logger.warning(
        "Deno NOT FOUND."
    )


# ============================================================
# FLASK
# ============================================================

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "Resso Music Bot is running."


@flask_app.route("/health")
def health():
    return "OK"


def run_flask():
    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )


# ============================================================
# COOKIE PREPARATION
# ============================================================

def prepare_cookie_file():

    if not os.path.isfile(COOKIE_SOURCE):

        logger.warning(
            "Cookie source not found: %s",
            COOKIE_SOURCE
        )

        return False

    try:

        shutil.copyfile(
            COOKIE_SOURCE,
            COOKIE_FILE
        )

        try:
            os.chmod(
                COOKIE_FILE,
                0o600
            )
        except Exception:
            pass

        logger.info(
            "YouTube cookies prepared: %s",
            COOKIE_FILE
        )

        return True

    except Exception as error:

        logger.exception(
            "Cookie preparation failed: %s",
            error
        )

        return False


# ============================================================
# QUEUE
# ============================================================

queues = {}
playing = set()
locks = {}


def get_queue(chat_id):

    if chat_id not in queues:
        queues[chat_id] = []

    return queues[chat_id]


def get_lock(chat_id):

    if chat_id not in locks:
        locks[chat_id] = asyncio.Lock()

    return locks[chat_id]


# ============================================================
# KEYBOARD
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup(
        [
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
                    "📋 Queue",
                    callback_data="queue"
                ),
                InlineKeyboardButton(
                    "🎵 Now",
                    callback_data="now"
                ),
            ],
            [
                InlineKeyboardButton(
                    "ℹ️ Help",
                    callback_data="help"
                ),
                InlineKeyboardButton(
                    "📊 Status",
                    callback_data="status"
                ),
            ],
        ]
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    text = (
        "🎵 *Resso Music Bot*\n\n"
        "YouTube se music search karke audio bhejta hoon.\n\n"

        "▶️ *Play*\n"
        "`/play Kesariya`\n"
        "`/play Arijit Singh Kesariya`\n\n"

        "📋 *Queue*\n"
        "`/queue`\n\n"

        "🎵 *Now*\n"
        "`/now`\n\n"

        "⏭️ *Skip*\n"
        "`/skip`\n\n"

        "⏹️ *Stop*\n"
        "`/stop`\n\n"

        "❌ *Remove*\n"
        "`/remove 2`\n\n"

        "🧹 *Clear*\n"
        "`/clear`\n\n"

        "📊 *Status*\n"
        "`/status`\n\n"

        "ℹ️ `/help`"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    text = (
        "🎵 *Resso Music Bot Help*\n\n"

        "▶️ `/play <song>`\n"
        "Song search + audio send.\n\n"

        "📋 `/queue`\n"
        "Waiting songs dekho.\n\n"

        "🎵 `/now`\n"
        "Current processing status.\n\n"

        "⏭️ `/skip`\n"
        "Next waiting song remove.\n\n"

        "⏹️ `/stop`\n"
        "Queue clear karo.\n\n"

        "❌ `/remove 2`\n"
        "Queue item #2 remove karo.\n\n"

        "🧹 `/clear`\n"
        "Complete waiting queue clear.\n\n"

        "📊 `/status`\n"
        "Cookie/Deno/FFmpeg status."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# STATUS
# ============================================================

def get_status_text():

    cookie_exists = os.path.isfile(
        COOKIE_FILE
    )

    cookie_size = (
        os.path.getsize(COOKIE_FILE)
        if cookie_exists
        else 0
    )

    deno_path = shutil.which("deno")

    ffmpeg_path = shutil.which("ffmpeg")

    return (
        "📊 *Resso Status*\n\n"

        "🟢 Bot: Running\n"

        f"🍪 Cookies: "
        f"{'FOUND' if cookie_exists else 'NOT FOUND'}\n"

        f"📦 Cookie size: "
        f"{cookie_size:,} bytes\n"

        f"🦕 Deno: "
        f"{'FOUND' if deno_path else 'NOT FOUND'}\n"

        f"🎬 FFmpeg: "
        f"{'FOUND' if ffmpeg_path else 'NOT FOUND'}\n\n"

        f"📁 Cookie path:\n"
        f"`{COOKIE_FILE}`\n\n"

        f"📋 Max queue: {MAX_QUEUE}\n"
        f"💾 Max audio: {MAX_AUDIO_MB} MB"
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    await update.message.reply_text(
        get_status_text(),
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# YOUTUBE DOWNLOAD
# ============================================================

def download_youtube_audio(query):

    temp_dir = tempfile.mkdtemp(
        prefix="resso_"
    )

    output_template = os.path.join(
        temp_dir,
        "%(id)s.%(ext)s"
    )

    # --------------------------------------------------------
    # DENO
    # --------------------------------------------------------

    deno_path = shutil.which("deno")

    if not deno_path:

        possible_paths = [
            PROJECT_DENO_BIN / "deno",
            HOME_DENO_BIN / "deno",
        ]

        for path in possible_paths:

            if path.is_file():
                deno_path = str(path)
                break

    logger.info(
        "Download Deno path: %s",
        deno_path
    )

    # --------------------------------------------------------
    # YT-DLP OPTIONS
    # --------------------------------------------------------

    ydl_opts = {

        "format": "bestaudio/best",

        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": True,

        "no_warnings": False,

        "socket_timeout": 30,

        "retries": 3,

        "fragment_retries": 3,

        "continuedl": False,

        "overwrites": True,

        "restrictfilenames": True,

        "cachedir": False,

        # yt-dlp EJS remote components
        "remote_components": {
            "ejs": "github"
        },
    }

    # --------------------------------------------------------
    # JAVASCRIPT RUNTIME
    # --------------------------------------------------------

    if deno_path:

        ydl_opts["js_runtimes"] = {
            "deno": {
                "path": deno_path
            }
        }

        logger.info(
            "Using Deno runtime: %s",
            deno_path
        )

    else:

        logger.warning(
            "Deno unavailable. Continuing without Deno."
        )

    # --------------------------------------------------------
    # COOKIES
    # --------------------------------------------------------

    if os.path.isfile(COOKIE_FILE):

        ydl_opts["cookiefile"] = COOKIE_FILE

        logger.info(
            "Using YouTube cookie file: %s",
            COOKIE_FILE
        )

    else:

        logger.warning(
            "Cookie file not found: %s",
            COOKIE_FILE
        )

    # --------------------------------------------------------
    # SEARCH + DOWNLOAD
    # --------------------------------------------------------

    logger.info(
        "Searching YouTube: %s",
        query
    )

    try:

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(
                f"ytsearch1:{query}",
                download=True,
            )

    except Exception:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        raise

    # --------------------------------------------------------
    # RESULT CHECK
    # --------------------------------------------------------

    if not info:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        raise RuntimeError(
            "YouTube returned no result."
        )

    entries = info.get("entries") or []

    video = next(
        (
            entry
            for entry in entries
            if entry
        ),
        None
    )

    if not video:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        raise RuntimeError(
            "No YouTube result found."
        )

    # --------------------------------------------------------
    # VIDEO INFO
    # --------------------------------------------------------

    title = video.get(
        "title"
    ) or query

    video_id = video.get(
        "id"
    )

    webpage_url = video.get(
        "webpage_url"
    )

    if not webpage_url and video_id:

        webpage_url = (
            "https://www.youtube.com/watch?v="
            + video_id
        )

    downloaded_file = None

    # --------------------------------------------------------
    # FIND DOWNLOADED FILE
    # --------------------------------------------------------

    requested_downloads = (
        video.get(
            "requested_downloads"
        ) or []
    )

    for item in requested_downloads:

        filepath = item.get(
            "filepath"
        )

        if (
            filepath
            and os.path.isfile(filepath)
        ):

            downloaded_file = filepath
            break

    # --------------------------------------------------------
    # FALLBACK 1
    # --------------------------------------------------------

    if downloaded_file is None and video_id:

        matches = list(
            Path(temp_dir).glob(
                f"{video_id}.*"
            )
        )

        matches = [
            p
            for p in matches
            if (
                p.is_file()
                and not p.name.endswith(".part")
            )
        ]

        if matches:

            downloaded_file = str(
                max(
                    matches,
                    key=lambda p:
                    p.stat().st_mtime
                )
            )

    # --------------------------------------------------------
    # FALLBACK 2
    # --------------------------------------------------------

    if downloaded_file is None:

        files = [
            p
            for p in Path(
                temp_dir
            ).iterdir()
            if (
                p.is_file()
                and not p.name.endswith(".part")
            )
        ]

        if files:

            downloaded_file = str(
                max(
                    files,
                    key=lambda p:
                    p.stat().st_mtime
                )
            )

    # --------------------------------------------------------
    # FILE CHECK
    # --------------------------------------------------------

    if downloaded_file is None:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        raise RuntimeError(
            "yt-dlp completed but audio file was not found."
        )

    # --------------------------------------------------------
    # SIZE CHECK
    # --------------------------------------------------------

    size_mb = (
        os.path.getsize(
            downloaded_file
        )
        / (1024 * 1024)
    )

    if size_mb > MAX_AUDIO_MB:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        raise RuntimeError(
            f"Audio is {size_mb:.1f} MB. "
            f"Maximum allowed is {MAX_AUDIO_MB} MB."
        )

    return (
        downloaded_file,
        title,
        webpage_url,
        temp_dir
    )


# ============================================================
# QUEUE PROCESSOR
# ============================================================

async def process_queue(
    context,
    chat_id,
    first_status=None
):

    lock = get_lock(chat_id)

    async with lock:

        if chat_id in playing:
            return

        playing.add(chat_id)

        try:

            queue = get_queue(chat_id)

            while queue:

                query = queue.pop(0)

                status_message = first_status
                first_status = None

                file_path = None
                temp_dir = None

                try:

                    if status_message is None:

                        status_message = (
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    "🔎 Searching YouTube...\n\n"
                                    f"🎵 {query}"
                                )
                            )
                        )

                    # ------------------------------------------------
                    # DOWNLOAD
                    # ------------------------------------------------

                    (
                        file_path,
                        title,
                        webpage_url,
                        temp_dir
                    ) = await asyncio.to_thread(
                        download_youtube_audio,
                        query
                    )

                    # ------------------------------------------------
                    # UPDATE STATUS
                    # ------------------------------------------------

                    try:

                        await status_message.edit_text(
                            "⬇️ Downloaded\n\n"
                            f"🎵 {title[:150]}\n\n"
                            "📤 Sending audio..."
                        )

                    except Exception:
                        pass

                    # ------------------------------------------------
                    # CAPTION
                    # ------------------------------------------------

                    caption = f"🎵 {title[:900]}"

                    if webpage_url:

                        caption += (
                            "\n\n🔗 "
                            + webpage_url
                        )

                    # ------------------------------------------------
                    # SEND AUDIO
                    # ------------------------------------------------

                    with open(
                        file_path,
                        "rb"
                    ) as audio_file:

                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=audio_file,
                            title=title[:250],
                            performer="Resso",
                            caption=caption,
                            reply_markup=main_keyboard(),
                        )

                except Exception as error:

                    logger.exception(
                        "Download failed: %s",
                        query
                    )

                    safe_query = (
                        query.replace("`", "'")
                    )

                    error_text = (
                        str(error)
                        .replace("`", "'")
                    )

                    if len(error_text) > 900:

                        error_text = (
                            error_text[-900:]
                        )

                    message = (
                        "❌ *YouTube download failed.*\n\n"

                        f"🎵 `{safe_query}`\n\n"

                        "Possible reasons:\n"
                        "• YouTube cookies expired\n"
                        "• Deno/EJS unavailable\n"
                        "• YouTube player changed\n"
                        "• Render IP challenged\n"
                        "• Network error\n\n"

                        "📊 `/status` se check karo.\n\n"

                        "*Technical error:*\n"
                        f"`{error_text}`"
                    )

                    try:

                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=message,
                            parse_mode="Markdown",
                            reply_markup=main_keyboard(),
                        )

                    except Exception:

                        logger.exception(
                            "Could not send error message."
                        )

                finally:

                    if status_message:

                        try:
                            await status_message.delete()
                        except Exception:
                            pass

                    if temp_dir:

                        shutil.rmtree(
                            temp_dir,
                            ignore_errors=True
                        )

                await asyncio.sleep(0.5)

        finally:

            playing.discard(chat_id)


# ============================================================
# PLAY
# ============================================================

async def play_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    chat_id = update.effective_chat.id

    if not context.args:

        await update.message.reply_text(
            "🎵 Use:\n\n"
            "/play <song name>\n\n"
            "Example:\n"
            "/play Kesariya"
        )

        return

    query = (
        " ".join(
            context.args
        )
        .strip()
    )

    if not query:

        await update.message.reply_text(
            "❌ Song name missing."
        )

        return

    queue = get_queue(chat_id)

    if len(queue) >= MAX_QUEUE:

        await update.message.reply_text(
            f"❌ Queue full.\n"
            f"Maximum {MAX_QUEUE} songs."
        )

        return

    queue.append(query)

    if chat_id in playing:

        await update.message.reply_text(
            "➕ *Added to queue*\n\n"
            f"🎵 {query}\n"
            f"📍 Position: {len(queue)}",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )

        return

    status = await update.message.reply_text(
        "🔎 Searching YouTube...\n\n"
        f"🎵 {query}"
    )

    context.application.create_task(
        process_queue(
            context,
            chat_id,
            status
        )
    )


# ============================================================
# QUEUE
# ============================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    queue = get_queue(
        update.effective_chat.id
    )

    if not queue:

        await update.message.reply_text(
            "📋 Queue empty.",
            reply_markup=main_keyboard()
        )

        return

    text = "📋 *Waiting Queue*\n\n"

    for i, song in enumerate(
        queue,
        start=1
    ):

        text += f"{i}. {song}\n"

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# NOW
# ============================================================

async def now_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    chat_id = update.effective_chat.id

    queue = get_queue(chat_id)

    if chat_id not in playing:

        await update.message.reply_text(
            "🎵 Nothing is processing."
        )

        return

    await update.message.reply_text(
        "🎵 *Currently processing*\n\n"
        f"📋 Waiting: {len(queue)}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# CLEAR
# ============================================================

async def clear_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    queue = get_queue(
        update.effective_chat.id
    )

    count = len(queue)

    queue.clear()

    await update.message.reply_text(
        f"🧹 Cleared {count} song(s).",
        reply_markup=main_keyboard()
    )


# ============================================================
# STOP
# ============================================================

async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    queue = get_queue(
        update.effective_chat.id
    )

    count = len(queue)

    queue.clear()

    await update.message.reply_text(
        f"⏹️ Queue stopped.\n"
        f"🧹 Removed {count} waiting song(s).\n\n"
        "Current download ko forcibly kill nahi kiya ja raha.",
        reply_markup=main_keyboard()
    )


# ============================================================
# REMOVE
# ============================================================

async def remove_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if (
        not context.args
        or not context.args[0].isdigit()
    ):

        await update.message.reply_text(
            "Use:\n/remove 2"
        )

        return

    index = int(
        context.args[0]
    )

    queue = get_queue(
        update.effective_chat.id
    )

    if (
        index < 1
        or index > len(queue)
    ):

        await update.message.reply_text(
            "❌ Invalid queue number."
        )

        return

    removed = queue.pop(
        index - 1
    )

    await update.message.reply_text(
        "❌ Removed:\n"
        f"🎵 {removed}",
        reply_markup=main_keyboard()
    )


# ============================================================
# SKIP
# ============================================================

async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    queue = get_queue(
        update.effective_chat.id
    )

    if queue:

        skipped = queue.pop(0)

        await update.message.reply_text(
            "⏭️ Skipped:\n"
            f"🎵 {skipped}",
            reply_markup=main_keyboard()
        )

    else:

        await update.message.reply_text(
            "📋 Queue empty.",
            reply_markup=main_keyboard()
        )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    chat_id = query.message.chat_id

    data = query.data

    if data == "help":

        await query.message.reply_text(
            "🎵 /play <song>\n"
            "📋 /queue\n"
            "🎵 /now\n"
            "⏭️ /skip\n"
            "⏹️ /stop\n"
            "❌ /remove <number>\n"
            "🧹 /clear\n"
            "📊 /status",
            reply_markup=main_keyboard()
        )

    elif data == "queue":

        queue = get_queue(chat_id)

        if not queue:

            await query.message.reply_text(
                "📋 Queue empty."
            )

            return

        text = "📋 *Queue*\n\n"

        for i, song in enumerate(
            queue,
            start=1
        ):

            text += f"{i}. {song}\n"

        await query.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    elif data == "now":

        if chat_id in playing:

            await query.message.reply_text(
                "🎵 Processing current song...\n"
                f"📋 Waiting: "
                f"{len(get_queue(chat_id))}"
            )

        else:

            await query.message.reply_text(
                "🎵 Nothing is processing."
            )

    elif data in ("stop", "clear"):

        queue = get_queue(chat_id)

        count = len(queue)

        queue.clear()

        await query.message.reply_text(
            f"🧹 Queue cleared.\n"
            f"Removed: {count}"
        )

    elif data == "skip":

        queue = get_queue(chat_id)

        if queue:

            skipped = queue.pop(0)

            await query.message.reply_text(
                "⏭️ Skipped:\n"
                f"🎵 {skipped}"
            )

        else:

            await query.message.reply_text(
                "📋 Queue empty."
            )

    elif data == "status":

        await query.message.reply_text(
            get_status_text(),
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    logger.error(
        "Telegram error: %s",
        context.error,
        exc_info=context.error
    )


# ============================================================
# TELEGRAM BOT
# ============================================================

def run_telegram_bot():

    logger.info(
        "Starting Telegram bot..."
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
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
            "stop",
            stop_command
        )
    )

    application.add_handler(
        CommandHandler(
            "clear",
            clear_command
        )
    )

    application.add_handler(
        CommandHandler(
            "remove",
            remove_command
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
            "status",
            status_command
        )
    )

    # Buttons

    application.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # Error handler

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Telegram polling started."
    )

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logger.info(
        "Preparing YouTube cookies..."
    )

    prepare_cookie_file()

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    logger.info(
        "Flask server started on port %s",
        PORT
    )

    run_telegram_bot()
