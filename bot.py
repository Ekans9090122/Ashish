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
# ENVIRONMENT VARIABLES
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

PORT = int(os.getenv("PORT", "10000"))

# Example:
# ADMIN_IDS=123456789,987654321
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
# FLASK SERVER FOR RENDER
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

# Separate queue for every Telegram chat.
queues = defaultdict(deque)

# Currently selected/playing song for every chat.
current_song = {}

# Pause state.
paused = defaultdict(bool)


# ============================================================
# HELPERS
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


def escape_markdown(text: str) -> str:
    """
    Escape Telegram Markdown special characters.
    """

    if not text:
        return ""

    chars = r"_*[]()~`>#+-=|{}.!\\"

    for char in chars:
        text = text.replace(char, "\\" + char)

    return text


def get_youtube_video_id(url: str):
    if not url:
        return None

    try:
        if "v=" in url:
            return (
                url.split("v=", 1)[1]
                .split("&", 1)[0]
            )

        if "youtu.be/" in url:
            return (
                url.split("youtu.be/", 1)[1]
                .split("?", 1)[0]
            )

    except Exception:
        pass

    return None


# ============================================================
# SPOTIFY SEARCH
# ============================================================

def spotify_search(query: str, limit: int = 5):

    if spotify is None:
        return []

    query = query.strip()

    if not query:
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

        results = []

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
                "images",
                [],
            )

            thumbnail = (
                images[0].get("url", "")
                if images
                else ""
            )

            track_name = (
                track.get("name")
                or "Unknown"
            )

            album_name = (
                album.get("name")
                or "Unknown"
            )

            external_urls = (
                track.get("external_urls")
                or {}
            )

            results.append(
                {
                    "name": track_name,
                    "artists": artists or "Unknown",
                    "album": album_name,
                    "url": external_urls.get(
                        "spotify",
                        "",
                    ),
                    "duration": (
                        int(
                            track.get(
                                "duration_ms",
                                0,
                            )
                            or 0
                        )
                        // 1000
                    ),
                    "thumbnail": thumbnail,
                    "query": (
                        f"{track_name} "
                        f"{artists}"
                    ).strip(),
                }
            )

        return results

    except Exception:
        logger.exception(
            "Spotify search failed: %s",
            query,
        )

        return []


# ============================================================
# YOUTUBE SEARCH + AUDIO DOWNLOAD
# ============================================================

def download_youtube_audio(query: str):

    """
    Search YouTube and download first result.

    No FFmpeg conversion is required.
    Telegram can accept common audio formats
    such as m4a.
    """

    output_template = str(
        DOWNLOAD_DIR / "%(id)s.%(ext)s"
    )

    ydl_options = {
        "format": (
            "bestaudio[ext=m4a]/"
            "bestaudio/best"
        ),

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "default_search": "ytsearch1",

        "outtmpl": output_template,

        "retries": 3,

        "fragment_retries": 3,

        "socket_timeout": 30,

        "concurrent_fragment_downloads": 1,

        "overwrites": False,

        "restrictfilenames": True,

        "extractor_args": {
            "youtube": {
                "player_client": [
                    "android",
                    "web",
                ],
            }
        },
    }

    try:

        with yt_dlp.YoutubeDL(
            ydl_options
        ) as ydl:

            info = ydl.extract_info(
                f"ytsearch1:{query}",
                download=True,
            )

            if not info:
                raise RuntimeError(
                    "YouTube returned no result."
                )

            entries = info.get("entries")

            if entries:
                video = entries[0]
            else:
                video = info

            if not video:
                raise RuntimeError(
                    "YouTube result was empty."
                )

            video_id = video.get("id")

            if not video_id:
                raise RuntimeError(
                    "YouTube video ID missing."
                )

            candidates = [
                path
                for path in DOWNLOAD_DIR.glob(
                    f"{video_id}.*"
                )
                if path.suffix.lower()
                not in {
                    ".part",
                    ".ytdl",
                }
            ]

            if not candidates:
                raise FileNotFoundError(
                    "Downloaded audio file "
                    "was not found."
                )

            # Select largest downloaded file.
            audio_file = max(
                candidates,
                key=lambda p: p.stat().st_size,
            )

            if audio_file.stat().st_size < 1024:
                raise RuntimeError(
                    "Downloaded audio file is too small."
                )

            return {
                "file": str(audio_file),

                "title": (
                    video.get("title")
                    or query
                ),

                "url": (
                    video.get("webpage_url")
                    or ""
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
                    or ""
                ),

                "video_id": video_id,
            }

    except Exception as error:

        logger.exception(
            "YouTube download failed for %r: %s",
            query,
            error,
        )

        return None


