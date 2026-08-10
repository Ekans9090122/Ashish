import os
import asyncio
import logging
import threading
import time
import glob
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

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing in Render Environment Variables."
    )

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
        logger.exception("Flask server stopped.")


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

else:
    logger.warning(
        "Spotify credentials missing. "
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


def is_admin(update: Update) -> bool:
    user = update.effective_user

    return bool(
        user and user.id in ADMIN_IDS
    )


async def require_admin(
    update: Update,
) -> bool:

    if is_admin(update):
        return True

    message = update.effective_message

    if message:
        await message.reply_text(
            "⛔ This command is admin-only."
        )

    return False


def fmt_duration(seconds):

    try:
        seconds = int(seconds or 0)

    except (TypeError, ValueError):
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
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


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

            album = track.get("album") or {}

            images = album.get(
                "images"
            ) or []

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
# YOUTUBE SEARCH
# ============================================================

def youtube_search(query: str):

    search_options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": True,

        # Allow yt-dlp to use its current default
        # YouTube client selection.
        "extractor_args": {
            "youtube": {
                "player_client": [
                    "default",
                    "web_safari",
                    "tv",
                ],
            }
        },

        "retries": 3,
        "socket_timeout": 30,
    }

    try:

        with yt_dlp.YoutubeDL(
            search_options
        ) as ydl:

            info = ydl.extract_info(
                f"ytsearch1:{query}",
                download=False,
            )

        if not info:
            logger.error(
                "YouTube search returned nothing."
            )
            return None

        entries = info.get(
            "entries"
        )

        if not entries:
            return None

        video = entries[0]

        if not video:
            return None

        video_id = video.get("id")

        if not video_id:
            return None

        webpage_url = (
            video.get("webpage_url")
            or video.get("url")
        )

        if not webpage_url:

            webpage_url = (
                f"https://www.youtube.com/watch?v="
                f"{video_id}"
            )

        return {
            "id": video_id,
            "url": webpage_url,
            "title": video.get(
                "title"
            ) or query,
            "duration": (
                video.get(
                    "duration"
                )
                or 0
            ),
            "thumbnail": (
                video.get(
                    "thumbnail"
                )
                or ""
            ),
            "uploader": (
                video.get(
                    "uploader"
                )
                or video.get(
                    "channel"
                )
                or ""
            ),
        }

    except Exception as exc:

        logger.exception(
            "YouTube search failed: %s",
            exc,
        )

        return None


# ============================================================
# YOUTUBE AUDIO DOWNLOAD
# ============================================================

