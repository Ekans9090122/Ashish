import os
import asyncio
import logging
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import requests
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType
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
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("resso-bot")

# ============================================================
# ENVIRONMENT
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
PORT = int(os.getenv("PORT", "10000"))

# Comma-separated Telegram numeric user IDs.
# Example: ADMIN_IDS=123456789,987654321
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in Render Environment Variables.")

# ============================================================
# FLASK / RENDER HEALTH SERVER
# ============================================================

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "🎵 Resso Music Bot is running!"


@flask_app.route("/health")
def health():
    return "OK"


def run_flask():
    try:
        flask_app.run(
            host="0.0.0.0",
            port=PORT,
            threaded=True,
            use_reloader=False,
        )
    except Exception:
        logger.exception("Flask server stopped unexpectedly.")


# ============================================================
# SPOTIFY
# ============================================================

spotify = None

if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    try:
        spotify = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
            ),
            requests_timeout=20,
            retries=2,
        )
        logger.info("Spotify initialized.")
    except Exception:
        logger.exception("Spotify initialization failed.")
else:
    logger.warning(
        "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET missing. "
        "Spotify search will be disabled."
    )


# ============================================================
# MUSIC STATE
# ============================================================

queues = defaultdict(deque)
current_song = {}
paused = defaultdict(bool)


# ============================================================
# UI
# ============================================================

