import os
import asyncio
import logging
import threading
from pathlib import Path
from collections import defaultdict, deque

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

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

PORT = int(os.getenv("PORT", "10000"))


if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing!")


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

        logger.info("Spotify initialized successfully.")

    except Exception as e:
        logger.error("Spotify initialization failed: %s", e)

else:
    logger.warning(
        "Spotify credentials missing. Spotify search disabled."
    )


# ============================================================
# MUSIC STATE
# ============================================================

# Queue for every chat
queues = defaultdict(deque)

# Current song for every chat
current_song = {}

# Pause state
paused = defaultdict(bool)


# ============================================================
# BUTTON MENU
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
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "🎵 *Resso Music Bot*\n\n"
        "Welcome! 👋\n\n"
        "You can search Spotify and get the matching "
        "YouTube audio.\n\n"
        "Use the buttons below or type:\n"
        "`/play Kesariya`"
    )

    await update.message.reply_text(
        text,
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

    text = (
        "🎵 *Music Bot Commands*\n\n"
        "/start - Open menu\n"
        "/help - Show help\n"
        "/play <song> - Play/search song\n"
        "/search <song> - Search Spotify\n"
        "/pause - Pause current queue\n"
        "/resume - Resume queue\n"
        "/skip - Skip current song\n"
        "/stop - Stop and clear queue\n"
        "/queue - Show queue"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ============================================================
# SPOTIFY SEARCH
# ============================================================

def spotify_search(song: str):

    if spotify is None:
        return None

    try:

        results = spotify.search(
            q=song,
            type="track",
            limit=5,
        )

        tracks = results.get("tracks", {}).get("items", [])

        if not tracks:
            return None

        track = tracks[0]

        return {
            "name": track["name"],
            "artists": ", ".join(
                artist["name"]
                for artist in track["artists"]
            ),
            "album": track["album"]["name"],
            "url": track["external_urls"]["spotify"],
            "duration": track["duration_ms"] // 1000,
            "query": (
                f'{track["name"]} '
                f'{", ".join(a["name"] for a in track["artists"])}'
            ),
        }

    except Exception as e:
        logger.error("Spotify search error: %s", e)
        return None


# ============================================================
# YOUTUBE SEARCH + DOWNLOAD
# ============================================================

def download_youtube_audio(query: str):

    output_dir = Path("downloads")
    output_dir.mkdir(exist_ok=True)

    output_template = str(
        output_dir / "%(id)s.%(ext)s"
    )

    ydl_options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "outtmpl": output_template,

        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    try:

        with yt_dlp.YoutubeDL(ydl_options) as ydl:

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

            video_id = video.get("id")

            if not video_id:
                return None

            filename = output_dir / f"{video_id}.mp3"

            if not filename.exists():

                # Sometimes yt-dlp chooses a different extension
                matches = list(
                    output_dir.glob(f"{video_id}.*")
                )

                mp3_files = [
                    x for x in matches
                    if x.suffix.lower() == ".mp3"
                ]

                if mp3_files:
                    filename = mp3_files[0]

            if not filename.exists():
                return None

            return {
                "file": str(filename),
                "title": video.get("title", query),
                "url": video.get("webpage_url", ""),
            }

    except Exception as e:

        logger.error(
            "YouTube download error: %s",
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
            "🎵 *Usage:*\n\n"
            "`/play Kesariya`\n\n"
            "Example:\n"
            "`/play Arijit Singh Kesariya`",
            parse_mode="Markdown",
        )

        return

    song = " ".join(context.args)

    searching = await update.message.reply_text(
        f"🔎 Searching for:\n"
        f"🎵 *{song}*\n\n"
        f"⏳ Please wait...",
        parse_mode="Markdown",
    )

    # Spotify search
    spotify_track = await asyncio.to_thread(
        spotify_search,
        song,
    )

    if spotify_track:

        youtube_query = spotify_track["query"]

        info_text = (
            f"🎵 *{spotify_track['name']}*\n"
            f"👤 {spotify_track['artists']}\n"
            f"💿 {spotify_track['album']}\n\n"
            f"🔗 Spotify\n"
            f"{spotify_track['url']}"
        )

    else:

        youtube_query = song

        info_text = (
            f"🎵 *{song}*\n"
            f"🔎 Spotify result not found.\n"
            f"Using YouTube search..."
        )

    await searching.edit_text(
        info_text +
        "\n\n⬇️ Downloading audio...",
        parse_mode="Markdown",
    )

    # Download from YouTube
    youtube = await asyncio.to_thread(
        download_youtube_audio,
        youtube_query,
    )

    if not youtube:

        await searching.edit_text(
            "❌ Could not download audio.\n\n"
            "Try another song name.",
        )

        return

    song_data = {
        "title": youtube["title"],
        "file": youtube["file"],
        "spotify": spotify_track,
        "youtube_url": youtube["url"],
    }

    queues[chat_id].append(song_data)

    # If nothing is currently playing
    if chat_id not in current_song:

        await play_next(
            update,
            context,
        )

    else:

        position = len(queues[chat_id])

        await searching.edit_text(
            f"✅ Added to queue!\n\n"
            f"🎵 *{youtube['title']}*\n"
            f"📋 Queue position: {position}",
            parse_mode="Markdown",
        )


# ============================================================
# PLAY NEXT
# ============================================================

async def play_next(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    if not queues[chat_id]:

        current_song.pop(chat_id, None)

        await update.effective_chat.send_message(
            "📭 Queue is empty."
        )

        return

    song = queues[chat_id].popleft()

    current_song[chat_id] = song
    paused[chat_id] = False

    try:

        await update.effective_chat.send_audio(
            audio=song["file"],
            caption=(
                f"🎵 *Now Playing*\n\n"
                f"{song['title']}\n\n"
                f"Use the buttons below."
            ),
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )

    except Exception as e:

        logger.error(
            "Telegram audio error: %s",
            e,
        )

        current_song.pop(chat_id, None)

        await update.effective_chat.send_message(
            "❌ Failed to send audio."
        )

        # Try next song
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

        await update.message.reply_text(
            "❌ Nothing is playing."
        )

        return

    if paused[chat_id]:

        await update.message.reply_text(
            "⏸ Already paused."
        )

        return

    paused[chat_id] = True

    await update.message.reply_text(
        "⏸ Playback paused.\n\n"
        "⚠️ In Telegram Bot API mode this pauses the bot's "
        "playback state; it cannot pause an already-sent "
        "Telegram audio message.",
        reply_markup=main_menu(),
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

        await update.message.reply_text(
            "❌ Nothing is playing."
        )

        return

    if not paused[chat_id]:

        await update.message.reply_text(
            "▶️ Playback is already active."
        )

        return

    paused[chat_id] = False

    await update.message.reply_text(
        "▶️ Playback resumed.",
        reply_markup=main_menu(),
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
            f"🎵 {old_song['title']}"
        )

    if queues[chat_id]:

        await play_next(
            update,
            context,
        )

    else:

        await update.message.reply_text(
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

    await update.message.reply_text(
        "⏹ Music stopped.\n"
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

    text = "📋 *Music Queue*\n\n"

    if chat_id in current_song:

        text += (
            "🎵 *Now Playing:*\n"
            f"{current_song[chat_id]['title']}\n\n"
        )

    if not queues[chat_id]:

        text += "📭 Queue is empty."

    else:

        for index, song in enumerate(
            list(queues[chat_id])[:10],
            start=1,
        ):

            text += (
                f"{index}. "
                f"{song['title']}\n"
            )

    await update.message.reply_text(
        text,
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

    if not context.args:

        await update.message.reply_text(
            "🔎 Usage:\n"
            "`/search song name`",
            parse_mode="Markdown",
        )

        return

    if spotify is None:

        await update.message.reply_text(
            "❌ Spotify is not configured.\n"
            "Add SPOTIFY_CLIENT_ID and "
            "SPOTIFY_CLIENT_SECRET in Render."
        )

        return

    query = " ".join(context.args)

    await update.message.reply_text(
        f"🔎 Searching Spotify for `{query}`...",
        parse_mode="Markdown",
    )

    result = await asyncio.to_thread(
        spotify_search,
        query,
    )

    if not result:

        await update.message.reply_text(
            "❌ No Spotify result found."
        )

        return

    await update.message.reply_text(
        f"🎵 *{result['name']}*\n\n"
        f"👤 Artist: {result['artists']}\n"
        f"💿 Album: {result['album']}\n"
        f"⏱ Duration: {result['duration']} sec\n\n"
        f"🔗 {result['url']}",
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

    if query.data == "play_help":

        await query.message.reply_text(
            "🎵 Use:\n"
            "`/play <song name>`\n\n"
            "Example:\n"
            "`/play Tum Hi Ho`",
            parse_mode="Markdown",
        )

    elif query.data == "search_help":

        await query.message.reply_text(
            "🔎 Use:\n"
            "`/search <song name>`",
            parse_mode="Markdown",
        )

    elif query.data == "pause":

        if chat_id not in current_song:

            await query.message.reply_text(
                "❌ Nothing is playing."
            )

        else:

            paused[chat_id] = True

            await query.message.reply_text(
                "⏸ Paused.",
            )

    elif query.data == "resume":

        if chat_id not in current_song:

            await query.message.reply_text(
                "❌ Nothing is playing."
            )

        else:

            paused[chat_id] = False

            await query.message.reply_text(
                "▶️ Resumed.",
            )

    elif query.data == "skip":

        if chat_id not in current_song:

            await query.message.reply_text(
                "❌ Nothing is playing."
            )

        else:

            current_song.pop(
                chat_id,
                None,
            )

            paused[chat_id] = False

            if queues[chat_id]:

                await play_next(
                    query,
                    context,
                )

            else:

                await query.message.reply_text(
                    "📭 Queue is empty."
                )

    elif query.data == "stop":

        queues[chat_id].clear()

        current_song.pop(
            chat_id,
            None,
        )

        paused[chat_id] = False

        await query.message.reply_text(
            "⏹ Stopped and queue cleared."
        )

    elif query.data == "queue":

        text = "📋 *Queue*\n\n"

        if chat_id in current_song:

            text += (
                f"🎵 Current:\n"
                f"{current_song[chat_id]['title']}\n\n"
            )

        if queues[chat_id]:

            for i, song in enumerate(
                list(queues[chat_id])[:10],
                1,
            ):

                text += (
                    f"{i}. {song['title']}\n"
                )

        else:

            text += "📭 Queue is empty."

        await query.message.reply_text(
            text,
            parse_mode="Markdown",
        )

    elif query.data == "help":

        await query.message.reply_text(
            "🎵 *Commands*\n\n"
            "/start\n"
            "/play <song>\n"
            "/search <song>\n"
            "/pause\n"
            "/resume\n"
            "/skip\n"
            "/stop\n"
            "/queue",
            parse_mode="Markdown",
        )


# ============================================================
# CLEANUP OLD FILES
# ============================================================

def cleanup_downloads():

    directory = Path("downloads")

    if not directory.exists():
        return

    for file in directory.iterdir():

        try:

            file.unlink()

        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

def main():

    # Start Render web server
    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )

    flask_thread.start()

    # Optional cleanup
    cleanup_downloads()

    # Telegram application
    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # Commands
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
            "queue",
            queue_command,
        )
    )

    # Buttons
    app.add_handler(
        CallbackQueryHandler(
            button_callback,
        )
    )

    logger.info(
        "🎵 Resso Music Bot started!"
    )

    # Start Telegram polling
    app.run_polling(
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
