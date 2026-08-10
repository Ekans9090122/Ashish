import os
import asyncio
import logging
import threading
import time
from pathlib import Path
from collections import defaultdict, deque

import requests
import yt_dlp

from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("resso-bot")


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

# Optional admin IDs:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS = set()

for value in os.getenv("ADMIN_IDS", "").split(","):
    value = value.strip()
    if value.isdigit():
        ADMIN_IDS.add(int(value))


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Add BOT_TOKEN in Render Environment Variables."
    )


# ============================================================
# DOWNLOAD DIRECTORY
# ============================================================

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# FLASK HEALTH SERVER
# ============================================================

web_app = Flask("resso_health")


@web_app.route("/")
def home():
    return "Resso Music Bot is running!"


@web_app.route("/health")
def health():
    return "OK"


def start_web_server():
    try:
        web_app.run(
            host="0.0.0.0",
            port=PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    except Exception:
        logger.exception("Flask server crashed.")


# ============================================================
# MUSIC STATE
# ============================================================

queues = defaultdict(deque)
current_song = {}
paused = defaultdict(bool)


# ============================================================
# BUTTONS
# ============================================================

def main_menu():
    keyboard = [
        [
            InlineKeyboardButton("🎵 Play", callback_data="play_help"),
            InlineKeyboardButton("🔎 Search", callback_data="search_help"),
        ],
        [
            InlineKeyboardButton("⏸ Pause", callback_data="pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="resume"),
        ],
        [
            InlineKeyboardButton("⏭ Skip", callback_data="skip"),
            InlineKeyboardButton("⏹ Stop", callback_data="stop"),
        ],
        [
            InlineKeyboardButton("📋 Queue", callback_data="queue"),
            InlineKeyboardButton("ℹ️ Help", callback_data="help"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# ============================================================
# HELPERS
# ============================================================

def format_duration(seconds):
    try:
        seconds = int(seconds or 0)
    except Exception:
        return "Unknown"

    if seconds <= 0:
        return "Unknown"

    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def is_admin(update: Update):
    user = update.effective_user

    if not user:
        return False

    return user.id in ADMIN_IDS


async def admin_only(update: Update):
    if is_admin(update):
        return True

    message = update.effective_message

    if message:
        await message.reply_text(
            "⛔ This command is available only to the bot admin."
        )

    return False


# ============================================================
# YOUTUBE SEARCH + DOWNLOAD
# ============================================================

def youtube_download(song_query):
    """
    Searches YouTube and downloads the best available audio.

    IMPORTANT:
    YouTube API registration is NOT required.
    yt-dlp talks directly to YouTube.
    """

    logger.info("YouTube search started: %s", song_query)

    output_template = str(
        DOWNLOAD_DIR / "%(id)s.%(ext)s"
    )

    options = {
        # Prefer m4a because Telegram generally handles it well.
        "format": (
            "bestaudio[ext=m4a]/"
            "bestaudio[ext=mp4]/"
            "bestaudio/best"
        ),

        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": False,
        "no_warnings": False,

        "default_search": "ytsearch1",

        "retries": 3,
        "fragment_retries": 3,

        "socket_timeout": 30,

        "continuedl": True,

        "overwrites": False,

        "restrictfilenames": True,

        # Try multiple YouTube clients.
        "extractor_args": {
            "youtube": {
                "player_client": [
                    "android",
                    "web",
                    "web_safari",
                ]
            }
        },
    }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:

            result = ydl.extract_info(
                f"ytsearch1:{song_query}",
                download=True,
            )

            if not result:
                logger.error("YouTube returned no result.")
                return None

            entries = result.get("entries")

            if entries:
                video = entries[0]
            else:
                video = result

            if not video:
                logger.error("YouTube video result is empty.")
                return None

            video_id = video.get("id")

            if not video_id:
                logger.error("YouTube video ID missing.")
                return None

            logger.info(
                "YouTube result: %s | ID: %s",
                video.get("title"),
                video_id,
            )

            # Find downloaded file.
            files = []

            for file in DOWNLOAD_DIR.glob(f"{video_id}.*"):
                if file.suffix.lower() not in (
                    ".part",
                    ".ytdl",
                ):
                    files.append(file)

            if not files:
                logger.error(
                    "Downloaded file not found for video %s",
                    video_id,
                )
                return None

            # Pick largest downloaded file.
            audio_file = max(
                files,
                key=lambda x: x.stat().st_size,
            )

            file_size = audio_file.stat().st_size

            logger.info(
                "Downloaded file: %s | Size: %s bytes",
                audio_file,
                file_size,
            )

            if file_size < 1024:
                logger.error("Downloaded file is too small.")
                return None

            return {
                "file": str(audio_file),
                "id": video_id,
                "title": video.get("title") or song_query,
                "url": video.get("webpage_url") or "",
                "thumbnail": video.get("thumbnail") or "",
                "duration": video.get("duration") or 0,
                "uploader": video.get("uploader") or "",
            }

    except Exception as error:
        logger.exception(
            "YOUTUBE DOWNLOAD ERROR: %s",
            error,
        )

        return None


# ============================================================
# THUMBNAIL
# ============================================================

def download_thumbnail(url, video_id):

    if not url:
        return None

    path = DOWNLOAD_DIR / f"{video_id}_thumbnail.jpg"

    try:
        response = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Linux; Android 10) "
                    "AppleWebKit/537.36 "
                    "Chrome/120 Safari/537.36"
                )
            },
        )

        response.raise_for_status()

        path.write_bytes(response.content)

        if path.exists() and path.stat().st_size > 100:
            return str(path)

    except Exception:
        logger.exception("Thumbnail download failed.")

    return None


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.effective_message.reply_text(
        "🎵 *Resso Music Bot*\n\n"
        "YouTube se music search karke audio bhejta hai.\n\n"
        "🎧 Example:\n"
        "`/play Kesariya`\n\n"
        "Ya:\n"
        "`/play Arijit Singh Kesariya`\n\n"
        "Neeche buttons use kar sakte ho.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.effective_message.reply_text(
        "🎵 *Resso Bot Commands*\n\n"

        "/start - Bot start\n"
        "/help - Help\n"
        "/play <song> - Song play/download\n"
        "/search <song> - YouTube search\n"
        "/pause - Pause state\n"
        "/resume - Resume state\n"
        "/skip - Skip song\n"
        "/stop - Stop and clear queue\n"
        "/queue - Show queue\n\n"

        "👑 *Admin Commands*\n"
        "/clearqueue - Clear queue\n"
        "/force_skip - Force skip\n"
        "/stats - Bot statistics",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )


# ============================================================
# PLAY
# ============================================================

async def play_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    chat_id = chat.id

    if not context.args:
        await message.reply_text(
            "🎵 *Use:*\n"
            "`/play <song name>`\n\n"
            "*Example:*\n"
            "`/play Kesariya`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    query = " ".join(context.args).strip()

    status = await message.reply_text(
        f"🔎 Searching YouTube for:\n\n"
        f"🎵 *{query}*",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Run blocking yt-dlp outside Telegram event loop.
    song = await asyncio.to_thread(
        youtube_download,
        query,
    )

    if not song:

        await status.edit_text(
            "❌ *YouTube audio download failed.*\n\n"
            "Possible reasons:\n"
            "• YouTube extraction blocked\n"
            "• Render IP restriction\n"
            "• yt-dlp extraction problem\n"
            "• YouTube changed something\n\n"
            "Render Logs mein `YOUTUBE DOWNLOAD ERROR` search karo.",
            parse_mode=ParseMode.MARKDOWN,
        )

        return

    song["added_by"] = (
        update.effective_user.id
        if update.effective_user
        else None
    )

    # Add to queue.
    already_playing = chat_id in current_song

    queues[chat_id].append(song)

    # If something is already playing, don't send immediately.
    if already_playing:

        position = len(queues[chat_id])

        await status.edit_text(
            f"✅ *Added to queue*\n\n"
            f"🎵 {song['title']}\n"
            f"📋 Position: {position}",
            parse_mode=ParseMode.MARKDOWN,
        )

        return

    try:
        await status.delete()
    except Exception:
        pass

    await play_next(
        chat_id,
        chat,
    )


# ============================================================
# PLAY NEXT
# ============================================================

async def play_next(chat_id, chat):

    if not queues[chat_id]:

        current_song.pop(chat_id, None)
        paused[chat_id] = False

        return

    song = queues[chat_id].popleft()

    current_song[chat_id] = song
    paused[chat_id] = False

    thumbnail = None

    if song.get("thumbnail") and song.get("id"):

        thumbnail = await asyncio.to_thread(
            download_thumbnail,
            song["thumbnail"],
            song["id"],
        )

    caption = (
        "🎵 *Now Playing*\n\n"
        f"*{song['title']}*\n\n"
        f"👤 {song.get('uploader') or 'Unknown'}\n"
        f"⏱ {format_duration(song.get('duration'))}\n\n"
        "🎧 Enjoy your music!"
    )

    try:

        with open(song["file"], "rb") as audio_file:

            kwargs = {
                "audio": audio_file,
                "caption": caption,
                "parse_mode": ParseMode.MARKDOWN,
                "title": song["title"][:64],
                "performer": (
                    song.get("uploader") or "Resso"
                )[:64],
                "duration": int(
                    song.get("duration") or 0
                ),
                "reply_markup": main_menu(),
            }

            if thumbnail and Path(thumbnail).exists():

                with open(thumbnail, "rb") as thumb_file:

                    kwargs["thumbnail"] = thumb_file

                    await chat.send_audio(**kwargs)

            else:

                await chat.send_audio(**kwargs)

        logger.info(
            "Audio sent successfully: %s",
            song["title"],
        )

    except Exception as error:

        logger.exception(
            "Telegram audio upload failed: %s",
            error,
        )

        await chat.send_message(
            "❌ Audio Telegram par send nahi ho saka.\n\n"
            "Downloaded file unsupported ya bahut large ho sakti hai."
        )

        current_song.pop(chat_id, None)

        # Automatically continue queue.
        await play_next(chat_id, chat)


# ============================================================
# SEARCH
# ============================================================

async def search_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message

    if not context.args:

        await message.reply_text(
            "🔎 *Use:*\n"
            "`/search song name`",
            parse_mode=ParseMode.MARKDOWN,
        )

        return

    query = " ".join(context.args)

    status = await message.reply_text(
        f"🔎 Searching YouTube for *{query}*...",
        parse_mode=ParseMode.MARKDOWN,
    )

    def search_only():

        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
        }

        try:

            with yt_dlp.YoutubeDL(options) as ydl:

                result = ydl.extract_info(
                    f"ytsearch5:{query}",
                    download=False,
                )

                return result.get("entries", [])

        except Exception:

            logger.exception(
                "YouTube search failed."
            )

            return []

    results = await asyncio.to_thread(
        search_only
    )

    if not results:

        await status.edit_text(
            "❌ No YouTube results found."
        )

        return

    lines = [
        "🔎 *YouTube Search Results*\n"
    ]

    for index, item in enumerate(results[:5], 1):

        title = item.get("title") or "Unknown"

        url = (
            item.get("url")
            or item.get("webpage_url")
            or ""
        )

        if url and not url.startswith("http"):

            video_id = item.get("id")

            if video_id:
                url = (
                    "https://www.youtube.com/watch?v="
                    + video_id
                )

        lines.append(
            f"*{index}. {title}*\n"
            f"🔗 {url}\n"
        )

    await status.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


# ============================================================
# PAUSE
# ============================================================

async def pause_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    if chat_id not in current_song:

        await update.effective_message.reply_text(
            "❌ Nothing is playing."
        )

        return

    paused[chat_id] = True

    await update.effective_message.reply_text(
        "⏸ Pause state enabled.\n\n"
        "⚠️ Telegram Bot API already-sent audio ko "
        "actual playback mein pause nahi kar sakta."
    )


# ============================================================
# RESUME
# ============================================================

async def resume_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    if chat_id not in current_song:

        await update.effective_message.reply_text(
            "❌ Nothing is playing."
        )

        return

    paused[chat_id] = False

    await update.effective_message.reply_text(
        "▶️ Resume state enabled."
    )


# ============================================================
# SKIP
# ============================================================

async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat
    chat_id = chat.id

    if chat_id not in current_song:

        await update.effective_message.reply_text(
            "❌ Nothing is playing."
        )

        return

    old_song = current_song.pop(
        chat_id,
        None,
    )

    paused[chat_id] = False

    if old_song:

        await update.effective_message.reply_text(
            f"⏭ Skipped:\n{old_song['title']}"
        )

    await play_next(
        chat_id,
        chat,
    )

    if chat_id not in current_song:

        await update.effective_message.reply_text(
            "📭 Queue is empty."
        )


# ============================================================
# STOP
# ============================================================

async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    queues[chat_id].clear()

    current_song.pop(
        chat_id,
        None,
    )

    paused[chat_id] = False

    await update.effective_message.reply_text(
        "⏹ *Stopped*\n\n"
        "🗑 Queue cleared.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )


# ============================================================
# QUEUE
# ============================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    lines = [
        "📋 *Music Queue*\n"
    ]

    if chat_id in current_song:

        lines.append(
            "🎵 *Current:*\n"
            f"{current_song[chat_id]['title']}\n"
        )

    songs = list(
        queues[chat_id]
    )[:15]

    if not songs:

        lines.append(
            "📭 Queue is empty."
        )

    else:

        for index, song in enumerate(
            songs,
            1,
        ):

            lines.append(
                f"{index}. {song['title']}"
            )

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )


# ============================================================
# ADMIN - CLEAR QUEUE
# ============================================================

async def clearqueue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await admin_only(update):
        return

    chat_id = update.effective_chat.id

    queues[chat_id].clear()

    await update.effective_message.reply_text(
        "👑 Queue cleared by admin."
    )


# ============================================================
# ADMIN - FORCE SKIP
# ============================================================

async def force_skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await admin_only(update):
        return

    chat = update.effective_chat
    chat_id = chat.id

    if chat_id not in current_song:

        await update.effective_message.reply_text(
            "❌ Nothing is playing."
        )

        return

    current_song.pop(
        chat_id,
        None,
    )

    paused[chat_id] = False

    await update.effective_message.reply_text(
        "👑 Admin skipped current song."
    )

    await play_next(
        chat_id,
        chat,
    )


# ============================================================
# ADMIN - STATS
# ============================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await admin_only(update):
        return

    active_chats = len(current_song)

    queued_songs = sum(
        len(queue)
        for queue in queues.values()
    )

    await update.effective_message.reply_text(
        "👑 *Bot Statistics*\n\n"
        f"🎧 Active chats: {active_chats}\n"
        f"📋 Queued songs: {queued_songs}\n"
        f"👑 Admins: {len(ADMIN_IDS)}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# BUTTON CALLBACK
# ============================================================

async def button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    chat = query.message.chat
    chat_id = chat.id

    data = query.data

    # --------------------------------------------------------
    # PLAY HELP
    # --------------------------------------------------------

    if data == "play_help":

        await query.message.reply_text(
            "🎵 *Use:*\n"
            "`/play <song name>`\n\n"
            "Example:\n"
            "`/play Kesariya`",
            parse_mode=ParseMode.MARKDOWN,
        )

    # --------------------------------------------------------
    # SEARCH HELP
    # --------------------------------------------------------

    elif data == "search_help":

        await query.message.reply_text(
            "🔎 *Use:*\n"
            "`/search <song name>`",
            parse_mode=ParseMode.MARKDOWN,
        )

    # --------------------------------------------------------
    # PAUSE
    # --------------------------------------------------------

    elif data == "pause":

        if chat_id not in current_song:

            await query.message.reply_text(
                "❌ Nothing is playing."
            )

        else:

            paused[chat_id] = True

            await query.message.reply_text(
                "⏸ Pause state enabled."
            )

    # --------------------------------------------------------
    # RESUME
    # --------------------------------------------------------

    elif data == "resume":

        if chat_id not in current_song:

            await query.message.reply_text(
                "❌ Nothing is playing."
            )

        else:

            paused[chat_id] = False

            await query.message.reply_text(
                "▶️ Resume state enabled."
            )

    # --------------------------------------------------------
    # SKIP
    # --------------------------------------------------------

    elif data == "skip":

        if chat_id not in current_song:

            await query.message.reply_text(
                "❌ Nothing is playing."
            )

        else:

            old = current_song.pop(
                chat_id,
                None,
            )

            paused[chat_id] = False

            if old:

                await query.message.reply_text(
                    f"⏭ Skipped:\n{old['title']}"
                )

            await play_next(
                chat_id,
                chat,
            )

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    elif data == "stop":

        queues[chat_id].clear()

        current_song.pop(
            chat_id,
            None,
        )

        paused[chat_id] = False

        await query.message.reply_text(
            "⏹ Stopped.\n"
            "🗑 Queue cleared."
        )

    # --------------------------------------------------------
    # QUEUE
    # --------------------------------------------------------

    elif data == "queue":

        lines = [
            "📋 *Music Queue*\n"
        ]

        if chat_id in current_song:

            lines.append(
                f"🎵 Current:\n"
                f"{current_song[chat_id]['title']}\n"
            )

        songs = list(
            queues[chat_id]
        )[:15]

        if songs:

            for index, song in enumerate(
                songs,
                1,
            ):

                lines.append(
                    f"{index}. {song['title']}"
                )

        else:

            lines.append(
                "📭 Queue is empty."
            )

        await query.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    elif data == "help":

        await query.message.reply_text(
            "🎵 *Commands*\n\n"
            "/start\n"
            "/play <song>\n"
            "/search <song>\n"
            "/pause\n"
            "/resume\n"
            "/skip\n"
            "/stop\n"
            "/queue\n\n"
            "👑 Admin:\n"
            "/clearqueue\n"
            "/force_skip\n"
            "/stats",
            parse_mode=ParseMode.MARKDOWN,
        )


# ============================================================
# CLEANUP OLD FILES
# ============================================================

def cleanup_files():

    now = time.time()

    for file in DOWNLOAD_DIR.iterdir():

        try:

            if not file.is_file():
                continue

            age = (
                now - file.stat().st_mtime
            )

            # Delete files older than 6 hours.
            if age > 6 * 60 * 60:

                file.unlink(
                    missing_ok=True
                )

                logger.info(
                    "Deleted old file: %s",
                    file,
                )

        except Exception:

            logger.exception(
                "Could not delete %s",
                file,
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
        exc_info=True,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info("===================================")
    logger.info("Starting Resso Music Bot")
    logger.info("===================================")

    logger.info(
        "Admin IDs: %s",
        sorted(ADMIN_IDS),
    )

    # Start Render health server.
    web_thread = threading.Thread(
        target=start_web_server,
        daemon=True,
        name="render-health-server",
    )

    web_thread.start()

    # Cleanup old downloads.
    cleanup_files()

    # Build Telegram application.
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
            "search",
            search_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "pause",
            pause_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "resume",
            resume_command,
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
            "queue",
            queue_command,
        )
    )

    # --------------------------------------------------------
    # ADMIN COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "clearqueue",
            clearqueue_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "force_skip",
            force_skip_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats_command,
        )
    )

    # --------------------------------------------------------
    # BUTTONS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            button_callback
        )
    )

    # --------------------------------------------------------
    # ERROR HANDLER
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Telegram bot is starting..."
    )

    # Start polling.
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
