import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message
from pymongo import MongoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("file-forward-bot")


# =========================
# ENVIRONMENT VARIABLES
# =========================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = os.getenv("DB_NAME", "file_forward_bot")

SOURCE_CHATS = {
    int(x.strip())
    for x in os.environ["SOURCE_CHATS"].split(",")
    if x.strip()
}

DESTINATION_CHAT = int(os.environ["DESTINATION_CHAT"])

ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip()
}

DEFAULT_PREFIX = os.getenv("PREFIX", "")
DEFAULT_CAPTION = os.getenv("CAPTION", "{caption}")


# =========================
# DEFAULT METADATA
# =========================

DEFAULT_METADATA = {
    "general": {
        "title": "",
        "artist": "",
        "album": "",
        "comment": "",
        "encoder": "",
        "description": "",
    },
    "video": {
        "title": "",
        "language": "",
    },
    "audio": {
        "title": "",
        "artist": "",
        "album": "",
        "language": "",
    },
    "subtitle": {
        "title": "",
        "language": "",
    },
}


# =========================
# TELEGRAM CLIENT
# =========================

app = Client(
    "file_forward_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


# =========================
# MONGODB
# =========================

mongo = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=10000,
)

db = mongo[DB_NAME]

files_col = db["files"]
settings_col = db["settings"]

files_col.create_index(
    "unique_id",
    unique=True,
    sparse=True,
)

files_col.create_index(
    "sha256",
    unique=True,
    sparse=True,
)


# =========================
# ADMIN
# =========================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# =========================
# SETTINGS
# =========================

def get_settings():

    data = settings_col.find_one(
        {"_id": "settings"}
    ) or {}

    return {
        "prefix": data.get(
            "prefix",
            DEFAULT_PREFIX
        ),

        "caption": data.get(
            "caption",
            DEFAULT_CAPTION
        ),

        "metadata": data.get(
            "metadata",
            DEFAULT_METADATA
        ),

        "metadata_enabled": data.get(
            "metadata_enabled",
            False
        ),
    }


def save_settings(data):

    settings_col.update_one(
        {"_id": "settings"},
        {"$set": data},
        upsert=True,
    )


# =========================
# MEDIA DETECTION
# =========================

def extract_media(message: Message):

    if message.document:
        return message.document, "document"

    if message.video:
        return message.video, "video"

    if message.audio:
        return message.audio, "audio"

    if message.animation:
        return message.animation, "animation"

    if message.photo:
        return message.photo, "photo"

    if message.voice:
        return message.voice, "voice"

    return None, None


# =========================
# FILE NAME
# =========================

def original_name(message, media, kind):

    if kind == "document":
        return media.file_name or "file"

    if kind == "video":
        return media.file_name or "video.mp4"

    if kind == "audio":
        return media.file_name or "audio.mp3"

    if kind == "animation":
        return media.file_name or "animation.mp4"

    if kind == "photo":
        return "photo.jpg"

    if kind == "voice":
        return "voice.ogg"

    return "file"


def apply_prefix(filename, prefix):

    if not prefix:
        return filename

    return f"{prefix}{filename}"


# =========================
# CAPTION
# =========================

def render_caption(
    template,
    message,
    filename,
):

    original_caption = message.caption or ""

    values = {
        "filename": filename,
        "caption": original_caption,
        "chat_id": str(message.chat.id),
        "message_id": str(message.id),
    }

    try:
        result = template.format(**values)

    except Exception:
        result = template

    return result[:1024]


# =========================
# FFMPEG
# =========================

def ffmpeg_available():

    return (
        shutil.which("ffmpeg") is not None
        and shutil.which("ffprobe") is not None
    )


def media_can_have_metadata(path):

    extensions = {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".m4v",
        ".avi",
        ".flv",
        ".ts",
        ".mts",
        ".m2ts",
        ".mp3",
        ".m4a",
        ".aac",
        ".flac",
        ".ogg",
        ".opus",
        ".wav",
    }

    return path.suffix.lower() in extensions


