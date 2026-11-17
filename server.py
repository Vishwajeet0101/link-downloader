"""Web server for the downloader: the same page works on the laptop browser and on the phone."""
import hmac
import io
import json
import mimetypes
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import engine

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "settings.json"
# CLOUD=1: running as a public website. Then nobody counts as "the laptop", the PIN comes from the
# APP_PIN secret, and laptop-only buttons (open folder, update, quit, QR) are off.
CLOUD = os.environ.get("CLOUD") == "1"
PORT = int(os.environ.get("PORT") or 8770)


def load_settings():
    if CLOUD:
        return {"pin": "".join(ch for ch in os.environ.get("APP_PIN", "") if ch.isdigit())}
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not data.get("pin"):
        data["pin"] = f"{secrets.randbelow(10000):04d}"
        SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no data is sent; this just picks the Wi-Fi address
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def phone_url():
    return f"http://{lan_ip()}:{PORT}/?pin={load_settings()['pin']}"


def qr_svg(text):
    import qrcode
    from qrcode.image.svg import SvgPathFillImage     # pure Python - no Pillow needed
    img = qrcode.make(text, image_factory=SvgPathFillImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


class Guard:
    failures = 0
    locked_until = 0

    @classmethod
    def check(cls, pin):
        if time.time() < cls.locked_until:
            return "locked"
        given = "".join(ch for ch in str(pin or "") if ch.isdigit())
        if hmac.compare_digest(given, load_settings()["pin"]):
            cls.failures = 0
            return "ok"
        if given:
            cls.failures += 1
            if cls.failures >= 10:
                cls.locked_until = time.time() + 30
                cls.failures = 0
        return "bad"


def update_from_pypi(packages=("yt-dlp", "yt-dlp-ejs")):
    """Portable copy has no pip: fetch the newest wheels from PyPI and unpack them into site-packages."""
    import urllib.request
    import zipfile
    site = Path(engine.__file__).resolve().parent.parent / "python" / "Lib" / "site-packages"
    for pkg in packages:
        meta = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=30))
        wheel = next(u for u in meta["urls"] if u["filename"].endswith("py3-none-any.whl"))
        data = urllib.request.urlopen(wheel["url"], timeout=60).read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            tops = {n.split("/")[0] for n in z.namelist()}
            for old in site.glob(pkg.replace("-", "_") + "-*.dist-info"):
                shutil.rmtree(old)
            for top in tops:
                if not top.endswith(".dist-info") and (site / top).is_dir():
                    shutil.rmtree(site / top)
            z.extractall(site, members=[n for n in z.namelist() if not n.split("/")[0].endswith(".data")])
    return True, "updated"


def update_ytdlp():
    """Update yt-dlp (YouTube changes often), then restart this app so the new version loads."""
    if engine.PORTABLE:
        try:
            return update_from_pypi()
        except Exception as e:  # noqa: BLE001
            return False, str(e)
    python = Path(sys.executable).with_name("python.exe")
    r = subprocess.run([str(python), "-m", "pip", "install", "-U", "yt-dlp[default]"],
                       capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return r.returncode == 0, (r.stdout + r.stderr)[-400:]


def restart_app():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pythonw), str(HERE / "downloader.pyw"), "--restart"], cwd=str(HERE))
    threading.Timer(0.5, lambda: os._exit(0)).start()


