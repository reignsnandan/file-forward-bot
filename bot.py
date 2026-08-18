import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.errors import RPCError
from pymongo import MongoClient


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("file-forward-bot")


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

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

DESTINATION_CHAT = int(
    os.environ["DESTINATION_CHAT"]
)

ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get(
        "ADMIN_IDS",
        "",
    ).split(",")
    if x.strip()
}

PREFIX = os.getenv(
    "PREFIX",
    "",
)

CAPTION = os.getenv(
    "CAPTION",
    "{caption}",
)


# ============================================================
# DEFAULT METADATA
# ============================================================

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


# ============================================================
# TELEGRAM CLIENT
# ============================================================

app = Client(
    "file_forward_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


# ============================================================
# MONGODB
# ============================================================

mongo = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=10000,
)

db = mongo[DB_NAME]

files_col = db["files"]
settings_col = db["settings"]


try:
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

except Exception as error:
    log.warning(
        "MongoDB index warning: %s",
        error,
    )


# ============================================================
# SETTINGS
# ============================================================

def get_settings():

    data = settings_col.find_one(
        {"_id": "settings"}
    ) or {}

    return {
        "prefix": data.get(
            "prefix",
            PREFIX,
        ),

        "caption": data.get(
            "caption",
            CAPTION,
        ),

        "metadata": data.get(
            "metadata",
            DEFAULT_METADATA,
        ),

        "metadata_enabled": data.get(
            "metadata_enabled",
            False,
        ),
    }


def save_settings(data):

    settings_col.update_one(
        {"_id": "settings"},
        {"$set": data},
        upsert=True,
    )


def is_admin(user_id):

    return user_id in ADMIN_IDS


# ============================================================
# MEDIA DETECTION
# ============================================================

def extract_media(message):

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


def get_filename(
    message,
    media,
    media_type,
):

    if media_type == "document":
        return media.file_name or "file"

    if media_type == "video":
        return media.file_name or "video.mp4"

    if media_type == "audio":
        return media.file_name or "audio.mp3"

    if media_type == "animation":
        return media.file_name or "animation.mp4"

    if media_type == "photo":
        return "photo.jpg"

    if media_type == "voice":
        return "voice.ogg"

    return "file"


# ============================================================
# PREFIX
# ============================================================

def add_prefix(
    filename,
    prefix,
):

    if not prefix:
        return filename

    return f"{prefix}{filename}"


# ============================================================
# CAPTION
# ============================================================

def make_caption(
    template,
    message,
    filename,
):

    original_caption = (
        message.caption or ""
    )

    values = {
        "filename": filename,
        "caption": original_caption,
        "chat_id": str(
            message.chat.id
        ),
        "message_id": str(
            message.id
        ),
    }

    try:

        result = template.format(
            **values
        )

    except Exception as error:

        log.warning(
            "Caption formatting error: %s",
            error,
        )

        result = original_caption

    return result[:1024]


# ============================================================
# FFMPEG
# ============================================================

def ffmpeg_available():

    try:

        imageio_ffmpeg.get_ffmpeg_exe()

        return True

    except Exception:

        return False


def ffmpeg_path():

    return imageio_ffmpeg.get_ffmpeg_exe()


def can_process_metadata(path):

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

    return (
        path.suffix.lower()
        in extensions
    )


def metadata_arguments(metadata):

    args = []

    general = metadata.get(
        "general",
        {},
    )

    for key in (
        "title",
        "artist",
        "album",
        "comment",
        "encoder",
        "description",
    ):

        value = general.get(key)

        if value:

            args.extend(
                [
                    "-metadata",
                    f"{key}={value}",
                ]
            )


    video = metadata.get(
        "video",
        {},
    )

    if video.get("title"):

        args.extend(
            [
                "-metadata:s:v:0",
                f"title={video['title']}",
            ]
        )

    if video.get("language"):

        args.extend(
            [
                "-metadata:s:v:0",
                f"language={video['language']}",
            ]
        )


    audio = metadata.get(
        "audio",
        {},
    )

    for key in (
        "title",
        "artist",
        "album",
        "language",
    ):

        value = audio.get(key)

        if value:

            args.extend(
                [
                    "-metadata:s:a:0",
                    f"{key}={value}",
                ]
            )


    subtitle = metadata.get(
        "subtitle",
        {},
    )

    if subtitle.get("title"):

        args.extend(
            [
                "-metadata:s:s:0",
                f"title={subtitle['title']}",
            ]
        )

    if subtitle.get("language"):

        args.extend(
            [
                "-metadata:s:s:0",
                f"language={subtitle['language']}",
            ]
        )

    return args


