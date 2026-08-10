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

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}


DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing in Render Environment Variables."
    )


# ============================================================
# FLASK HEALTH SERVER
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
        logger.exception(
            "Flask server stopped."
        )


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

        logger.info(
            "Spotify initialized successfully."
        )

    except Exception:

        logger.exception(
            "Spotify initialization failed."
        )

else:

    logger.warning(
        "Spotify credentials missing."
    )


# ============================================================
# MUSIC STATE
# ============================================================

queues = defaultdict(deque)

current_song = {}

paused = defaultdict(bool)


# ============================================================
# UI MENU
# ============================================================

def main_menu():

    keyboard = [

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

    return InlineKeyboardMarkup(keyboard)


# ============================================================
# ADMIN
# ============================================================

def is_admin(update: Update):

    user = update.effective_user

    return bool(
        user and user.id in ADMIN_IDS
    )


async def require_admin(update: Update):

    if is_admin(update):
        return True

    message = update.effective_message

    if message:
        await message.reply_text(
            "⛔ This command is admin-only."
        )

    return False


# ============================================================
# DURATION
# ============================================================

def fmt_duration(seconds):

    try:
        seconds = int(seconds or 0)

    except Exception:
        return "Unknown"

    if seconds <= 0:
        return "Unknown"

    minutes, secs = divmod(
        seconds,
        60,
    )

    hours, minutes = divmod(
        minutes,
        60,
    )

    if hours:

        return (
            f"{hours}:"
            f"{minutes:02d}:"
            f"{secs:02d}"
        )

    return (
        f"{minutes}:"
        f"{secs:02d}"
    )


# ============================================================
# SPOTIFY SEARCH
# ============================================================

def spotify_search(
    query: str,
    limit: int = 5,
):

    if spotify is None:
        return []

    try:

        result = spotify.search(
            q=query,
            type="track",
            limit=max(
                1,
                min(limit, 10),
            ),
        )

        tracks = (
            result
            .get("tracks", {})
            .get("items", [])
        )

        output = []

        for track in tracks:

            artists = ", ".join(
                artist.get("name", "")
                for artist in track.get(
                    "artists",
                    [],
                )
            )

            album = (
                track.get("album")
                or {}
            )

            images = (
                album.get("images")
                or []
            )

            output.append(
                {
                    "name": track.get(
                        "name",
                        "Unknown",
                    ),

                    "artists": (
                        artists
                        or "Unknown"
                    ),

                    "album": album.get(
                        "name",
                        "Unknown",
                    ),

                    "url": (
                        track.get(
                            "external_urls"
                        )
                        or {}
                    ).get(
                        "spotify",
                        "",
                    ),

                    "duration": (
                        int(
                            track.get(
                                "duration_ms"
                            )
                            or 0
                        )
                        // 1000
                    ),

                    "thumbnail": (
                        images[0].get(
                            "url",
                            "",
                        )
                        if images
                        else ""
                    ),

                    "query": (
                        f"{track.get('name', '')} "
                        f"{artists}"
                    ).strip(),
                }
            )

        return output

    except Exception:

        logger.exception(
            "Spotify search failed: %r",
            query,
        )

        return []


# ============================================================
# YOUTUBE DOWNLOAD
# ============================================================

def _find_downloaded_file(
    video_id: str,
):

    candidates = []

    for file in DOWNLOAD_DIR.glob(
        f"{video_id}.*"
    ):

        if file.suffix.lower() in {
            ".part",
            ".ytdl",
            ".temp",
        }:
            continue

        if file.is_file():

            try:

                if file.stat().st_size > 1024:
                    candidates.append(file)

            except Exception:
                pass

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda x: x.stat().st_size,
    )


