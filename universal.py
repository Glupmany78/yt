#!/usr/bin/env python3
"""
universal.py — yt-dlp powered downloader with a local web UI and progress bar.

usage:
    python3 universal.py --server [--host 127.0.0.1] [--port 8765]
    python3 universal.py --update              # update yt-dlp and exit
    python3 universal.py <url> [-q 1080p] [-o DIR] [-c COOKIES] [-p PROXY]
    DOWNLOADER_DEBUG=1 python3 universal.py --server   # verbose yt-dlp output
    YT_CLIENTS=default,mweb,tv_simply python3 universal.py --server  # override player clients

First run creates a local .venv and installs yt-dlp + flask.
ffmpeg is required for merging high-res video+audio and for MP3.
Install with:  sudo apt install ffmpeg   (then restart)
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
VENV_DIR = SCRIPT_DIR / ".venv"
IS_WINDOWS = os.name == "nt"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")

REQUIRED = ["yt-dlp", "flask"]
DEBUG = os.environ.get("DOWNLOADER_DEBUG") == "1"


# ══════════════════════════════════════════════════════════════════════
#  venv bootstrap
# ══════════════════════════════════════════════════════════════════════

def _in_venv() -> bool:
    return sys.prefix != sys.base_prefix


def _deps_ok() -> bool:
    if not VENV_PYTHON.exists():
        return False
    r = subprocess.run(
        [str(VENV_PYTHON), "-c", "import yt_dlp, flask"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return r.returncode == 0


def _bootstrap():
    if not VENV_PYTHON.exists():
        print("[*] creating virtual environment (first run, one-time)...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    if not _deps_ok():
        print("[*] installing dependencies (one-time)...")
        subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip", "-q"],
            check=True,
        )
        subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "-q"] + REQUIRED,
            check=True,
        )
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), __file__] + sys.argv[1:])


if not _in_venv():
    _bootstrap()


# ══════════════════════════════════════════════════════════════════════
#  imports (after bootstrap)
# ══════════════════════════════════════════════════════════════════════

import re
import time
import secrets
import argparse
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory


VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv",
    ".m4v", ".ts", ".mpg", ".mpeg", ".3gp",
}

# yt-dlp's own muxed formats on YouTube. When we download one of these,
# we know the user didn't get the resolution they asked for (unless they
# asked for 360p).
YOUTUBE_MUXED_FORMATS = {"17", "18", "22", "36", "59", "78", "82", "83", "84", "85"}


# ══════════════════════════════════════════════════════════════════════
#  helpers
# ══════════════════════════════════════════════════════════════════════

def _ffmpeg_path():
    return shutil.which("ffmpeg")


def _check_ffmpeg() -> bool:
    path = _ffmpeg_path()
    if path:
        print(f"[*] ffmpeg: {path}", flush=True)
        return True
    print("", flush=True)
    print("[!] ─────────────────────────────────────────────────────────", flush=True)
    print("[!] ffmpeg NOT FOUND on PATH.", flush=True)
    print("[!] Without ffmpeg, yt-dlp CANNOT merge 1080p video + audio.", flush=True)
    print("[!] Downloads will silently fall back to the 720p muxed stream.", flush=True)
    print("[!] Fix:  sudo apt install ffmpeg   (then restart this server)", flush=True)
    print("[!] ─────────────────────────────────────────────────────────", flush=True)
    print("", flush=True)
    return False


def _check_ytdlp_version():
    try:
        ver = yt_dlp.version.__version__
        print(f"[*] yt-dlp: {ver}", flush=True)
        try:
            release = datetime.strptime(ver, "%Y.%m.%d")
            age = (datetime.now() - release).days
            if age > 30:
                print(f"[!] yt-dlp is {age} days old — run with --update to refresh.",
                      flush=True)
        except (ValueError, TypeError):
            pass
    except Exception:
        pass


def _update_ytdlp():
    print("[*] Updating yt-dlp (this helps with YouTube format availability)...", flush=True)
    r = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "yt-dlp"],
        check=False, timeout=180,
    )
    if r.returncode == 0:
        r2 = subprocess.run(
            [str(VENV_PYTHON), "-c", "import yt_dlp; print(yt_dlp.version.__version__)"],
            capture_output=True, text=True, check=False,
        )
        if r2.returncode == 0:
            print(f"[*] yt-dlp now: {r2.stdout.strip()}", flush=True)
    else:
        print("[!] yt-dlp update failed (network or permissions).", flush=True)


def _is_youtube(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(n in host for n in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def _youtube_extractor_args():
    """
    Return extractor_args for YouTube, or None to use yt-dlp's defaults.

    By default we let yt-dlp pick its own client list — the defaults
    track the yt-dlp version, so a freshly-updated yt-dlp gets the
    freshest clients without us having to maintain a list.

    Override with: YT_CLIENTS="default,mweb,tv_simply,web_safari"
    """
    override = os.environ.get("YT_CLIENTS", "").strip()
    if override:
        clients = [c.strip() for c in override.split(",") if c.strip()]
        if clients:
            return {"youtube": {"player_client": clients}}
    return None


def _format_bytes(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def _web_extract_info(url, cookies_file=None, proxy=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "socket_timeout": 30,
        "retries": 3,
    }
    # IMPORTANT: use the SAME extractor_args as the download path, so
    # /api/info shows exactly what the download will be able to fetch.
    if _is_youtube(url):
        ext = _youtube_extractor_args()
        if ext:
            opts["extractor_args"] = ext
    if cookies_file and Path(cookies_file).is_file():
        opts["cookiefile"] = cookies_file
    if proxy:
        opts["proxy"] = proxy
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return {}
    entries = info.get("entries") if isinstance(info, dict) else None
    if entries:
        info = next((e for e in entries if e), None)
    return info if isinstance(info, dict) else {}


def _web_quality_options(info):
    formats = info.get("formats") or []
    heights = set()
    has_video = False
    has_audio_only = False

    for f in formats:
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        h = f.get("height") or 0
        if vcodec and vcodec != "none":
            has_video = True
            if h > 0:
                heights.add(int(h))
        elif acodec and acodec != "none":
            has_audio_only = True

    if info.get("height") and info.get("vcodec") not in (None, "none"):
        has_video = True
        try:
            heights.add(int(info["height"]))
        except (TypeError, ValueError):
            pass

    options = []
    for h in sorted(heights, reverse=True):
        options.append({"value": f"{h}p", "label": f"{h}p"})
    if has_video and not options:
        options.append({"value": "best", "label": "Best available"})
    if has_video or has_audio_only:
        options.append({"value": "mp3", "label": "MP3 Audio"})
    return options


def _resolve_format_string(quality, for_youtube=False):
    """
    Format selector chain:
      1. exact height, mp4/h264 (most compatible)
      2. exact height, any codec (VP9/AV1)
      3. closest adaptive height <= requested
      4. muxed fallback (last resort — audio+video in one file)

    The muxed fallback is essential: if YouTube only serves fmt=18
    (360p muxed), options 1-3 match nothing and yt-dlp errors with
    "Requested format is not available". Option 4 lets the download
    succeed, and we warn loudly that quality was downgraded.
    """
    q = (quality or "").strip().lower()
    if q in ("mp3", "audio", "m4a"):
        return "bestaudio/best"
    if q in ("best", "highest"):
        return "bestvideo+bestaudio/best"
    if q in ("worst", "lowest"):
        return "worstvideo+worstaudio/worst"

    m = re.fullmatch(r"(\d{2,4})p?", q)
    if m:
        h = int(m.group(1))
        return (
            f"bestvideo[height={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height={h}]+bestaudio/"
            f"bestvideo[height<={h}]+bestaudio/"
            f"best[height<={h}]"
        )
    return "bestvideo+bestaudio/best"


def _build_ydl_opts(outtmpl, format_str, cookies_file=None, proxy=None,
                    with_mp3_pp=False, for_youtube=False, interactive=False,
                    url=None):
    opts = {
        "outtmpl": outtmpl,
        "format": format_str,

        # Prefer h264 → then larger files (higher bitrate). Without this,
        # yt-dlp's default picks AV1/VP9 which many players render softer.
        "format_sort": ["res", "vcodec:h264", "acodec:m4a", "filesize"],

        # mp4 for h264/aac, mkv for VP9/opus. Remux only, never transcode.
        "merge_output_format": "mp4/mkv",

        "quiet": not (DEBUG or interactive),
        "no_warnings": not (DEBUG or interactive),
        "noprogress": not (DEBUG or interactive),
        "verbose": DEBUG,
        "noplaylist": True,
        "windowsfilenames": True,
        "restrictfilenames": True,

        # Robustness
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 10,
        "concurrent_fragment_downloads": 6,
        "http_chunk_size": 5 * 1024 * 1024,
        "skip_unavailable_fragments": True,
        "continuedl": True,
        "overwrites": False,
    }

    if for_youtube or (url and _is_youtube(url)):
        ext = _youtube_extractor_args()
        if ext:
            opts["extractor_args"] = ext

    if cookies_file and Path(cookies_file).is_file():
        opts["cookiefile"] = cookies_file
    if proxy:
        opts["proxy"] = proxy

    if with_mp3_pp:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    return opts


# ══════════════════════════════════════════════════════════════════════
#  Local web UI + API
# ══════════════════════════════════════════════════════════════════════

def run_server(host="127.0.0.1", port=8765, output_dir="./downloads",
               cookies_file=None, proxy=None):
    app = Flask(__name__, static_folder=None)
    download_root = Path(output_dir).expanduser().resolve()
    download_root.mkdir(parents=True, exist_ok=True)

    info_cache = {}
    file_cache = {}
    progress_cache = {}
    cache_lock = threading.Lock()

    @app.route("/")
    def home():
        return send_from_directory(str(SCRIPT_DIR), "index.html")

    @app.route("/style.css")
    def stylesheet():
        return send_from_directory(str(SCRIPT_DIR), "style.css")

    @app.route("/script.js")
    def javascript():
        return send_from_directory(str(SCRIPT_DIR), "script.js")

    @app.route("/api/health")
    def health():
        return jsonify({"ok": True, "service": "yt-dlp-downloader"})

    @app.route("/api/info", methods=["POST"])
    def inspect_url():
        payload = request.get_json(silent=True) or {}
        url = str(payload.get("url") or "").strip()
        parsed = urlparse(url)
        if (parsed.scheme not in ("http", "https")
                or not parsed.netloc
                or parsed.username
                or parsed.password):
            return jsonify({"error": "Enter a valid http:// or https:// URL."}), 400

        try:
            info = _web_extract_info(url, cookies_file, proxy)
        except Exception as exc:
            print(f"[web-api] inspect failed: {type(exc).__name__}: {exc}", flush=True)
            return jsonify({"error": f"Could not read this link: {exc}"}), 422

        if not info:
            return jsonify({"error": "No media information found for this link."}), 422

        qualities = _web_quality_options(info)
        if not qualities:
            return jsonify({"error": "No downloadable video or audio formats found."}), 422

        token = secrets.token_urlsafe(24)
        title = str(info.get("title") or info.get("id") or "Video")[:250]

        available_heights = sorted(
            {int(f["height"]) for f in (info.get("formats") or [])
             if f.get("vcodec") and f["vcodec"] != "none"
             and f.get("acodec") == "none"
             and f.get("height")},
            reverse=True,
        )

        response = {
            "token": token,
            "title": title,
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "formats": qualities,
        }
        with cache_lock:
            info_cache[token] = {
                "url": url,
                "title": title,
                "qualities": {o["value"] for o in qualities},
                "available_heights": available_heights,
                "created": time.time(),
            }
        return jsonify(response)

    @app.route("/api/download", methods=["POST"])
    def download_selected():
        payload = request.get_json(silent=True) or {}
        token = str(payload.get("token") or "")
        quality = str(payload.get("quality") or "")

        with cache_lock:
            cached = info_cache.get(token)
        if not cached:
            return jsonify({"error": "This link expired. Fetch it again."}), 400
        if quality not in cached["qualities"]:
            return jsonify({"error": "That format is not available for this link."}), 400

        is_yt = _is_youtube(cached["url"])
        req_h = None
        if quality.endswith("p"):
            try:
                req_h = int(quality[:-1])
            except ValueError:
                req_h = None

        # Hard-fail if the user picked a resolution above 720p on YouTube
        # and ffmpeg is missing (otherwise the merge silently downgrades).
        if is_yt and req_h and req_h > 720 and not _ffmpeg_path():
            return jsonify({
                "error": f"ffmpeg is required for {req_h}p on YouTube. "
                         f"Install with: sudo apt install ffmpeg, then restart."
            }), 500

        # Pre-flight: if the highest *adaptive* video-only format is lower
        # than the requested height, the merge will fall back to a muxed
        # stream. Tell the user up front instead of letting them wait.
        adaptive_heights = cached.get("available_heights") or []
        if req_h and adaptive_heights and max(adaptive_heights) < req_h:
            max_h = max(adaptive_heights)
            return jsonify({
                "error": f"YouTube is only serving up to {max_h}p as an "
                         f"adaptive stream to this client. You requested "
                         f"{req_h}p.\n\n"
                         f"To fix: run `python3 universal.py --update` "
                         f"(updates yt-dlp), or pass a cookies file with "
                         f"--cookies, then restart and try again."
            }), 400

        job_token = secrets.token_hex(10)
        outtmpl = str(download_root / f"{job_token}_%(title).120B.%(ext)s")
        format_str = _resolve_format_string(quality, for_youtube=is_yt)

        opts = _build_ydl_opts(
            outtmpl=outtmpl,
            format_str=format_str,
            cookies_file=cookies_file,
            proxy=proxy,
            with_mp3_pp=(quality == "mp3"),
            for_youtube=is_yt,
            url=cached["url"],
        )

        with cache_lock:
            progress_cache[job_token] = {
                "status": "queued", "percent": 0.0,
                "downloaded": 0, "total": 0, "speed": 0, "eta": 0,
                "filename": "", "error": None, "download_url": None,
                "warning": None,
                "_tracked_total": 0,
                "created": time.time(),
            }

        print(f"[run] {quality} → {format_str}", flush=True)
        print(f"[run] url: {cached['url']}", flush=True)

        def _hook(d):
            try:
                status = d.get("status")
                with cache_lock:
                    job = progress_cache.get(job_token)
                    if not job:
                        return

                    if status == "downloading":
                        downloaded = int(d.get("downloaded_bytes") or 0)
                        total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
                        frag_idx = d.get("fragment_index")
                        frag_count = d.get("fragment_count")

                        if frag_count and frag_idx is not None and frag_count > 0:
                            pct = float(frag_idx) / float(frag_count) * 100.0
                        elif total > 0:
                            pct = float(downloaded) / float(total) * 100.0
                        else:
                            pct = float(job.get("percent", 0.0))

                        tracked = job.get("_tracked_total", 0)
                        if total > 0 and tracked > 0 and total < tracked:
                            return
                        if total > 0:
                            job["_tracked_total"] = total

                        speed = d.get("speed") or 0
                        if not isinstance(speed, (int, float)) or speed != speed:
                            speed = 0
                        eta = d.get("eta") or 0
                        if not isinstance(eta, (int, float)) or eta != eta:
                            eta = 0

                        job.update({
                            "status": "downloading",
                            "percent": round(min(pct, 98.0), 2),
                            "downloaded": downloaded,
                            "total": total,
                            "speed": int(speed),
                            "eta": int(eta),
                            "filename": str(d.get("filename") or job.get("filename", "")),
                        })
                        print(
                            f"[hook] {job['percent']:5.2f}%  "
                            f"{_format_bytes(downloaded)}/{_format_bytes(total)}  "
                            f"frag={frag_idx}/{frag_count}",
                            flush=True,
                        )

                    elif status == "finished":
                        info = d.get("info_dict") or {}
                        w = info.get("width")
                        h = info.get("height")
                        vcodec = info.get("vcodec") or "?"
                        acodec = info.get("acodec") or "?"
                        fid = str(info.get("format_id") or "?")
                        tbr = info.get("tbr") or 0
                        fsize = info.get("filesize") or info.get("filesize_approx") or 0

                        print(
                            f"[hook] ✓ {w}x{h} | v={vcodec} a={acodec} | "
                            f"{_format_bytes(fsize)} | {int(tbr)} kbps | fmt={fid}",
                            flush=True,
                        )

                        # Warn loudly when the actual resolution is lower than
                        # what the user asked for. This is the "low quality"
                        # complaint, surfaced instead of hidden.
                        is_muxed = fid in YOUTUBE_MUXED_FORMATS
                        is_short = bool(req_h and h and int(h) < req_h)
                        if is_short or (is_muxed and req_h and req_h > 360):
                            warning = (
                                f"You requested {req_h}p but got "
                                f"{h or '?'}p (muxed format {fid}). "
                                f"YouTube is only serving the 360p/720p muxed "
                                f"stream to this yt-dlp client. Fix: run "
                                f"`python3 universal.py --update` to refresh "
                                f"yt-dlp, or provide a cookies file with "
                                f"--cookies, then try again."
                            )
                            print(f"[hook] ⚠️  QUALITY WARNING: {warning}", flush=True)
                            job["warning"] = warning

                        current = float(job.get("percent", 0.0) or 0.0)
                        job["percent"] = max(current, 50.0)
            except Exception as e:
                print(f"[hook] error: {e}", flush=True)

        opts["progress_hooks"] = [_hook]

        def _run():
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([cached["url"]])

                with cache_lock:
                    if job_token in progress_cache:
                        progress_cache[job_token].update({
                            "status": "processing", "percent": 99.0,
                        })

                generated = [
                    p for p in download_root.glob(f"{job_token}_*")
                    if p.is_file() and not p.name.endswith((".part", ".ytdl"))
                ]
                if quality == "mp3":
                    generated = [p for p in generated if p.suffix.lower() == ".mp3"]
                else:
                    generated = [p for p in generated if p.suffix.lower() in VIDEO_SUFFIXES]
                if not generated:
                    raise RuntimeError("The downloader finished but no output file was found.")

                result_path = max(generated, key=lambda p: p.stat().st_mtime_ns)
                file_token = secrets.token_urlsafe(24)
                with cache_lock:
                    file_cache[file_token] = {
                        "path": str(result_path), "created": time.time(),
                    }
                    if job_token in progress_cache:
                        progress_cache[job_token].update({
                            "status": "done",
                            "percent": 100.0,
                            "download_url": f"/api/file/{file_token}",
                            "filename": result_path.name,
                        })
                print(f"[run] done: {result_path.name} "
                      f"({_format_bytes(result_path.stat().st_size)})", flush=True)
            except Exception as exc:
                msg = str(exc)
                if quality == "mp3" and (
                        "ffmpeg" in msg.lower() or "postprocessor" in msg.lower()):
                    msg = "MP3 conversion requires FFmpeg. Install ffmpeg and restart."
                if "Requested format is not available" in msg:
                    msg = (
                        "No matching format found for this resolution. "
                        "YouTube may be restricting formats for this video. "
                        "Try: (1) run `python3 universal.py --update`, "
                        "(2) provide cookies with --cookies, or "
                        "(3) pick a lower resolution in the UI."
                    )
                print(f"[run] FAILED: {type(exc).__name__}: {exc}", flush=True)
                with cache_lock:
                    if job_token in progress_cache:
                        progress_cache[job_token].update({
                            "status": "error",
                            "error": (msg or "Download failed.")[:500],
                        })

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"job_token": job_token})

    @app.route("/api/progress/<job_token>")
    def get_progress(job_token):
        with cache_lock:
            job = progress_cache.get(job_token)
            if not job:
                return jsonify({"error": "Unknown job."}), 404
            snap = dict(job)
            if len(progress_cache) > 50:
                now = time.time()
                for k in [k for k, v in progress_cache.items()
                          if now - v.get("created", 0) > 3600]:
                    progress_cache.pop(k, None)
        return jsonify(snap)

    @app.route("/api/file/<file_token>")
    def serve_download(file_token):
        with cache_lock:
            item = file_cache.get(file_token)
        if not item:
            return jsonify({"error": "Download link not found or expired."}), 404
        path = Path(item["path"]).resolve()
        if not path.is_file() or path.parent != download_root:
            return jsonify({"error": "The downloaded file is no longer available."}), 404
        return send_file(path, as_attachment=True,
                         download_name=path.name, conditional=True)

    print(f"\n[web] Open http://{host}:{port} in your browser")
    print(f"[web] Download folder: {download_root}")
    print("[web] Local-only by default. Press Ctrl+C to stop.\n")
    app.run(host=host, port=port, debug=False, threaded=True)


# ══════════════════════════════════════════════════════════════════════
#  CLI mode
# ══════════════════════════════════════════════════════════════════════

def run_cli(url, quality="best", output_dir="./downloads",
            cookies_file=None, proxy=None):
    out_root = Path(output_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    is_yt = _is_youtube(url)
    req_h = None
    if quality.endswith("p"):
        try:
            req_h = int(quality[:-1])
        except ValueError:
            req_h = None

    needs_ffmpeg = (quality == "mp3") or (is_yt and req_h and req_h > 720)
    if needs_ffmpeg and not _ffmpeg_path():
        print("[!] ffmpeg is required for this download.", flush=True)
        print("[!] Install with: sudo apt install ffmpeg", flush=True)
        sys.exit(1)

    format_str = _resolve_format_string(quality, for_youtube=is_yt)
    outtmpl = str(out_root / "%(title).120B.%(ext)s")

    opts = _build_ydl_opts(
        outtmpl=outtmpl,
        format_str=format_str,
        cookies_file=cookies_file,
        proxy=proxy,
        with_mp3_pp=(quality == "mp3"),
        for_youtube=is_yt,
        interactive=True,
        url=url,
    )

    def _hook(d):
        if d.get("status") == "finished":
            info = d.get("info_dict") or {}
            w = info.get("width")
            h = info.get("height")
            if h:
                v = info.get("vcodec") or "?"
                a = info.get("acodec") or "?"
                fid = info.get("format_id") or "?"
                fsize = info.get("filesize") or info.get("filesize_approx") or 0
                print(
                    f"[hook] ✓ {w}x{h} | v={v} a={a} | "
                    f"{_format_bytes(fsize)} | fmt={fid}",
                    flush=True,
                )
                if req_h and int(h) < req_h:
                    print(
                        f"[hook] ⚠️  QUALITY WARNING: requested {req_h}p, got {h}p. "
                        f"Try `--update` or `--cookies`.", flush=True,
                    )

    opts["progress_hooks"] = [_hook]

    print(f"[run] {quality} → {format_str}", flush=True)
    print(f"[run] url: {url}", flush=True)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        print(f"[run] FAILED: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)

    print("[run] done", flush=True)


# ══════════════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════════════

def main():
    global DEBUG

    p = argparse.ArgumentParser(
        description="yt-dlp powered downloader (local web UI or CLI).",
    )
    p.add_argument("url", nargs="?", help="Video/audio URL (CLI mode)")
    p.add_argument("--server", action="store_true", help="Run the local web UI")
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="Bind port (default 8765)")
    p.add_argument("-q", "--quality", default="best",
                   help="Quality for CLI mode (1080p, 720p, mp3, best, worst)")
    p.add_argument("-o", "--output-dir", default="./downloads",
                   help="Download folder (default ./downloads)")
    p.add_argument("-c", "--cookies", default=None,
                   help="Path to cookies.txt (for age-restricted / members-only)")
    p.add_argument("-p", "--proxy", default=None,
                   help="Proxy URL, e.g. http://127.0.0.1:8080")
    p.add_argument("--update", action="store_true",
                   help="Update yt-dlp to the latest version and exit")
    p.add_argument("--debug", action="store_true", help="Verbose yt-dlp output")
    args = p.parse_args()

    if args.debug:
        DEBUG = True

    if args.update:
        _update_ytdlp()
        sys.exit(0)

    _check_ytdlp_version()

    if args.server:
        _check_ffmpeg()
        run_server(
            host=args.host, port=args.port,
            output_dir=args.output_dir,
            cookies_file=args.cookies, proxy=args.proxy,
        )
    elif args.url:
        run_cli(
            args.url,
            quality=args.quality,
            output_dir=args.output_dir,
            cookies_file=args.cookies, proxy=args.proxy,
        )
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()