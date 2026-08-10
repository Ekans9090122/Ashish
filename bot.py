import os
import asyncio
import logging
import threading
import time
from pathlib import Path
from collections import defaultdict, deque

import requests
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from flask import Flask

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

PORT = int(os.getenv("PORT", "10000"))

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

web_app = Flask(__name__)


@web_app.get("/")
def home():
    return "Resso Music Bot is running!"


@web_app.get("/health")
def health():
    return "OK"


def run_web_server():
    try:
        web_app.run(
            host="0.0.0.0",
            port=PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    except Exception:
        logger.exception("Flask server error")


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

        logger.info("Spotify initialized successfully.")

    except Exception:
        logger.exception("Spotify initialization failed.")
        spotify = None

else:
    logger.warning(
        "Spotify credentials not configured. "
        "Spotify search will be disabled."
    )


# ============================================================
# MUSIC STATE
# ============================================================

music_queue = defaultdict(deque)
current_song = {}
paused_state = defaultdict(bool)


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


async def require_admin(update: Update):

    if is_admin(update):
        return True

    message = update.effective_message

    if message:
        await message.reply_text(
            "⛔ This command is available only to the bot admin."
        )

    return False


# ============================================================
# BUTTON MENU
# ============================================================

def main_menu():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎵 Play",
                    callback_data="play_help",
                ),
                InlineKeyboardButton(
                    "🔎 Search",
                    callback_data="search_help",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⏸ Pause",
                    callback_data="pause",
                ),
                InlineKeyboardButton(
                    "▶️ Resume",
                    callback_data="resume",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⏭ Skip",
                    callback_data="skip",
                ),
                InlineKeyboardButton(
                    "⏹ Stop",
                    callback_data="stop",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Queue",
                    callback_data="queue",
                ),
                InlineKeyboardButton(
                    "ℹ️ Help",
                    callback_data="help",
                ),
            ],
        ]
    )


# ============================================================
# SPOTIFY SEARCH
# ============================================================

def search_spotify(query, limit=5):

    if spotify is None:
        return []

    try:

        result = spotify.search(
            q=query,
            type="track",
            limit=min(max(limit, 1), 10),
        )

        tracks = result.get("tracks", {}).get("items", [])

        results = []

        for track in tracks:

            artists = ", ".join(
                artist.get("name", "")
                for artist in track.get("artists", [])
            )

            album = track.get("album") or {}

            images = album.get("images") or []

            results.append(
                {
                    "name": track.get("name", "Unknown"),
                    "artists": artists or "Unknown",
                    "album": album.get("name", "Unknown"),
                    "duration": int(
                        track.get("duration_ms") or 0
                    )
                    // 1000,
                    "spotify_url": (
                        track.get("external_urls") or {}
                    ).get("spotify", ""),
                    "thumbnail": (
                        images[0].get("url", "")
                        if images
                        else ""
                    ),
                    "youtube_query": (
                        f"{track.get('name', '')} "
                        f"{artists}"
                    ).strip(),
                }
            )

        return results

    except Exception:
        logger.exception(
            "Spotify search error for %s",
            query,
        )

        return []


# ============================================================
# YOUTUBE SEARCH + DOWNLOAD
# ============================================================

