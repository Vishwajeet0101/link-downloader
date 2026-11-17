# Link Downloader

Paste a YouTube or Instagram link, pick a video quality (360p–4K MP4) or audio (MP3 / M4A), download.
Works on laptop and phone browsers. A PIN is required; files are deleted from the server after 2 hours.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Vishwajeet0101/link-downloader)

- `server.py` – web server + page, `engine.py` – yt-dlp downloads, `cloud.py` – website entry point (`CLOUD=1`).
- Set `APP_PIN` (digits) as an environment variable.

Only download content you have the right to save.
