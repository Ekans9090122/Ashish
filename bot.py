def download_youtube(url, workdir):
    import os
    import glob
    import yt_dlp

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # Clean old download files
    for f in workdir.iterdir():
        try:
            if f.is_file():
                f.unlink()
        except Exception:
            pass

    output = str(workdir / "resso_audio.%(ext)s")

    cookie_file = os.getenv("YOUTUBE_COOKIES_FILE")

    base_opts = {
        "outtmpl": output,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,

        # Audio only
        "format": "bestaudio/best",

        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 5,

        "socket_timeout": 30,

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        },
    }

    if cookie_file and os.path.isfile(cookie_file):
        base_opts["cookiefile"] = cookie_file
        logger.info("YouTube cookies loaded: %s", cookie_file)
    else:
        logger.warning("YouTube cookie file not found")

    # Try clients one by one.
    # This is important because YouTube changes available formats.
    strategies = [
        ["default", "web_embedded"],
        ["web_embedded"],
        ["default"],
    ]

    last_error = None

    for clients in strategies:

        try:
            logger.info(
                "Trying YouTube clients: %s",
                clients
            )

            opts = dict(base_opts)

            opts["extractor_args"] = {
                "youtube": {
                    "player_client": clients
                }
            }

            with yt_dlp.YoutubeDL(opts) as ydl:

                info = ydl.extract_info(
                    url,
                    download=True
                )

                logger.info(
                    "yt-dlp finished: title=%s id=%s ext=%s",
                    info.get("title"),
                    info.get("id"),
                    info.get("ext"),
                )

            # -----------------------------------------
            # DO NOT TRUST THE TITLE/EXTENSION
            # Search the actual directory.
            # -----------------------------------------

            files = []

            for f in workdir.rglob("*"):

                if not f.is_file():
                    continue

                name = f.name.lower()

                if name.endswith(".part"):
                    continue

                if name.endswith(".ytdl"):
                    continue

                if name.endswith(".json"):
                    continue

                if name.endswith(".jpg"):
                    continue

                if name.endswith(".jpeg"):
                    continue

                if name.endswith(".png"):
                    continue

                if name.endswith(".webp"):
                    continue

                if f.stat().st_size < 1024:
                    continue

                files.append(f)

            if files:

                # Largest file is normally the actual media file
                files.sort(
                    key=lambda x: x.stat().st_size,
                    reverse=True
                )

                result = files[0]

                logger.info(
                    "FOUND AUDIO FILE: %s (%d bytes)",
                    result,
                    result.stat().st_size
                )

                return result

            # Nothing found
            directory_files = []

            for f in workdir.rglob("*"):
                directory_files.append(
                    f"{f.name} ({f.stat().st_size} bytes)"
                    if f.is_file()
                    else f"{f.name}/"
                )

            raise RuntimeError(
                "yt-dlp completed but no media file was created. "
                f"Directory contents: {directory_files}"
            )

        except Exception as e:

            last_error = str(e)

            logger.exception(
                "YouTube strategy failed (%s): %s",
                clients,
                e
            )

            # Clean partial files before next strategy
            for f in workdir.rglob("*"):
                try:
                    if f.is_file():
                        f.unlink()
                except Exception:
                    pass

    raise RuntimeError(
        "YouTube download failed after all strategies. "
        f"Last error: {last_error}"
    )
