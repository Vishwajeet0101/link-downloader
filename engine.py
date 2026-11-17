"""yt-dlp wrapper: look up a link, list quality options, and run downloads in the background."""
import glob
import os
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Portable copy (the ZIP shared with friends): app\ sits next to python\. ffmpeg + Deno aren't in the
# ZIP (they're 270 MB) - setup_tools() downloads them into bin\ on the first run.
BUNDLED_BIN = HERE.parent / "bin"
PORTABLE = (HERE.parent / "python" / "pythonw.exe").exists()
ROOT = HERE.parent if PORTABLE else HERE
OUT_DIR = Path(os.environ.get("OUT_DIR") or Path.home() / "Downloads" / "Downloader")
CLOUD = os.environ.get("CLOUD") == "1"
MAX_JOBS = threading.BoundedSemaphore(3 if CLOUD else 50)   # a free server is small: 3 downloads at a time
COOKIES = ROOT / "cookies.txt"          # optional: browser-exported cookies for private posts

VIDEO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360]
QUALITY_NAMES = {2160: "4K", 1440: "2K", 1080: "Full HD", 720: "HD", 480: "SD", 360: "Low"}
AUDIO_OPTIONS = [
    ("mp3-320", "MP3", "320 kbps", "Best MP3"),
    ("mp3-192", "MP3", "192 kbps", "Good, smaller"),
    ("mp3-128", "MP3", "128 kbps", "Smallest"),
    ("m4a", "M4A", "Original", "No re-encode, best quality"),
]


def ffmpeg_dir():
    """Folder that holds ffmpeg.exe (bundled, on PATH or installed by winget), or None."""
    if PORTABLE:
        return str(BUNDLED_BIN) if (BUNDLED_BIN / "ffmpeg.exe").exists() else None
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).parent)
    pattern = str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/ffmpeg.exe")
    hits = glob.glob(pattern, recursive=True)
    return str(Path(hits[0]).parent) if hits else None


# ---------------- first-run setup (portable copy only) ----------------

TOOLS = [  # (name shown in the page, url, files to keep from the zip)
    ("ffmpeg", "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip",
     lambda n: n.endswith((".dll", "ffmpeg.exe", "ffprobe.exe"))),
    ("YouTube helper (Deno)", "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
     lambda n: n == "deno.exe"),
]
SETUP = {"status": "done" if not PORTABLE else "idle", "step": "", "percent": 0, "error": ""}


def tools_ready():
    return not PORTABLE or ((BUNDLED_BIN / "ffmpeg.exe").exists() and (BUNDLED_BIN / "deno.exe").exists())


def setup_tools():
    """Download ffmpeg + Deno into the bin folder once. Progress goes to SETUP for the page."""
    import urllib.request
    if tools_ready():
        SETUP.update(status="done", percent=100)
        return
    SETUP.update(status="running", error="")
    BUNDLED_BIN.mkdir(exist_ok=True)
    try:
        for i, (name, url, keep) in enumerate(TOOLS):
            SETUP["step"] = f"Downloading {name} ({i + 1}/{len(TOOLS)})"
            tmp = BUNDLED_BIN / f"{i}.zip.part"
            with urllib.request.urlopen(url, timeout=30) as r, open(tmp, "wb") as f:
                total, done = int(r.headers.get("Content-Length") or 0), 0
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        SETUP["percent"] = round((i + done / total) / len(TOOLS) * 100)
            SETUP["step"] = f"Unpacking {name}"
            with zipfile.ZipFile(tmp) as z:
                for info in z.infolist():
                    fname = Path(info.filename).name
                    if not info.is_dir() and keep(fname):
                        with z.open(info) as src, open(BUNDLED_BIN / fname, "wb") as dst:
                            shutil.copyfileobj(src, dst)
            tmp.unlink()
        SETUP.update(status="done", percent=100, step="")
    except Exception as e:  # noqa: BLE001
        SETUP.update(status="error", error=f"First-time setup failed: {friendly_error(e)} Check the internet, then click Retry.")