def _yt_download_attempt(
    query: str,
    attempt: int,
):

    output_template = str(
        DOWNLOAD_DIR
        / "%(id)s.%(ext)s"
    )


    # --------------------------------------------------------
    # ATTEMPT 1
    # --------------------------------------------------------

    if attempt == 1:

        options = {

            "format":
                "bestaudio[ext=m4a]/"
                "bestaudio/best",

            "noplaylist": True,

            "quiet": True,

            "no_warnings": False,

            "default_search":
                "ytsearch1",

            "outtmpl":
                output_template,

            "retries": 3,

            "fragment_retries": 3,

            "socket_timeout": 30,

            "concurrent_fragment_downloads": 1,

            "restrictfilenames": True,

            "overwrites": False,

            "js_runtimes": {
                "deno": {}
            },

            "remote_components": {
                "ejs": "github"
            },

            "extractor_args": {
                "youtube": {
                    "player_client": [
                        "web"
                    ]
                }
            },
        }


    # --------------------------------------------------------
    # ATTEMPT 2
    # --------------------------------------------------------

    elif attempt == 2:

        options = {

            "format":
                "bestaudio/best",

            "noplaylist": True,

            "quiet": True,

            "no_warnings": False,

            "default_search":
                "ytsearch1",

            "outtmpl":
                output_template,

            "retries": 3,

            "fragment_retries": 3,

            "socket_timeout": 30,

            "concurrent_fragment_downloads": 1,

            "restrictfilenames": True,

            "overwrites": False,

            "js_runtimes": {
                "deno": {}
            },

            "remote_components": {
                "ejs": "github"
            },

            "extractor_args": {
                "youtube": {
                    "player_client": [
                        "android"
                    ]
                }
            },
        }


    # --------------------------------------------------------
    # ATTEMPT 3
    # --------------------------------------------------------

    else:

        options = {

            "format":
                "bestaudio/best",

            "noplaylist": True,

            "quiet": True,

            "no_warnings": False,

            "default_search":
                "ytsearch1",

            "outtmpl":
                output_template,

            "retries": 3,

            "fragment_retries": 3,

            "socket_timeout": 30,

            "concurrent_fragment_downloads": 1,

            "restrictfilenames": True,

            "overwrites": False,

            "js_runtimes": {
                "deno": {}
            },

            "remote_components": {
                "ejs": "github"
            },

            "extractor_args": {
                "youtube": {
                    "player_client": [
                        "tv"
                    ]
                }
            },
        }


    logger.info(
        "YouTube attempt %s started for: %s",
        attempt,
        query,
    )


    with yt_dlp.YoutubeDL(
        options
    ) as ydl:

        info = ydl.extract_info(
            f"ytsearch1:{query}",
            download=True,
        )

        if not info:
            raise RuntimeError(
                "yt-dlp returned no info."
            )

        entries = info.get(
            "entries"
        )

        video = (
            entries[0]
            if entries
            else info
        )

        if not video:
            raise RuntimeError(
                "YouTube search returned no video."
            )

        video_id = video.get("id")

        if not video_id:
            raise RuntimeError(
                "YouTube video ID missing."
            )

        audio_file = _find_downloaded_file(
            video_id
        )

        if not audio_file:
            raise FileNotFoundError(
                f"Downloaded file not found: "
                f"{video_id}"
            )

        return {
            "file": str(
                audio_file
            ),

            "title": (
                video.get("title")
                or query
            ),

            "url": (
                video.get(
                    "webpage_url"
                )
                or ""
            ),

            "thumbnail": (
                video.get(
                    "thumbnail"
                )
                or ""
            ),

            "duration": (
                video.get(
                    "duration"
                )
                or 0
            ),

            "uploader": (
                video.get(
                    "uploader"
                )
                or ""
            ),
        }