def youtube_download(query):

    output_template = str(
        DOWNLOAD_DIR / "%(id)s.%(ext)s"
    )

    attempts = [
        {
            "name": "android",
            "player_client": ["android"],
        },
        {
            "name": "web",
            "player_client": ["web"],
        },
        {
            "name": "ios",
            "player_client": ["ios"],
        },
    ]

    last_error = None

    for attempt in attempts:

        try:

            logger.info(
                "YouTube attempt: %s | query=%s",
                attempt["name"],
                query,
            )

            options = {
                "format": (
                    "bestaudio[ext=m4a]/"
                    "bestaudio[ext=webm]/"
                    "bestaudio/best"
                ),
                "outtmpl": output_template,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "default_search": "ytsearch1",
                "retries": 2,
                "fragment_retries": 2,
                "file_access_retries": 2,
                "socket_timeout": 30,
                "continuedl": True,
                "overwrites": False,
                "restrictfilenames": True,
                "geo_bypass": True,
                "extractor_args": {
                    "youtube": {
                        "player_client": attempt[
                            "player_client"
                        ],
                    }
                },
            }

            with yt_dlp.YoutubeDL(options) as ydl:

                info = ydl.extract_info(
                    "ytsearch1:" + query,
                    download=True,
                )

            if not info:
                raise RuntimeError(
                    "YouTube returned no information."
                )

            entries = info.get("entries")

            if entries:
                video = entries[0]
            else:
                video = info

            if not video:
                raise RuntimeError(
                    "YouTube search returned no video."
                )

            video_id = video.get("id")

            if not video_id:
                raise RuntimeError(
                    "YouTube video ID not found."
                )

            files = []

            for file in DOWNLOAD_DIR.glob(
                video_id + ".*"
            ):

                if file.suffix.lower() in (
                    ".part",
                    ".ytdl",
                ):
                    continue

                if file.is_file():
                    files.append(file)

            if not files:
                raise RuntimeError(
                    "Downloaded file was not found."
                )

            audio_file = max(
                files,
                key=lambda p: p.stat().st_size,
            )

            if audio_file.stat().st_size < 1024:
                raise RuntimeError(
                    "Downloaded file is too small."
                )

            logger.info(
                "YouTube download successful: %s",
                audio_file,
            )

            return {
                "file": str(audio_file),
                "title": video.get("title")
                or query,
                "url": video.get("webpage_url")
                or "",
                "thumbnail": video.get("thumbnail")
                or "",
                "duration": video.get("duration")
                or 0,
                "uploader": video.get("uploader")
                or "",
            }

        except Exception as error:

            last_error = error

            logger.exception(
                "YouTube attempt %s failed: %s",
                attempt["name"],
                error,
            )

            continue

    logger.error(
        "All YouTube download attempts failed: %s",
        last_error,
    )

    return None


# ============================================================
# THUMBNAIL
# ============================================================

def get_thumbnail(url, name):

    if not url:
        return None

    path = DOWNLOAD_DIR / (
        f"{name}_thumbnail.jpg"
    )

    try:

        response = requests.get(
            url,
            timeout=15,
        )

        response.raise_for_status()

        path.write_bytes(response.content)

        if path.stat().st_size < 100:
            return None

        return str(path)

    except Exception:

        logger.exception(
            "Thumbnail download failed."
        )

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
        "Search and download music.\n\n"
        "Example:\n"
        "`/play Kesariya`\n\n"
        "Use the buttons below.",
        parse_mode="Markdown",
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
        "🎵 *Commands*\n\n"
        "/start - Start bot\n"
        "/help - Help\n"
        "/play <song> - Play/download song\n"
        "/search <song> - Spotify search\n"
        "/pause - Pause state\n"
        "/resume - Resume state\n"
        "/skip - Skip song\n"
        "/stop - Stop and clear queue\n"
        "/queue - Show queue\n\n"
        "👑 *Admin commands*\n"
        "/clearqueue\n"
        "/force_skip\n"
        "/stats",
        parse_mode="Markdown",
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
    chat_id = update.effective_chat.id

    if not context.args:

        await message.reply_text(
            "🎵 Use:\n"
            "`/play <song name>`\n\n"
            "Example:\n"
            "`/play Kesariya`",
            parse_mode="Markdown",
        )

        return

    query = " ".join(
        context.args
    ).strip()

    status = await message.reply_text(
        f"🔎 Searching for *{query}*...",
        parse_mode="Markdown",
    )

    spotify_results = await asyncio.to_thread(
        search_spotify,
        query,
        5,
    )

    spotify_track = (
        spotify_results[0]
        if spotify_results
        else None
    )

    if spotify_track:

        youtube_query = (
            spotify_track["youtube_query"]
        )

        await status.edit_text(
            "🎵 *Found song*\n\n"
            f"*{spotify_track['name']}*\n"
            f"👤 {spotify_track['artists']}\n"
            f"💿 {spotify_track['album']}\n"
            f"⏱ {format_duration(spotify_track['duration'])}\n\n"
            "⬇️ Downloading audio...",
            parse_mode="Markdown",
        )

    else:

        youtube_query = query

        await status.edit_text(
            f"🔎 Spotify result not found.\n\n"
            f"▶️ Searching YouTube for *{query}*...",
            parse_mode="Markdown",
        )

    youtube = await asyncio.to_thread(
        youtube_download,
        youtube_query,
    )

    if not youtube:

        await status.edit_text(
            "❌ *YouTube audio download failed.*\n\n"
            "Render Logs mein `YouTube attempt` "
            "search karke exact error dekho.",
            parse_mode="Markdown",
        )

        return

    song = {
        "title": youtube["title"],
        "file": youtube["file"],
        "url": youtube["url"],
        "thumbnail": youtube["thumbnail"],
        "duration": youtube["duration"],
        "uploader": youtube["uploader"],
        "spotify": spotify_track,
        "added_by": (
            update.effective_user.id
            if update.effective_user
            else None
        ),
    }

    music_queue[chat_id].append(song)

    if chat_id in current_song:

        position = len(
            music_queue[chat_id]
        )

        await status.edit_text(
            "✅ *Added to queue*\n\n"
            f"🎵 {song['title']}\n"
            f"📋 Position: {position}",
            parse_mode="Markdown",
        )

        return

    try:
        await status.delete()
    except Exception:
        pass

    await send_next_song(
        update,
        context,
        chat_id,
    )