class _QuietLogger:
    """pythonw has no console, so yt-dlp messages must not go to stdout/stderr."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _base_opts():
    opts = {
        "logger": _QuietLogger(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "windowsfilenames": True,
        "socket_timeout": 20,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        # YouTube needs a JavaScript runtime to unlock formats: the bundled Deno, else Node/Deno if installed.
        "js_runtimes": ({"deno": {"path": str(BUNDLED_BIN / "deno.exe")}} if (BUNDLED_BIN / "deno.exe").exists()
                        else {"node": {}, "deno": {}}),
        "remote_components": ["ejs:github"],
    }
    ff = ffmpeg_dir()
    if ff:
        opts["ffmpeg_location"] = ff
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    return opts


def friendly_error(e):
    msg = str(e).replace("ERROR: ", "")
    low = msg.lower()
    if "unsupported url" in low:
        return "This link isn't supported. Paste a YouTube, Instagram (or other video site) link."
    if "login" in low or "private" in low or "rate-limit" in low or "cookies" in low:
        return ("This post needs a login (private account or Instagram is rate-limiting). "
                "Put a cookies.txt from your browser in the Downloader folder and try again.")
    if "sign in to confirm" in low or "not a bot" in low:
        if CLOUD:
            return "YouTube is blocking the server right now (it does this to cloud servers). Try again later or another video."
        return "YouTube wants a sign-in for this video. Add cookies.txt to the Downloader folder and try again."
    if "larger than max-filesize" in low or "file is larger" in low:
        return "That file is too big for the online version (limit 2 GB). Pick a lower quality."
    if "video unavailable" in low or "404" in low:
        return "That video is unavailable (deleted, private or blocked in your country)."
    if "getaddrinfo" in low or "timed out" in low or "connection" in low:
        return "No internet connection (or it's too slow). Check the hotspot and try again."
    return msg[:300]


# ---------------- info ----------------

def _size_mb(fmt):
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    return size / 1_048_576 if size else None


def _video_options(formats, duration):
    videos = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")]
    audios = [f for f in formats if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    best_audio = max((_size_mb(a) or 0 for a in audios if (a.get("ext") == "m4a")), default=0) or \
        max((_size_mb(a) or 0 for a in audios), default=0)
    # "1080p" means the SHORT side, so a vertical 1080x1920 reel is 1080p too.
    groups = {}
    for f in videos:
        short = min(f.get("width") or f["height"], f["height"])
        bucket = min(VIDEO_HEIGHTS, key=lambda b: abs(short - b))
        if abs(short - bucket) / bucket > 0.08:          # odd size like 240p -> keep its own label
            bucket = short
        groups.setdefault(bucket, []).append(f)
    options = []
    for bucket in sorted(groups, reverse=True):
        group = groups[bucket]
        max_h = max(f["height"] for f in group)          # download filter: best video up to this height
        best = [f for f in group if f["height"] == max_h]
        sizes = [_size_mb(f) for f in best if _size_mb(f)]
        size = (min(sizes) + (best_audio if best[0].get("acodec") in (None, "none") else 0)) if sizes else None
        options.append({
            "id": f"v{max_h}",
            "label": f"{bucket}p",
            "tag": QUALITY_NAMES.get(bucket, ""),
            "ext": "MP4",
            "size": f"~{size:.0f} MB" if size and size >= 1 else (f"~{size:.1f} MB" if size and size >= 0.1 else ""),
        })
    good = [o for o in options if int(o["label"][:-1]) >= 200]     # drop 144p-style junk if better exists
    return good or options


def get_info(url):
    import yt_dlp
    opts = _base_opts() | {"noplaylist": True, "extract_flat": False}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [e for e in (info.get("entries") or []) if e] if info.get("_type") == "playlist" else []
    first = entries[0] if entries else info
    formats = first.get("formats") or []
    duration = first.get("duration")
    video = _video_options(formats, duration)
    has_audio = any(f.get("acodec") != "none" for f in formats) or not formats   # None = unknown, maybe
    return {
        "title": info.get("title") or first.get("title") or "Untitled",
        "uploader": info.get("uploader") or first.get("uploader") or info.get("channel") or "",
        "site": info.get("extractor_key") or "",
        "thumbnail": first.get("thumbnail") or info.get("thumbnail") or "",
        "duration": duration,
        "count": len(entries) or 1,
        "video": video,
        "audio": [{"id": i, "label": a, "tag": t, "ext": e, "size": ""} for i, e, a, t in AUDIO_OPTIONS] if has_audio else [],
        "ffmpeg": bool(ffmpeg_dir()),
    }


# ---------------- downloads ----------------

JOBS = {}
_lock = threading.Lock()


def _download_opts(option, job):
    opts = _base_opts() | {"noplaylist": True, "paths": {"home": str(OUT_DIR), "temp": str(OUT_DIR / ".temp")}}
    if CLOUD:
        opts["max_filesize"] = 2 * 1024 ** 3
    if option.startswith("v"):
        h = int(option[1:])
        label = job.get("label") or f"{h}p"
        # Prefer H.264 + AAC (plays on every phone) but only at the chosen resolution - 4K/2K on
        # YouTube exist only as VP9/AV1, so then take the best there is.
        lo = int(h * 0.9)
        opts["format"] = (f"bv*[height<={h}][height>={lo}][vcodec^=avc1]+ba[ext=m4a]/"
                          f"bv*[height<={h}]+ba[ext=m4a]/bv*[height<={h}]+ba/"
                          f"b[height<={h}]/bv*[height<={h}]/bv*+ba/b/bv*")   # last ones: clips with no sound
        opts["merge_output_format"] = "mp4"
    else:
        label = {"mp3-320": "MP3 320", "mp3-192": "MP3 192", "mp3-128": "MP3 128", "m4a": "M4A"}[option]
        opts["format"] = "ba[ext=m4a]/ba/b"
        codec, quality = ("m4a", "0") if option == "m4a" else ("mp3", option.split("-")[1])
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": quality},
            {"key": "FFmpegMetadata"},
        ]
    multi = "%(playlist_index|)s"
    opts["outtmpl"] = {"default": f"%(title).80B{' ' + multi if job['count'] > 1 else ''} ({label}).%(ext)s"}

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            part = done / total if total else 0
            # several streams (video + audio, carousel items) -> spread the bar over all of them
            job["percent"] = round(min(99, (job["stage"] + part) / job["stages"] * 100), 1)
            job["speed"] = d.get("_speed_str", "").strip()
            job["eta"] = d.get("_eta_str", "").strip()
            job["status"] = "downloading"
        elif d["status"] == "finished":
            job["stage"] = min(job["stage"] + 1, job["stages"] - 1)
            job["status"] = "processing"

    def pp_hook(d):
        if d["status"] == "started":
            job["status"] = "processing"

    opts["progress_hooks"] = [hook]
    opts["postprocessor_hooks"] = [pp_hook]
    return opts


def _collect_files(info):
    files = []
    items = info.get("entries") if info.get("_type") == "playlist" else [info]
    for item in items or []:
        if not item:
            continue
        for rd in item.get("requested_downloads") or []:
            p = rd.get("filepath") or rd.get("_filename")
            if p and Path(p).exists():
                files.append(Path(p))
    return files


def _run(job, url, option):
    import yt_dlp
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        job["status"] = "queued"
        with MAX_JOBS, yt_dlp.YoutubeDL(_download_opts(option, job)) as ydl:
            job["status"] = "starting"
            info = ydl.extract_info(url, download=True)
        files = _collect_files(info)
        if not files:
            raise RuntimeError("Download finished but no file was found.")
        if len(files) > 1:                 # Instagram carousel -> items in a folder + one zip for the phone
            name = "".join(c for c in (job["title"] or files[0].stem) if c not in r'<>:"/\|?*').strip()[:80] or "Post"
            folder = OUT_DIR / name
            folder.mkdir(exist_ok=True)
            zpath = OUT_DIR / f"{name} ({len(files)} items).zip"
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
                for f in files:
                    z.write(f, f.name)
                    shutil.move(str(f), str(folder / f.name))
            files = [zpath]
        job.update(status="done", percent=100, file=files[0].name,
                   size=f"{files[0].stat().st_size / 1_048_576:.1f} MB")
    except Exception as e:  # noqa: BLE001 - every failure becomes a message in the page
        job.update(status="error", error=friendly_error(e))


def start_download(url, option, title="", count=1, label=""):
    job_id = uuid.uuid4().hex[:10]
    stages = (count or 1) * (2 if option.startswith("v") else 1)
    label = "".join(ch for ch in str(label) if ch.isalnum())[:10]     # e.g. "1080p"
    job = {"id": job_id, "status": "starting", "percent": 0, "speed": "", "eta": "", "title": title, "label": label,
           "option": option, "count": count or 1, "stage": 0, "stages": stages, "started": time.time()}
    with _lock:
        JOBS[job_id] = job
    threading.Thread(target=_run, args=(job, url, option), daemon=True).start()
    return job_id


def recent_files(limit=15):
    if not OUT_DIR.exists():
        return []
    files = [p for p in OUT_DIR.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size": f"{p.stat().st_size / 1_048_576:.1f} MB"} for p in files[:limit]]


def safe_file(name):
    """Resolve a file name inside OUT_DIR, refusing anything that escapes it."""
    p = (OUT_DIR / name).resolve()
    if p.parent != OUT_DIR.resolve() or not p.is_file():
        return None
    return p


def cleanup_loop(max_age=2 * 3600):
    """Website mode: delete downloads older than 2 hours so the free server's disk never fills up."""
    while True:
        cutoff = time.time() - max_age
        for p in OUT_DIR.rglob("*") if OUT_DIR.exists() else []:
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
        for d in sorted((p for p in OUT_DIR.rglob("*") if p.is_dir()), reverse=True) if OUT_DIR.exists() else []:
            try:
                d.rmdir()                     # only succeeds when empty
            except OSError:
                pass
        with _lock:
            for jid in [j for j, job in JOBS.items() if job["started"] < cutoff]:
                del JOBS[jid]
        time.sleep(600)