def download_youtube_audio(
    query: str
):

    last_error = None


    for attempt in range(
        1,
        4,
    ):

        try:

            result = _yt_download_attempt(
                query,
                attempt,
            )

            if result:

                logger.info(
                    "YouTube download successful "
                    "on attempt %s: %s",
                    attempt,
                    result["file"],
                )

                return result

        except Exception as exc:

            last_error = exc

            logger.exception(
                "YouTube attempt %s failed: %s",
                attempt,
                exc,
            )

            # Remove incomplete files
            try:

                for file in DOWNLOAD_DIR.iterdir():

                    if file.suffix.lower() in {
                        ".part",
                        ".ytdl",
                        ".temp",
                    }:

                        file.unlink(
                            missing_ok=True
                        )

            except Exception:
                pass


    logger.error(
        "ALL YouTube download attempts failed. "
        "Last error: %s",
        last_error,
    )

    return None


# ============================================================
# THUMBNAIL
# ============================================================

def download_thumbnail(
    url: str,
    video_id: str,
):

    if not url:
        return None

    path = (
        DOWNLOAD_DIR
        / f"{video_id}_thumb.jpg"
    )

    try:

        response = requests.get(
            url,
            timeout=15,
        )

        response.raise_for_status()

        path.write_bytes(
            response.content
        )

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

        "Welcome! 👋\n\n"

        "Search Spotify and get "
        "matching YouTube audio.\n\n"

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

        "🎵 *Music Bot Commands*\n\n"

        "/start - Open menu\n"
        "/help - Show help\n"
        "/play <song> - Play song\n"
        "/search <song> - Spotify search\n"
        "/pause - Pause state\n"
        "/resume - Resume state\n"
        "/skip - Skip song\n"
        "/stop - Stop and clear queue\n"
        "/queue - Show queue\n\n"

        "👑 *Admin Commands*\n"
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

            "🎵 *Usage:*\n\n"
            "`/play Kesariya`\n\n"
            "Example:\n"
            "`/play Arijit Singh Kesariya`",

            parse_mode="Markdown",
        )

        return


    query = " ".join(
        context.args
    ).strip()


    status = await message.reply_text(

        f"🔎 Searching for\n"
        f"🎵 *{query}*",

        parse_mode="Markdown",
    )


    # Spotify
    spotify_results = (
        await asyncio.to_thread(
            spotify_search,
            query,
            5,
        )
    )


    spotify_track = (
        spotify_results[0]
        if spotify_results
        else None
    )


    youtube_query = (
        spotify_track["query"]
        if spotify_track
        else query
    )


    if spotify_track:

        await status.edit_text(

            f"🎵 *{spotify_track['name']}*\n"
            f"👤 {spotify_track['artists']}\n"
            f"💿 {spotify_track['album']}\n"
            f"⏱ {fmt_duration(spotify_track['duration'])}\n\n"
            f"🔎 Searching YouTube...\n"
            f"⬇️ Downloading audio...",

            parse_mode="Markdown",
        )

    else:

        await status.edit_text(

            f"🔎 Spotify result not found.\n\n"
            f"▶️ Searching YouTube...\n"
            f"🎵 *{query}*",

            parse_mode="Markdown",
        )


    # YouTube
    youtube = await asyncio.to_thread(
        download_youtube_audio,
        youtube_query,
    )


    if not youtube:

        await status.edit_text(

            "❌ *YouTube audio download failed.*\n\n"

            "Possible reason:\n"
            "YouTube extraction is blocked or "
            "the current Render IP is restricted.\n\n"

            "📋 Open Render Logs and look for:\n"
            "`YouTube attempt 1 failed`",

            parse_mode="Markdown",
        )

        return


    song = {

        "title":
            youtube["title"],

        "file":
            youtube["file"],

        "youtube_url":
            youtube["url"],

        "thumbnail":
            youtube["thumbnail"],

        "duration":
            youtube["duration"],

        "uploader":
            youtube["uploader"],

        "spotify":
            spotify_track,

        "added_by":
            (
                update.effective_user.id
                if update.effective_user
                else None
            ),
    }


    was_playing = (
        chat_id in current_song
    )


    queues[chat_id].append(
        song
    )


    if was_playing:

        position = len(
            queues[chat_id]
        )


        await status.edit_text(

            f"✅ *Added to queue!*\n\n"

            f"🎵 {song['title']}\n"

            f"📋 Position: {position}",

            parse_mode="Markdown",
        )

        return


    try:

        await status.delete()

    except Exception:
        pass


    await play_next(
        update,
        context,
    )


