def download_youtube_audio(query: str):
    output_dir = Path("downloads")
    output_dir.mkdir(parents=True, exist_ok=True)

    ydl_options = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        "default_search": "ytsearch1",

        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),

        # YouTube extraction
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },

        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_options) as ydl:

            info = ydl.extract_info(
                f"ytsearch1:{query}",
                download=True,
            )

            if not info:
                logger.error("No YouTube result")
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
            files = list(
                output_dir.glob(f"{video_id}.*")
            )

            files = [
                f for f in files
                if f.suffix.lower() not in {
                    ".part",
                    ".ytdl",
                }
            ]

            if not files:
                logger.error(
                    "Downloaded file not found for %s",
                    video_id,
                )
                return None

            audio_file = files[0]

            logger.info(
                "YouTube audio downloaded: %s",
                audio_file,
            )

            return {
                "file": str(audio_file),
                "title": video.get("title") or query,
                "url": video.get("webpage_url") or "",
                "thumbnail": video.get("thumbnail") or "",
                "duration": video.get("duration") or 0,
                "uploader": video.get("uploader") or "",
            }

    except Exception as e:
        logger.exception(
            "YouTube download error: %s",
            e,
        )
        return None
