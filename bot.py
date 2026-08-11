import os
import asyncio
import logging
import threading
import subprocess
from pathlib import Path

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# Telegram ke liye safe target
MAX_AUDIO_MB = 45
TARGET_AUDIO_MB = 40

DOWNLOAD_DIR = Path("/tmp/resso_audio")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COOKIE_FILE = "/tmp/youtube_cookies.txt"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("Resso")


# ============================================================
# FLASK SERVER - RENDER
# ============================================================

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "Resso Music Bot is running 🎵", 200


@flask_app.route("/health")
def health():
    return "OK", 200


def run_flask():
    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )


# ============================================================
# BOT BUTTONS
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Skip", callback_data="skip"),
            InlineKeyboardButton("⏹ Stop", callback_data="stop"),
        ],
        [
            InlineKeyboardButton("📋 Queue", callback_data="queue"),
            InlineKeyboardButton("🎵 Now", callback_data="now"),
        ],
        [
            InlineKeyboardButton("ℹ️ Help", callback_data="help"),
            InlineKeyboardButton("📊 Status", callback_data="status"),
        ],
    ])


# ============================================================
# BASIC COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *Welcome to Resso Bot!*\n\n"
        "Music search ke liye:\n"
        "`/play song name`\n\n"
        "Example:\n"
        "`/play Kesariya`\n\n"
        "Help ke liye `/help` bhejo.",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *Resso Music Bot*\n\n"
        "/start - Bot start karo\n"
        "/help - Commands\n"
        "/play <song> - Song play/download\n"
        "/skip - Skip current\n"
        "/stop - Stop\n"
        "/queue - Queue\n"
        "/now - Current song\n"
        "/status - Bot status",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# FILE SIZE
# ============================================================

def file_size_mb(file_path):
    return os.path.getsize(file_path) / (1024 * 1024)


# ============================================================
# FFmpeg COMPRESSION
# ============================================================

def compress_audio(input_file):
    """
    Audio ko Telegram-safe size me convert karta hai.
    Target: <= 40 MB
    """

    input_file = str(input_file)

    original_size = file_size_mb(input_file)

    logger.info(
        "Original audio size: %.2f MB",
        original_size,
    )

    # Already safe
    if original_size <= TARGET_AUDIO_MB:
        return input_file

    compressed_file = (
        str(Path(input_file).with_suffix(""))
        + "_compressed.mp3"
    )

    logger.info("Compressing audio with FFmpeg...")

    # First attempt: 96 kbps
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_file,
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "96k",
            compressed_file,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
    )

    size = file_size_mb(compressed_file)

    logger.info(
        "After 96k compression: %.2f MB",
        size,
    )

    # Still too large -> 64 kbps
    if size > TARGET_AUDIO_MB:

        compressed_file_2 = (
            str(Path(input_file).with_suffix(""))
            + "_compressed64.mp3"
        )

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_file,
                "-vn",
                "-ac",
                "2",
                "-ar",
                "44100",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "64k",
                compressed_file_2,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )

        if os.path.exists(compressed_file):
            os.remove(compressed_file)

        compressed_file = compressed_file_2

        size = file_size_mb(compressed_file)

        logger.info(
            "After 64k compression: %.2f MB",
            size,
        )

    # Final check
    if size > MAX_AUDIO_MB:
        raise RuntimeError(
            f"Audio is still too large: {size:.2f} MB"
        )

    # Delete original
    if os.path.exists(input_file):
        os.remove(input_file)

    return compressed_file


# ============================================================
# YOUTUBE SEARCH + DOWNLOAD
# ============================================================

def download_song(search_text):

    output_template = str(
        DOWNLOAD_DIR / "%(id)s.%(ext)s"
    )

    ydl_options = {
        "format": "bestaudio/best",

        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": True,
        "no_warnings": True,

        "geo_bypass": True,

        "socket_timeout": 30,

        "retries": 3,

        "fragment_retries": 3,

        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
    }

    # Optional YouTube cookies
    if os.path.exists(COOKIE_FILE):
        ydl_options["cookiefile"] = COOKIE_FILE

    query = "ytsearch1:" + search_text

    with yt_dlp.YoutubeDL(ydl_options) as ydl:

        info = ydl.extract_info(
            query,
            download=True,
        )

        if not info:
            raise RuntimeError(
                "YouTube search failed."
            )

        if "entries" in info:
            entries = info.get("entries")

            if not entries:
                raise RuntimeError(
                    "Song not found."
                )

            info = entries[0]

        title = info.get(
            "title",
            "Unknown Song",
        )

        uploader = info.get(
            "uploader",
            "Unknown Artist",
        )

        video_id = info.get("id")

        # Find downloaded MP3
        possible_files = list(
            DOWNLOAD_DIR.glob(f"{video_id}.*")
        )

        audio_file = None

        for file in possible_files:
            if file.suffix.lower() in [
                ".mp3",
                ".m4a",
                ".webm",
                ".opus",
            ]:
                audio_file = file
                break

        if not audio_file:
            raise RuntimeError(
                "Downloaded audio file not found."
            )

        return (
            str(audio_file),
            title,
            uploader,
        )


# ============================================================
# PLAY
# ============================================================

