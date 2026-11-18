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
if CLOUD and Path("/etc/secrets/cookies.txt").exists():
    # Render "Secret File" (read-only) -> writable copy, because yt-dlp saves cookies back when it finishes
    COOKIES = Path("/tmp/cookies.txt")
    shutil.copyfile("/etc/secrets/cookies.txt", COOKIES)

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
    import re
    tag = re.search(r"\[([a-z0-9:_-]+)\]", low)
    site = {"youtube": "YouTube", "instagram": "Instagram"}.get(tag.group(1).split(":")[0], tag.group(1).split(":")[0].title()) if tag else "The site"
    if "sign in to confirm" in low or "not a bot" in low:       # check first: this message also mentions cookies
        if CLOUD:
            return ("YouTube is blocking this server (it does that to cloud servers). "
                    "The laptop app still works for YouTube.")
        return "YouTube wants a sign-in for this video. Add cookies.txt to the Downloader folder and try again."
    if "login" in low or "private" in low or "rate-limit" in low or "cookies" in low:
        if CLOUD:
            return (f"{site} is blocking this server or the post is private. "
                    "The laptop app still works - or the website owner can add a cookies.txt login.")
        return (f"{site} needs a login for this (private post, or it is limiting downloads). "
                "Put a cookies.txt from your browser in the Downloader folder and try again.")
    if "larger than max-filesize" in low or "file is larger" in low:
        return "That file is too big for the online version (limit 2 GB). Pick a lower quality."
    if "video unavailable" in low or "404" in low:
        return "That video is unavailable (deleted, private or blocked in your country)."
    if ("timed out" in low or "getaddrinfo" in low) and tag and tag.group(1) != "generic":
        return (f"Couldn't reach {site}. It may be blocked in your country (e.g. TikTok in India) "
                "or down right now.")
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
    if good or options:
        return good or options
    # Some sites (e.g. Facebook "hd"/"sd") don't say the resolution - offer their formats by name instead.
    named = [f for f in formats if f.get("vcodec") != "none" and f.get("format_id")
             and not (f.get("acodec") != "none" and f.get("vcodec") is None and f.get("ext") in ("m4a", "mp3", "aac"))]
    order = {"hd": 0, "high": 0, "sd": 1, "low": 2}
    named.sort(key=lambda f: order.get(str(f["format_id"]).lower(), 1))
    return [{"id": "f:" + str(f["format_id"]), "label": str(f.get("format_note") or f["format_id"]).upper()[:12],
             "tag": "Best" if i == 0 else "", "ext": (f.get("ext") or "mp4").upper(),
             "size": f"~{_size_mb(f):.0f} MB" if _size_mb(f) and _size_mb(f) >= 1 else ""}
            for i, f in enumerate(named[:6])]


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
             "image/heic": "heic", "image/avif": "avif"}