# ============================================================
# PLAY NEXT
# ============================================================

async def play_next(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = (
        update.effective_chat.id
    )


    if not queues[chat_id]:

        current_song.pop(
            chat_id,
            None
        )

        paused[chat_id] = False

        return


    song = queues[chat_id].popleft()


    current_song[chat_id] = song

    paused[chat_id] = False


    caption = (

        "🎵 *Now Playing*\n\n"

        f"*{song['title']}*\n"

        f"👤 {song.get('uploader') or 'Unknown'}\n"

        f"⏱ {fmt_duration(song.get('duration'))}\n\n"

        "Use the buttons below."
    )


    try:

        thumb = None


        youtube_url = (
            song.get(
                "youtube_url",
                ""
            )
        )


        video_id = ""


        if "v=" in youtube_url:

            video_id = (
                youtube_url
                .split("v=", 1)[1]
                .split("&", 1)[0]
            )

        elif "youtu.be/" in youtube_url:

            video_id = (
                youtube_url
                .split(
                    "youtu.be/",
                    1
                )[1]
                .split(
                    "?",
                    1
                )[0]
            )


        if video_id:

            thumb = (
                await asyncio.to_thread(
                    download_thumbnail,
                    song.get(
                        "thumbnail",
                        ""
                    ),
                    video_id,
                )
            )


        send_kwargs = {

            "audio":
                song["file"],

            "caption":
                caption,

            "parse_mode":
                "Markdown",

            "reply_markup":
                main_menu(),

            "title":
                song["title"][:64],

            "performer":
                (
                    song.get(
                        "uploader",
                        ""
                    )[:64]
                    or None
                ),

            "duration":
                int(
                    song.get(
                        "duration"
                    )
                    or 0
                ),
        }


        if (
            thumb
            and Path(thumb).exists()
        ):

            send_kwargs[
                "thumbnail"
            ] = thumb


        await update.effective_chat.send_audio(
            **send_kwargs
        )


        # Remove local thumbnail
        if thumb:

            try:
                Path(thumb).unlink(
                    missing_ok=True
                )
            except Exception:
                pass


    except Exception as exc:

        logger.exception(
            "Telegram audio upload failed: %s",
            exc,
        )


        current_song.pop(
            chat_id,
            None,
        )


        await update.effective_chat.send_message(

            "❌ Telegram audio upload failed.\n\n"
            "The downloaded file could not be "
            "sent by Telegram."
        )


# ============================================================
# PAUSE
# ============================================================

async def pause_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = (
        update.effective_chat.id
    )


    if chat_id not in current_song:

        await update.effective_message.reply_text(
            "❌ Nothing is playing."
        )

        return


    paused[chat_id] = True


    await update.effective_message.reply_text(

        "⏸ Pause state enabled.\n\n"

        "⚠️ Telegram Bot API cannot "
        "pause an already-sent audio message.",

        reply_markup=main_menu(),
    )


# ============================================================
# RESUME
# ============================================================

async def resume_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = (
        update.effective_chat.id
    )


    if chat_id not in current_song:

        await update.effective_message.reply_text(
            "❌ Nothing is playing."
        )

        return


    paused[chat_id] = False


    await update.effective_message.reply_text(

        "▶️ Resume state enabled.",

        reply_markup=main_menu(),
    )


# ============================================================
# SKIP
# ============================================================

async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = (
        update.effective_chat.id
    )


    if chat_id not in current_song:

        await update.effective_message.reply_text(
            "❌ Nothing is playing."
        )

        return


    old = current_song.pop(
        chat_id,
        None,
    )


    paused[chat_id] = False


    if old:

        await update.effective_message.reply_text(

            f"⏭ Skipped:\n"
            f"🎵 {old['title']}"
        )


    await play_next(
        update,
        context,
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

    chat_id = (
        update.effective_chat.id
    )


    queues[chat_id].clear()


    current_song.pop(
        chat_id,
        None,
    )


    paused[chat_id] = False


    await update.effective_message.reply_text(

        "⏹ *Music stopped.*\n"
        "🗑 Queue cleared.",

        parse_mode="Markdown",

        reply_markup=main_menu(),
    )


# ============================================================
# QUEUE
# ============================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = (
        update.effective_chat.id
    )


    lines = [
        "📋 *Music Queue*",
        "",
    ]


    if chat_id in current_song:

        lines.extend(
            [
                "🎵 *Now Playing:*",
                current_song[
                    chat_id
                ]["title"],
                "",
            ]
        )


    items = list(
        queues[chat_id]
    )[:15]


    if not items:

        lines.append(
            "📭 Queue is empty."
        )

    else:

        for i, song in enumerate(
            items,
            1,
        ):

            lines.append(
                f"{i}. {song['title']}"
            )


    await update.effective_message.reply_text(

        "\n".join(lines),

        parse_mode="Markdown",

        reply_markup=main_menu(),
    )


# ============================================================
# SEARCH COMMAND
# ============================================================

async def search_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = (
        update.effective_message
    )


    if not context.args:

        await message.reply_text(

            "🔎 Usage:\n"
            "`/search song name`",

            parse_mode="Markdown",
        )

        return


    if spotify is None:

        await message.reply_text(

            "❌ Spotify is not configured.\n\n"

            "Add:\n"
            "SPOTIFY_CLIENT_ID\n"
            "SPOTIFY_CLIENT_SECRET\n\n"
            "in Render Environment Variables."
        )

        return


    query = " ".join(
        context.args
    )


    results = await asyncio.to_thread(
        spotify_search,
        query,
        5,
    )


    if not results:

        await message.reply_text(
            "❌ No Spotify results found."
        )

        return


    for i, result in enumerate(
        results,
        1,
    ):

        await message.reply_text(

            f"*{i}. {result['name']}*\n\n"

            f"👤 {result['artists']}\n"
            f"💿 {result['album']}\n"
            f"⏱ {fmt_duration(result['duration'])}\n\n"

            f"🔗 {result['url']}",

            parse_mode="Markdown",
        )


# ============================================================
# ADMIN: CLEAR QUEUE
# ============================================================

async def clearqueue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(update):
        return


    chat_id = (
        update.effective_chat.id
    )


    queues[chat_id].clear()


    await update.effective_message.reply_text(
        "👑 Queue cleared by admin."
    )


# ============================================================
# ADMIN: FORCE SKIP
# ============================================================

async def force_skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(update):
        return


    chat_id = (
        update.effective_chat.id
    )


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
        update,
        context,
    )