def build_ffmpeg_metadata_args(metadata):

    args = []

    general = metadata.get(
        "general",
        {}
    )

    general_fields = {
        "title": general.get("title"),
        "artist": general.get("artist"),
        "album": general.get("album"),
        "comment": general.get("comment"),
        "encoder": general.get("encoder"),
        "description": general.get("description"),
    }

    for key, value in general_fields.items():

        if value:
            args += [
                "-metadata",
                f"{key}={value}"
            ]


    video = metadata.get(
        "video",
        {}
    )

    if video.get("title"):
        args += [
            "-metadata:s:v:0",
            f"title={video['title']}"
        ]

    if video.get("language"):
        args += [
            "-metadata:s:v:0",
            f"language={video['language']}"
        ]


    audio = metadata.get(
        "audio",
        {}
    )

    if audio.get("title"):
        args += [
            "-metadata:s:a:0",
            f"title={audio['title']}"
        ]

    if audio.get("artist"):
        args += [
            "-metadata:s:a:0",
            f"artist={audio['artist']}"
        ]

    if audio.get("album"):
        args += [
            "-metadata:s:a:0",
            f"album={audio['album']}"
        ]

    if audio.get("language"):
        args += [
            "-metadata:s:a:0",
            f"language={audio['language']}"
        ]


    subtitle = metadata.get(
        "subtitle",
        {}
    )

    if subtitle.get("title"):
        args += [
            "-metadata:s:s:0",
            f"title={subtitle['title']}"
        ]

    if subtitle.get("language"):
        args += [
            "-metadata:s:s:0",
            f"language={subtitle['language']}"
        ]

    return args


def process_metadata(
    source,
    destination,
    metadata,
):

    args = build_ffmpeg_metadata_args(
        metadata
    )

    if not args:
        return False

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-map_metadata",
        "0",
        "-c",
        "copy",
        *args,
        str(destination),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:

        log.error(
            "FFmpeg error: %s",
            result.stderr[-2000:],
        )

        return False

    return True


# =========================
# SHA256
# =========================

async def sha256_file(path):

    def calculate():

        sha = hashlib.sha256()

        with path.open("rb") as file:

            while True:

                chunk = file.read(
                    4 * 1024 * 1024
                )

                if not chunk:
                    break

                sha.update(chunk)

        return sha.hexdigest()

    return await asyncio.to_thread(
        calculate
    )


# =========================
# DUPLICATE CHECK
# =========================

def already_seen(unique_id):

    return bool(
        files_col.find_one(
            {"unique_id": unique_id},
            {"_id": 1},
        )
    )


def already_hashed(sha256):

    return bool(
        files_col.find_one(
            {"sha256": sha256},
            {"_id": 1},
        )
    )


def mark_seen(
    unique_id,
    sha256,
    source_chat,
    source_message,
):

    document = {
        "unique_id": unique_id,
        "sha256": sha256,
        "source_chat": source_chat,
        "source_message": source_message,
    }

    try:

        files_col.update_one(
            {"unique_id": unique_id},
            {"$setOnInsert": document},
            upsert=True,
        )

    except Exception as error:

        log.warning(
            "Database error: %s",
            error,
        )


# =========================
# SEND FILE
# =========================

async def send_processed(
    client,
    path,
    filename,
    caption,
    kind,
):

    if kind in {
        "video",
        "animation",
    }:

        await client.send_video(
            DESTINATION_CHAT,
            video=str(path),
            caption=caption,
            supports_streaming=True,
        )

    elif kind == "audio":

        await client.send_audio(
            DESTINATION_CHAT,
            audio=str(path),
            caption=caption,
            title=filename,
        )

    elif kind == "photo":

        await client.send_photo(
            DESTINATION_CHAT,
            photo=str(path),
            caption=caption,
        )

    elif kind == "voice":

        await client.send_voice(
            DESTINATION_CHAT,
            voice=str(path),
            caption=caption,
        )

    else:

        await client.send_document(
            DESTINATION_CHAT,
            document=str(path),
            file_name=filename,
            caption=caption,
        )


# =========================
# /START
# =========================

