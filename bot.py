async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Music Bot Commands\n\n"
        "/start - Start bot\n"
        "/help - Show commands\n"
        "/play <song> - Play a song"
        "/pause - Pause music" 
        "/resume - Resume music"
        "/skip - Skip song"
        "/stop - Stop music"
        "/queue - Show queue"