def download_youtube_audio(
    query: str,
):

    """
    Search YouTube first and then download the selected
    video's audio.

    No FFmpeg conversion is required because we prefer an
    already-supported audio container such as m4a/mp4.
    """

    video = youtube_search(query)

    if not video:

        logger.error(
            "Could not find YouTube video for %r",
            query,
        )

        return None

    video_id = video["id"]

    logger.info(
        "YouTube result: %s | %s",
        video_id,
        video["title"],
    )

    # --------------------------------------------------------
    # Important:
    # Do NOT force only android/web.
    #
    # YouTube currently changes client requirements regularly.
    # Let yt-dlp try modern clients.
    # --------------------------------------------------------

    outtmpl = str(
        DOWNLOAD_DIR
        / f"{video_id}.%(ext)s"
    )

    ydl_options = {

        # Prefer a Telegram-friendly audio container.
        "format": (
            "bestaudio[ext=m4a]/"
            "bestaudio[ext=mp4]/"
            "bestaudio"
        ),

        "outtmpl": outtmpl,

        "noplaylist": True,

        "quiet": False,

        "no_warnings": False,

        "retries": 5,

        "fragment_retries": 5,

        "file_access_retries": 3,

        "socket_timeout": 60,

        "concurrent_fragment_downloads": 1,

        "overwrites": False,

        "continuedl": True,

        "restrictfilenames": True,

        # Current YouTube client fallback.
        "extractor_args": {
            "youtube": {
                "player_client": [
                    "default",
                    "web_safari",
                    "tv",
                ],
            }
        },

        # Use the current yt-dlp EJS components if available.
        "remote_components": {
            "ejs": "github",
        },

        # Helpful for Render/network environments.
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 "
                "Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    try:

        with yt_dlp.YoutubeDL(
            ydl_options
        ) as ydl:

            result = ydl.extract_info(
                video["url"],
                download=True,
            )

            if not result:

                logger.error(
                    "yt-dlp returned no result."
                )

                return None

            # ------------------------------------------------
            # Find downloaded file
            # ------------------------------------------------

            candidates = []

            for file_path in glob.glob(
                str(
                    DOWNLOAD_DIR
                    / f"{video_id}.*"
                )
            ):

                path = Path(file_path)

                if not path.is_file():
                    continue

                if path.suffix.lower() in {
                    ".part",
                    ".ytdl",
                    ".temp",
                }:
                    continue

                if path.stat().st_size <= 1024:
                    continue

                candidates.append(path)

            if not candidates:

                logger.error(
                    "Downloaded file not found "
                    "for %s",
                    video_id,
                )

                return None

            # Pick largest valid file.
            audio_file = max(
                candidates,
                key=lambda p: p.stat().st_size,
            )

            logger.info(
                "Downloaded audio: %s | %.2f MB",
                audio_file,
                audio_file.stat().st_size
                / 1024
                / 1024,
            )

            return {
                "file": str(
                    audio_file
                ),
                "title": (
                    result.get("title")
                    or video.get("title")
                    or query
                ),
                "url": (
                    result.get(
                        "webpage_url"
                    )
                    or video["url"]
                ),
                "thumbnail": (
                    result.get(
                        "thumbnail"
                    )
                    or video.get(
                        "thumbnail"
                    )
                    or ""
                ),
                "duration": (
                    result.get(
                        "duration"
                    )
                    or video.get(
                        "duration"
                    )
                    or 0
                ),
                "uploader": (
                    result.get(
                        "uploader"
                    )
                    or video.get(
                        "uploader"
                    )
                    or ""
                ),
            }

    except Exception as exc:

        logger.exception(
            "YouTube audio download failed "
            "for %r: %s",
            query,
            exc,
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
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Linux; Android 13)"
                )
            },
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
        "Search Spotify and find the matching "
        "YouTube audio.\n\n"
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
        "/start - Open menu\n"
        "/help - Show help\n"
        "/play <song> - Play/add music\n"
        "/search <song> - Spotify search\n"
        "/pause - Pause state\n"
        "/resume - Resume state\n"
        "/skip - Skip current item\n"
        "/stop - Stop and clear queue\n"
        "/queue - Show queue\n\n"
        "👑 *Admin*\n"
        "/clearqueue - Clear queue\n"
        "/force_skip - Admin skip\n"
        "/stats - Bot statistics",
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

    chat_id = (
        update.effective_chat.id
    )

    if not context.args:

        await message.reply_text(
            "🎵 Usage:\n"
            "`/play Kesariya`",
            parse_mode="Markdown",
        )

        return

    query = " ".join(
        context.args
    ).strip()

    status = await message.reply_text(
        f"🔎 Searching for "
        f"*{query}*...",
        parse_mode="Markdown",
    )

    # --------------------------------------------------------
    # Spotify search
    # --------------------------------------------------------

    spotify_results = await asyncio.to_thread(
        spotify_search,
        query,
        5,
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
            f"⏱ "
            f"{fmt_duration(spotify_track['duration'])}\n\n"
            f"⬇️ Finding YouTube audio...",
            parse_mode="Markdown",
        )

    else:

        await status.edit_text(
            f"🔎 Spotify result not found.\n\n"
            f"▶️ Searching YouTube for "
            f"*{query}*...",
            parse_mode="Markdown",
        )

    # --------------------------------------------------------
    # YouTube
    # --------------------------------------------------------

    youtube = await asyncio.to_thread(
        download_youtube_audio,
        youtube_query,
    )

    if not youtube:

        await status.edit_text(
            "❌ YouTube audio download failed.\n\n"
            "Render Logs mein "
            "`YouTube audio download failed` "
            "search karo."
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
        "added_by": (
            update.effective_user.id
            if update.effective_user
            else None
        ),
    }

    # --------------------------------------------------------
    # Queue
    # --------------------------------------------------------

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
            f"✅ *Added to queue*\n\n"
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
            None,
        )

        paused[chat_id] = False

        return

    song = queues[
        chat_id
    ].popleft()

    current_song[
        chat_id
    ] = song

    paused[chat_id] = False

    caption = (
        f"🎵 *Now Playing*\n\n"
        f"*{song['title']}*\n"
        f"👤 {song.get('uploader') or 'Unknown'}\n"
        f"⏱ "
        f"{fmt_duration(song.get('duration'))}\n\n"
        f"Use the buttons below."
    )

    try:

        # ----------------------------------------------------
        # Thumbnail
        # ----------------------------------------------------

        video_id = ""

        youtube_url = (
            song.get(
                "youtube_url"
            )
            or ""
        )

        if "v=" in youtube_url:

            video_id = (
                youtube_url
                .split("v=", 1)[1]
                .split("&", 1)[0]
            )

        thumb = None

        if video_id:

            thumb = await asyncio.to_thread(
                download_thumbnail,
                song.get(
                    "thumbnail",
                    "",
                ),
                video_id,
            )

        # ----------------------------------------------------
        # Telegram audio
        # ----------------------------------------------------

        kwargs = {
            "audio": song["file"],
            "caption": caption,
            "parse_mode": "Markdown",
            "reply_markup": main_menu(),
            "title": (
                song["title"][:64]
            ),
            "performer": (
                song.get(
                    "uploader",
                    "",
                )[:64]
                or None
            ),
            "duration": int(
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

            kwargs[
                "thumbnail"
            ] = thumb

        await update.effective_chat.send_audio(
            **kwargs
        )

        # ----------------------------------------------------
        # Remove local file after Telegram has accepted it.
        # Telegram already has its own copy.
        # ----------------------------------------------------

        try:

            audio_path = Path(
                song["file"]
            )

            if audio_path.exists():
                audio_path.unlink()

        except Exception:
            logger.exception(
                "Could not remove audio file."
            )

        if thumb:

            try:

                thumb_path = Path(
                    thumb
                )

                if thumb_path.exists():
                    thumb_path.unlink()

            except Exception:
                pass

    except Exception:

        logger.exception(
            "Telegram audio upload failed."
        )

        current_song.pop(
            chat_id,
            None,
        )

        try:

            audio_path = Path(
                song["file"]
            )

            if audio_path.exists():
                audio_path.unlink()

        except Exception:
            pass

        await update.effective_chat.send_message(
            "❌ Telegram audio upload failed.\n\n"
            "Downloaded file Telegram ke liye "
            "compatible nahi tha ya file too large thi."
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
        "⚠️ Telegram Bot API already-sent "
        "audio ko actual playback mein pause "
        "nahi kar sakta.",
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
            f"⏭ Skipped: "
            f"{old['title']}"
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

    queues[
        chat_id
    ].clear()

    current_song.pop(
        chat_id,
        None,
    )

    paused[
        chat_id
    ] = False

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

        for index, song in enumerate(
            items,
            1,
        ):

            lines.append(
                f"{index}. "
                f"{song['title']}"
            )

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ============================================================
# SPOTIFY SEARCH COMMAND
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
            "❌ Spotify is not configured.\n"
            "Add SPOTIFY_CLIENT_ID and "
            "SPOTIFY_CLIENT_SECRET in Render."
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
            f"*{i}. {result['name']}*\n"
            f"👤 {result['artists']}\n"
            f"💿 {result['album']}\n"
            f"⏱ "
            f"{fmt_duration(result['duration'])}\n"
            f"🔗 {result['url']}",
            parse_mode="Markdown",
        )


# ============================================================
# ADMIN
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

    queues[
        chat_id
    ].clear()

    await update.effective_message.reply_text(
        "👑 Queue cleared by admin."
    )


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

    paused[
        chat_id
    ] = False

    await update.effective_message.reply_text(
        "👑 Admin skipped current song."
    )

    await play_next(
        update,
        context,
    )


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
        f"Admins configured: "
        f"{len(ADMIN_IDS)}",
        parse_mode="Markdown",
    )


# ============================================================
# BUTTONS
# ============================================================

async def button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = (
        update.callback_query
    )

    await query.answer()

    chat_id = (
        query.message.chat.id
    )

    data = query.data

    # --------------------------------------------------------
    # PLAY HELP
    # --------------------------------------------------------

    if data == "play_help":

        await query.message.reply_text(
            "🎵 `/play <song name>`\n\n"
            "Example:\n"
            "`/play Kesariya`",
            parse_mode="Markdown",
        )

    # --------------------------------------------------------
    # SEARCH HELP
    # --------------------------------------------------------

    elif data == "search_help":

        await query.message.reply_text(
            "🔎 `/search <song name>`",
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

            paused[
                chat_id
            ] = True

            await query.message.reply_text(
                "⏸ Pause state enabled.\n"
                "Telegram Bot API cannot pause "
                "already-sent audio."
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

            paused[
                chat_id
            ] = False

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

            current_song.pop(
                chat_id,
                None,
            )

            paused[
                chat_id
            ] = False

            await play_next(
                query.message,
                context,
            )

            if chat_id not in current_song:

                await query.message.reply_text(
                    "📭 Queue is empty."
                )

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    elif data == "stop":

        queues[
            chat_id
        ].clear()

        current_song.pop(
            chat_id,
            None,
        )

        paused[
            chat_id
        ] = False

        await query.message.reply_text(
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
                    "🎵 Current:",
                    current_song[
                        chat_id
                    ]["title"],
                    "",
                ]
            )

        items = list(
            queues[chat_id]
        )[:15]

        if items:

            lines.extend(
                f"{i}. {song['title']}"
                for i, song in enumerate(
                    items,
                    1,
                )
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
# CLEANUP
# ============================================================

def cleanup_old_files(
    max_age_hours=6,
):

    now = time.time()

    try:

        files = list(
            DOWNLOAD_DIR.iterdir()
        )

    except Exception:

        return

    for path in files:

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
                "Failed to clean %s",
                path,
            )


# ============================================================
# PERIODIC CLEANUP
# ============================================================

async def cleanup_job(
    context: ContextTypes.DEFAULT_TYPE,
):

    await asyncio.to_thread(
        cleanup_old_files
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Unhandled Telegram error: %s",
        context.error,
        exc_info=(
            type(context.error),
            context.error,
            context.error.__traceback__
            if context.error
            else None,
        ),
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # Start Flask for Render
    # --------------------------------------------------------

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
        name="flask-server",
    )

    flask_thread.start()

    # --------------------------------------------------------
    # Initial cleanup
    # --------------------------------------------------------

    cleanup_old_files()

    # --------------------------------------------------------
    # Telegram application
    # --------------------------------------------------------

    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )

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
    # Admin
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
    # Buttons
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            button_callback
        )
    )

    # --------------------------------------------------------
    # Errors
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # Periodic cleanup
    # --------------------------------------------------------

    if application.job_queue:

        application.job_queue.run_repeating(
            cleanup_job,
            interval=3600,
            first=3600,
        )

    # --------------------------------------------------------
    # Start bot
    # --------------------------------------------------------

    logger.info(
        "🎵 Resso Music Bot starting..."
    )

    logger.info(
        "yt-dlp version: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "Admins configured: %s",
        sorted(ADMIN_IDS),
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