@app.on_message(
    filters.command("start")
    & filters.private
)
async def start_handler(
    client,
    message,
):

    await message.reply_text(
        "🤖 File Forward Bot is running.\n\n"
        "Automatic forwarding is enabled."
    )


# =========================
# /SETTINGS
# =========================

@app.on_message(
    filters.command("settings")
    & filters.private
)
async def settings_handler(
    client,
    message,
):

    if not is_admin(
        message.from_user.id
    ):
        return

    settings = get_settings()

    metadata_status = (
        "SET"
        if settings["metadata_enabled"]
        else "NOT SET"
    )

    await message.reply_text(
        "〄 Metadata Setting:\n"
        "╭\n"
        f"┊ METADATA: {metadata_status}\n"
        "╰ Description: Metadata Is Information "
        "Added To Streams "
        "(General, Video, Audio, Subtitle).\n\n"
        f"〄 PREFIX: {settings['prefix'] or 'NOT SET'}\n"
        f"〄 CAPTION: {settings['caption'] or 'NOT SET'}"
    )


# =========================
# /SETPREFIX
# =========================

@app.on_message(
    filters.command("setprefix")
    & filters.private
)
async def setprefix_handler(
    client,
    message,
):

    if not is_admin(
        message.from_user.id
    ):
        return

    parts = message.text.split(
        maxsplit=1
    )

    value = (
        parts[1]
        if len(parts) > 1
        else ""
    )

    save_settings(
        {
            "prefix": value
        }
    )

    await message.reply_text(
        f"✅ Prefix set to:\n{value or 'NOT SET'}"
    )


# =========================
# /SETCAPTION
# =========================

@app.on_message(
    filters.command("setcaption")
    & filters.private
)
async def setcaption_handler(
    client,
    message,
):

    if not is_admin(
        message.from_user.id
    ):
        return

    parts = message.text.split(
        maxsplit=1
    )

    value = (
        parts[1]
        if len(parts) > 1
        else ""
    )

    save_settings(
        {
            "caption": value
        }
    )

    await message.reply_text(
        "✅ Caption saved.\n\n"
        "Available variables:\n"
        "{filename}\n"
        "{caption}\n"
        "{chat_id}\n"
        "{message_id}"
    )


# =========================
# /SETMETADATA
# =========================

@app.on_message(
    filters.command("setmetadata")
    & filters.private
)
async def setmetadata_handler(
    client,
    message,
):

    if not is_admin(
        message.from_user.id
    ):
        return

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        await message.reply_text(
            "❌ Send metadata JSON.\n\n"
            "Example:\n"
            "/setmetadata "
            '{"general":{"title":"GoodMovies"}}'
        )

        return

    try:

        incoming = json.loads(
            parts[1]
        )

        metadata = {
            "general": {
                **DEFAULT_METADATA["general"],
                **incoming.get(
                    "general",
                    {}
                ),
            },

            "video": {
                **DEFAULT_METADATA["video"],
                **incoming.get(
                    "video",
                    {}
                ),
            },

            "audio": {
                **DEFAULT_METADATA["audio"],
                **incoming.get(
                    "audio",
                    {}
                ),
            },

            "subtitle": {
                **DEFAULT_METADATA["subtitle"],
                **incoming.get(
                    "subtitle",
                    {}
                ),
            },
        }

        save_settings(
            {
                "metadata": metadata,
                "metadata_enabled": True,
            }
        )

        await message.reply_text(
            "✅ Metadata enabled and saved."
        )

    except json.JSONDecodeError:

        await message.reply_text(
            "❌ Invalid JSON."
        )


# =========================
# /CLEARMETADATA
# =========================

@app.on_message(
    filters.command("clearmetadata")
    & filters.private
)
async def clearmetadata_handler(
    client,
    message,
):

    if not is_admin(
        message.from_user.id
    ):
        return

    save_settings(
        {
            "metadata": DEFAULT_METADATA,
            "metadata_enabled": False,
        }
    )

    await message.reply_text(
        "✅ Metadata disabled."
    )


# =========================
# /RESETDUPLICATES
# =========================

