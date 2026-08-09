from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import os
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

TOKEN = os.getenv("BOT_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")


spotify = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET
    )
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Welcome to Resso Music Bot!\n\n"
        "Type /help to see commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Music Bot Commands\n\n"
        "/start - Start bot\n"
        "/help - Show commands\n"
        "/play <song> - Search Spotify\n"
        "/pause - Pause music\n"
        "/resume - Resume music\n"
        "/skip - Skip song\n"
        "/stop - Stop music\n"
        "/queue - Show queue"
    )


async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🎵 Usage:\n/play <song name>\n\n"
            "Example:\n/play Kesariya"
        )
        return

    song = " ".join(context.args)

    await update.message.reply_text(
        f"🔎 Searching Spotify for:\n🎵 {song}..."
    )

    try:
        results = spotify.search(q=song, type="track", limit=1)
        tracks = results["tracks"]["items"]

        if not tracks:
            await update.message.reply_text(
                "❌ Song nahi mila Spotify par."
            )
            return

        track = tracks[0]
        name = track["name"]
        artists = ", ".join(
            artist["name"] for artist in track["artists"]
        )
        url = track["external_urls"]["spotify"]

        await update.message.reply_text(
            f"🎵 {name}\n"
            f"👤 {artists}\n\n"
            f"🔗 Spotify:\n{url}"
        )

    except Exception as e:
        print("Spotify Error:", e)
        await update.message.reply_text(
            "❌ Spotify search mein error aa gaya."
        )


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("play", play_command))

    print("🎵 Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