def _extract(url):
    """yt-dlp lookup that doesn't fail on photo-only posts (they simply come back without formats)."""
    import yt_dlp
    opts = _base_opts() | {"noplaylist": True, "ignore_no_formats_error": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _items(info):
    entries = [e for e in (info.get("entries") or []) if e] if info.get("_type") == "playlist" else []
    return entries or [info]


def _has_video(item):
    return any(f.get("vcodec") not in (None, "none") or f.get("acodec") not in (None, "none")
               for f in item.get("formats") or [])


VIDEO_ONLY_SITES = {"youtube", "vimeo", "twitch", "dailymotion"}


def _blocked_video(item):
    """A video whose streams the site refused to give (only its thumbnail/storyboard came back) - not a photo."""
    if _has_video(item):
        return False
    return (str(item.get("extractor_key") or item.get("ie_key") or "").lower() in VIDEO_ONLY_SITES
            or bool(item.get("duration"))
            or any("storyboard" in str(f.get("format_note") or f.get("format_id") or "").lower()
                   for f in item.get("formats") or []))


def _photo_urls(info):
    """Best-quality image of every photo (not video) item - e.g. Instagram photo posts and carousels."""
    urls = []
    for item in _items(info):
        if _has_video(item) or _blocked_video(item):
            continue
        thumbs = [t for t in item.get("thumbnails") or [] if t.get("url")]
        if thumbs:
            best = max(thumbs, key=lambda t: ((t.get("width") or 0) * (t.get("height") or 0), t.get("preference") or -99))
            urls.append(best["url"])
        elif item.get("thumbnail"):
            urls.append(item["thumbnail"])
    return urls


def _probe_file(url):
    """For links yt-dlp doesn't know: is it a plain downloadable file (PDF, ZIP, image...)? -> (name, size, type)."""
    import urllib.parse
    import urllib.request
    from email.message import Message
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not ctype or ctype in ("text/html", "application/xhtml+xml"):
            return None
        total = r.headers.get("Content-Range", "").rpartition("/")[2] or r.headers.get("Content-Length") or ""
        msg = Message()
        msg["content-disposition"] = r.headers.get("Content-Disposition") or ""
        name = msg.get_filename() or urllib.parse.unquote(Path(urllib.parse.urlparse(r.url).path).name) or "download"
    name = "".join(c for c in name if c not in r'<>:"/\|?*').strip()[:120] or "download"
    if "." not in name and ctype in IMAGE_EXT:
        name += "." + IMAGE_EXT[ctype]
    size = int(total) if total.isdigit() else 0
    return name, size, ctype


def _file_info(url):
    probed = _probe_file(url)
    if not probed:
        return None
    name, size, ctype = probed
    ext = Path(name).suffix.lstrip(".").upper() or ctype.split("/")[-1].upper()
    return {"title": name, "uploader": "", "site": "Direct file", "thumbnail": url if ctype.startswith("image/") else "",
            "duration": None, "count": 1, "video": [], "audio": [], "photo": [],
            "file": [{"id": "file", "label": "Original file", "tag": f"{size / 1_048_576:.1f} MB" if size else "",
                      "ext": ext, "size": ""}],
            "ffmpeg": bool(ffmpeg_dir())}


def get_info(url):
    try:
        info = _extract(url)
    except Exception as e:  # noqa: BLE001
        low = str(e).lower()
        if "unsupported url" in low or "[generic]" in low:     # not a video site -> maybe a direct file link
            try:
                found = _file_info(url)
            except Exception:  # noqa: BLE001
                found = None
            if found:
                return found
        raise
    entries = [e for e in (info.get("entries") or []) if e] if info.get("_type") == "playlist" else []
    first = next((e for e in entries if _has_video(e)), entries[0] if entries else info)
    formats = first.get("formats") or []
    duration = first.get("duration")
    video = _video_options(formats, duration)
    has_audio = any(f.get("acodec") != "none" for f in formats)
    photos = _photo_urls(info)
    if not video and not has_audio and not photos:
        if any(_blocked_video(i) for i in _items(info)):
            site = info.get("extractor_key") or "The site"
            if CLOUD:
                raise RuntimeError(f"{site} is blocking this server, so only the thumbnail came back. "
                                   "The laptop app still works for this link.")
            raise RuntimeError(f"{site} didn't send the video (only its thumbnail). Click 'Update engine' "
                               "and try again.")
        try:
            found = _file_info(url)
        except Exception:  # noqa: BLE001
            found = None
        if found:
            return found
        raise RuntimeError("No video, audio or photo found at this link.")
    return {
        "title": info.get("title") or first.get("title") or "Untitled",
        "uploader": info.get("uploader") or first.get("uploader") or info.get("channel") or "",
        "site": info.get("extractor_key") or "",
        "thumbnail": first.get("thumbnail") or info.get("thumbnail") or "",
        "duration": duration,
        "count": len(entries) or 1,
        "video": video,
        "audio": [{"id": i, "label": a, "tag": t, "ext": e, "size": ""} for i, e, a, t in AUDIO_OPTIONS] if has_audio else [],
        "photo": [{"id": "photo", "label": "Original", "tag": f"{len(photos)} photo{'s' if len(photos) > 1 else ''}",
                   "ext": "JPG", "size": ""}] if photos else [],
        "file": [],
        "ffmpeg": bool(ffmpeg_dir()),
    }


# ---------------- downloads ----------------

JOBS = {}
_lock = threading.Lock()


def _download_opts(option, job):
    opts = _base_opts() | {"noplaylist": True, "paths": {"home": str(OUT_DIR), "temp": str(OUT_DIR / ".temp")}}
    if CLOUD:
        opts["max_filesize"] = 2 * 1024 ** 3
    if option.startswith("f:"):
        fid = option[2:]
        label = job.get("label") or fid.upper()
        opts["format"] = f"{fid}+ba/{fid}/b"
        opts["merge_output_format"] = "mp4"
    elif option.startswith("v"):
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


def _fetch(url, dest, job, part=0, parts=1):
    """Plain HTTP download with progress (photos, direct files)."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        total, done, t0 = int(r.headers.get("Content-Length") or 0), 0, time.time()
        if CLOUD and total > 2 * 1024 ** 3:
            raise RuntimeError("larger than max-filesize")
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not dest.suffix and ctype in IMAGE_EXT:
            dest = _unique(dest.with_name(dest.name + "." + IMAGE_EXT[ctype]))
        with open(dest, "wb") as f:
            while chunk := r.read(1 << 18):
                f.write(chunk)
                done += len(chunk)
                speed = done / max(time.time() - t0, 0.1)
                job.update(status="downloading", speed=f"{speed / 1_048_576:.1f}MiB/s",
                           percent=round(min(99, (part + (done / total if total else 0)) / parts * 100), 1),
                           eta=f"{int((total - done) / speed)}s" if total and speed else "")
    return dest


def _unique(path):
    n = 1
    original = path
    while path.exists():
        path = original.with_name(f"{original.stem} ({n}){original.suffix}")
        n += 1
    return path


def _download_plain(job, url, option):
    if option == "file":
        probed = _probe_file(url)
        return [_fetch(url, _unique(OUT_DIR / (probed[0] if probed else "download")), job)]
    urls = _photo_urls(_extract(url))
    if not urls:
        raise RuntimeError("No photos found at this link.")
    base = "".join(c for c in (job["title"] or "Photo") if c not in r'<>:"/\|?*').strip()[:70] or "Photo"
    files = []
    for i, u in enumerate(urls):
        stem = f"{base} {i + 1}" if len(urls) > 1 else base
        files.append(_fetch(u, OUT_DIR / stem, job, i, len(urls)))
    return files


def _run(job, url, option):
    import yt_dlp
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        job["status"] = "queued"
        with MAX_JOBS:
            job["status"] = "starting"
            if option in ("photo", "file"):
                files = _download_plain(job, url, option)
            else:
                with yt_dlp.YoutubeDL(_download_opts(option, job)) as ydl:
                    files = _collect_files(ydl.extract_info(url, download=True))
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
        job.update(status="done", percent=100, file=files[0].name, size=human_size(files[0].stat().st_size))
    except Exception as e:  # noqa: BLE001 - every failure becomes a message in the page
        job.update(status="error", error=friendly_error(e))


def start_download(url, option, title="", count=1, label=""):
    job_id = uuid.uuid4().hex[:10]
    stages = (count or 1) * (2 if option[:1] == "v" else 1)     # video = 2 streams (picture + sound)
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
    return [{"name": p.name, "size": human_size(p.stat().st_size)} for p in files[:limit]]


def human_size(n):
    return f"{n / 1_048_576:.1f} MB" if n >= 102_400 else f"{max(1, round(n / 1024))} KB"


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
