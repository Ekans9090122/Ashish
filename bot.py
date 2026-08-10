def download_youtube_audio(query):

    temp_dir = tempfile.mkdtemp(
        prefix="resso_"
    )

    output_template = os.path.join(
        temp_dir,
        "%(id)s.%(ext)s"
    )

    # ========================================================
    # YT-DLP OPTIONS
    # ========================================================

    ydl_opts = {
        "format": "bestaudio/best",

        "outtmpl": output_template,

        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,

        "continuedl": False,
        "overwrites": True,
        "restrictfilenames": True,
        "cachedir": False,

        # yt-dlp EJS support
        "remote_components": {
            "ejs": "github"
        },
    }

    # ========================================================
    # COOKIES
    # ========================================================

    if os.path.isfile(COOKIE_FILE):

        ydl_opts["cookiefile"] = COOKIE_FILE

        logger.info(
            "Using YouTube cookie file: %s",
            COOKIE_FILE
        )

    else:

        logger.warning(
            "Cookie file not found: %s",
            COOKIE_FILE
        )

    # ========================================================
    # SEARCH + DOWNLOAD
    # ========================================================

    logger.info(
        "Searching YouTube: %s",
        query
    )

    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                f"ytsearch1:{query}",
                download=True,
            )

    except Exception:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        raise

    # ========================================================
    # CHECK RESULT
    # ========================================================

    if not info:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        raise RuntimeError(
            "YouTube returned no result."
        )

    entries = (
        info.get("entries")
        or []
    )

    video = next(
        (
            entry
            for entry in entries
            if entry
        ),
        None
    )

    if not video:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        raise RuntimeError(
            "No YouTube result found."
        )

    # ========================================================
    # VIDEO INFO
    # ========================================================

    title = (
        video.get("title")
        or query
    )

    video_id = video.get("id")

    webpage_url = video.get(
        "webpage_url"
    )

    if not webpage_url and video_id:

        webpage_url = (
            "https://www.youtube.com/watch?v="
            + video_id
        )

    downloaded_file = None

    # ========================================================
    # FIND DOWNLOADED FILE
    # ========================================================

    requested_downloads = (
        video.get("requested_downloads")
        or []
    )

    for item in requested_downloads:

        filepath = item.get(
            "filepath"
        )

        if (
            filepath
            and os.path.isfile(filepath)
        ):

            downloaded_file = filepath
            break

    # ========================================================
    # FALLBACK 1
    # ========================================================

    if (
        downloaded_file is None
        and video_id
    ):

        matches = list(
            Path(temp_dir).glob(
                f"{video_id}.*"
            )
        )

        matches = [
            p
            for p in matches
            if (
                p.is_file()
                and not p.name.endswith(".part")
            )
        ]

        if matches:

            downloaded_file = str(
                max(
                    matches,
                    key=lambda p: p.stat().st_mtime
                )
            )

    # ========================================================
    # FALLBACK 2
    # ========================================================

    if downloaded_file is None:

        files = [
            p
            for p in Path(temp_dir).iterdir()
            if (
                p.is_file()
                and not p.name.endswith(".part")
            )
        ]

        if files:

            downloaded_file = str(
                max(
                    files,
                    key=lambda p: p.stat().st_mtime
                )
            )

    # ========================================================
    # FILE NOT FOUND
    # ========================================================

    if downloaded_file is None:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        raise RuntimeError(
            "yt-dlp completed but audio "
            "file was not found."
        )

    # ========================================================
    # SIZE CHECK
    # ========================================================

    size_mb = (
        os.path.getsize(downloaded_file)
        / (1024 * 1024)
    )

    if size_mb > MAX_AUDIO_MB:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        raise RuntimeError(
            f"Audio is {size_mb:.1f} MB. "
            f"Maximum allowed is "
            f"{MAX_AUDIO_MB} MB."
        )

    return (
        downloaded_file,
        title,
        webpage_url,
        temp_dir
        )
