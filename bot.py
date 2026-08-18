import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message
from pymongo import MongoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("file-forward-bot")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = os.getenv("DB_NAME", "file_forward_bot")
SOURCE_CHATS = {
    int(x.strip()) for x in os.environ["SOURCE_CHATS"].split(",") if x.strip()
}
DESTINATION_CHAT = int(os.environ["DESTINATION_CHAT"])
ADMIN_IDS = {
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
}

DEFAULT_PREFIX = os.getenv("PREFIX", "")
DEFAULT_CAPTION = os.getenv("CAPTION", "{caption}")
DEFAULT_METADATA = {
    "general": {
        "title": "",
        "artist": "",
        "album": "",
        "comment": "",
        "encoder": "",
        "description": "",
    },
    "video": {"title": "", "language": ""},
    "audio": {"title": "", "artist": "", "album": "", "language": ""},
    "subtitle": {"title": "", "language": ""},
}

app = Client(
    "file_forward_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo[DB_NAME]
files_col = db["files"]
settings_col = db["settings"]

files_col.create_index("unique_id", unique=True, sparse=True)
files_col.create_index("sha256", unique=True, sparse=True)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_settings():
    data = settings_col.find_one({"_id": "settings"}) or {}
    return {
        "prefix": data.get("prefix", DEFAULT_PREFIX),
        "caption": data.get("caption", DEFAULT_CAPTION),
        "metadata": data.get("metadata", DEFAULT_METADATA),
        "metadata_enabled": data.get("metadata_enabled", False),
    }


def save_settings(data):
    settings_col.update_one({"_id": "settings"}, {"$set": data}, upsert=True)


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


def original_name(message: Message, media, kind: str) -> str:
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


def apply_prefix(filename: str, prefix: str) -> str:
    if not prefix:
        return filename
    return f"{prefix}{filename}"


def render_caption(template: str, message: Message, filename: str) -> str:
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


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def media_can_have_metadata(path: Path) -> bool:
    ext = path.suffix.lower()
    return ext in {
        ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv",
        ".ts", ".mts", ".m2ts", ".mp3", ".m4a", ".aac", ".flac",
        ".ogg", ".opus", ".wav"
    }


def build_ffmpeg_metadata_args(metadata: dict):
    args = []

    general = metadata.get("general", {})
    mapping = {
        "title": general.get("title"),
        "artist": general.get("artist"),
        "album": general.get("album"),
        "comment": general.get("comment"),
        "encoder": general.get("encoder"),
        "description": general.get("description"),
    }
    for key, value in mapping.items():
        if value:
            args += ["-metadata", f"{key}={value}"]

    video = metadata.get("video", {})
    if video.get("title"):
        args += ["-metadata:s:v:0", f"title={video['title']}"]
    if video.get("language"):
        args += ["-metadata:s:v:0", f"language={video['language']}"]

    audio = metadata.get("audio", {})
    if audio.get("title"):
        args += ["-metadata:s:a:0", f"title={audio['title']}"]
    if audio.get("artist"):
        args += ["-metadata:s:a:0", f"artist={audio['artist']}"]
    if audio.get("album"):
        args += ["-metadata:s:a:0", f"album={audio['album']}"]
    if audio.get("language"):
        args += ["-metadata:s:a:0", f"language={audio['language']}"]

    subtitle = metadata.get("subtitle", {})
    if subtitle.get("title"):
        args += ["-metadata:s:s:0", f"title={subtitle['title']}"]
    if subtitle.get("language"):
        args += ["-metadata:s:s:0", f"language={subtitle['language']}"]

    return args


def process_metadata(src: Path, dst: Path, metadata: dict):
    args = build_ffmpeg_metadata_args(metadata)
    if not args:
        return False

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-map", "0",
        "-map_metadata", "0",
        "-c", "copy",
        *args,
        str(dst),
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        log.error("FFmpeg failed: %s", completed.stderr[-2000:])
        return False
    return True


async def sha256_file(path: Path) -> str:
    def _calc():
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    return await asyncio.to_thread(_calc)


def already_seen(unique_id: str) -> bool:
    return bool(files_col.find_one({"unique_id": unique_id}, {"_id": 1}))


def already_hashed(sha256: str) -> bool:
    return bool(files_col.find_one({"sha256": sha256}, {"_id": 1}))


def mark_seen(unique_id: str, sha256: str | None, source_chat: int, source_message: int):
    doc = {
        "unique_id": unique_id,
        "sha256": sha256,
        "source_chat": source_chat,
        "source_message": source_message,
    }
    try:
        files_col.update_one(
            {"unique_id": unique_id},
            {"$setOnInsert": doc},
            upsert=True,
        )
    except Exception as e:
        log.warning("Could not store duplicate record: %s", e)


async def send_processed(client: Client, message: Message, path: Path, filename: str, caption: str, kind: str):
    if kind in {"video", "animation"}:
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


@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    await message.reply_text(
        "🤖 File Forward Bot is running.\n\n"
        "It automatically processes new files from the configured source chats."
    )


@app.on_message(filters.command("settings") & filters.private)
async def settings_handler(client, message):
    if not is_admin(message.from_user.id):
        return
    s = get_settings()
    await message.reply_text(
        "〄 Metadata Setting:\n"
        "╭\n"
        f"┊ METADATA: {'SET' if s['metadata_enabled'] else 'NOT SET'}\n"
        "╰ Description: Metadata Is Information Added To Streams "
        "(General, Video, Audio, Subtitle).\n\n"
        f"〄 PREFIX: {s['prefix'] or 'NOT SET'}\n"
        f"〄 CAPTION: {s['caption'] or 'NOT SET'}"
    )


@app.on_message(filters.command("setprefix") & filters.private)
async def setprefix_handler(client, message):
    if not is_admin(message.from_user.id):
        return
    value = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    save_settings({"prefix": value})
    await message.reply_text(f"✅ Prefix set to: {value or 'NOT SET'}")


@app.on_message(filters.command("setcaption") & filters.private)
async def setcaption_handler(client, message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    value = parts[1] if len(parts) > 1 else ""
    save_settings({"caption": value})
    await message.reply_text(
        "✅ Caption set.\n\n"
        "Variables: {filename}, {caption}, {chat_id}, {message_id}"
    )


@app.on_message(filters.command("clearmetadata") & filters.private)
async def clearmetadata_handler(client, message):
    if not is_admin(message.from_user.id):
        return
    save_settings({"metadata": DEFAULT_METADATA, "metadata_enabled": False})
    await message.reply_text("✅ Metadata disabled and reset.")


@app.on_message(filters.command("setmetadata") & filters.private)
async def setmetadata_handler(client, message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text(
            "Send JSON after /setmetadata.\n"
            "Example:\n"
            "/setmetadata {\"general\":{\"title\":\"GoodMovies\",\"comment\":\"@GoodMovies\"}}"
        )
        return

    try:
        incoming = json.loads(parts[1])
        metadata = DEFAULT_METADATA.copy()
        for section in ("general", "video", "audio", "subtitle"):
            if section in incoming and isinstance(incoming[section], dict):
                metadata[section] = {
                    **DEFAULT_METADATA[section],
                    **incoming[section],
                }
        save_settings({"metadata": metadata, "metadata_enabled": True})
        await message.reply_text("✅ Metadata enabled and saved.")
    except json.JSONDecodeError:
        await message.reply_text("❌ Invalid JSON.")


@app.on_message(filters.command("resetduplicates") & filters.private)
async def resetduplicates_handler(client, message):
    if not is_admin(message.from_user.id):
        return
    files_col.delete_many({})
    await message.reply_text("⚠️ Duplicate database cleared. Previously seen files can be forwarded again.")


@app.on_message(filters.channel | filters.group)
async def auto_forward(client, message):
    if message.chat.id not in SOURCE_CHATS:
        return

    media, kind = extract_media(message)
    if not media:
        return

    unique_id = getattr(media, "file_unique_id", None)
    if unique_id and await asyncio.to_thread(already_seen, unique_id):
        log.info("Skipping duplicate by file_unique_id: %s", unique_id)
        return

    settings = await asyncio.to_thread(get_settings)
    source_name = original_name(message, media, kind)
    filename = apply_prefix(source_name, settings["prefix"])
    caption = render_caption(settings["caption"], message, filename)

    with tempfile.TemporaryDirectory(prefix="ffbot_") as tmp:
        tmp_path = Path(tmp)
        source_path = tmp_path / source_name
        output_path = tmp_path / filename

        try:
            downloaded = await client.download_media(message, file_name=str(source_path))
            if not downloaded:
                log.error("Download failed for message %s", message.id)
                return

            source_path = Path(downloaded)

            sha256 = await sha256_file(source_path)
            if await asyncio.to_thread(already_hashed, sha256):
                if unique_id:
                    await asyncio.to_thread(
                        mark_seen, unique_id, sha256, message.chat.id, message.id
                    )
                log.info("Skipping duplicate by SHA-256: %s", sha256)
                return

            final_path = source_path
            if settings["metadata_enabled"] and ffmpeg_available() and media_can_have_metadata(source_path):
                if process_metadata(source_path, output_path, settings["metadata"]):
                    final_path = output_path
                else:
                    log.warning("Metadata processing failed; sending original file.")

            await send_processed(
                client, message, final_path, filename, caption, kind
            )

            if unique_id:
                await asyncio.to_thread(
                    mark_seen, unique_id, sha256, message.chat.id, message.id
                )
            else:
                await asyncio.to_thread(
                    mark_seen, f"sha256:{sha256}", sha256, message.chat.id, message.id
                )

            log.info("Forwarded %s from %s/%s", filename, message.chat.id, message.id)

        except Exception:
            log.exception("Failed to process %s/%s", message.chat.id, message.id)


async def main():
    await app.start()
    log.info("Bot started. Source chats: %s -> %s", SOURCE_CHATS, DESTINATION_CHAT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
