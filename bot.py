import os
import asyncio
import logging
import threading
import html
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
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("RessoBot")


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

PORT = int(os.getenv("PORT", "10000"))

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


# ============================================================
# VALIDATION
# ============================================================

if not TOKEN:
    raise RuntimeError("❌ BOT_TOKEN is missing!")

logger.info("✅ BOT_TOKEN loaded.")


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
    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
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
            )
        )

        logger.info("✅ Spotify initialized.")

    except Exception as e:
        logger.error(
            "❌ Spotify initialization failed: %s",
            e,
        )

else:
    logger.warning(
        "⚠️ Spotify credentials missing."
    )


# ============================================================
# MUSIC STATE
# ============================================================

queues = defaultdict(deque)

current_song = {}

paused = defaultdict(bool)


# ============================================================
# BUTTON MENU
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
# ADMIN CHECK
# ============================================================

async def is_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return False

    # Private chat
    if chat.type == "private":
        return True

    try:

        member = await context.bot.get_chat_member(
            chat.id,
            user.id,
        )

        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )

    except Exception as e:

        logger.error(
            "Admin check error: %s",
            e,
        )

        return False


async def require_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if await is_admin(update, context):
        return True

    if update.callback_query:

        await update.callback_query.answer(
            "❌ Only group admins can use this.",
            show_alert=True,
        )

    elif update.message:

        await update.message.reply_text(
            "👑 This command is available only to group admins."
        )

    return False


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "🎵 <b>Resso Music Bot</b>\n\n"
        "Welcome! 👋\n\n"
        "🔎 Search Spotify\n"
        "▶️ Find music on YouTube\n"
        "🖼️ Show thumbnail & details\n"
        "📋 Manage music queue\n\n"
        "<b>Example:</b>\n"
        "<code>/play Kesariya</code>"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "🎵 <b>Resso Music Bot</b>\n\n"

        "🎧 <b>Music</b>\n"
        "/play &lt;song&gt; - Play/search song\n"
        "/search &lt;song&gt; - Spotify search\n"
        "/queue - Show queue\n\n"

        "👑 <b>Admin Controls</b>\n"
        "/pause - Pause state\n"
        "/resume - Resume state\n"
        "/skip - Skip current song\n"
        "/stop - Stop & clear queue\n\n"

        "<b>Example:</b>\n"
        "<code>/play Arijit Singh Kesariya</code>"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# ============================================================
# SPOTIFY SEARCH
# ============================================================

def spotify_search(query: str):

    if spotify is None:
        return []

    try:

        results = spotify.search(
            q=query,
            type="track",
            limit=5,
        )

        tracks = (
            results
            .get("tracks", {})
            .get("items", [])
        )

        output = []

        for track in tracks:

            artists = ", ".join(
                artist["name"]
                for artist in track.get(
                    "artists",
                    [],
                )
            )

            album = track.get(
                "album",
                {},
            )

            images = album.get(
                "images",
                [],
            )

            thumbnail = (
                images[0]["url"]
                if images
                else None
            )

            duration_ms = track.get(
                "duration_ms",
                0,
            )

            duration_seconds = (
                duration_ms // 1000
            )

            minutes = (
                duration_seconds // 60
            )

            seconds = (
                duration_seconds % 60
            )

            duration = (
                f"{minutes}:{seconds:02d}"
            )

            output.append({
                "name": track.get(
                    "name",
                    "Unknown",
                ),
                "artists": artists,
                "album": album.get(
                    "name",
                    "Unknown",
                ),
                "duration": duration,
                "spotify_url": track.get(
                    "external_urls",
                    {}).get(
                        "spotify",
                        "",
                    ),
                "thumbnail": thumbnail,
                "youtube_query": (
                    f"{track.get('name', '')} "
                    f"{artists}"
                ),
            })

        return output

    except Exception as e:

        logger.error(
            "Spotify search error: %s",
            e,
        )

        return []


# ============================================================
# YOUTUBE DOWNLOAD
# ============================================================

def download_youtube_audio(query: str):

    output_template = str(
        DOWNLOAD_DIR / "%(id)s.%(ext)s"
    )

    ydl_options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        "default_search": "ytsearch1",

        "outtmpl": output_template,

        "restrictfilenames": True,
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
                return None

            entries = info.get("entries")

            if entries:
                video = entries[0]
            else:
                video = info

            if not video:
                return None

            video_id = video.get("id")

            if not video_id:
                return None

            # Find downloaded file
            matches = list(
                DOWNLOAD_DIR.glob(
                    f"{video_id}.*"
                )
            )

            if not matches:
                return None

            # Ignore temporary files
            matches = [
                file
                for file in matches
                if not file.name.endswith(
                    ".part"
                )
            ]

            if not matches:
                return None

            audio_file = matches[0]

            return {
                "file": str(audio_file),
                "title": video.get(
                    "title",
                    query,
                ),
                "youtube_url": video.get(
                    "webpage_url",
                    "",
                ),
                "duration": video.get(
                    "duration",
                    0,
                ),
                "thumbnail": video.get(
                    "thumbnail",
                    "",
                ),
            }

    except Exception as e:

        logger.error(
            "YouTube download error: %s",
            e,
        )

        return None