# ============================================================
# ADMIN: STATS
# ============================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(update):
        return


    active = len(
        current_song
    )


    queued = sum(
        len(q)
        for q in queues.values()
    )


    await update.effective_message.reply_text(

        "👑 *Bot Stats*\n\n"

        f"Active chats: {active}\n"
        f"Queued songs: {queued}\n"
        f"Admins: {len(ADMIN_IDS)}",

        parse_mode="Markdown",
    )


# ============================================================
# BUTTON CALLBACK
# ============================================================

async def button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    callback = (
        update.callback_query
    )


    await callback.answer()


    message = callback.message


    if not message:
        return


    chat_id = message.chat.id

    data = callback.data


    # --------------------------------------------------------
    # PLAY HELP
    # --------------------------------------------------------

    if data == "play_help":

        await message.reply_text(

            "🎵 *Use:*\n\n"
            "`/play <song name>`\n\n"
            "Example:\n"
            "`/play Kesariya`",

            parse_mode="Markdown",
        )


    # --------------------------------------------------------
    # SEARCH HELP
    # --------------------------------------------------------

    elif data == "search_help":

        await message.reply_text(

            "🔎 *Use:*\n\n"
            "`/search <song name>`",

            parse_mode="Markdown",
        )


    # --------------------------------------------------------
    # PAUSE
    # --------------------------------------------------------

    elif data == "pause":

        if chat_id not in current_song:

            await message.reply_text(
                "❌ Nothing is playing."
            )

        else:

            paused[chat_id] = True

            await message.reply_text(

                "⏸ Pause state enabled.\n\n"

                "⚠️ Telegram Bot API cannot "
                "pause an already-sent audio message."
            )


    # --------------------------------------------------------
    # RESUME
    # --------------------------------------------------------

    elif data == "resume":

        if chat_id not in current_song:

            await message.reply_text(
                "❌ Nothing is playing."
            )

        else:

            paused[chat_id] = False

            await message.reply_text(
                "▶️ Resume state enabled."
            )


    # --------------------------------------------------------
    # SKIP
    # --------------------------------------------------------

    elif data == "skip":

        if chat_id not in current_song:

            await message.reply_text(
                "❌ Nothing is playing."
            )

        else:

            current_song.pop(
                chat_id,
                None,
            )

            paused[chat_id] = False


            await play_next(
                message,
                context,
            )


            if chat_id not in current_song:

                await message.reply_text(
                    "📭 Queue is empty."
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


        await message.reply_text(
            "⏹ Stopped and queue cleared."
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
                    "🎵 *Current:*",
                    current_song[
                        chat_id
                    ]["title"],
                    "",
                ]
            )


        items = list(
            queues[chat_id]
        )[:15]


        if not items:

            lines.append(
                "📭 Queue is empty."
            )

        else:

            for i, song in enumerate(
                items,
                1,
            ):

                lines.append(
                    f"{i}. {song['title']}"
                )


        await message.reply_text(

            "\n".join(lines),

            parse_mode="Markdown",
        )


    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    elif data == "help":

        await message.reply_text(

            "🎵 *Commands*\n\n"

            "/start\n"
            "/play <song>\n"
            "/search <song>\n"
            "/pause\n"
            "/resume\n"
            "/skip\n"
            "/stop\n"
            "/queue\n\n"

            "👑 *Admin*\n"
            "/clearqueue\n"
            "/force_skip\n"
            "/stats",

            parse_mode="Markdown",
        )