async def play_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not context.args:
        await update.message.reply_text(
            "❌ Song name do.\n\n"
            "Example:\n"
            "`/play Kesariya`",
            parse_mode="Markdown",
        )
        return

    song_name = " ".join(context.args)

    status_message = await update.message.reply_text(
        f"🔎 Searching YouTube...\n\n"
        f"🎵 {song_name}"
    )

    audio_file = None

    try:

        # Download is blocking, so run in thread
        audio_file, title, artist = await asyncio.to_thread(
            download_song,
            song_name,
        )

        await status_message.edit_text(
            f"⬇️ Downloaded!\n\n"
            f"🎵 {title}\n"
            f"👤 {artist}\n\n"
            f"⚙️ Checking audio size..."
        )

        original_size = file_size_mb(audio_file)

        # Compress if required
        if original_size > TARGET_AUDIO_MB:

            await status_message.edit_text(
                f"🎵 {title}\n\n"
                f"📦 Audio: {original_size:.1f} MB\n"
                f"⚙️ Compressing..."
            )

            audio_file = await asyncio.to_thread(
                compress_audio,
                audio_file,
            )

        final_size = file_size_mb(audio_file)

        if final_size > MAX_AUDIO_MB:
            raise RuntimeError(
                f"Final audio is {final_size:.1f} MB."
            )

        await status_message.edit_text(
            f"📤 Sending...\n\n"
            f"🎵 {title}\n"
            f"📦 {final_size:.1f} MB"
        )

        # Send audio
        with open(audio_file, "rb") as audio:

            await update.message.reply_audio(
                audio=audio,
                title=title[:64],
                performer=artist[:64],
                caption=(
                    f"🎵 {title}\n"
                    f"👤 {artist}\n\n"
                    f"⚡ Resso Music Bot"
                ),
                reply_markup=main_keyboard(),
            )

        await status_message.delete()

    except Exception as e:

        logger.exception(
            "PLAY ERROR"
        )

        error_text = str(e)

        if len(error_text) > 500:
            error_text = error_text[:500]

        await status_message.edit_text(
            "❌ *YouTube download failed.*\n\n"
            f"🎵 {song_name}\n\n"
            "Possible reasons:\n"
            "• YouTube changed something\n"
            "• Cookies expired\n"
            "• Render network issue\n"
            "• FFmpeg error\n\n"
            f"Technical error:\n"
            f"`{error_text}`\n\n"
            "📊 `/status` se check karo.",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )

    finally:

        # Cleanup temporary files
        try:

            for file in DOWNLOAD_DIR.iterdir():

                if file.is_file():

                    try:
                        file.unlink()
                    except Exception:
                        pass

        except Exception:
            pass


# ============================================================
# STATUS
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    bot_status = "🟢 Running"

    cookie_status = (
        "FOUND"
        if os.path.exists(COOKIE_FILE)
        else "NOT FOUND"
    )

    ffmpeg_status = "NOT FOUND"

    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if result.returncode == 0:
            ffmpeg_status = "FOUND"

    except Exception:
        pass

    cookie_size = 0

    if os.path.exists(COOKIE_FILE):

        try:
            cookie_size = os.path.getsize(
                COOKIE_FILE
            )
        except Exception:
            cookie_size = 0

    text = (
        "📊 *Resso Status*\n\n"
        f"{bot_status}\n"
        f"🍪 Cookies: {cookie_status}\n"
        f"📦 Cookie size: {cookie_size:,} bytes\n"
        f"🎬 FFmpeg: {ffmpeg_status}\n\n"
        f"📁 Cookie path:\n"
        f"`{COOKIE_FILE}`\n\n"
        f"💾 Target audio: {TARGET_AUDIO_MB} MB\n"
        f"🚫 Maximum: {MAX_AUDIO_MB} MB"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# QUEUE
# ============================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "📋 *Queue*\n\n"
        "Queue system ready.\n"
        "Currently no songs queued.",
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

    await update.message.reply_text(
        "🎵 *Now Playing*\n\n"
        "No song is currently playing.",
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

    await update.message.reply_text(
        "⏭️ Queue me next song nahi hai.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# STOP
# ============================================================

async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "⏹️ Stopped.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# BUTTONS
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    if query.data == "help":

        await query.message.reply_text(
            "ℹ️ *Resso Help*\n\n"
            "/play <song> - Play song\n"
            "/status - Bot status\n"
            "/queue - Queue\n"
            "/now - Current song\n"
            "/skip - Skip\n"
            "/stop - Stop",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )

    elif query.data == "status":

        bot_status = "🟢 Running"

        ffmpeg_status = "NOT FOUND"

        try:

            result = subprocess.run(
                ["ffmpeg", "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            if result.returncode == 0:
                ffmpeg_status = "FOUND"

        except Exception:
            pass

        await query.message.reply_text(
            "📊 *Resso Status*\n\n"
            f"{bot_status}\n"
            f"🎬 FFmpeg: {ffmpeg_status}\n"
            f"💾 Target: {TARGET_AUDIO_MB} MB\n"
            f"🚫 Maximum: {MAX_AUDIO_MB} MB",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )

    elif query.data == "queue":

        await query.message.reply_text(
            "📋 Queue empty.",
            reply_markup=main_keyboard(),
        )

    elif query.data == "now":

        await query.message.reply_text(
            "🎵 Nothing playing.",
            reply_markup=main_keyboard(),
        )

    elif query.data == "skip":

        await query.message.reply_text(
            "⏭️ Nothing to skip.",
            reply_markup=main_keyboard(),
        )

    elif query.data == "stop":

        await query.message.reply_text(
            "⏹️ Stopped.",
            reply_markup=main_keyboard(),
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Telegram error: %s",
        context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    logger.info("Starting Resso Bot...")

    # Flask server
    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )

    flask_thread.start()

    logger.info(
        "Flask server started on port %s",
        PORT,
    )

    # Telegram application
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start_command,
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
            "status",
            status_command,
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
        CallbackQueryHandler(
            button_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Resso Bot is starting polling..."
    )

    # IMPORTANT:
    # Only ONE Render service/instance should run this bot.
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