@app.on_message(
    filters.command("resetduplicates")
    & filters.private
)
async def resetduplicates_handler(
    client,
    message,
):

    if not is_admin(
        message.from_user.id
    ):
        return

    files_col.delete_many({})

    await message.reply_text(
        "⚠️ Duplicate database cleared."
    )


# =========================
# AUTOMATIC FORWARDING
# =========================

@app.on_message(
    filters.channel
    | filters.group
)
async def auto_forward(
    client,
    message,
):

    if message.chat.id not in SOURCE_CHATS:
        return

    media, kind = extract_media(
        message
    )

    if not media:
        return


    # Telegram duplicate check
    unique_id = getattr(
        media,
        "file_unique_id",
        None,
    )

    if unique_id:

        if await asyncio.to_thread(
            already_seen,
            unique_id,
        ):

            log.info(
                "Duplicate skipped: %s",
                unique_id,
            )

            return


    settings = await asyncio.to_thread(
        get_settings
    )


    source_filename = original_name(
        message,
        media,
        kind,
    )

    filename = apply_prefix(
        source_filename,
        settings["prefix"],
    )

    caption = render_caption(
        settings["caption"],
        message,
        filename,
    )


    # Temporary processing directory
    with tempfile.TemporaryDirectory(
        prefix="ffbot_"
    ) as temp:

        temp_path = Path(temp)

        source_path = (
            temp_path
            / source_filename
        )

        output_path = (
            temp_path
            / filename
        )


        try:

            # Download
            downloaded = await client.download_media(
                message,
                file_name=str(
                    source_path
                ),
            )

            if not downloaded:

                log.error(
                    "Download failed."
                )

                return


            source_path = Path(
                downloaded
            )


            # SHA-256 duplicate check
            sha256 = await sha256_file(
                source_path
            )

            if await asyncio.to_thread(
                already_hashed,
                sha256,
            ):

                if unique_id:

                    await asyncio.to_thread(
                        mark_seen,
                        unique_id,
                        sha256,
                        message.chat.id,
                        message.id,
                    )

                log.info(
                    "SHA256 duplicate skipped."
                )

                return


            final_path = source_path


            # Metadata
            if (
                settings["metadata_enabled"]
                and ffmpeg_available()
                and media_can_have_metadata(
                    source_path
                )
            ):

                success = await asyncio.to_thread(
                    process_metadata,
                    source_path,
                    output_path,
                    settings["metadata"],
                )

                if success:

                    final_path = output_path

                    log.info(
                        "Metadata applied."
                    )

                else:

                    log.warning(
                        "Metadata failed. "
                        "Sending original."
                    )


            # Send
            await send_processed(
                client,
                final_path,
                filename,
                caption,
                kind,
            )


            # Save duplicate record
            await asyncio.to_thread(
                mark_seen,
                unique_id or f"sha256:{sha256}",
                sha256,
                message.chat.id,
                message.id,
            )


            log.info(
                "Successfully forwarded: %s",
                filename,
            )


        except Exception:

            log.exception(
                "File processing failed."
            )


# =========================
# KOYEB HEALTH SERVER
# =========================

async def health_handler(
    request,
):

    return web.Response(
        text="File Forward Bot is running."
    )


async def start_health_server():

    port = int(
        os.getenv(
            "PORT",
            "8000",
        )
    )

    web_app = web.Application()

    web_app.router.add_get(
        "/",
        health_handler,
    )

    web_app.router.add_get(
        "/health",
        health_handler,
    )

    runner = web.AppRunner(
        web_app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    log.info(
        "Health server running on port %s",
        port,
    )

    return runner


# =========================
# START
# =========================

async def main():

    await app.start()

    await start_health_server()

    log.info(
        "================================="
    )

    log.info(
        "FILE FORWARD BOT STARTED"
    )

    log.info(
        "SOURCE: %s",
        SOURCE_CHATS,
    )

    log.info(
        "DESTINATION: %s",
        DESTINATION_CHAT,
    )

    log.info(
        "================================="
    )

    await asyncio.Event().wait()


if __name__ == "__main__":

    asyncio.run(
        main()
    )
