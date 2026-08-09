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


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))

    print("🎵 Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