class Handler(BaseHTTPRequestHandler):
    on_quit = None

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionError, OSError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _local(self):
        if CLOUD:
            return False
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _authorized(self, query):
        if self._local():
            return True
        pin = self.headers.get("X-Pin") or parse_qs(query).get("pin", [""])[0]
        result = Guard.check(pin)
        if result == "ok":
            return True
        self._json(429 if result == "locked" else 403,
                   {"error": "Too many wrong PINs - wait 30 seconds." if result == "locked"
                    else "Wrong PIN." if CLOUD else "Wrong PIN. Scan the QR code on the laptop again.", "needPin": True})
        return False

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    # ---------- GET ----------

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/manifest.json":
            return self._send(200, MANIFEST, "application/manifest+json", cache=True)
        if path in ("/icon.svg", "/favicon.ico"):
            return self._send(200, ICON_SVG, "image/svg+xml", cache=True)
        if path == "/qr.svg" and self._local():
            return self._send(200, qr_svg(phone_url()), "image/svg+xml")

        if not path.startswith("/api/"):
            return self._json(404, {"error": "Not found"})
        if not self._authorized(url.query):
            return

        if path == "/api/hello":
            return self._json(200, {"local": self._local(), "phoneUrl": phone_url() if self._local() else "",
                                    "ffmpeg": bool(engine.ffmpeg_dir()), "folder": "" if CLOUD else str(engine.OUT_DIR),
                                    "setup": engine.SETUP, "cloud": CLOUD})
        if path.startswith("/api/job/"):
            job = engine.JOBS.get(path.rsplit("/", 1)[-1])
            return self._json(200, job) if job else self._json(404, {"error": "Unknown download"})
        if path == "/api/recent":                # on the website everyone's files are private: the page keeps its own list
            return self._json(200, {"files": [] if CLOUD else engine.recent_files()})
        if path == "/api/file":
            return self._file(parse_qs(url.query).get("name", [""])[0])
        self._json(404, {"error": "Not found"})

    def _file(self, name):
        p = engine.safe_file(name)
        if not p:
            return self._json(404, {"error": "File not found (maybe it was moved or deleted)."})
        ascii_name = p.name.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(p.name)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
        except (ConnectionError, OSError):
            pass

    # ---------- POST ----------

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        if not self._authorized(url.query):
            return
        body = self._body()

        if path in ("/api/info", "/api/download") and not engine.tools_ready():
            return self._json(503, {"error": "Still setting up (first time only) - wait for it to finish on the laptop."})
        if path == "/api/info":
            link = (body.get("url") or "").strip()
            if not link.startswith(("http://", "https://")):
                return self._json(400, {"error": "Paste a full link that starts with https://"})
            try:
                return self._json(200, engine.get_info(link))
            except Exception as e:  # noqa: BLE001
                return self._json(400, {"error": engine.friendly_error(e)})
        if path == "/api/download":
            link, option = (body.get("url") or "").strip(), body.get("option") or ""
            is_video = (option[:1] == "v" and option[1:].isdigit()) or \
                (option[:2] == "f:" and 0 < len(option) <= 60 and all(c.isalnum() or c in "-_." for c in option[2:]))
            plain = option in ("photo", "file")              # photos / direct files don't need ffmpeg
            if not link.startswith(("http://", "https://")) or not (is_video or plain or option in {o[0] for o in engine.AUDIO_OPTIONS}):
                return self._json(400, {"error": "Pick a format first."})
            if not plain and not engine.ffmpeg_dir():
                return self._json(400, {"error": "ffmpeg is missing on the laptop. Run: winget install Gyan.FFmpeg"})
            job_id = engine.start_download(link, option, body.get("title", ""), int(body.get("count") or 1),
                                           body.get("label", "") if is_video else "")
            return self._json(200, {"id": job_id})

        # laptop-only actions
        if not self._local():
            return self._json(403, {"error": "Only from the laptop."})
        if path == "/api/open":
            name = body.get("name")
            p = engine.safe_file(name) if name else None
            if p:
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                engine.OUT_DIR.mkdir(parents=True, exist_ok=True)
                os.startfile(engine.OUT_DIR)
            return self._json(200, {"ok": True})
        if path == "/api/setup":
            if engine.SETUP["status"] != "running":
                threading.Thread(target=engine.setup_tools, daemon=True).start()
            return self._json(200, {"ok": True})
        if path == "/api/quit":
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return self._json(200, {"ok": True})
        if path == "/api/update":
            ok, log = update_ytdlp()
            if ok:
                restart_app()
            return self._json(200 if ok else 500, {"ok": ok, "log": log})
        self._json(404, {"error": "Not found"})


