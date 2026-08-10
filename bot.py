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

# Optional:
# If you later want to use a cookies.txt file, set:
# YOUTUBE_COOKIES_FILE=/path/to/cookies.txt
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()

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

# Prevent two /play commands from modifying the same chat
# at exactly the same time.
chat_locks = defaultdict(asyncio.Lock)


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


async def require_admin(update: Update) -> bool:
    if is_admin(update):
        return True

    message = update.effective_message

    if message:
        await message.reply_text(
            "⛔ This command is admin-only."
        )

    return False


# ============================================================
# FORMAT DURATION
# ============================================================

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

        tracks = (
            result
            .get("tracks", {})
            .get("items", [])
        )

        output = []

        for track in tracks:
            artists = ", ".join(
                artist.get("name", "")
                for artist in track.get("artists", [])
            )

            album = track.get("album") or {}
            images = album.get("images") or []

            name = track.get(
                "name",
                "Unknown",
            )

            output.append(
                {
                    "name": name,
                    "artists": artists or "Unknown",
                    "album": album.get(
                        "name",
                        "Unknown",
                    ),
                    "url": (
                        track.get("external_urls")
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
                        images[0].get("url", "")
                        if images
                        else ""
                    ),
                    "query": (
                        f"{name} {artists}"
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
# YOUTUBE AUDIO DOWNLOAD
# ============================================================

def download_youtube_audio(query: str):
    """
    Search YouTube and download the best available
    audio-only format.

    IMPORTANT:
    We intentionally DO NOT force the old
    android/web clients here.

    Current yt-dlp handles YouTube client selection itself.
    """

    output_template = str(
        DOWNLOAD_DIR / "%(id)s.%(ext)s"
    )

    ydl_options = {
        # Prefer an audio-only format.
        # Fall back to best available format.
        "format": (
            "bestaudio[ext=m4a]/"
            "bestaudio/"
            "best"
        ),

        "noplaylist": True,

        "quiet": False,

        "no_warnings": False,

        "default_search": "ytsearch1",

        "outtmpl": output_template,

        # Do NOT force android/web here.
        #
        # yt-dlp's current default YouTube clients
        # are maintained by yt-dlp itself.

        "retries": 5,

        "fragment_retries": 5,

        "extractor_retries": 3,

        "socket_timeout": 30,

        "connect_timeout": 30,

        "concurrent_fragment_downloads": 1,

        "overwrites": False,

        "restrictfilenames": True,

        "continuedl": True,

        "noplaylist": True,

        "geo_bypass": False,

        "ignoreerrors": False,
    }

    # --------------------------------------------------------
    # Optional cookies file
    # --------------------------------------------------------

    if YOUTUBE_COOKIES_FILE:
        cookie_path = Path(
            YOUTUBE_COOKIES_FILE
        )

        if cookie_path.exists():
            ydl_options["cookiefile"] = str(
                cookie_path
            )

            logger.info(
                "Using YouTube cookies file: %s",
                cookie_path,
            )
        else:
            logger.warning(
                "YOUTUBE_COOKIES_FILE was set but "
                "file does not exist: %s",
                cookie_path,
            )

    # --------------------------------------------------------
    # Download
    # --------------------------------------------------------

    try:
        logger.info(
            "YouTube search started: %s",
            query,
        )

        with yt_dlp.YoutubeDL(
            ydl_options
        ) as ydl:

            info = ydl.extract_info(
                f"ytsearch1:{query}",
                download=True,
            )

            if not info:
                logger.error(
                    "YouTube returned no information."
                )

                return None

            entries = info.get("entries")

            if entries:
                video = next(
                    (
                        item
                        for item in entries
                        if item
                    ),
                    None,
                )
            else:
                video = info

            if not video:
                logger.error(
                    "YouTube search returned empty result."
                )

                return None

            video_id = video.get("id")

            if not video_id:
                logger.error(
                    "YouTube video ID missing."
                )

                return None

            # ------------------------------------------------
            # Find downloaded file
            # ------------------------------------------------

            candidates = []

            for path in DOWNLOAD_DIR.glob(
                f"{video_id}.*"
            ):
                if not path.is_file():
                    continue

                if path.suffix.lower() in {
                    ".part",
                    ".ytdl",
                }:
                    continue

                try:
                    if path.stat().st_size < 1024:
                        continue
                except OSError:
                    continue

                candidates.append(path)

            if not candidates:
                logger.error(
                    "Downloaded file not found for %s",
                    video_id,
                )

                return None

            audio_file = max(
                candidates,
                key=lambda p: p.stat().st_size,
            )

            logger.info(
                "YouTube audio downloaded: %s",
                audio_file,
            )

            return {
                "file": str(audio_file),

                "title": (
                    video.get("title")
                    or query
                ),

                "url": (
                    video.get("webpage_url")
                    or (
                        f"https://www.youtube.com/watch?v="
                        f"{video_id}"
                    )
                ),

                "thumbnail": (
                    video.get("thumbnail")
                    or ""
                ),

                "duration": (
                    video.get("duration")
                    or 0
                ),

                "uploader": (
                    video.get("uploader")
                    or video.get("channel")
                    or ""
                ),

                "id": video_id,
            }

    except Exception as exc:

        logger.exception(
            "YouTube download failed for %r: %s",
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
                    "(Android 13; Mobile)"
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
        "/play <song> - Play/add song\n"
        "/search <song> - Search Spotify\n"
        "/pause - Pause state\n"
        "/resume - Resume state\n"
        "/skip - Skip current song\n"
        "/stop - Stop and clear queue\n"
        "/queue - Show queue\n\n"

        "👑 *Admin Commands*\n"
        "/clearqueue - Clear queue\n"
        "/force_skip - Force skip\n"
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
    chat_id = update.effective_chat.id

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

    if not query:
        await message.reply_text(
            "❌ Please enter a song name."
        )

        return

    async with chat_locks[chat_id]:

        status = await message.reply_text(
            f"🔎 Searching for "
            f"*{query}*...",
            parse_mode="Markdown",
        )

        # ----------------------------------------------------
        # Spotify
        # ----------------------------------------------------

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

        if spotify_track:
            youtube_query = (
                spotify_track["query"]
            )

            await status.edit_text(
                f"🎵 *{spotify_track['name']}*\n"
                f"👤 {spotify_track['artists']}\n"
                f"💿 {spotify_track['album']}\n"
                f"⏱ {fmt_duration(spotify_track['duration'])}\n\n"
                f"⬇️ Finding YouTube audio...",
                parse_mode="Markdown",
            )

        else:
            youtube_query = query

            await status.edit_text(
                f"🔎 Spotify result not found.\n\n"
                f"▶️ Searching YouTube for "
                f"*{query}*...",
                parse_mode="Markdown",
            )

        # ----------------------------------------------------
        # YouTube
        # ----------------------------------------------------

        youtube = await asyncio.to_thread(
            download_youtube_audio,
            youtube_query,
        )

        if not youtube:

            await status.edit_text(
                "❌ *YouTube audio download failed.*\n\n"
                "This can happen when YouTube "
                "blocks the Render server/IP or "
                "requires authentication/PO-token support.\n\n"
                "📋 Open Render Logs and check the "
                "lines beginning with:\n"
                "`YouTube download failed`",
                parse_mode="Markdown",
            )

            return

        # ----------------------------------------------------
        # Song object
        # ----------------------------------------------------

        song = {
            "id": youtube.get("id"),

            "title": youtube.get(
                "title",
                query,
            ),

            "file": youtube["file"],

            "youtube_url": youtube.get(
                "url",
                "",
            ),

            "thumbnail": youtube.get(
                "thumbnail",
                "",
            ),

            "duration": youtube.get(
                "duration",
                0,
            ),

            "uploader": youtube.get(
                "uploader",
                "",
            ),

            "spotify": spotify_track,

            "added_by": (
                update.effective_user.id
                if update.effective_user
                else None
            ),
        }

        # ----------------------------------------------------
        # Add queue
        # ----------------------------------------------------

        was_playing = (
            chat_id in current_song
        )

        queues[chat_id].append(song)

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

        # ----------------------------------------------------
        # Start playback
        # ----------------------------------------------------

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
    update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Works with both:
    - Telegram Update
    - Telegram Message
    """

    if isinstance(update, Update):
        chat = update.effective_chat
    else:
        chat = update.chat

    if not chat:
        return

    chat_id = chat.id

    if not queues[chat_id]:

        current_song.pop(
            chat_id,
            None,
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

        # ----------------------------------------------------
        # Thumbnail
        # ----------------------------------------------------

        thumb = None

        video_id = song.get("id")

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
        # Send audio
        # ----------------------------------------------------

        audio_path = Path(
            song["file"]
        )

        if not audio_path.exists():

            raise FileNotFoundError(
                f"Audio file not found: "
                f"{audio_path}"
            )

        kwargs = {
            "audio": str(audio_path),

            "caption": caption,

            "parse_mode": "Markdown",

            "reply_markup": main_menu(),

            "title": (
                song["title"][:64]
            ),

            "performer": (
                song.get("uploader", "")
                [:64]
                or None
            ),

            "duration": int(
                song.get("duration")
                or 0
            ),
        }

        if (
            thumb
            and Path(thumb).exists()
        ):
            kwargs["thumbnail"] = thumb

        await chat.send_audio(
            **kwargs
        )

    except Exception as exc:

        logger.exception(
            "Failed to send audio: %s",
            exc,
        )

        current_song.pop(
            chat_id,
            None,
        )

        await chat.send_message(
            "❌ Telegram audio upload failed.\n\n"
            "The YouTube file was downloaded, "
            "but Telegram could not send it."
        )

        # Try next queue item
        if queues[chat_id]:
            await play_next(
                update,
                context,
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
        "⚠️ Telegram Bot API does not provide "
        "a way to pause an audio message after "
        "it has been sent."
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
    chat_id = update.effective_chat.id

    async with chat_locks[chat_id]:

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
    chat_id = update.effective_chat.id

    async with chat_locks[chat_id]:

        queues[chat_id].clear()

        current_song.pop(
            chat_id,
            None,
        )

        paused[chat_id] = False

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
    message = update.effective_message

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
            "Add these Render Environment Variables:\n"
            "SPOTIFY_CLIENT_ID\n"
            "SPOTIFY_CLIENT_SECRET"
        )

        return

    query = " ".join(
        context.args
    ).strip()

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
            f"⏱ {fmt_duration(result['duration'])}\n"
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

    chat_id = update.effective_chat.id

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
        f"Admins configured: "
        f"{len(ADMIN_IDS)}",
        parse_mode="Markdown",
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

    if not query.message:
        return

    message = query.message

    chat_id = message.chat.id

    data = query.data

    # --------------------------------------------------------
    # PLAY HELP
    # --------------------------------------------------------

    if data == "play_help":

        await message.reply_text(
            "🎵 `/play <song name>`\n\n"
            "Example:\n"
            "`/play Kesariya`",
            parse_mode="Markdown",
        )

    # --------------------------------------------------------
    # SEARCH HELP
    # --------------------------------------------------------

    elif data == "search_help":

        await message.reply_text(
            "🔎 `/search <song name>`\n\n"
            "Example:\n"
            "`/search Kesariya`",
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
                "Telegram Bot API cannot pause "
                "an already-sent audio message."
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

        async with chat_locks[chat_id]:

            if chat_id not in current_song:

                await message.reply_text(
                    "❌ Nothing is playing."
                )

            else:

                old = current_song.pop(
                    chat_id,
                    None,
                )

                paused[chat_id] = False

                if old:

                    await message.reply_text(
                        f"⏭ Skipped: "
                        f"{old['title']}"
                    )

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

        async with chat_locks[chat_id]:

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
                    "🎵 Current:",
                    current_song[chat_id][
                        "title"
                    ],
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

    if not DOWNLOAD_DIR.exists():
        return

    for path in DOWNLOAD_DIR.iterdir():

        try:

            if not path.is_file():
                continue

            age = (
                now
                - path.stat().st_mtime
            )

            if (
                age
                > max_age_hours * 3600
            ):
                path.unlink(
                    missing_ok=True
                )

                logger.info(
                    "Deleted old file: %s",
                    path,
                )

        except Exception:

            logger.exception(
                "Failed to clean %s",
                path,
            )


# ============================================================
# PERIODIC CLEANUP
# ============================================================

async def periodic_cleanup(
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        await asyncio.to_thread(
            cleanup_old_files,
            6,
        )

    except Exception:
        logger.exception(
            "Periodic cleanup failed."
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Unhandled Telegram error",
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
    # Start Render health server
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
    # Admin commands
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
            periodic_cleanup,
            interval=3600,
            first=3600,
        )

    # --------------------------------------------------------
    # Start bot
    # --------------------------------------------------------

    logger.info(
        "======================================"
    )

    logger.info(
        "🎵 Resso Music Bot starting..."
    )

    logger.info(
        "Admins configured: %s",
        sorted(ADMIN_IDS),
    )

    logger.info(
        "Download directory: %s",
        DOWNLOAD_DIR.resolve(),
    )

    logger.info(
        "yt-dlp version: %s",
        getattr(
            yt_dlp,
            "version",
            lambda: "unknown",
        )(),
    )

    logger.info(
        "======================================"
    )

    # IMPORTANT:
    # This keeps the Render process alive.

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