def apply_metadata(
    source,
    destination,
    metadata,
):

    args = metadata_arguments(
        metadata
    )

    if not args:
        return False

    command = [
        ffmpeg_path(),
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


# ============================================================
# HASH
# ============================================================

async def calculate_sha256(path):

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


# ============================================================
# DUPLICATE CHECK
# ============================================================

def duplicate_unique_id(
    unique_id,
):

    if not unique_id:
        return False

    return bool(
        files_col.find_one(
            {
                "unique_id": unique_id
            }
        )
    )


def duplicate_hash(
    sha256,
):

    return bool(
        files_col.find_one(
            {
                "sha256": sha256
            }
        )
    )


def save_file_record(
    unique_id,
    sha256,
    source_chat,
    source_message,
):

    try:

        files_col.update_one(
            {
                "unique_id": unique_id
            },
            {
                "$setOnInsert": {
                    "unique_id": unique_id,
                    "sha256": sha256,
                    "source_chat": source_chat,
                    "source_message": source_message,
                }
            },
            upsert=True,
        )

    except Exception as error:

        log.warning(
            "Could not save duplicate record: %s",
            error,
        )


# ============================================================
# SEND FILE
# ============================================================

async def send_file(
    client,
    path,
    filename,
    caption,
    media_type,
):

    log.info(
        "Sending file to destination: %s",
        DESTINATION_CHAT,
    )


    if media_type in (
        "video",
        "animation",
    ):

        await client.send_video(
            DESTINATION_CHAT,
            video=str(path),
            caption=caption,
            supports_streaming=True,
        )


    elif media_type == "audio":

        await client.send_audio(
            DESTINATION_CHAT,
            audio=str(path),
            caption=caption,
            title=filename,
        )


    elif media_type == "photo":

        await client.send_photo(
            DESTINATION_CHAT,
            photo=str(path),
            caption=caption,
        )


    elif media_type == "voice":

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


# ============================================================
# /START
# ============================================================

@app.on_message(
    filters.command("start")
    & filters.private
)
async def start_command(
    client,
    message,
):

    log.info(
        "START RECEIVED | user=%s",
        message.from_user.id,
    )

    await message.reply_text(
        "🤖 File Forward Bot is ONLINE!\n\n"
        "Send /check to test the bot."
    )


# ============================================================
# /CHECK
# ============================================================

@app.on_message(
    filters.command("check")
    & filters.private
)
async def check_command(
    client,
    message,
):

    log.info(
        "CHECK RECEIVED | user=%s",
        message.from_user.id,
    )

    result = []

    result.append(
        "🔍 BOT CHECK"
    )

    result.append("")

    # ----------------------------------------
    # Telegram identity
    # ----------------------------------------

    try:

        me = await client.get_me()

        result.append(
            "🤖 BOT:"
        )

        result.append(
            f"✅ @{me.username or 'no_username'}"
        )

        result.append(
            f"ID: {me.id}"
        )

    except Exception as error:

        result.append(
            f"❌ Bot identity error: {error}"
        )


    # ----------------------------------------
    # SOURCE
    # ----------------------------------------

    result.append("")
    result.append("📥 SOURCE:")


    for chat_id in SOURCE_CHATS:

        try:

            chat = await client.get_chat(
                chat_id
            )

            title = (
                chat.title
                or chat.first_name
                or "Unknown"
            )

            result.append(
                f"✅ {chat_id}"
            )

            result.append(
                f"   {title}"
            )

            log.info(
                "SOURCE ACCESS OK | %s | %s",
                chat_id,
                title,
            )

        except Exception as error:

            result.append(
                f"❌ {chat_id}"
            )

            result.append(
                f"   {error}"
            )

            log.exception(
                "SOURCE ACCESS FAILED"
            )


    # ----------------------------------------
    # DESTINATION
    # ----------------------------------------

    result.append("")
    result.append("📤 DESTINATION:")


    try:

        chat = await client.get_chat(
            DESTINATION_CHAT
        )

        title = (
            chat.title
            or chat.first_name
            or "Unknown"
        )

        result.append(
            f"✅ {DESTINATION_CHAT}"
        )

        result.append(
            f"   {title}"
        )

        log.info(
            "DESTINATION ACCESS OK | %s | %s",
            DESTINATION_CHAT,
            title,
        )

    except Exception as error:

        result.append(
            f"❌ {DESTINATION_CHAT}"
        )

        result.append(
            f"   {error}"
        )

        log.exception(
            "DESTINATION ACCESS FAILED"
        )


    # ----------------------------------------
    # FFMPEG
    # ----------------------------------------

    result.append("")
    result.append("🎬 FFMPEG:")


    if ffmpeg_available():

        result.append(
            "✅ Available"
        )

    else:

        result.append(
            "❌ Not available"
        )


    # ----------------------------------------
    # CONFIG
    # ----------------------------------------

    result.append("")
    result.append("⚙️ CONFIG:")
    result.append(
        f"Prefix: {PREFIX or 'None'}"
    )
    result.append(
        f"Caption: {CAPTION}"
    )

    result.append("")
    result.append(
        "Now upload a NEW file to the source."
    )


    await message.reply_text(
        "\n".join(result)
    )


# ============================================================
# /SETTINGS
# ============================================================

@app.on_message(
    filters.command("settings")
    & filters.private
)
async def settings_command(
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
        f"〄 PREFIX: "
        f"{settings['prefix'] or 'NOT SET'}\n"
        f"〄 CAPTION: "
        f"{settings['caption'] or 'NOT SET'}"

    )


# ============================================================
# /SETPREFIX
# ============================================================

@app.on_message(
    filters.command("setprefix")
    & filters.private
)
async def setprefix_command(
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
        "✅ Prefix updated:\n"
        f"{value or 'None'}"
    )


# ============================================================
# /SETCAPTION
# ============================================================

@app.on_message(
    filters.command("setcaption")
    & filters.private
)
async def setcaption_command(
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

        "✅ Caption updated.\n\n"
        "Variables:\n"
        "{filename}\n"
        "{caption}\n"
        "{chat_id}\n"
        "{message_id}"

    )


# ============================================================
# /SETMETADATA
# ============================================================

@app.on_message(
    filters.command("setmetadata")
    & filters.private
)
async def setmetadata_command(
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

            "Example:\n\n"
            '/setmetadata {"general":{"title":"GoodMovies"}}'

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
                    {},
                ),
            },

            "video": {
                **DEFAULT_METADATA["video"],
                **incoming.get(
                    "video",
                    {},
                ),
            },

            "audio": {
                **DEFAULT_METADATA["audio"],
                **incoming.get(
                    "audio",
                    {},
                ),
            },

            "subtitle": {
                **DEFAULT_METADATA["subtitle"],
                **incoming.get(
                    "subtitle",
                    {},
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
            "✅ Metadata enabled."
        )


    except Exception as error:

        await message.reply_text(
            f"❌ Invalid metadata JSON.\n{error}"
        )


# ============================================================
# /CLEARMETADATA
# ============================================================

@app.on_message(
    filters.command("clearmetadata")
    & filters.private
)
async def clearmetadata_command(
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


# ============================================================
# /RESETDUPLICATES
# ============================================================

@app.on_message(
    filters.command("resetduplicates")
    & filters.private
)
async def resetduplicates_command(
    client,
    message,
):

    if not is_admin(
        message.from_user.id
    ):
        return


    files_col.delete_many({})


    await message.reply_text(
        "✅ Duplicate database cleared."
    )


# ============================================================
# PRIVATE MESSAGE DEBUG
# ============================================================

@app.on_message(
    filters.private
)
async def private_debug(
    client,
    message,
):

    log.info(
        "PRIVATE MESSAGE | user=%s | text=%s",
        message.from_user.id
        if message.from_user
        else "unknown",
        message.text,
    )


# ============================================================
# CHANNEL / GROUP MESSAGE HANDLER
# ============================================================

@app.on_message(
    filters.channel
    | filters.group
)
async def file_forward_handler(
    client,
    message,
):

    log.info(
        "MESSAGE RECEIVED | chat=%s | id=%s | media=%s",
        message.chat.id,
        message.id,
        message.media,
    )


    # ----------------------------------------
    # SOURCE CHECK
    # ----------------------------------------

    if message.chat.id not in SOURCE_CHATS:

        log.info(
            "IGNORED NON-SOURCE CHAT | %s",
            message.chat.id,
        )

        return


    log.info(
        "SOURCE MATCHED | %s",
        message.chat.id,
    )


    # ----------------------------------------
    # MEDIA
    # ----------------------------------------

    media, media_type = extract_media(
        message
    )


    if not media:

        log.info(
            "SOURCE MESSAGE HAS NO SUPPORTED FILE."
        )

        return


    log.info(
        "FILE FOUND | type=%s",
        media_type,
    )


    # ----------------------------------------
    # TELEGRAM DUPLICATE
    # ----------------------------------------

    unique_id = getattr(
        media,
        "file_unique_id",
        None,
    )


    if unique_id:

        duplicate = await asyncio.to_thread(
            duplicate_unique_id,
            unique_id,
        )


        if duplicate:

            log.info(
                "DUPLICATE SKIPPED | %s",
                unique_id,
            )

            return


    # ----------------------------------------
    # SETTINGS
    # ----------------------------------------

    settings = await asyncio.to_thread(
        get_settings
    )


    original_filename = get_filename(
        message,
        media,
        media_type,
    )


    filename = add_prefix(
        original_filename,
        settings["prefix"],
    )


    caption = make_caption(
        settings["caption"],
        message,
        filename,
    )


    log.info(
        "FILENAME | original=%s | final=%s",
        original_filename,
        filename,
    )


    # ----------------------------------------
    # TEMP DIRECTORY
    # ----------------------------------------

    with tempfile.TemporaryDirectory(
        prefix="file_forward_"
    ) as temp_dir:


        temp = Path(temp_dir)


        source_path = (
            temp
            / original_filename
        )


        output_path = (
            temp
            / filename
        )


        try:

            # --------------------------------
            # DOWNLOAD
            # --------------------------------

            log.info(
                "DOWNLOADING..."
            )


            downloaded = (
                await client.download_media(
                    message,
                    file_name=str(
                        source_path
                    ),
                )
            )


            if not downloaded:

                log.error(
                    "DOWNLOAD FAILED"
                )

                return


            source_path = Path(
                downloaded
            )


            log.info(
                "DOWNLOAD COMPLETE"
            )


            # --------------------------------
            # SHA256
            # --------------------------------

            sha256 = await calculate_sha256(
                source_path
            )


            log.info(
                "SHA256: %s",
                sha256,
            )


            if await asyncio.to_thread(
                duplicate_hash,
                sha256,
            ):

                log.info(
                    "DUPLICATE SKIPPED | SHA256"
                )

                return


            # --------------------------------
            # METADATA
            # --------------------------------

            final_path = source_path


            if (
                settings["metadata_enabled"]
                and ffmpeg_available()
                and can_process_metadata(
                    source_path
                )
            ):

                log.info(
                    "APPLYING METADATA..."
                )


                success = (
                    await asyncio.to_thread(
                        apply_metadata,
                        source_path,
                        output_path,
                        settings["metadata"],
                    )
                )


                if success:

                    final_path = output_path

                    log.info(
                        "METADATA APPLIED"
                    )

                else:

                    log.warning(
                        "METADATA FAILED; "
                        "USING ORIGINAL FILE"
                    )


            # --------------------------------
            # FORWARD
            # --------------------------------

            await send_file(
                client,
                final_path,
                filename,
                caption,
                media_type,
            )


            # --------------------------------
            # SAVE DUPLICATE
            # --------------------------------

            await asyncio.to_thread(
                save_file_record,
                unique_id
                or f"sha256:{sha256}",
                sha256,
                message.chat.id,
                message.id,
            )


            log.info(
                "================================"
            )

            log.info(
                "FORWARD SUCCESS"
            )

            log.info(
                "FILE: %s",
                filename,
            )

            log.info(
                "================================"
            )


        except RPCError as error:

            log.exception(
                "TELEGRAM ERROR: %s",
                error,
            )


        except Exception as error:

            log.exception(
                "FORWARD ERROR: %s",
                error,
            )


# ============================================================
# KOYEB HEALTH SERVER
# ============================================================

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


    server = web.Application()


    server.router.add_get(
        "/",
        health_handler,
    )


    server.router.add_get(
        "/health",
        health_handler,
    )


    runner = web.AppRunner(
        server
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


# ============================================================
# MAIN
# ============================================================

async def main():

    log.info(
        "Starting Telegram client..."
    )


    await app.start()


    me = await app.get_me()


    log.info(
        "BOT CONNECTED | @%s | ID=%s",
        me.username or "unknown",
        me.id,
    )


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
        "ADMIN IDS: %s",
        ADMIN_IDS,
    )

    log.info(
        "================================="
    )


    await asyncio.Event().wait()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        pass

    except Exception:

        log.exception(
            "BOT CRASHED"
        )
