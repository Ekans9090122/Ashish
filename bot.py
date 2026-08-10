import os
import asyncio
import logging
import tempfile
import threading
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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# FLASK SERVER - REQUIRED FOR RENDER
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
# QUEUE
# ============================================================

queues = {}
playing = set()


def get_queue(chat_id):
    if chat_id not in queues:
        queues[chat_id] = []
    return queues[chat_id]


# ============================================================
# KEYBOARD
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
                InlineKeyboardButton("ℹ️ Help", callback_data="help"),
            ],
        ]
    )


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎵 *Resso Music Bot*\n\n"
        "YouTube se song search karke audio bhej sakta hoon.\n\n"
        "🎧 *Use:*\n"
        "`/play <song name>`\n\n"
        "Example:\n"
        "`/play Arijit Singh Kesariya`\n\n"
        "Commands:\n"
        "▶️ `/play song name`\n"
        "📋 `/queue`\n"
        "⏭️ `/skip`\n"
        "⏹️ `/stop`\n"
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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎵 *How to use the bot*\n\n"
        "▶️ `/play Kesariya`\n"
        "▶️ `/play Arijit Singh Kesariya`\n\n"
        "📋 `/queue` - queue dekho\n"
        "⏭️ `/skip` - current item ke baad next\n"
        "⏹️ `/stop` - queue clear\n\n"
        "⚠️ YouTube extraction Render ke IP/network par "
        "fail ho sakti hai. Agar aisa hua to error message milega."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# YOUTUBE SEARCH + DOWNLOAD
# ============================================================

def download_youtube_audio(query):
    """
    Search YouTube and download best available audio.

    Returns:
        (file_path, title, webpage_url)

    Raises:
        Exception on failure.
    """

    temp_dir = tempfile.mkdtemp(prefix="resso_")

    output_template = os.path.join(
        temp_dir,
        "%(id)s.%(ext)s",
    )

         ydl_opts = {
        "format": (
            "bestaudio[ext=m4a]/"
            "bestaudio[ext=webm]/"
            "bestaudio/"
            "best"
        ),
        "outtmpl": output_template,
        "cookiefile": "/etc/secrets/youtube_cookies.txt",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1:",
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "continuedl": False,
        "overwrites": True,
         }
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1:",
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "continuedl": False,
        "overwrites": True,
    }

    logger.info("Searching YouTube: %s", query)

    # IMPORTANT:
    # yt_dlp is a MODULE.
    # We must use yt_dlp.YoutubeDL(...)
    # and NOT yt_dlp(...)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        info = ydl.extract_info(
            f"ytsearch1:{query}",
            download=True,
        )

        if not info:
            raise RuntimeError("YouTube returned no result.")

        entries = info.get("entries")

        if not entries:
            raise RuntimeError("No YouTube result found.")

        video = entries[0]

        title = video.get("title") or query
        webpage_url = video.get("webpage_url")

        if not webpage_url:
            video_id = video.get("id")
            if video_id:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"

        # Find downloaded file
        downloaded_file = None

        requested_downloads = video.get("requested_downloads") or []

        for item in requested_downloads:
            filepath = item.get("filepath")
            if filepath and os.path.exists(filepath):
                downloaded_file = filepath
                break

        if downloaded_file is None:
            video_id = video.get("id")

            if video_id:
                matches = list(
                    Path(temp_dir).glob(f"{video_id}.*")
                )

                if matches:
                    downloaded_file = str(matches[0])

        if downloaded_file is None:
            # Last fallback: find any file in temp directory
            files = [
                p for p in Path(temp_dir).iterdir()
                if p.is_file()
            ]

            if files:
                downloaded_file = str(files[0])

        if downloaded_file is None:
            raise RuntimeError(
                "yt-dlp completed but downloaded file was not found."
            )

        return downloaded_file, title, webpage_url


# ============================================================
# PLAY COMMAND
# ============================================================

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "🎵 Use:\n"
            "/play <song name>\n\n"
            "Example:\n"
            "/play Kesariya"
        )
        return

    query = " ".join(context.args).strip()

    if not query:
        await update.message.reply_text(
            "❌ Song name missing."
        )
        return

    queue = get_queue(chat_id)

    # Add requested song to queue
    queue.append(query)

    position = len(queue)

    if chat_id in playing:
        await update.message.reply_text(
            f"➕ *Added to queue*\n\n"
            f"🎵 {query}\n"
            f"📍 Position: {position}",
            parse_mode="Markdown",
        )
        return

    await process_queue(
        update,
        context,
        chat_id,
    )


# ============================================================
# PROCESS QUEUE
# ============================================================