# ============================================================
# DOWNLOAD THUMBNAIL
# ============================================================

def download_thumbnail(
    url: str,
    video_id: str,
):

    if not url:
        return None

    try:

        filename = (
            DOWNLOAD_DIR
            / f"{video_id}_thumb.jpg"
        )

        response = requests.get(
            url,
            timeout=15,
        )

        response.raise_for_status()

        filename.write_bytes(
            response.content
        )

        return str(filename)

    except Exception as e:

        logger.warning(
            "Thumbnail download failed: %s",
            e,
        )

        return None


# ============================================================
# PLAY
# ============================================================

async def play_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    if not context.args:

        await update.message.reply_text(
            "🎵 <b>Usage:</b>\n\n"
            "<code>/play Kesariya</code>\n\n"
            "Example:\n"
            "<code>/play Arijit Singh Kesariya</code>",
            parse_mode="HTML",
        )

        return

    query = " ".join(
        context.args
    )

    searching = await update.message.reply_text(
        f"🔎 Searching:\n"
        f"🎵 <b>{html.escape(query)}</b>\n\n"
        f"⏳ Please wait...",
        parse_mode="HTML",
    )

    # --------------------------------------------------------
    # Spotify
    # --------------------------------------------------------

    spotify_results = await asyncio.to_thread(
        spotify_search,
        query,
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

        await searching.edit_text(
            f"✅ <b>Spotify match found</b>\n\n"
            f"🎵 <b>{html.escape(spotify_track['name'])}</b>\n"
            f"👤 {html.escape(spotify_track['artists'])}\n"
            f"💿 {html.escape(spotify_track['album'])}\n"
            f"⏱ {spotify_track['duration']}\n\n"
            f"⬇️ Searching YouTube...",
            parse_mode="HTML",
        )

    else:

        youtube_query = query

        await searching.edit_text(
            f"🔎 Spotify result not found.\n\n"
            f"▶️ Searching YouTube for:\n"
            f"<b>{html.escape(query)}</b>\n\n"
            f"⏳ Please wait...",
            parse_mode="HTML",
        )

    # --------------------------------------------------------
    # YouTube
    # --------------------------------------------------------

    youtube = await asyncio.to_thread(
        download_youtube_audio,
        youtube_query,
    )

    if not youtube:

        await searching.edit_text(
            "❌ YouTube audio download failed.\n\n"
            "Try another song.",
        )

        return

    # --------------------------------------------------------
    # Song data
    # --------------------------------------------------------

    song = {
        "title": youtube["title"],
        "file": youtube["file"],
        "youtube_url": youtube["youtube_url"],
        "youtube_thumbnail": youtube["thumbnail"],
        "spotify": spotify_track,
    }

    queues[chat_id].append(song)

    position = len(
        queues[chat_id]
    )

    # --------------------------------------------------------
    # Start immediately
    # --------------------------------------------------------

    if chat_id not in current_song:

        await searching.delete()

        await play_next(
            update.effective_chat,
            context,
        )

    else:

        await searching.edit_text(
            f"✅ <b>Added to queue!</b>\n\n"
            f"🎵 <b>{html.escape(youtube['title'])}</b>\n"
            f"📋 Position: <b>#{position}</b>",
            parse_mode="HTML",
        )


# ============================================================
# PLAY NEXT
# ============================================================

async def play_next(
    chat,
    context,
):

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

    try:

        spotify_track = song.get(
            "spotify"
        )

        # ----------------------------------------------------
        # Song details
        # ----------------------------------------------------

        if spotify_track:

            details = (
                f"🎵 <b>{html.escape(spotify_track['name'])}</b>\n"
                f"👤 {html.escape(spotify_track['artists'])}\n"
                f"💿 {html.escape(spotify_track['album'])}\n"
                f"⏱ {spotify_track['duration']}\n\n"
            )

        else:

            details = (
                f"🎵 <b>{html.escape(song['title'])}</b>\n\n"
            )

        details += (
            f"▶️ <b>Now Playing</b>\n"
            f"🔗 <a href=\"{html.escape(song['youtube_url'])}\">YouTube</a>"
        )

        # ----------------------------------------------------
        # Thumbnail
        # ----------------------------------------------------

        thumbnail_url = None

        if spotify_track:
            thumbnail_url = (
                spotify_track.get(
                    "thumbnail"
                )
            )

        if not thumbnail_url:
            thumbnail_url = song.get(
                "youtube_thumbnail"
            )

        # ----------------------------------------------------
        # Send thumbnail
        # ----------------------------------------------------

        if thumbnail_url:

            try:

                await chat.send_photo(
                    photo=thumbnail_url,
                    caption=details,
                    parse_mode="HTML",
                )

            except Exception as e:

                logger.warning(
                    "Thumbnail send failed: %s",
                    e,
                )

        # ----------------------------------------------------
        # Send audio
        # ----------------------------------------------------

        await chat.send_audio(
            audio=song["file"],
            title=song["title"],
            performer=(
                spotify_track["artists"]
                if spotify_track
                else None
            ),
            caption=(
                "🎵 <b>Now Playing</b>\n"
                f"{html.escape(song['title'])}"
            ),
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

        # ----------------------------------------------------
        # Delete local file after upload
        # ----------------------------------------------------

        try:

            file_path = Path(
                song["file"]
            )

            if file_path.exists():
                file_path.unlink()

        except Exception:
            pass

    except Exception as e:

        logger.error(
            "Audio send error: %s",
            e,
        )

        current_song.pop(
            chat_id,
            None,
        )

        await chat.send_message(
            "❌ Failed to send audio."
        )

        if queues[chat_id]:

            await play_next(
                chat,
                context,
            )


# ============================================================
# SEARCH
# ============================================================

async def search_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not context.args:

        await update.message.reply_text(
            "🔎 Usage:\n"
            "<code>/search song name</code>",
            parse_mode="HTML",
        )

        return

    query = " ".join(
        context.args
    )

    message = await update.message.reply_text(
        f"🔎 Searching Spotify for:\n"
        f"<b>{html.escape(query)}</b>",
        parse_mode="HTML",
    )

    results = await asyncio.to_thread(
        spotify_search,
        query,
    )

    if not results:

        await message.edit_text(
            "❌ No Spotify results found."
        )

        return

    text = "🔎 <b>Spotify Results</b>\n\n"

    for index, track in enumerate(
        results,
        start=1,
    ):

        text += (
            f"<b>{index}. "
            f"{html.escape(track['name'])}</b>\n"
            f"👤 {html.escape(track['artists'])}\n"
            f"💿 {html.escape(track['album'])}\n"
            f"⏱ {track['duration']}\n"
            f"🔗 <a href=\"{html.escape(track['spotify_url'])}\">Spotify</a>\n\n"
        )

    await message.edit_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ============================================================
# QUEUE
# ============================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    text = "📋 <b>Music Queue</b>\n\n"

    if chat_id in current_song:

        text += (
            "🎵 <b>Now Playing:</b>\n"
            f"{html.escape(current_song[chat_id]['title'])}\n\n"
        )

    songs = list(
        queues[chat_id]
    )

    if not songs:

        text += "📭 Queue is empty."

    else:

        for index, song in enumerate(
            songs[:10],
            start=1,
        ):

            text += (
                f"{index}. "
                f"{html.escape(song['title'])}\n"
            )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# ============================================================
# PAUSE
# ============================================================

async def pause_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(
        update,
        context,
    ):
        return

    chat_id = update.effective_chat.id

    if chat_id not in current_song:

        await update.message.reply_text(
            "❌ Nothing is playing."
        )

        return

    paused[chat_id] = True

    await update.message.reply_text(
        "⏸ <b>Playback paused.</b>\n\n"
        "⚠️ This bot uses Telegram Bot API audio messages, "
        "so this changes the playback state but cannot "
        "pause an already-sent audio message.",
        parse_mode="HTML",
    )


# ============================================================
# RESUME
# ============================================================

async def resume_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(
        update,
        context,
    ):
        return

    chat_id = update.effective_chat.id

    if chat_id not in current_song:

        await update.message.reply_text(
            "❌ Nothing is playing."
        )

        return

    paused[chat_id] = False

    await update.message.reply_text(
        "▶️ <b>Playback resumed.</b>",
        parse_mode="HTML",
    )


# ============================================================
# SKIP
# ============================================================

async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(
        update,
        context,
    ):
        return

    chat_id = update.effective_chat.id

    if chat_id not in current_song:

        await update.message.reply_text(
            "❌ Nothing is playing."
        )

        return

    old_song = current_song.pop(
        chat_id,
        None,
    )

    paused[chat_id] = False

    if old_song:

        await update.message.reply_text(
            f"⏭ Skipped:\n"
            f"🎵 {html.escape(old_song['title'])}",
            parse_mode="HTML",
        )

    await play_next(
        update.effective_chat,
        context,
    )


# ============================================================
# STOP
# ============================================================

async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(
        update,
        context,
    ):
        return

    chat_id = update.effective_chat.id

    # Delete queued files
    for song in queues[chat_id]:

        try:

            file_path = Path(
                song["file"]
            )

            if file_path.exists():
                file_path.unlink()

        except Exception:
            pass

    queues[chat_id].clear()

    current_song.pop(
        chat_id,
        None,
    )

    paused[chat_id] = False

    await update.message.reply_text(
        "⏹ <b>Music stopped.</b>\n"
        "🗑 Queue cleared.",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# ============================================================
# ADMIN HELP
# ============================================================

async def admin_help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_admin(
        update,
        context,
    ):
        return

    await update.message.reply_text(
        "👑 <b>Admin Controls</b>\n\n"
        "/pause - Pause state\n"
        "/resume - Resume state\n"
        "/skip - Skip current song\n"
        "/stop - Stop and clear queue\n\n"
        "Only Telegram group admins can use these commands.",
        parse_mode="HTML",
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

    chat_id = query.message.chat.id

    # --------------------------------------------------------
    # Simple help buttons
    # --------------------------------------------------------

    if query.data == "play_help":

        await query.message.reply_text(
            "🎵 Use:\n"
            "<code>/play song name</code>",
            parse_mode="HTML",
        )

        return

    if query.data == "search_help":

        await query.message.reply_text(
            "🔎 Use:\n"
            "<code>/search song name</code>",
            parse_mode="HTML",
        )

        return

    if query.data == "help":

        await query.message.reply_text(
            "🎵 <b>Commands</b>\n\n"
            "/play song\n"
            "/search song\n"
            "/queue\n"
            "/pause\n"
            "/resume\n"
            "/skip\n"
            "/stop\n"
            "/adminhelp",
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # Admin check for controls
    # --------------------------------------------------------

    fake_update = update

    if not await is_admin(
        fake_update,
        context,
    ):

        await query.answer(
            "❌ Admin only.",
            show_alert=True,
        )

        return

    # --------------------------------------------------------
    # Pause
    # --------------------------------------------------------

    if query.data == "pause":

        if chat_id not in current_song:

            await query.message.reply_text(
                "❌ Nothing is playing."
            )

            return

        paused[chat_id] = True

        await query.message.reply_text(
            "⏸ Paused."
        )

    # --------------------------------------------------------
    # Resume
    # --------------------------------------------------------

    elif query.data == "resume":

        if chat_id not in current_song:

            await query.message.reply_text(
                "❌ Nothing is playing."
            )

            return

        paused[chat_id] = False

        await query.message.reply_text(
            "▶️ Resumed."
        )

    # --------------------------------------------------------
    # Skip
    # --------------------------------------------------------

    elif query.data == "skip":

        if chat_id not in current_song:

            await query.message.reply_text(
                "❌ Nothing is playing."
            )

            return

        current_song.pop(
            chat_id,
            None,
        )

        paused[chat_id] = False

        await query.message.reply_text(
            "⏭ Skipped."
        )

        await play_next(
            query.message.chat,
            context,
        )

    # --------------------------------------------------------
    # Stop
    # --------------------------------------------------------

    elif query.data == "stop":

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
    # Queue
    # --------------------------------------------------------

    elif query.data == "queue":

        text = "📋 <b>Queue</b>\n\n"

        if chat_id in current_song:

            text += (
                "🎵 <b>Current:</b>\n"
                f"{html.escape(current_song[chat_id]['title'])}\n\n"
            )

        songs = list(
            queues[chat_id]
        )

        if songs:

            for index, song in enumerate(
                songs[:10],
                1,
            ):

                text += (
                    f"{index}. "
                    f"{html.escape(song['title'])}\n"
                )

        else:

            text += "📭 Queue empty."

        await query.message.reply_text(
            text,
            parse_mode="HTML",
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
# CLEANUP
# ============================================================

def cleanup_downloads():

    if not DOWNLOAD_DIR.exists():
        return

    for file in DOWNLOAD_DIR.iterdir():

        try:

            if file.is_file():
                file.unlink()

        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # Render Flask server
    # --------------------------------------------------------

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )

    flask_thread.start()

    logger.info(
        "🌐 Flask server started on port %s",
        PORT,
    )

    # --------------------------------------------------------
    # Cleanup old downloads
    # --------------------------------------------------------

    cleanup_downloads()

    # --------------------------------------------------------
    # Telegram application
    # --------------------------------------------------------

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "play",
            play_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "search",
            search_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "queue",
            queue_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "pause",
            pause_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "resume",
            resume_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "skip",
            skip_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "stop",
            stop_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "adminhelp",
            admin_help_command,
        )
    )

    # --------------------------------------------------------
    # Buttons
    # --------------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            button_callback,
        )
    )

    # --------------------------------------------------------
    # Error handler
    # --------------------------------------------------------

    app.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # Start
    # --------------------------------------------------------

    logger.info(
        "🎵 Resso Music Bot started successfully!"
    )

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