def main_menu():
    return InlineKeyboardMarkup(
        [
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
    )


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


async def require_admin(update: Update) -> bool:
    if is_admin(update):
        return True

    message = update.effective_message
    if message:
        await message.reply_text("⛔ This command is admin-only.")
    return False


def fmt_duration(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return "Unknown"

    if seconds <= 0:
        return "Unknown"

    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ============================================================
# SPOTIFY SEARCH
# ============================================================

def spotify_search(query: str, limit: int = 5):
    if spotify is None:
        return []

    try:
        result = spotify.search(
            q=query,
            type="track",
            limit=max(1, min(limit, 10)),
        )

        tracks = result.get("tracks", {}).get("items", [])
        output = []

        for track in tracks:
            artists = ", ".join(
                artist.get("name", "")
                for artist in track.get("artists", [])
            )
            album = track.get("album") or {}
            images = album.get("images") or []

            output.append(
                {
                    "name": track.get("name", "Unknown"),
                    "artists": artists or "Unknown",
                    "album": album.get("name", "Unknown"),
                    "url": (track.get("external_urls") or {}).get("spotify", ""),
                    "duration": int(track.get("duration_ms") or 0) // 1000,
                    "thumbnail": images[0].get("url", "") if images else "",
                    "query": f"{track.get('name', '')} {artists}".strip(),
                }
            )

        return output

    except Exception:
        logger.exception("Spotify search failed for query=%r", query)
        return []


# ============================================================
# YOUTUBE SEARCH + AUDIO DOWNLOAD
# ============================================================

def download_youtube_audio(query: str):
    """
    Searches YouTube and downloads the first result as a direct
    audio file. No FFmpeg conversion is required.
    """

    outtmpl = str(DOWNLOAD_DIR / "%(id)s.%(ext)s")

    ydl_options = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "outtmpl": outtmpl,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "overwrites": False,
        "restrictfilenames": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(
                f"ytsearch1:{query}",
                download=True,
            )

            if not info:
                raise RuntimeError("YouTube returned no result.")

            entries = info.get("entries")
            video = entries[0] if entries else info

            if not video:
                raise RuntimeError("YouTube result was empty.")

            video_id = video.get("id")
            if not video_id:
                raise RuntimeError("YouTube video ID was missing.")

            candidates = [
                p
                for p in DOWNLOAD_DIR.glob(f"{video_id}.*")
                if p.suffix.lower() not in {".part", ".ytdl"}
            ]

            if not candidates:
                raise FileNotFoundError(
                    f"Downloaded audio file not found for video {video_id}."
                )

            audio_file = max(candidates, key=lambda p: p.stat().st_size)

            if audio_file.stat().st_size < 1024:
                raise RuntimeError("Downloaded audio file is too small.")

            return {
                "file": str(audio_file),
                "title": video.get("title") or query,
                "url": video.get("webpage_url") or "",
                "thumbnail": video.get("thumbnail") or "",
                "duration": video.get("duration") or 0,
                "uploader": video.get("uploader") or "",
            }

    except Exception as exc:
        logger.exception("YouTube download failed for %r: %s", query, exc)
        return None


# ============================================================
# THUMBNAIL
# ============================================================

def download_thumbnail(url: str, video_id: str):
    if not url:
        return None

    path = DOWNLOAD_DIR / f"{video_id}_thumb.jpg"

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        path.write_bytes(response.content)

        if path.stat().st_size < 100:
            return None

        return str(path)
    except Exception:
        logger.exception("Thumbnail download failed.")
        return None


# ============================================================
# START / HELP
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎵 *Resso Music Bot*\n\n"
        "Search Spotify and get the matching YouTube audio.\n\n"
        "Example:\n"
        "`/play Kesariya`\n\n"
        "Use the buttons below.",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎵 *Commands*\n\n"
        "/start - Open menu\n"
        "/help - Show help\n"
        "/play <song> - Search and add music\n"
        "/search <song> - Search Spotify\n"
        "/pause - Pause bot state\n"
        "/resume - Resume bot state\n"
        "/skip - Skip current item\n"
        "/stop - Stop and clear queue\n"
        "/queue - Show queue\n\n"
        "👑 *Admin*\n"
        "/clearqueue - Clear current chat queue\n"
        "/force_skip - Admin skip\n"
        "/stats - Bot statistics",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ============================================================
# PLAY / QUEUE
# ============================================================

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id

    if not context.args:
        await message.reply_text(
            "🎵 Usage:\n`/play Kesariya`",
            parse_mode="Markdown",
        )
        return

    query = " ".join(context.args).strip()

    status = await message.reply_text(
        f"🔎 Searching for *{query}*...",
        parse_mode="Markdown",
    )

    spotify_results = await asyncio.to_thread(spotify_search, query, 5)

    spotify_track = spotify_results[0] if spotify_results else None
    youtube_query = spotify_track["query"] if spotify_track else query

    if spotify_track:
        await status.edit_text(
            f"🎵 *{spotify_track['name']}*\n"
            f"👤 {spotify_track['artists']}\n"
            f"💿 {spotify_track['album']}\n"
            f"⏱ {fmt_duration(spotify_track['duration'])}\n\n"
            f"⬇️ Finding audio on YouTube...",
            parse_mode="Markdown",
        )
    else:
        await status.edit_text(
            f"🔎 Spotify result not found.\n\n"
            f"▶️ Searching YouTube for *{query}*...",
            parse_mode="Markdown",
        )

    youtube = await asyncio.to_thread(
        download_youtube_audio,
        youtube_query,
    )

    if not youtube:
        await status.edit_text(
            "❌ YouTube audio download failed.\n\n"
            "Check Render Logs for `YouTube download failed` "
            "to see the exact extraction error.",
        )
        return

    song = {
        "title": youtube["title"],
        "file": youtube["file"],
        "youtube_url": youtube["url"],
        "thumbnail": youtube["thumbnail"],
        "duration": youtube["duration"],
        "uploader": youtube["uploader"],
        "spotify": spotify_track,
        "added_by": update.effective_user.id if update.effective_user else None,
    }

    was_playing = chat_id in current_song
    queues[chat_id].append(song)

    if was_playing:
        position = len(queues[chat_id])
        await status.edit_text(
            f"✅ *Added to queue*\n\n"
            f"🎵 {song['title']}\n"
            f"📋 Position: {position}",
            parse_mode="Markdown",
        )
        return

    await status.delete()
    await play_next(update, context)


async def play_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not queues[chat_id]:
        current_song.pop(chat_id, None)
        paused[chat_id] = False
        return

    song = queues[chat_id].popleft()
    current_song[chat_id] = song
    paused[chat_id] = False

    caption = (
        f"🎵 *Now Playing*\n\n"
        f"*{song['title']}*\n"
        f"👤 {song.get('uploader') or 'Unknown'}\n"
        f"⏱ {fmt_duration(song.get('duration'))}\n\n"
        f"Use the buttons below."
    )

    try:
        # Telegram's send_audio thumbnail support works with a local file.
        thumb = None
        video_id = song.get("youtube_url", "").split("v=")[-1].split("&")[0]
        if video_id:
            thumb = await asyncio.to_thread(
                download_thumbnail,
                song.get("thumbnail", ""),
                video_id,
            )

        kwargs = {
            "audio": song["file"],
            "caption": caption,
            "parse_mode": "Markdown",
            "reply_markup": main_menu(),
            "title": song["title"][:64],
            "performer": song.get("uploader", "")[:64] or None,
            "duration": int(song.get("duration") or 0),
        }

        if thumb and Path(thumb).exists():
            kwargs["thumbnail"] = thumb

        await update.effective_chat.send_audio(**kwargs)

    except Exception:
        logger.exception("Failed to send audio to Telegram.")
        current_song.pop(chat_id, None)

        await update.effective_chat.send_message(
            "❌ Telegram audio upload failed. "
            "The downloaded file may be unsupported or too large."
        )


# ============================================================
# PAUSE / RESUME / SKIP / STOP
# ============================================================

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in current_song:
        await update.effective_message.reply_text("❌ Nothing is queued/playing.")
        return

    paused[chat_id] = True
    await update.effective_message.reply_text(
        "⏸ Pause state enabled.\n\n"
        "⚠️ Telegram Bot API cannot pause an audio message that has "
        "already been sent. This state is used by the bot controls.",
        reply_markup=main_menu(),
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in current_song:
        await update.effective_message.reply_text("❌ Nothing is queued/playing.")
        return

    paused[chat_id] = False
    await update.effective_message.reply_text(
        "▶️ Resume state enabled.",
        reply_markup=main_menu(),
    )


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in current_song:
        await update.effective_message.reply_text("❌ Nothing is playing.")
        return

    old = current_song.pop(chat_id, None)
    paused[chat_id] = False

    if old:
        await update.effective_message.reply_text(
            f"⏭ Skipped: {old['title']}"
        )

    await play_next(update, context)

    if chat_id not in current_song:
        await update.effective_message.reply_text("📭 Queue is empty.")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    queues[chat_id].clear()
    current_song.pop(chat_id, None)
    paused[chat_id] = False

    await update.effective_message.reply_text(
        "⏹ Stopped.\n🗑 Queue cleared.",
        reply_markup=main_menu(),
    )


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lines = ["📋 *Music Queue*", ""]

    if chat_id in current_song:
        lines += [
            "🎵 *Current:*",
            current_song[chat_id]["title"],
            "",
        ]

    items = list(queues[chat_id])[:15]

    if not items:
        lines.append("📭 Queue is empty.")
    else:
        for index, song in enumerate(items, 1):
            lines.append(f"{index}. {song['title']}")

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ============================================================
# SPOTIFY SEARCH COMMAND
# ============================================================

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not context.args:
        await message.reply_text(
            "🔎 Usage:\n`/search song name`",
            parse_mode="Markdown",
        )
        return

    if spotify is None:
        await message.reply_text(
            "❌ Spotify is not configured.\n"
            "Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in Render."
        )
        return

    query = " ".join(context.args)

    results = await asyncio.to_thread(
        spotify_search,
        query,
        5,
    )

    if not results:
        await message.reply_text("❌ No Spotify results found.")
        return

    for i, result in enumerate(results, 1):
        await message.reply_text(
            f"*{i}. {result['name']}*\n"
            f"👤 {result['artists']}\n"
            f"💿 {result['album']}\n"
            f"⏱ {fmt_duration(result['duration'])}\n"
            f"🔗 {result['url']}",
            parse_mode="Markdown",
        )


# ============================================================
# ADMIN COMMANDS
# ============================================================

async def clearqueue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    chat_id = update.effective_chat.id
    queues[chat_id].clear()

    await update.effective_message.reply_text(
        "👑 Queue cleared by admin."
    )


async def force_skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    chat_id = update.effective_chat.id

    if chat_id not in current_song:
        await update.effective_message.reply_text("❌ Nothing is playing.")
        return

    current_song.pop(chat_id, None)
    paused[chat_id] = False

    await update.effective_message.reply_text("👑 Admin skipped current song.")
    await play_next(update, context)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    active = len(current_song)
    queued = sum(len(q) for q in queues.values())

    await update.effective_message.reply_text(
        "👑 *Bot Stats*\n\n"
        f"Active chats: {active}\n"
        f"Queued songs: {queued}\n"
        f"Admins configured: {len(ADMIN_IDS)}",
        parse_mode="Markdown",
    )


# ============================================================
# BUTTONS
# ============================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    data = query.data

    if data == "play_help":
        await query.message.reply_text(
            "🎵 `/play <song name>`\n\nExample:\n`/play Kesariya`",
            parse_mode="Markdown",
        )

    elif data == "search_help":
        await query.message.reply_text(
            "🔎 `/search <song name>`",
            parse_mode="Markdown",
        )

    elif data == "pause":
        if chat_id not in current_song:
            await query.message.reply_text("❌ Nothing is playing.")
        else:
            paused[chat_id] = True
            await query.message.reply_text(
                "⏸ Pause state enabled.\n"
                "Telegram Bot API cannot pause an already-sent audio message."
            )

    elif data == "resume":
        if chat_id not in current_song:
            await query.message.reply_text("❌ Nothing is playing.")
        else:
            paused[chat_id] = False
            await query.message.reply_text("▶️ Resume state enabled.")

    elif data == "skip":
        if chat_id not in current_song:
            await query.message.reply_text("❌ Nothing is playing.")
        else:
            current_song.pop(chat_id, None)
            paused[chat_id] = False
            await play_next(query.message, context)
            if chat_id not in current_song:
                await query.message.reply_text("📭 Queue is empty.")

    elif data == "stop":
        queues[chat_id].clear()
        current_song.pop(chat_id, None)
        paused[chat_id] = False
        await query.message.reply_text("⏹ Stopped and queue cleared.")

    elif data == "queue":
        lines = ["📋 *Queue*", ""]

        if chat_id in current_song:
            lines += [
                f"🎵 Current: {current_song[chat_id]['title']}",
                "",
            ]

        items = list(queues[chat_id])[:15]

        if items:
            lines.extend(
                f"{i}. {song['title']}"
                for i, song in enumerate(items, 1)
            )
        else:
            lines.append("📭 Queue is empty.")

        await query.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
        )

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
            parse_mode="Markdown",
        )


# ============================================================
# CLEANUP
# ============================================================

def cleanup_old_files(max_age_hours=6):
    now = time.time()

    for path in DOWNLOAD_DIR.iterdir():
        try:
            if not path.is_file():
                continue

            if now - path.stat().st_mtime > max_age_hours * 3600:
                path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to clean %s", path)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception(
        "Unhandled Telegram error",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
        name="flask-server",
    )
    flask_thread.start()

    cleanup_old_files()

    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("play", play_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("skip", skip_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("queue", queue_command))

    application.add_handler(
        CommandHandler("clearqueue", clearqueue_command)
    )
    application.add_handler(
        CommandHandler("force_skip", force_skip_command)
    )
    application.add_handler(
        CommandHandler("stats", stats_command)
    )

    application.add_handler(
        CallbackQueryHandler(button_callback)
    )

    application.add_error_handler(error_handler)

    logger.info("🎵 Resso Music Bot starting...")
    logger.info("Admins configured: %s", sorted(ADMIN_IDS))

    # This call blocks and keeps the Telegram bot process alive.
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