# ============================================================
# CLEANUP
# ============================================================

def cleanup_old_files(
    max_age_hours=6
):

    if not DOWNLOAD_DIR.exists():
        return


    now = time.time()


    for path in DOWNLOAD_DIR.iterdir():

        try:

            if not path.is_file():
                continue


            age = (
                now
                - path.stat().st_mtime
            )


            if age > (
                max_age_hours
                * 3600
            ):

                path.unlink(
                    missing_ok=True
                )


        except Exception:

            logger.exception(
                "Cleanup failed: %s",
                path,
            )


# ============================================================
# TELEGRAM ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Unhandled Telegram error",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # FLASK THREAD
    # --------------------------------------------------------

    flask_thread = threading.Thread(

        target=run_flask,

        daemon=True,

        name="flask-server",
    )


    flask_thread.start()


    # --------------------------------------------------------
    # CLEANUP
    # --------------------------------------------------------

    cleanup_old_files()


    # --------------------------------------------------------
    # TELEGRAM APP
    # --------------------------------------------------------

    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
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
        "======================================"
    )

    logger.info(
        "🎵 Resso Music Bot starting..."
    )

    logger.info(
        "yt-dlp version: %s",
        getattr(
            yt_dlp.version,
            "__version__",
            "unknown",
        ),
    )

    logger.info(
        "Admins configured: %s",
        sorted(ADMIN_IDS),
    )

    logger.info(
        "======================================"
    )


    # --------------------------------------------------------
    # START POLLING
    # --------------------------------------------------------

    application.run_polling(

        drop_pending_updates=True,

        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
