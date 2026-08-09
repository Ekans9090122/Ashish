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
