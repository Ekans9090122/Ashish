from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import os

TOKEN = os.getenv("BOT_TOKEN")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Welcome to Spotify Music Bot!\n\n"
        "Type /help to see commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Music Bot Commands\n\n"
        "/start - Start bot\n"
        "/help - Show commands\n"
        "/play <song> - Play a song\n"
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
        f"🔎 Searching Spotify for:\n🎵 {song}\n\n"
        "⏳ Please wait..."
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