async def process_queue(update, context, chat_id):

    if chat_id in playing:
        return

    playing.add(chat_id)

    try:

        queue = get_queue(chat_id)

        while queue:

            query = queue.pop(0)

            status_message = None

            try:
                # Send status
                if update and update.message:
                    status_message = await update.message.reply_text(
                        f"🔎 Searching YouTube...\n\n🎵 {query}"
                    )

                elif context and context.bot:
                    status_message = await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🔎 Searching YouTube...\n\n🎵 {query}",
                    )

                # Run blocking yt-dlp outside Telegram event loop
                file_path, title, webpage_url = await asyncio.to_thread(
                    download_youtube_audio,
                    query,
                )

                if status_message:
                    try:
                        await status_message.edit_text(
                            f"⬇️ Downloaded:\n🎵 {title}\n\n"
                            f"📤 Sending audio..."
                        )
                    except Exception:
                        pass

                # Send audio
                with open(file_path, "rb") as audio_file:

                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_file,
                        title=title[:250],
                        performer="YouTube",
                        caption=f"🎵 {title}",
                        reply_markup=main_keyboard(),
                    )

                if status_message:
                    try:
                        await status_message.delete()
                    except Exception:
                        pass

                # Cleanup
                try:
                    os.remove(file_path)
                except Exception:
                    pass

                # Small delay
                await asyncio.sleep(1)

            except Exception as e:

                logger.exception(
                    "YouTube/audio error for query: %s",
                    query,
                )

                error_text = str(e)

                # Don't expose huge traceback to Telegram
                if len(error_text) > 700:
                    error_text = error_text[-700:]

                message = (
                    "❌ *YouTube audio download failed.*\n\n"
                    f"🎵 `{query}`\n\n"
                    "Possible reasons:\n"
                    "• YouTube extraction blocked\n"
                    "• Render IP restricted\n"
                    "• YouTube changed its player\n"
                    "• Temporary YouTube error\n\n"
                    "📋 Render Logs mein exact yt-dlp error "
                    "check karo.\n\n"
                    f"Technical error:\n`{error_text}`"
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

                # Continue next queued song
                continue

    finally:
        playing.discard(chat_id)


# ============================================================
# QUEUE COMMAND
# ============================================================

async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id
    queue = get_queue(chat_id)

    if not queue:
        await update.message.reply_text(
            "📋 Queue empty.",
            reply_markup=main_keyboard(),
        )
        return

    text = "📋 *Current Queue*\n\n"

    for index, song in enumerate(queue, start=1):
        text += f"{index}. {song}\n"

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# STOP COMMAND
# ============================================================

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id

    queue = get_queue(chat_id)
    queue.clear()

    await update.message.reply_text(
        "⏹️ Queue stopped and cleared.\n\n"
        "⚠️ Telegram already-sent audio cannot be physically "
        "stopped by the bot.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# SKIP COMMAND
# ============================================================

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id
    queue = get_queue(chat_id)

    if queue:
        skipped = queue.pop(0)

        await update.message.reply_text(
            f"⏭️ Skipped:\n{skipped}",
            reply_markup=main_keyboard(),
        )
    else:
        await update.message.reply_text(
            "📋 Queue empty.",
            reply_markup=main_keyboard(),
        )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    chat_id = query.message.chat_id

    if query.data == "help":

        text = (
            "🎵 *Resso Music Bot*\n\n"
            "/play <song name>\n"
            "/queue\n"
            "/skip\n"
            "/stop\n"
            "/help"
        )

        await query.message.reply_text(
            text,
            parse_mode="Markdown",
        )

    elif query.data == "queue":

        queue = get_queue(chat_id)

        if not queue:
            await query.message.reply_text(
                "📋 Queue empty."
            )
            return

        text = "📋 *Queue*\n\n"

        for i, song in enumerate(queue, 1):
            text += f"{i}. {song}\n"

        await query.message.reply_text(
            text,
            parse_mode="Markdown",
        )

    elif query.data == "stop":

        get_queue(chat_id).clear()

        await query.message.reply_text(
            "⏹️ Queue cleared."
        )

    elif query.data == "skip":

        queue = get_queue(chat_id)

        if queue:
            skipped = queue.pop(0)

            await query.message.reply_text(
                f"⏭️ Skipped:\n{skipped}"
            )
        else:
            await query.message.reply_text(
                "📋 Queue empty."
            )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Telegram error:",
        exc_info=context.error,
    )


# ============================================================
# RUN TELEGRAM BOT
# ============================================================

def run_telegram_bot():

    logger.info("Starting Telegram bot...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("play", play_command)
    )

    application.add_handler(
        CommandHandler("queue", queue_command)
    )

    application.add_handler(
        CommandHandler("stop", stop_command)
    )

    application.add_handler(
        CommandHandler("skip", skip_command)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_error_handler(error_handler)

    logger.info("Telegram bot polling started.")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    # Flask starts in background
    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )

    flask_thread.start()

    logger.info(
        "Flask server started on port %s",
        PORT,
    )

    # Telegram bot runs in main thread
    run_telegram_bot()
