# Telegram File Forward Bot

Automatically downloads new media from configured Telegram source chats, skips duplicates, optionally edits FFmpeg metadata, adds a filename prefix/caption, and uploads the result to the destination chat.

## Features

- Automatic processing of new messages from one or more source chats.
- Duplicate protection using Telegram `file_unique_id`.
- Secondary SHA-256 duplicate protection.
- Filename prefix.
- Caption template.
- FFmpeg metadata editing for supported media containers.
- General, video, audio and subtitle metadata.
- MongoDB persistence.
- Docker/Koyeb ready.

## Important Telegram setup

1. Create a bot with BotFather and copy the bot token.
2. Get your Telegram API ID/API hash from Telegram's developer tools.
3. Add the bot to every source channel/group.
4. For channels, make the bot an administrator so it can receive channel posts.
5. Add the bot to the destination channel/group and give it permission to send media.
6. Put the numeric chat IDs into `.env`.

Do not publish `BOT_TOKEN`, `API_HASH`, or `MONGO_URI`.

## Configuration

Copy `.env.example` to `.env` for local testing.

Required:
- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `MONGO_URI`
- `SOURCE_CHATS`
- `DESTINATION_CHAT`
- `ADMIN_IDS`

`SOURCE_CHATS` supports multiple IDs separated by commas.

## Commands

Private chat with the bot, as an admin:

`/settings`
Shows current prefix, caption and metadata state.

`/setprefix [prefix]`
Example:
`/setprefix [GOODMOVIES] `

`/setcaption [template]`
Example:
`/setcaption 🎬 {filename}\n\n{caption}`

Available caption variables:
- `{filename}`
- `{caption}`
- `{chat_id}`
- `{message_id}`

`/setmetadata JSON`
Example:
`/setmetadata {"general":{"title":"GoodMovies","comment":"@GoodMovies"},"audio":{"language":"eng"}}`

`/clearmetadata`
Disables metadata processing.

`/resetduplicates`
Clears the duplicate database. Use this only when you intentionally want previously processed files to be eligible again.

## Metadata example

```json
{
  "general": {
    "title": "GoodMovies",
    "artist": "GoodMovies",
    "album": "Movies",
    "comment": "@GoodMovies",
    "encoder": "GoodMovies"
  },
  "video": {
    "title": "GoodMovies"
  },
  "audio": {
    "title": "GoodMovies",
    "artist": "GoodMovies",
    "language": "eng"
  },
  "subtitle": {
    "title": "English",
    "language": "eng"
  }
}
```

Metadata processing uses FFmpeg stream copying (`-c copy`), so it remuxes rather than re-encoding when the container/codec combination supports the requested metadata.

## Koyeb

This repository contains a Dockerfile with FFmpeg installed.

Recommended Koyeb service type: Worker.

Deploy from the project directory with the Koyeb CLI:

```bash
koyeb deploy . my-file-forward-bot/forwarder --archive-builder docker --type worker
```

Or connect the repository through Koyeb's GitHub deployment flow and use the Dockerfile builder.

Set the environment variables in Koyeb's service settings.

## Notes

- Metadata processing requires a download and re-upload, so it is slower than Telegram's server-side copy/forward operation.
- Large files need enough temporary disk space and network throughput.
- FFmpeg metadata support varies by container format.
- The bot must be able to read the source and send to the destination.