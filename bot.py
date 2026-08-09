import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello!\n\n"
        "Main tumhara Telegram bot hoon 🤖\n"
        "Mujhe koi message bhejo!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Commands:\n"
        "/start - Bot start karo\n"
        "/help - Help dekho"
    )


async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.message.text

    await update.message.reply_text(
        f"🤖 Tumne kaha:\n{message}"
    )


def main():
    token = os.getenv("BOT_TOKEN")

    if not token:
        raise ValueError("BOT_TOKEN environment variable missing!")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler
        )
    )

    print("🤖 Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