# ============================================================
# SEND NEXT SONG
# ============================================================

async def send_next_song(
    update,
    context,
    chat_id,
):

    if not music_queue[chat_id]:

        current_song.pop(
            chat_id,
            None,
        )

        paused_state[chat_id] = False

        return

    song = music_queue[chat_id].popleft()

    current_song[chat_id] = song

    paused_state[chat_id] = False

    caption = (
        "🎵 *Now Playing*\n\n"
        f"*{song['title']}*\n"
        f"👤 {song.get('uploader') or 'Unknown'}\n"
        f"⏱ {format_duration(song.get('duration'))}"
    )

    try:

        audio_path = song["file"]

        if not Path(audio_path).exists():

            raise FileNotFoundError(
                "Audio file no longer exists."
            )

        await context.bot.send_audio(
            chat_id=chat_id,
            audio=audio_path,
            title=song["title"][:64],
            performer=(
                song.get("uploader")
                or "Resso"
            )[:64],
            duration=int(
                song.get("duration") or 0
            ),
            caption=caption,
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )

        logger.info(
            "Audio sent successfully to chat %s",
            chat_id,
        )

    except Exception as error:

        logger.exception(
            "Telegram audio upload failed: %s",
            error,
        )

        current_song.pop(
            chat_id,
            None,
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ Telegram audio upload failed.\n\n"
                "Trying next song..."
            ),
        )

        await send_next_song(
            update,
            context,
            chat_id,
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

    paused_state[chat_id] = True

    await update.effective_message.reply_text(
        "⏸ Pause enabled.\n\n"
        "Telegram Bot API already-sent audio ko "
        "actually pause nahi kar sakta."
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

    paused_state[chat_id] = False

    await update.effective_message.reply_text(
        "▶️ Resume enabled."
    )


# ============================================================
# SKIP
# ============================================================

async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    if chat_id not in current_song:

        await update.effective_message.reply_text(
            "❌ Nothing is playing."
        )

        return

    old_song = current_song.pop(
        chat_id,
        None,
    )

    paused_state[chat_id] = False

    if old_song:

        await update.effective_message.reply_text(
            f"⏭ Skipped: {old_song['title']}"
        )

    await send_next_song(
        update,
        context,
        chat_id,
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

    music_queue[chat_id].clear()

    current_song.pop(
        chat_id,
        None,
    )

    paused_state[chat_id] = False

    await update.effective_message.reply_text(
        "⏹ Stopped.\n"
        "🗑 Queue cleared.",
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
        "📋 *Music Queue*",
        "",
    ]

    if chat_id in current_song:

        lines.extend(
            [
                "🎵 *Current:*",
                current_song[chat_id]["title"],
                "",
            ]
        )

    songs = list(
        music_queue[chat_id]
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
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ============================================================
# SPOTIFY SEARCH
# ============================================================

async def search_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message

    if not context.args:

        await message.reply_text(
            "🔎 Use:\n"
            "`/search <song name>`",
            parse_mode="Markdown",
        )

        return

    if spotify is None:

        await message.reply_text(
            "❌ Spotify is not configured.\n\n"
            "Render Environment Variables mein "
            "SPOTIFY_CLIENT_ID aur "
            "SPOTIFY_CLIENT_SECRET add karo."
        )

        return

    query = " ".join(
        context.args
    )

    results = await asyncio.to_thread(
        search_spotify,
        query,
        5,
    )

    if not results:

        await message.reply_text(
            "❌ No Spotify results found."
        )

        return

    for index, result in enumerate(
        results,
        1,
    ):

        text = (
            f"*{index}. {result['name']}*\n"
            f"👤 {result['artists']}\n"
            f"💿 {result['album']}\n"
            f"⏱ {format_duration(result['duration'])}\n"
            f"🔗 {result['spotify_url']}"
        )

        await message.reply_text(
            text,
            parse_mode="Markdown",
        )


# ============================================================
# ADMIN - CLEAR QUEUE
# ============================================================

async def clearqueue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(update):
        return

    chat_id = update.effective_chat.id

    music_queue[chat_id].clear()

    await update.effective_message.reply_text(
        "👑 Queue cleared."
    )


# ============================================================
# ADMIN - FORCE SKIP
# ============================================================

async def force_skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(update):
        return

    chat_id = update.effective_chat.id

    if chat_id not in current_song:

        await update.effective_message.reply_text(
            "❌ Nothing is playing."
        )

        return

    current_song.pop(
        chat_id,
        None,
    )

    paused_state[chat_id] = False

    await update.effective_message.reply_text(
        "👑 Admin skipped current song."
    )

    await send_next_song(
        update,
        context,
        chat_id,
    )


# ============================================================
# ADMIN - STATS
# ============================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(update):
        return

    active = len(current_song)

    queued = sum(
        len(queue)
        for queue in music_queue.values()
    )

    await update.effective_message.reply_text(
        "👑 *Bot Stats*\n\n"
        f"Active chats: {active}\n"
        f"Queued songs: {queued}\n"
        f"Admins: {len(ADMIN_IDS)}",
        parse_mode="Markdown",
    )


# ============================================================
# BUTTON CALLBACKS
# ============================================================

async def button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    chat_id = query.message.chat.id

    data = query.data

    # --------------------------------------------------------
    # PLAY HELP
    # --------------------------------------------------------

    if data == "play_help":

        await query.message.reply_text(
            "🎵 Use:\n"
            "`/play <song name>`\n\n"
            "Example:\n"
            "`/play Kesariya`",
            parse_mode="Markdown",
        )

    # --------------------------------------------------------
    # SEARCH HELP
    # --------------------------------------------------------

    elif data == "search_help":

        await query.message.reply_text(
            "🔎 Use:\n"
            "`/search <song name>`",
            parse_mode="Markdown",
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

            paused_state[chat_id] = True

            await query.message.reply_text(
                "⏸ Pause enabled.\n"
                "Telegram Bot API already-sent "
                "audio ko pause nahi kar sakta."
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

            paused_state[chat_id] = False

            await query.message.reply_text(
                "▶️ Resume enabled."
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

            current_song.pop(
                chat_id,
                None,
            )

            paused_state[chat_id] = False

            await send_next_song(
                update,
                context,
                chat_id,
            )

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    elif data == "stop":

        music_queue[chat_id].clear()

        current_song.pop(
            chat_id,
            None,
        )

        paused_state[chat_id] = False

        await query.message.reply_text(
            "⏹ Stopped.\n"
            "🗑 Queue cleared."
        )

    # --------------------------------------------------------
    # QUEUE
    # --------------------------------------------------------

    elif data == "queue":

        lines = [
            "📋 *Queue*",
            "",
        ]

        if chat_id in current_song:

            lines.extend(
                [
                    "🎵 Current:",
                    current_song[chat_id]["title"],
                    "",
                ]
            )

        songs = list(
            music_queue[chat_id]
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
            parse_mode="Markdown",
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
            parse_mode="Markdown",
        )


# ============================================================
# CLEAN OLD FILES
# ============================================================

def cleanup_files():

    now = time.time()

    for file in DOWNLOAD_DIR.iterdir():

        try:

            if not file.is_file():
                continue

            age = now - file.stat().st_mtime

            if age > 6 * 60 * 60:

                file.unlink(
                    missing_ok=True
                )

        except Exception:

            logger.exception(
                "Could not remove %s",
                file,
            )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Telegram update error: %s",
        context.error,
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "========================================"
    )

    logger.info(
        "Starting Resso Music Bot..."
    )

    logger.info(
        "Python Telegram Bot initialized."
    )

    logger.info(
        "Admins configured: %s",
        sorted(ADMIN_IDS),
    )

    cleanup_files()

    # --------------------------------------------------------
    # START FLASK
    # --------------------------------------------------------

    flask_thread = threading.Thread(
        target=run_web_server,
        daemon=True,
        name="render-health-server",
    )

    flask_thread.start()

    # --------------------------------------------------------
    # CREATE TELEGRAM APPLICATION
    # --------------------------------------------------------

    try:

        application = (
            Application
            .builder()
            .token(BOT_TOKEN)
            .build()
        )

    except Exception:

        logger.exception(
            "Telegram Application initialization failed."
        )

        raise

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

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # START POLLING
    # --------------------------------------------------------

    logger.info(
        "Bot is starting polling..."
    )

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