class DualStackServer(ThreadingHTTPServer):
    """Listens on IPv4 and IPv6 so both the laptop and the phone can connect."""
    address_family = socket.AF_INET6
    daemon_threads = True
    allow_reuse_address = False          # on Windows "reuse" lets a 2nd copy grab the same port

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):      # Windows only - Linux servers (Render) don't have it
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(retries=20):
    for _ in range(retries):         # after a restart the old copy may still hold the port for a moment
        try:
            return DualStackServer(("::", PORT), Handler)
        except OSError:
            if CLOUD:                    # some containers have no IPv6
                return ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
            time.sleep(0.5)
    return None


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#7c5cff"/>
<path d="M32 14v26m-11-11 11 11 11-11M18 48h28" stroke="#fff" stroke-width="6" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>"""

MANIFEST = json.dumps({
    "name": "Link Downloader", "short_name": "Downloader", "start_url": "/", "display": "standalone",
    "background_color": "#0f1117", "theme_color": "#0f1117",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
})

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Link Downloader</title>
<link rel="manifest" href="/manifest.json"><link rel="icon" href="/icon.svg"><meta name="theme-color" content="#0f1117">
<style>
:root{--bg:#0f1117;--card:#181b24;--card2:#20242f;--line:#2b3040;--text:#eef0f6;--dim:#9aa1b5;--acc:#7c5cff;--acc2:#a28bff;--ok:#2fbf71;--bad:#ff5c6c}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:640px;overflow-x:hidden;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:4px 0 2px;display:flex;align-items:center;gap:10px}
h1 img{width:30px;height:30px}
.sub{color:var(--dim);margin:0 0 18px;font-size:13.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:14px}
.row{display:flex;gap:8px}
input{flex:1;min-width:0;background:var(--card2);border:1px solid var(--line);color:var(--text);border-radius:12px;padding:13px 12px;font-size:15px;outline:none}
input:focus{border-color:var(--acc)}
button{border:0;border-radius:12px;padding:12px 16px;font-size:15px;font-weight:600;cursor:pointer;color:#fff;background:var(--acc)}
button.ghost{background:var(--card2);color:var(--text);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:default}
.go{width:100%;margin-top:10px;padding:14px}
.preview{display:flex;gap:12px;align-items:center}
.preview img{width:120px;aspect-ratio:16/9;object-fit:cover;border-radius:10px;background:var(--card2);flex-shrink:0}
.preview .t{font-weight:600;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.meta{color:var(--dim);font-size:13px;margin-top:4px}
.tabs{display:flex;background:var(--card2);border-radius:12px;padding:4px;margin:14px 0 10px}
.tabs button{flex:1;background:none;color:var(--dim);padding:10px}
.tabs button.on{background:var(--acc);color:#fff}
.opts{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}
.opt{background:var(--card2);border:2px solid transparent;border-radius:12px;padding:10px 12px;text-align:left;color:var(--text);font-weight:400}
.opt b{font-size:17px;display:block}
.opt span{color:var(--dim);font-size:12.5px}
.opt.sel{border-color:var(--acc);background:#251f45}
.bar{height:10px;background:var(--card2);border-radius:6px;overflow:hidden;margin:10px 0 6px}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .3s}
.err{color:var(--bad);margin-top:10px;font-size:14px}
.okmsg{color:var(--ok);font-weight:600}
.hide{display:none!important}
.list a,.list div.item{display:flex;justify-content:space-between;gap:10px;padding:10px 2px;border-bottom:1px solid var(--line);color:var(--text);text-decoration:none;font-size:14px}
.list .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.list .s{color:var(--dim);flex-shrink:0}
.foot{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.qr{text-align:center}
.qr img{width:210px;height:210px;border-radius:12px;background:#fff;padding:6px}
.small{color:var(--dim);font-size:12.5px;word-break:break-all}
.spin{display:inline-block;width:16px;height:16px;border:2px solid #fff6;border-top-color:#fff;border-radius:50%;animation:s .8s linear infinite;vertical-align:-3px}
@keyframes s{to{transform:rotate(360deg)}}
</style></head><body><main>
<h1><img src="/icon.svg" alt="">Link Downloader</h1>
<p class="sub">YouTube, Instagram (reels, posts, photos), Facebook, X, TikTok, 1000+ sites &amp; direct file links</p>

<div id="pinCard" class="card hide">
  <b>Enter the PIN</b><p class="meta">On the laptop: under "Open on phone". Online: ask the person who shared the link.</p>
  <div class="row"><input id="pin" inputmode="numeric" maxlength="8" placeholder="4-digit PIN"><button id="pinBtn">OK</button></div>
  <div id="pinErr" class="err"></div>
</div>

<div id="app">
<div id="setupCard" class="card hide">
  <b>Getting ready (first time only)</b>
  <p class="meta">Downloading the tools that join HD video and make MP3s (about 125 MB). Keep this open.</p>
  <div class="bar"><i id="setupFill"></i></div>
  <div id="setupText" class="meta"></div>
  <button id="setupRetry" class="ghost hide" style="margin-top:8px">Retry</button>
</div>
<div class="card">
  <div class="row">
    <input id="url" type="url" placeholder="Paste a YouTube or Instagram link" autocomplete="off">
    <button id="paste" class="ghost" title="Paste">Paste</button>
  </div>
  <button id="find" class="go">Get options</button>
  <div id="err" class="err"></div>
</div>

<div id="result" class="card hide">
  <div class="preview"><img id="thumb" alt="" referrerpolicy="no-referrer"><div><div id="title" class="t"></div><div id="meta" class="meta"></div></div></div>
  <div class="tabs"><button data-tab="video" class="on">🎬 Video</button><button data-tab="audio">🎵 Audio</button><button data-tab="photo">📷 Photo</button><button data-tab="file">📄 File</button></div>
  <div id="opts" class="opts"></div>
  <button id="dl" class="go" disabled>Download</button>
  <div id="prog" class="hide">
    <div class="bar"><i id="barFill"></i></div>
    <div id="progText" class="meta"></div>
  </div>
  <div id="done" class="hide">
    <p class="okmsg">✓ Downloaded: <span id="doneName"></span></p>
    <div class="foot"><a id="saveBtn"><button>Save to this device</button></a><button id="showBtn" class="ghost hide">Show in folder</button></div>
  </div>
  <div id="dlErr" class="err"></div>
</div>

<div class="card">
  <b>Recent downloads</b>
  <p id="recentNote" class="meta hide">Files stay on the server for 2 hours - save them to your device.</p>
  <div id="recent" class="list"><p class="meta">Nothing yet.</p></div>
  <div class="foot laptop hide" style="margin-top:12px">
    <button class="ghost" id="folderBtn">📂 Open folder</button>
    <button class="ghost" id="phoneBtn">📱 Open on phone</button>
    <button class="ghost" id="updBtn">⟳ Update engine</button>
    <button class="ghost" id="quitBtn">⏻ Quit</button>
  </div>
</div>

<div id="phoneCard" class="card qr hide">
  <b>Scan with your phone camera</b>
  <p class="meta">Phone and laptop must be on the same Wi-Fi / hotspot.</p>
  <img id="qrImg" alt="QR code">
  <p class="small" id="phoneUrl"></p>
  <p class="meta">Tip: in Chrome on the phone, ⋮ → "Add to Home screen" to use it like an app.</p>
</div>
</div>

<script>
const $ = id => document.getElementById(id);
const qs = new URLSearchParams(location.search);
let pin = qs.get("pin") || localStorage.getItem("dl-pin") || "";
if (qs.get("pin")) { localStorage.setItem("dl-pin", pin); history.replaceState(null, "", "/"); }
let info = null, tab = "video", choice = null, isLocal = false, isCloud = false, curUrl = "";

async function api(path, body) {
  const opt = { headers: { "X-Pin": pin } };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt).catch(() => null);
  if (!r) throw new Error("Can't reach the laptop. Is the Downloader running and are you on the same Wi-Fi?");
  const j = await r.json().catch(() => ({}));
  if (j.needPin) { showPin(j.error); throw new Error(j.error); }
  if (!r.ok) throw new Error(j.error || "Something went wrong.");
  return j;
}

function showPin(msg) { $("pinCard").classList.remove("hide"); $("app").classList.add("hide"); $("pinErr").textContent = msg && pin ? msg : ""; }
$("pinBtn").onclick = () => { pin = $("pin").value.trim(); localStorage.setItem("dl-pin", pin); hello(); };
$("pin").onkeydown = e => { if (e.key === "Enter") $("pinBtn").click(); };

async function hello() {
  try {
    const j = await api("/api/hello");
    $("pinCard").classList.add("hide"); $("app").classList.remove("hide");
    isLocal = j.local; isCloud = j.cloud;
    if (isCloud) $("recentNote").classList.remove("hide");
    document.querySelectorAll(".laptop").forEach(el => el.classList.toggle("hide", !isLocal));
    if (isLocal) $("phoneUrl").textContent = j.phoneUrl;
    showSetup(j.setup);
    if (!j.ffmpeg && j.setup.status === "done") $("err").textContent = "ffmpeg is missing on the laptop - HD video and MP3 need it. Run: winget install Gyan.FFmpeg";
    loadRecent();
  } catch (e) {}
}

$("paste").onclick = async () => {
  try { $("url").value = (await navigator.clipboard.readText()).trim(); find(); }
  catch { $("url").focus(); $("err").textContent = "Long-press the box and choose Paste."; }
};
$("url").onkeydown = e => { if (e.key === "Enter") find(); };
$("url").addEventListener("paste", () => setTimeout(find, 50));
$("find").onclick = find;

function fmtTime(s) { if (!s) return ""; s = Math.round(s); const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return (h ? h + ":" + String(m).padStart(2,"0") : m) + ":" + String(x).padStart(2,"0"); }

async function find() {
  const url = $("url").value.trim();
  if (!url || url === curUrl && info) return;
  $("err").textContent = ""; $("result").classList.add("hide");
  $("find").disabled = true; $("find").innerHTML = '<span class="spin"></span> Looking up the link…';
  try {
    info = await api("/api/info", { url }); curUrl = url;
    $("thumb").src = info.thumbnail || "/icon.svg";
    $("title").textContent = info.title;
    $("meta").textContent = [info.site, info.uploader, fmtTime(info.duration), info.count > 1 ? info.count + " items" : ""].filter(Boolean).join(" · ");
    const kinds = ["video", "audio", "photo", "file"];
    document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("hide", !(info[b.dataset.tab] || []).length));
    tab = kinds.find(k => (info[k] || []).length) || "video"; choice = null;
    renderOpts(); resetDl();
    $("result").classList.remove("hide");
  } catch (e) { info = null; curUrl = ""; $("err").textContent = e.message; }
  $("find").disabled = false; $("find").textContent = "Get options";
}

document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => { tab = b.dataset.tab; choice = null; renderOpts(); });

function renderOpts() {
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  const list = info[tab] || [];
  $("opts").innerHTML = "";
  if (!list.length) { $("opts").innerHTML = '<p class="meta">No ' + tab + ' in this link.</p>'; $("dl").disabled = true; }
  // default pick: 1080p if it exists, otherwise the best one (video); MP3 320 (audio)
  const def = Math.max(0, list.findIndex(o => o.label === "1080p"));
  list.forEach((o, i) => {
    const b = document.createElement("button");
    b.className = "opt"; b.innerHTML = `<b></b><span></span>`;
    b.querySelector("b").textContent = o.label + " " + o.ext;
    b.querySelector("span").textContent = [o.tag, o.size].filter(Boolean).join(" · ");
    b.onclick = () => { choice = o; document.querySelectorAll(".opt").forEach(x => x.classList.remove("sel")); b.classList.add("sel"); $("dl").disabled = false;
      $("dl").textContent = "Download " + o.label + " " + o.ext; };
    $("opts").appendChild(b);
    if (i === def) b.click();
  });
  if (!list.length) $("dl").disabled = true;
}

function resetDl() { $("prog").classList.add("hide"); $("done").classList.add("hide"); $("dlErr").textContent = ""; $("dl").classList.remove("hide"); }

$("dl").onclick = async () => {
  if (!choice) return;
  resetDl(); $("dl").disabled = true;
  $("prog").classList.remove("hide"); $("barFill").style.width = "0"; $("progText").textContent = "Starting…";
  try {
    const { id } = await api("/api/download", { url: curUrl, option: choice.id, label: choice.label, title: info.title, count: info.count });
    poll(id);
  } catch (e) { $("prog").classList.add("hide"); $("dlErr").textContent = e.message; $("dl").disabled = false; }
};

async function poll(id) {
  let j;
  try { j = await api("/api/job/" + id); } catch (e) { setTimeout(() => poll(id), 2000); return; }
  $("barFill").style.width = j.percent + "%";
  if (j.status === "downloading") $("progText").textContent = `${Math.floor(j.percent)}%  ·  ${j.speed || ""}  ·  ${j.eta ? j.eta + " left" : ""}`;
  else if (j.status === "processing") $("progText").textContent = "Finishing up (joining video + audio / converting)…";
  else if (j.status === "queued") $("progText").textContent = "Waiting - other downloads are running, yours is next…";
  if (j.status === "done") return finished(j);
  if (j.status === "error") { $("prog").classList.add("hide"); $("dlErr").textContent = j.error; $("dl").disabled = false; return; }
  setTimeout(() => poll(id), 700);
}

function fileLink(name) { return "/api/file?name=" + encodeURIComponent(name) + (isLocal ? "" : "&pin=" + encodeURIComponent(pin)); }

function finished(j) {
  $("prog").classList.add("hide"); $("dl").disabled = false;
  $("done").classList.remove("hide");
  $("doneName").textContent = j.file + " (" + j.size + ")";
  $("saveBtn").href = fileLink(j.file); $("saveBtn").setAttribute("download", j.file);
  $("showBtn").classList.toggle("hide", !isLocal);
  $("showBtn").onclick = () => api("/api/open", { name: j.file });
  $("saveBtn").classList.toggle("hide", isLocal);       // on the laptop it's already in Downloads\Downloader
  if (!isLocal) $("saveBtn").click();                    // on the phone: save straight to the phone
  if (isCloud) { const mine = myFiles().filter(f => f.name !== j.file); mine.unshift({ name: j.file, size: j.size });
    try { localStorage.setItem("dl-mine", JSON.stringify(mine.slice(0, 15))); } catch {} }
  loadRecent();
}

function myFiles() { try { return JSON.parse(localStorage.getItem("dl-mine") || "[]"); } catch { return []; } }

async function loadRecent() {
  try {
    const files = isCloud ? myFiles() : (await api("/api/recent")).files;
    $("recent").innerHTML = files.length ? "" : '<p class="meta">Nothing yet.</p>';
    files.forEach(f => {
      const a = document.createElement("a");
      a.href = fileLink(f.name); a.setAttribute("download", f.name);
      if (isLocal) a.onclick = e => { e.preventDefault(); api("/api/open", { name: f.name }); };
      a.innerHTML = '<span class="n"></span><span class="s"></span>';
      a.querySelector(".n").textContent = (/\.(mp3|m4a)$/i.test(f.name) ? "🎵 " : /\.zip$/i.test(f.name) ? "🗂 " : "🎬 ") + f.name;
      a.querySelector(".s").textContent = f.size + (isLocal ? "" : " ⬇");
      $("recent").appendChild(a);
    });
  } catch (e) {}
}

function showSetup(st) {
  const busy = st.status !== "done";
  $("setupCard").classList.toggle("hide", !busy);
  $("find").disabled = busy;
  if (!busy) { $("err").textContent = ""; return; }
  $("setupFill").style.width = (st.percent || 0) + "%";
  $("setupText").textContent = st.status === "error" ? st.error : (st.step || "Starting…") + (st.percent ? " · " + st.percent + "%" : "");
  $("setupText").style.color = st.status === "error" ? "var(--bad)" : "";
  $("setupRetry").classList.toggle("hide", st.status !== "error" || !isLocal);
  if (st.status !== "error") setTimeout(async () => { try { showSetup((await api("/api/hello")).setup); } catch {} }, 1000);
}
$("setupRetry").onclick = async () => { await api("/api/setup", {}); showSetup({ status: "running", percent: 0 }); };
$("quitBtn").onclick = async () => { if (confirm("Close Link Downloader?")) { await api("/api/quit", {}).catch(() => {}); document.body.innerHTML = '<main><h1>Link Downloader is closed.</h1><p class="sub">Open it again from the Desktop shortcut.</p></main>'; } };
$("folderBtn").onclick = () => api("/api/open", {});
$("phoneBtn").onclick = () => { $("qrImg").src = "/qr.svg?t=" + Date.now(); $("phoneCard").classList.toggle("hide"); $("phoneCard").scrollIntoView({ behavior: "smooth" }); };
$("updBtn").onclick = async () => {
  $("updBtn").disabled = true; $("updBtn").innerHTML = '<span class="spin"></span> Updating…';
  try { await api("/api/update", {}); $("updBtn").textContent = "✓ Updated, restarting…"; setTimeout(() => location.reload(), 4000); }
  catch (e) { $("updBtn").textContent = "Update failed"; $("err").textContent = e.message; }
};
if (location.hash === "#phone") setTimeout(() => $("phoneBtn").click(), 300);
hello().then(() => { if (qs.get("url")) { $("url").value = qs.get("url"); find(); } });
</script></main></body></html>"""