# ============================================================
# THUMBNAIL DOWNLOAD
# ============================================================

def download_thumbnail(
    url: str,
    video_id: str,
):

    if not url or not video_id:
        return None

    thumbnail_path = (
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
                    "(Telegram Music Bot)"
                )
            },
        )

        response.raise_for_status()

        thumbnail_path.write_bytes(
            response.content
        )

        if thumbnail_path.stat().st_size < 100:
            return None

        return str(thumbnail_path)

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
        "Search Spotify and get matching "
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
        "🎵 *Music Bot Commands*\n\n"

        "🎧 *Music*\n"
        "/play <song> - Play/add song\n"
        "/search <song> - Spotify search\n"
        "/queue - Show queue\n"
        "/pause - Pause state\n"
        "/resume - Resume state\n"
        "/skip - Skip current song\n"
        "/stop - Stop and clear queue\n\n"

        "👑 *Admin*\n"
        "/clearqueue - Clear queue\n"
        "/force_skip - Force skip\n"
        "/stats - Bot statistics",

        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ============================================================
# PLAY COMMAND
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
        f"🔎 Searching for:\n"
        f"🎵 *{query}*",
        parse_mode="Markdown",
    )

    # --------------------------------------------------------
    # Spotify
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

    if spotify_track:

        youtube_query = (
            spotify_track["query"]
        )

        await status.edit_text(
            f"🎵 *{escape_markdown(spotify_track['name'])}*\n"
            f"👤 {escape_markdown(spotify_track['artists'])}\n"
            f"💿 {escape_markdown(spotify_track['album'])}\n"
            f"⏱ {fmt_duration(spotify_track['duration'])}\n\n"
            f"▶️ Finding audio on YouTube...",
            parse_mode="MarkdownV2",
        )

    else:

        youtube_query = query

        await status.edit_text(
            f"🔎 Spotify result not found.\n\n"
            f"▶️ Searching YouTube for:\n"
            f"🎵 {escape_markdown(query)}",
            parse_mode="MarkdownV2",
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
            "Please check Render Logs for the "
            "exact yt-dlp error.",
        )

        return

    song = {
        "title": youtube["title"],
        "file": youtube["file"],
        "youtube_url": youtube["url"],
        "thumbnail": youtube["thumbnail"],
        "duration": youtube["duration"],
        "uploader": youtube["uploader"],
        "video_id": youtube["video_id"],
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

    queues[chat_id].append(song)

    if was_playing:

        position = len(
            queues[chat_id]
        )

        await status.edit_text(
            "✅ *Added to queue!*\n\n"
            f"🎵 {escape_markdown(song['title'])}\n"
            f"📋 Position: {position}",
            parse_mode="MarkdownV2",
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
# SEND / PLAY NEXT SONG
# ============================================================

async def play_next(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

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

    title = song.get(
        "title",
        "Unknown",
    )

    uploader = song.get(
        "uploader",
        "Unknown",
    )

    caption = (
        "🎵 *Now Playing*\n\n"
        f"*{escape_markdown(title)}*\n"
        f"👤 {escape_markdown(uploader)}\n"
        f"⏱ {fmt_duration(song.get('duration'))}\n\n"
        "Use the buttons below."
    )

    try:

        thumbnail = await asyncio.to_thread(
            download_thumbnail,
            song.get("thumbnail"),
            song.get("video_id"),
        )

        audio_kwargs = {
            "audio": song["file"],
            "caption": caption,
            "parse_mode": "MarkdownV2",
            "reply_markup": main_menu(),
            "title": title[:64],
            "performer": uploader[:64]
            if uploader
            else None,
            "duration": int(
                song.get("duration")
                or 0
            ),
        }

        if (
            thumbnail
            and Path(thumbnail).exists()
        ):
            audio_kwargs["thumbnail"] = thumbnail

        await update.effective_chat.send_audio(
            **audio_kwargs
        )

    except Exception:

        logger.exception(
            "Telegram audio upload failed."
        )

        current_song.pop(
            chat_id,
            None,
        )

        await update.effective_chat.send_message(
            "❌ Telegram audio upload failed.\n\n"
            "The downloaded file may be unsupported "
            "or too large."
        )

        # Automatically continue with next item.
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

    if paused[chat_id]:

        await update.effective_message.reply_text(
            "⏸ Already paused."
        )

        return

    paused[chat_id] = True

    await update.effective_message.reply_text(
        "⏸ Pause state enabled.\n\n"
        "⚠️ Telegram Bot API cannot physically pause "
        "an audio message that has already been sent."
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

    if not paused[chat_id]:

        await update.effective_message.reply_text(
            "▶️ Playback is already active."
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
            "⏭ Skipped:\n"
            f"🎵 {old_song['title']}"
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

    chat_id = update.effective_chat.id

    lines = [
        "📋 *Music Queue*",
        "",
    ]

    if chat_id in current_song:

        lines.extend(
            [
                "🎵 *Now Playing:*",
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
            start=1,
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
    )

    searching = await message.reply_text(
        f"🔎 Searching Spotify for:\n"
        f"🎵 {query}"
    )

    results = await asyncio.to_thread(
        spotify_search,
        query,
        5,
    )

    if not results:

        await searching.edit_text(
            "❌ No Spotify results found."
        )

        return

    try:
        await searching.delete()
    except Exception:
        pass

    for index, result in enumerate(
        results,
        start=1,
    ):

        text = (
            f"🎵 *{index}. "
            f"{escape_markdown(result['name'])}*\n\n"
            f"👤 {escape_markdown(result['artists'])}\n"
            f"💿 {escape_markdown(result['album'])}\n"
            f"⏱ {fmt_duration(result['duration'])}\n\n"
            f"🔗 {result['url']}"
        )

        await message.reply_text(
            text,
            parse_mode="MarkdownV2",
        )


# ============================================================
# ADMIN COMMANDS
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


async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(update):
        return

    active_chats = len(
        current_song
    )

    queued_songs = sum(
        len(queue)
        for queue in queues.values()
    )

    await update.effective_message.reply_text(
        "👑 *Bot Statistics*\n\n"
        f"🎵 Active chats: {active_chats}\n"
        f"📋 Queued songs: {queued_songs}\n"
        f"👑 Admins configured: {len(ADMIN_IDS)}",
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

    message = query.message

    chat_id = message.chat.id

    data = query.data

    # --------------------------------------------------------
    # PLAY HELP
    # --------------------------------------------------------

    if data == "play_help":

        await message.reply_text(
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

        await message.reply_text(
            "🔎 Use:\n"
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
                "⚠️ Telegram Bot API cannot physically "
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

            old_song = current_song.pop(
                chat_id,
                None,
            )

            paused[chat_id] = False

            if old_song:

                await message.reply_text(
                    "⏭ Skipped:\n"
                    f"🎵 {old_song['title']}"
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

        queues[chat_id].clear()

        current_song.pop(
            chat_id,
            None,
        )

        paused[chat_id] = False

        await message.reply_text(
            "⏹ Stopped.\n"
            "🗑 Queue cleared."
        )

    # --------------------------------------------------------
    # QUEUE
    # --------------------------------------------------------

    elif data == "queue":

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

        if items:

            for index, song in enumerate(
                items,
                start=1,
            ):

                lines.append(
                    f"{index}. {song['title']}"
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
            "👑 *Admin*\n"
            "/clearqueue\n"
            "/force_skip\n"
            "/stats",
            parse_mode="Markdown",
        )


# ============================================================
# CLEANUP OLD FILES
# ============================================================

def cleanup_old_files(
    max_age_hours=6,
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
                max_age_hours * 3600
            ):

                path.unlink(
                    missing_ok=True
                )

        except Exception:

            logger.exception(
                "Failed to remove file: %s",
                path,
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
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # Start Flask
    # --------------------------------------------------------

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
        name="flask-server",
    )

    flask_thread.start()

    # --------------------------------------------------------
    # Cleanup old downloads
    # --------------------------------------------------------

    cleanup_old_files()

    # --------------------------------------------------------
    # Telegram Application
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
    # Admin Commands
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
    # Inline Buttons
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            button_callback
        )
    )

    # --------------------------------------------------------
    # Error Handler
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "🎵 Resso Music Bot starting..."
    )

    logger.info(
        "Admins configured: %s",
        sorted(ADMIN_IDS),
    )

    # --------------------------------------------------------
    # Start Telegram Polling
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
