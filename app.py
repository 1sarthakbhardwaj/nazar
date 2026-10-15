#!/usr/bin/env python3
"""Nazar: an AI camera that watches any video source and messages you when something goes wrong.

Dashboard (demo and live):
  python3 app.py --source clip.mp4 --start 12 --end 30    play just that part of a clip
  python3 app.py --source 0                               your webcam
  python3 app.py --source rtsp://user:pass@cam/stream     a CCTV camera
  python3 app.py --profile shop --source ...              what to watch for (see ai.PROFILES)
  python3 app.py --source rtsp://... --headless           no dashboard, alerts only (for a server)

Live cameras:
  python3 app.py --brand tapo --ip 192.168.1.20           a home camera (user/password from .env)
  python3 app.py watch                                    every camera in cameras.json at once, headless
  python3 app.py cameras                                  stream addresses for Tapo, Hikvision, CP Plus, Reolink...
  python3 app.py probe --source rtsp://...                check a stream works and save a frame (no AI calls)
  python3 app.py discover                                 find ONVIF cameras on your network

Tools:
  python3 app.py fetch                      download real demo clips (GMDCSA-24, CC BY 4.0) with fall times
  python3 app.py find --source clip.mp4     list the moments with motion and the --start to use (no AI calls)
  python3 app.py once --source rtsp://...   one short look, prints only on an incident (Hermes cron)
  python3 app.py report                     today's checks and alerts (Hermes skill)
  python3 app.py check                      check keys, quota and Telegram (no model calls)
  python3 app.py telegram                   find your Telegram chat id
  python3 app.py smoke --source clip.mp4     one real Nemotron call on a few seconds of it (uses 1 call)
  python3 app.py hermes                     install the Hermes Agent skill and cron script
"""
import argparse
import json
import os
import queue
import secrets
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import config

try:
    import cv2  # noqa: F401
    import numpy  # noqa: F401
except ImportError:
    sys.exit("Missing packages. Run: python3 -m pip install -r requirements.txt")

import ai  # noqa: E402
import alerts  # noqa: E402
from engine import Engine, Source, ffmpeg_exe, fit, judge, log_verdict, motion_between, stamp, tiny_grey  # noqa: E402
from net import error_text, http  # noqa: E402

WEB = config.ROOT / "web"


# ------------------------------------------------------------------ dashboard server

def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Nazar/1.0"

        def log_message(self, *args):
            pass

        def send_json(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/stream.mjpg":
                return self.stream()
            if path == "/events":
                return self.events()
            if path == "/api/status":
                return self.send_json(200, engine.status())
            if path.startswith("/data/"):
                base, rel = config.DATA, path[len("/data/"):]
                if not rel.startswith(("sheets/", "clips/")):
                    return self.send_json(404, {"error": "not found"})
            else:
                base, rel = WEB, "index.html" if path == "/" else path.lstrip("/")
            target = (base / rel).resolve()
            if base.resolve() not in target.parents or not target.is_file():
                return self.send_json(404, {"error": "not found"})
            data = target.read_bytes()
            types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".jpg": "image/jpeg",
                     ".mp4": "video/mp4", ".svg": "image/svg+xml", ".png": "image/png"}
            self.send_response(200)
            self.send_header("Content-Type", types.get(target.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            name = urlparse(self.path).path.removeprefix("/api/")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                body = {}
            if name in ("arm", "disarm", "check", "restart", "pause"):
                engine.command(name)
            elif name == "trigger":
                engine.command("trigger", float(body.get("value", config.MOTION_TRIGGER)))
            else:
                return self.send_json(404, {"error": "not found"})
            self.send_json(200, {"ok": True})

        def stream(self):
            """MJPEG: the frames the engine is watching, about 20 a second."""
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = -1
            try:
                while True:
                    with engine.jpeg_cond:
                        engine.jpeg_cond.wait_for(lambda: engine.jpeg_seq != seq, timeout=5)
                        jpg, seq = engine.jpeg, engine.jpeg_seq
                    if jpg:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def events(self):
            """Server-sent events: motion, phases, checks, verdicts and alerts."""
            q = engine.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(f"data: {json.dumps(engine.status())}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                    except queue.Empty:
                        msg = None
                    self.wfile.write(f"data: {msg}\n\n".encode() if msg else b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                engine.unsubscribe(q)

    return Handler


def serve(args):
    if not ai.ready():
        print("OPENROUTER_API_KEY is missing. Copy .env.example to .env and add your key (or set PROVIDER=ollama).")
        return 1
    try:
        src = Source(args.source, args.start, args.end, args.loop)
    except Exception as e:
        print(f"Can't open the video source: {e}")
        clips = sorted(str(p.relative_to(config.ROOT)) for p in (config.ROOT / "datasets").rglob("*.mp4"))
        if clips:
            print("Clips you have:\n" + "\n".join(f"  python3 app.py --source {c}" for c in clips))
        else:
            print("Get the demo clips first: python3 app.py fetch")
        return 1
    engine = Engine(src, headless=args.headless, min_gap=config.MIN_CHECK_GAP if args.headless and src.live else 0)
    if args.trigger:
        engine.trigger = args.trigger
    span = f" ({src.start:g}s to {src.end:g}s)" if src.kind == "file" and src.end else (f" (from {src.start:g}s)" if src.start else "")
    print(f"\n  \033[1m{config.APP_NAME.upper()}\033[0m")
    print(f"  Watching   {config.CAMERA} · {ai.PROFILE_NAME} pack: {', '.join(ai.TITLES[k] for k in ai.PROFILE['rules'])}")
    print(f"  Source     {src.name}{span}")
    model = f"{config.OLLAMA_MODEL} via Ollama (local)" if config.PROVIDER == "ollama" else config.MODEL
    print(f"  Model      {model} · sends {'video clips' if config.AI_INPUT == 'video' else '4-frame sheets'}")
    if config.PROVIDER == "ollama":
        print("  Budget     local model, no daily limit")
    else:
        q = ai.free_quota(max_age=0)
        left = f" · OpenRouter: {q['remaining']} free calls left today" if q and q.get("remaining") is not None else ""
        print(f"  Budget     {ai.used_today()}/{config.DAILY_CAP} calls used today{left}")
    print(f"  Alerts     {', '.join(config.ALERTS) or 'none'}")
    engine.start()
    if args.headless:
        print("  Watching. Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    url = f"http://localhost:{args.port}"
    print(f"  Dashboard  {url}\n")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(engine))
    except OSError:
        status, body, _ = http("GET", f"{url}/api/status", timeout=3)
        if status == 200 and body.get("app"):
            print(f"  Already running at {url}, opening it.\n")
            webbrowser.open(url)
            return 0
        print(f"  Port {args.port} is busy. Run with --port {args.port + 1}")
        return 1
    server.daemon_threads = True
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


# ------------------------------------------------------------------ one-off looks (Hermes cron and skill)

def look(spec, start, seconds):
    """Read `seconds` of video. Returns (clip frames, peak motion %, when motion first crossed the trigger, source)."""
    src = Source(spec, start)
    clip, small, peak, onset = [], deque(), 0.0, None
    first = next_m = next_c = None
    while True:
        ok, frame, t = src.read()
        if not ok:
            break
        first = t if first is None else first
        if t - first > seconds:
            break
        if next_m is None or t >= next_m:
            next_m = t + 0.25
            grey = tiny_grey(frame)
            small.append((t, grey))
            while small and small[0][0] < t - 4:
                small.popleft()
            ref = next((g for ft, g in reversed(small) if ft <= t - 1), None)
            m = motion_between(grey, ref) if ref is not None else 0.0
            peak = max(peak, m)
            if onset is None and m >= config.MOTION_TRIGGER:
                onset = t
        if next_c is None or t >= next_c:
            next_c = t + 1 / config.CLIP_FPS
            clip.append((t, fit(frame, 640)))
    src.release()
    return clip, peak, onset, src


def cmd_once(args):
    try:
        clip, peak, onset, src = look(args.source, args.start, args.seconds)
    except Exception as e:
        print(f"Camera error: {e}", file=sys.stderr)
        return 1
    if not clip:
        print("No frames came from the camera.", file=sys.stderr)
        return 1
    moved = peak >= config.MOTION_TRIGGER
    if not moved and not args.force:
        if args.verbose:
            print(f"All quiet: nothing moved in {args.seconds:g} seconds.")
        return 0  # empty output = a silent tick for Hermes cron
    if moved:
        frames = [f for f in clip if onset - config.PRE_ROLL <= f[0] <= onset + config.POST_ROLL] or clip
        t0 = onset
    else:
        frames = clip[-int(config.CLIP_FPS * (config.PRE_ROLL + config.POST_ROLL)):]
        t0 = frames[0][0]
    res, kind, sheet_path, clip_path = judge(frames, config.CAMERA)
    record = {"at": datetime.now().isoformat(timespec="seconds"), "source": src.name, "trigger": "once",
              "from": round(frames[0][0], 1), "to": round(frames[-1][0], 1), "input": kind, "sheet": str(sheet_path),
              "clip": str(clip_path) if clip_path else None, "ok": res["ok"]}
    if not res["ok"]:
        record.update(error=res["error"], message=res["message"])
        log_verdict(record)
        if res["error"] == "daily_cap":
            print(res["message"], file=sys.stderr)
            return 0
        print(f"⚠️ {config.APP_NAME} couldn't check {config.CAMERA}: {res['message']}")
        return 0
    v = res["verdict"]
    is_alert = v["alert"] and v["confidence"] >= config.MIN_CONF
    incident_t = v["t"] if frames[0][0] - 1 <= v["t"] <= frames[-1][0] + 1 else t0
    record.update(verdict=v, alert=is_alert, latency=res["latency"], profile=ai.PROFILE_NAME,
                  title=ai.TITLES.get(v["rule"], ai.TITLES["other"]))
    if is_alert:
        a = {**v, "t": incident_t, "origin": src.origin if src.live else None, "camera": config.CAMERA,
             "after": max(0.0, frames[-1][0] - incident_t) + res["latency"], "call": res["call"]}
        if args.send:
            record["deliveries"] = alerts.send(a, sheet_path, clip_path)
        print(alerts.lines(a, rich=False))
        print(f"MEDIA:{sheet_path.resolve()}")
    elif args.verbose:
        unsure = f" (maybe {ai.RULES.get(v['rule'], v['rule'])}, {round(v['confidence'] * 100)}% sure)" if v["alert"] else ""
        print(f"✅ No incident on {config.CAMERA}{unsure}: {v['why']}")
        print(f"MEDIA:{sheet_path.resolve()}")
    log_verdict(record)
    return 0


def cmd_report(args):
    path = config.DATA / "verdicts.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    rows = [r for r in rows if r.get("at", "").startswith(date.today().isoformat())]
    found = [r for r in rows if r.get("alert")]
    used = "local model" if config.PROVIDER == "ollama" else f"Nemotron calls used: {ai.used_today()}/{config.DAILY_CAP}"
    print(f"Today on {config.CAMERA}: {len(rows)} checks, {len(found)} alerts. {used}.")
    for r in found[-5:]:
        v = r["verdict"]
        print(f"- {r['at'][11:16]} {r.get('title') or ai.TITLES.get(v['rule'], ai.TITLES['other'])}: {v['why']} "
              f"({round(v['confidence'] * 100)}% sure)")
    if found:
        print(f"MEDIA:{found[-1]['sheet']}")
    return 0


# ------------------------------------------------------------------ finding the moments in a clip

def cmd_find(args):
    """Scan a video for moments with motion (no AI calls) and print the --start/--end to demo each one."""
    try:
        src = Source(args.source, args.start, args.end)
    except Exception as e:
        print(f"Can't open the video: {e}")
        return 1
    if src.kind != "file":
        print("find works on video files.")
        return 1
    samples, small, next_m = [], deque(), None
    while True:
        ok, frame, t = src.read()
        if not ok:
            break
        if next_m is not None and t < next_m:
            continue
        next_m = t + 0.25
        grey = tiny_grey(frame)
        small.append((t, grey))
        while small and small[0][0] < t - 4:
            small.popleft()
        ref = next((g for ft, g in reversed(small) if ft <= t - 1), None)
        samples.append((t, motion_between(grey, ref) if ref is not None else 0.0))
    moments = []
    for t, m in samples:
        if m < config.MOTION_TRIGGER:
            continue
        if moments and t - moments[-1]["to"] <= 2.0:
            mo = moments[-1]
            mo["to"] = t
            if m > mo["peak"]:
                mo["peak"], mo["at"] = m, t
        else:
            moments.append({"from": t, "to": t, "peak": m, "at": t})
    length = samples[-1][0] if samples else 0
    print(f"\n{src.name}: {length:.1f}s scanned, {len(moments)} moments with motion above {config.MOTION_TRIGGER:g}%\n")
    if not moments:
        print("Nothing moved enough. Try a lower MOTION_TRIGGER in .env, like 0.4.")
        return 0
    out_dir = config.DATA / "find"
    out_dir.mkdir(parents=True, exist_ok=True)
    import numpy as np
    import cv2
    print(f"  {'#':<3}{'from':>7}{'to':>8}{'peak':>8}   demo command")
    for i, mo in enumerate(moments, 1):
        start, end = max(0.0, mo["from"] - 3), mo["to"] + 6
        print(f"  {i:<3}{mo['from']:>6.1f}s{mo['to']:>7.1f}s{mo['peak']:>7.1f}%   "
              f"python3 app.py --source \"{args.source}\" --start {start:.0f} --end {end:.0f}")
        tiles = []
        for tt in (mo["from"] - 1, mo["from"] + 0.5, (mo["from"] + mo["to"]) / 2, mo["to"] + 1):
            src.seek(max(0.0, tt))
            ok, frame, real_t = src.read()
            if ok:
                tiles.append(stamp(fit(frame, 480), real_t))
        if len(tiles) == 4 and all(x.shape == tiles[0].shape for x in tiles):
            sheet = np.vstack([np.hstack(tiles[:2]), np.hstack(tiles[2:])])
            cv2.imwrite(str(out_dir / f"{Path(src.name).stem}-{i}.jpg"), sheet)
    print(f"\n  A 4-frame preview of each moment is in {out_dir}/ (no AI calls were used).\n")
    return 0


# Real footage for the demo: GMDCSA-24 (Alam et al., 2024), CC BY 4.0, recorded falls with labelled times.
GMDCSA = "https://raw.githubusercontent.com/ekramalam/GMDCSA24-A-Dataset-for-Human-Fall-Detection-in-Videos/HEAD"
DEMO_CLIPS = {
    "s2-fall-12": ("Subject 2", "Fall", "12.mp4"),
    "s3-fall-14": ("Subject 3", "Fall", "14.mp4"),
    "s2-fall-04": ("Subject 2", "Fall", "04.mp4"),
    "s3-adl-01": ("Subject 3", "ADL", "01.mp4"),  # everyday activity, no fall: should NOT alert
}
CREDIT = ("Footage: GMDCSA-24 (E. Alam, A. Sufian, P. Dutta, M. Leo, I. A. Hameed, Data in Brief, 2024), "
          "CC BY 4.0, https://doi.org/10.5281/zenodo.13354453")


def cmd_fetch(args):
    """Download the demo clips from GMDCSA-24 and their labelled fall times."""
    import csv
    import io
    import re
    from urllib.parse import quote
    from urllib.request import urlopen

    out = config.ROOT / "datasets" / "gmdcsa24"
    out.mkdir(parents=True, exist_ok=True)
    rows = {}
    for subject, kind in sorted({(s_, k) for s_, k, _ in DEMO_CLIPS.values()}):
        with urlopen(f"{GMDCSA}/{quote(subject)}/{kind}.csv", timeout=30) as r:
            for line in csv.reader(io.StringIO(r.read().decode("utf-8", "replace"))):
                if line and line[0].strip().endswith(".mp4"):
                    rows[(subject, kind, line[0].strip())] = {"length": line[1].strip(), "description": line[4].strip(),
                                                              "classes": line[-1].strip()}
    labels = {}
    for name, (subject, kind, file) in DEMO_CLIPS.items():
        dest = out / f"{name}.mp4"
        if not dest.exists():
            print(f"Downloading {subject}/{kind}/{file} …", flush=True)
            with urlopen(f"{GMDCSA}/{quote(subject)}/{kind}/{file}", timeout=120) as r:
                dest.write_bytes(r.read())
        row = rows.get((subject, kind, file), {})
        span = re.search(r"Fall\w*[^\[;]*\[\s*([\d.]+)\s*(?:to\s*)?([\d.]+)\s*\]", row.get("classes", ""))
        fall = [float(span.group(1)), float(span.group(2))] if span else None
        labels[name] = {"file": str(dest.relative_to(config.ROOT)), "source": f"{subject}/{kind}/{file}",
                        "description": row.get("description"), "fall": fall, "classes": row.get("classes")}
    (out / "labels.json").write_text(json.dumps({"credit": CREDIT, "clips": labels}, indent=1))
    print(f"\nSaved in {out}/\n")
    for name, lab in labels.items():
        if lab["fall"]:
            start = max(0.0, lab["fall"][0] - 4)
            print(f"  {name}: {lab['description']}. Fall starts at {lab['fall'][0]:g}s.")
            print(f"    python3 app.py --source {lab['file']} --start {start:g}\n")
        else:
            print(f"  {name}: {lab['description']}. No fall (the model should stay quiet).")
            print(f"    python3 app.py --source {lab['file']}\n")
    print(f"  {CREDIT}\n")
    return 0


# ------------------------------------------------------------------ live cameras

CAMERA_BRANDS = {  # brand: (HD stream, lighter sub-stream, how to turn it on)
    "tapo": ("rtsp://{auth}{ip}:554/stream1", "rtsp://{auth}{ip}:554/stream2",
             "TP-Link Tapo: Tapo app > camera > Advanced settings > Camera account gives the user and password."),
    "hikvision": ("rtsp://{auth}{ip}:554/Streaming/Channels/101", "rtsp://{auth}{ip}:554/Streaming/Channels/102",
                  "Hikvision cameras and NVRs (channel 2 is 201 / 202)."),
    "dahua": ("rtsp://{auth}{ip}:554/cam/realmonitor?channel=1&subtype=0",
              "rtsp://{auth}{ip}:554/cam/realmonitor?channel=1&subtype=1",
              "Dahua and its OEMs: CP Plus, Amcrest, Lorex."),
    "reolink": ("rtsp://{auth}{ip}:554/h264Preview_01_main", "rtsp://{auth}{ip}:554/h264Preview_01_sub",
                "Reolink: turn on RTSP in the camera's advanced network settings."),
    "ezviz": ("rtsp://{auth}{ip}:554/h264/ch1/main/av_stream", "rtsp://{auth}{ip}:554/h264/ch1/sub/av_stream",
              "EZVIZ: user admin, password = the verification code on the camera's sticker; turn on RTSP in the app."),
    "ipwebcam": ("http://{ip}:8080/video", "http://{ip}:8080/video",
                 "An Android phone as a camera: the IP Webcam app, then Start server."),
    "go2rtc": ("rtsp://{ip}:8554/{name}", "rtsp://{ip}:8554/{name}",
               "go2rtc, Frigate or Home Assistant restreams: the way in for Ring, Nest, Wyze and other cloud cameras."),
}
CAMERA_BRANDS["cpplus"] = CAMERA_BRANDS["amcrest"] = CAMERA_BRANDS["dahua"]


def build_source(brand, ip, user=None, password=None, hd=False, name="camera"):
    """A stream URL from a brand template. User and password default to CAMERA_USER / CAMERA_PASS in .env."""
    from urllib.parse import quote
    if brand not in CAMERA_BRANDS:
        raise SystemExit(f"Unknown camera brand {brand!r}. Known: {', '.join(sorted(CAMERA_BRANDS))}")
    user = os.environ.get("CAMERA_USER", "") if user is None else user
    password = os.environ.get("CAMERA_PASS", "") if password is None else password
    if brand == "ezviz" and not user:
        user = "admin"
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user or password else ""
    hd_url, sub_url, _ = CAMERA_BRANDS[brand]
    return (hd_url if hd else sub_url).format(auth=auth, ip=ip, name=name)


def masked(spec):
    import re
    return re.sub(r"//[^/@]+@", "//***@", str(spec))


def expand_env(value):
    """${VAR} inside cameras.json comes from .env, so passwords never sit in the JSON."""
    import re
    if not isinstance(value, str):
        return value

    def sub(m):
        if m.group(1) not in os.environ:
            print(f"  ⚠ {m.group(1)} is not set in .env", file=sys.stderr)
        return os.environ.get(m.group(1), "")

    return re.sub(r"\$\{([A-Za-z0-9_]+)\}", sub, value)


def cmd_cameras(args):
    print("\n  Stream addresses for common cameras. The sub-stream is lighter and plenty for the AI.\n")
    done = set()
    for brand, (hd_url, sub_url, how) in CAMERA_BRANDS.items():
        if (hd_url, sub_url) in done:
            continue
        done.add((hd_url, sub_url))
        aliases = [b for b, v in CAMERA_BRANDS.items() if v == (hd_url, sub_url, how)]
        print(f"  {' / '.join(aliases):<26} {sub_url.format(auth='USER:PASS@', ip='IP', name='NAME')}")
        print(f"  {'':<26} {how}\n")
    print("  Put CAMERA_USER and CAMERA_PASS in .env, then:")
    print("    python3 app.py probe --brand tapo --ip 192.168.1.20      check the stream works")
    print("    python3 app.py --brand tapo --ip 192.168.1.20            open the dashboard on it")
    print("  Several cameras at once: cp cameras.example.json cameras.json, edit it, then python3 app.py watch")
    print("  Find cameras on your network: python3 app.py discover\n")
    return 0


def cmd_probe(args):
    """Open a camera or stream, check it delivers frames, and save one. No AI calls."""
    print(f"Opening {masked(args.source)} …")
    try:
        src = Source(args.source)
    except Exception as e:
        print(f"Couldn't open it: {e}")
        print("Check the IP, the user and password, and that RTSP is switched on in the camera's app.")
        return 1
    frame, frames, began = None, 0, time.monotonic()
    while time.monotonic() - began < 3:
        ok, f, _ = src.read()
        if not ok:
            break
        frame, frames = f, frames + 1
    src.release()
    if frame is None:
        print("It opened, but no frames came through.")
        return 1
    h, w = frame.shape[:2]
    rate = src.fps if src.kind == "file" else frames / max(0.1, time.monotonic() - began)
    config.DATA.mkdir(exist_ok=True)
    import cv2
    cv2.imwrite(str(config.DATA / "probe.jpg"), frame)
    print(f"Works: {w}x{h} at about {rate:.0f} frames a second. One frame saved to data/probe.jpg")
    if max(w, h) > 1920:
        print("Tip: use the camera's sub-stream (python3 app.py cameras): the AI only needs 640px.")
    return 0


def cmd_discover(args):
    """Find ONVIF cameras on the local network with a WS-Discovery probe. No AI calls."""
    import re
    import socket
    import uuid
    from urllib.parse import unquote
    probe = (
        '<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f'<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>'
        '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
        '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    print("Looking for ONVIF cameras on your network for 4 seconds…")
    sock.sendto(probe.encode(), ("239.255.255.250", 3702))
    found, deadline = {}, time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            data, (ip, _) = sock.recvfrom(65535)
        except socket.timeout:
            continue
        text = data.decode("utf-8", "ignore")
        scopes = " ".join(re.findall(r"Scopes>\s*([^<]+?)\s*<", text))
        xaddrs = " ".join(re.findall(r"XAddrs>\s*([^<]+?)\s*<", text))
        name = re.search(r"onvif://www\.onvif\.org/name/(\S+)", scopes)
        hardware = re.search(r"onvif://www\.onvif\.org/hardware/(\S+)", scopes)
        found[ip] = (unquote(name.group(1)) if name else "", unquote(hardware.group(1)) if hardware else "", xaddrs)
    sock.close()
    if not found:
        print("No ONVIF cameras answered. Many cameras need ONVIF switched on in their app; "
              "you can still connect with --brand and --ip (python3 app.py cameras).")
        return 0
    guesses = [("tapo", ("tapo", ":2020/")), ("hikvision", ("hikvision", "ds-")), ("dahua", ("dahua", "ipc-")),
               ("cpplus", ("cp plus", "cpplus")), ("reolink", ("reolink",)), ("ezviz", ("ezviz",))]
    for ip, (name, hardware, xaddrs) in sorted(found.items()):
        hay = f"{name} {hardware} {xaddrs}".lower()
        brand = next((b for b, keys in guesses if any(k in hay for k in keys)), None)
        print(f"  {ip:<16} {name or 'camera'} {hardware}".rstrip())
        print(f"  {'':<16} " + (f"python3 app.py probe --brand {brand} --ip {ip}" if brand
                                 else f"try rtsp://USER:PASS@{ip}:554/ with your camera's stream path"))
    return 0


def cmd_watch(args):
    """Watch every camera in cameras.json at once, without a dashboard; alerts go to Telegram / Hermes."""
    path = Path(args.config) if args.config else config.ROOT / "cameras.json"
    if not path.exists():
        print(f"No {path.name} yet. Start from the example:  cp cameras.example.json cameras.json")
        return 1
    if not ai.ready():
        print("OPENROUTER_API_KEY is missing in .env (or set PROVIDER=ollama)")
        return 1
    cams = [c for c in json.loads(path.read_text()).get("cameras", []) if c.get("enabled", True)]
    engines = []
    print(f"\n  {config.APP_NAME.upper()} · watching {len(cams)} camera(s)\n")
    for i, c in enumerate(cams, 1):
        name = c.get("name") or f"Camera {i}"
        if c.get("brand"):
            spec = build_source(c["brand"], c.get("ip", ""), expand_env(c.get("user")), expand_env(c.get("password")),
                                c.get("hd", False), c.get("stream_name", "camera"))
        else:
            spec = expand_env(c.get("source", ""))
        profile = c.get("profile") or ai.PROFILE_NAME
        try:
            src = Source(spec)
        except Exception as e:
            print(f"  ✗ {name}: couldn't open {masked(spec)} ({e}). Skipped; check it with python3 app.py probe")
            continue
        engine = Engine(src, headless=True, camera=name, profile=profile, zone=c.get("zone"),
                        min_gap=c.get("min_gap", config.MIN_CHECK_GAP if src.live else 0), tag=name)
        if c.get("trigger") is not None:
            engine.trigger = float(c["trigger"])
        engines.append(engine)
        titles = ", ".join(t for k, t in ai.pack(profile)["titles"].items() if k != "other")
        print(f"  ✓ {name:<16} {src.name} · {profile}: {titles}")
    if not engines:
        return 1
    left = "" if config.PROVIDER == "ollama" else f" · budget {ai.used_today()}/{config.DAILY_CAP} calls today"
    print(f"\n  Model {config.MODEL_LABEL}{left} · alerts to {', '.join(config.ALERTS)}. Ctrl+C to stop.\n")
    for engine in engines:
        engine.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


# ------------------------------------------------------------------ setup helpers

def row(name, value):
    print(f"  {name:<18} {value}")


def cmd_check(args):
    print(f"\n  {config.APP_NAME} setup check (no model calls)\n")
    row("Settings file", config.ENV_PATH.name if config.ENV_PATH.exists() else "missing: run `cp .env.example .env` and fill it in")
    row("ffmpeg", ffmpeg_exe() or "missing (needed for video clips): brew install ffmpeg")
    if config.PROVIDER == "ollama":
        status, body, _ = http("GET", f"{config.OLLAMA_URL}/api/tags", timeout=5)
        names = [m.get("name", "") for m in (body.get("models") or [])] if status == 200 else []
        pulled = any(n.split(":")[0] == config.OLLAMA_MODEL.split(":")[0] for n in names)
        row("Model (local)", f"{config.OLLAMA_MODEL} via Ollama · " + (
            "ready" if pulled else f"not pulled: ollama pull {config.OLLAMA_MODEL}" if status == 200
            else f"Ollama not reachable at {config.OLLAMA_URL}"))
    else:
        status, body, _ = http("GET", f"{ai.OPENROUTER}/models/{config.MODEL}/endpoints", timeout=10)
        inputs = ((body.get("data") or {}).get("architecture") or {}).get("input_modalities") or []
        row("Model", f"{config.MODEL} " + (f"(takes {', '.join(inputs)})" if inputs else f"NOT FOUND ({error_text(body, status)})"))
    if config.PROVIDER == "ollama":
        row("Budget", "local model, no daily limit")
    else:
        if config.OPENROUTER_KEY:
            status, body, _ = http("GET", f"{ai.OPENROUTER}/key", headers=ai.auth(), timeout=10)
            if status == 200:
                q = (body.get("data") or {}).get("free_model_daily_requests") or {}
                row("OpenRouter key", f"ok · free calls today: {q.get('used', '?')} used, {q.get('remaining', '?')} left of {q.get('limit', '?')}")
            else:
                row("OpenRouter key", f"rejected: {error_text(body, status)}")
        else:
            row("OpenRouter key", "missing (OPENROUTER_API_KEY)")
        row("Local budget", f"{ai.used_today()}/{config.DAILY_CAP} calls used today")
    row("Alerts go to", ", ".join(config.ALERTS) or "nowhere")
    if config.TELEGRAM_BOT_TOKEN:
        ok, me = alerts.telegram("getMe", {})
        row("Telegram bot", f"@{me['username']}" if ok else f"rejected: {me}")
        if ok and config.TELEGRAM_CHAT_ID:
            ok, info = alerts.telegram("sendMessage", {"chat_id": config.TELEGRAM_CHAT_ID,
                                                        "text": f"✅ {config.APP_NAME} test: alerts will arrive here."})
            row("Telegram chat", f"{config.TELEGRAM_CHAT_ID} · test message sent" if ok else f"failed: {info}")
        elif ok:
            row("Telegram chat", "missing: run `python3 app.py telegram`")
    elif "telegram" in config.ALERTS:
        row("Telegram bot", "missing (TELEGRAM_BOT_TOKEN)")
    if "hermes" in config.ALERTS:
        health = config.HERMES_WEBHOOK_URL.split("/webhooks/")[0] + "/health"
        status, body, _ = http("GET", health, timeout=5)
        row("Hermes webhook", "up" if status == 200 else f"not reachable at {health} (is `hermes gateway` running?)")
    print()
    return 0


def cmd_telegram(args):
    if not config.TELEGRAM_BOT_TOKEN:
        print("Add TELEGRAM_BOT_TOKEN to .env first. Get it from @BotFather in Telegram with /newbot.")
        return 1
    ok, me = alerts.telegram("getMe", {})
    if not ok:
        print(f"Telegram rejected the bot token: {me}")
        return 1
    print(f"\nOpen Telegram, search @{me['username']}, press Start (or send it any message). "
          f"Waiting {args.wait:g} seconds…", flush=True)
    deadline, chat = time.time() + args.wait, None
    while time.time() < deadline and chat is None:
        ok, updates = alerts.telegram("getUpdates", {"timeout": 10, "allowed_updates": ["message"]}, timeout=20)
        if not ok:
            if "conflict" in str(updates).lower():
                print("Hermes (or another program) is reading this bot right now. Stop it for a minute "
                      "(`hermes gateway stop`), run this again, then start Hermes back up.")
                return 1
            time.sleep(2)
            continue
        for u in updates:
            msg = u.get("message") or {}
            if msg.get("chat", {}).get("id"):
                chat = msg["chat"]
    if chat is None:
        print("No message arrived. Send your bot a message and run this again.")
        return 1
    config.write_env("TELEGRAM_CHAT_ID", str(chat["id"]))
    config.TELEGRAM_CHAT_ID = str(chat["id"])
    alerts.telegram("sendMessage", {"chat_id": chat["id"], "text": f"✅ {config.APP_NAME} is connected. Alerts will arrive here."})
    print(f"Connected to {chat.get('first_name') or chat.get('title') or chat['id']} (chat id {chat['id']}). "
          "Saved it in {config.ENV_PATH.name} and sent you a test message.")
    return 0


def cmd_smoke(args):
    """One real Nemotron call on a few seconds of real footage, to prove the key, the model and the JSON work."""
    if not ai.ready():
        print("OPENROUTER_API_KEY is missing in .env (or set PROVIDER=ollama)")
        return 1
    if args.source == "0" and not config.ENV_PATH.exists():
        print("Pass a clip: python3 app.py smoke --source datasets/gmdcsa24/s2-fall-12.mp4 --start 5")
        return 1
    if not args.yes and input(f"This uses 1 of today's free Nemotron calls ({ai.used_today()}/{config.DAILY_CAP} used). "
                              "Go? [y/N] ").strip().lower() != "y":
        return 0
    try:
        clip, _, _, src = look(args.source, args.start, config.PRE_ROLL + config.POST_ROLL)
    except Exception as e:
        print(f"Can't read {args.source}: {e}")
        return 1
    print(f"Sending {'a video clip' if config.AI_INPUT == 'video' else '4 frames'} of {src.name} "
          f"({clip[0][0]:.1f}s to {clip[-1][0]:.1f}s) to {config.MODEL}…")
    res, kind, sheet_path, _ = judge(clip, config.CAMERA)
    if not res["ok"]:
        msg = res["message"]
        print(f"Failed ({res.get('status', '')}): {msg}")
        low = msg.lower()
        if kind == "video" and any(w in low for w in ("video", "modality", "unsupported", "invalid", "mime")):
            print("Fix: this endpoint may not take video right now. Add AI_INPUT=frames to .env and run smoke again.")
        if "reasoning" in low:
            print("Fix: add REASONING=default to .env")
        if "response_format" in low or "schema" in low:
            print("Fix: add STRUCTURED=0 to .env")
        return 1
    print(f"Answered in {res['latency']:.1f}s (sent as {kind}): {res['verdict']}")
    print(f"Frames it saw: {sheet_path}")
    print(f"Tokens: {res.get('usage')} · calls used today: {ai.used_today()}/{config.DAILY_CAP}")
    return 0


def cmd_hermes(args):
    """Install the Nazar skill and cron script into Hermes Agent, and print the remaining steps."""
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    if not home.exists():
        print("Hermes Agent isn't installed yet (no ~/.hermes). Install it first:\n"
              "  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash\n"
              "then run `hermes model` and `hermes gateway setup`, and run this again.")
        return 1
    secret = config.HERMES_WEBHOOK_SECRET
    if not secret:
        secret = secrets.token_hex(16)
        config.write_env("HERMES_WEBHOOK_SECRET", secret)
    source = args.source
    if "://" not in source and not source.isdigit():
        source = str(Path(source).expanduser().resolve())
    values = {"{{PYTHON}}": sys.executable, "{{APP}}": str(config.ROOT / "app.py"), "{{SOURCE}}": source,
              "{{CAMERA}}": config.CAMERA, "{{PROFILE}}": ai.PROFILE_NAME,
              "{{RULES}}": ", ".join(ai.TITLES[k].lower() for k in ai.PROFILE["rules"])}

    def fill(text):
        for k, v in values.items():
            text = text.replace(k, v)
        return text

    skill_dir = home / "skills" / "nazar"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(fill((config.ROOT / "hermes" / "skills" / "nazar" / "SKILL.md").read_text()))
    script = home / "scripts" / "nazar_check.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(fill((config.ROOT / "hermes" / "scripts" / "nazar_check.py").read_text()))
    script.chmod(0o755)
    print(f"""
Installed into Hermes:
  skill   {skill_dir}/SKILL.md     (ask Hermes on Telegram: "check the camera", "any alerts today?")
  script  {script}

Three steps left:

1. Turn on the Hermes webhook, so Nazar can hand alerts to Hermes. Add to {home}/.env:
     WEBHOOK_ENABLED=true
     WEBHOOK_PORT=8644
   and add this route to {home}/config.yaml (merge it with any platforms: you already have):
     platforms:
       webhook:
         enabled: true
         extra:
           port: 8644
           routes:
             nazar:
               secret: "{secret}"
               deliver: "telegram"
               deliver_only: true
               prompt: "{{text}}"
   Then restart the gateway: hermes gateway

2. Send Nazar alerts through Hermes: in this folder's .env set
     ALERTS=hermes            (or ALERTS=telegram,hermes to get both)

3. Optional, a check every 15 minutes even when nobody is watching the dashboard:
     hermes cron create "every 15m" --no-agent --script nazar_check.py --deliver telegram --name nazar
   It stays silent unless Nemotron sees an incident. Each look that sees motion uses 1 free call.

Camera used by the skill and the cron job: {source}
""")
    return 0


# ------------------------------------------------------------------ main

def main():
    p = argparse.ArgumentParser(description="Nazar: AI camera alerts with NVIDIA Nemotron (free).",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("command", nargs="?", default="serve",
                   choices=["serve", "watch", "cameras", "probe", "discover", "fetch", "find", "once", "report", "check",
                            "telegram", "smoke", "hermes"])
    p.add_argument("--source", default=config.SOURCE, help="video file, webcam index (0) or stream URL")
    p.add_argument("--brand", help="camera brand for the stream URL (see: python3 app.py cameras)")
    p.add_argument("--ip", help="camera IP address, with --brand")
    p.add_argument("--hd", action="store_true", help="with --brand: the HD stream instead of the sub-stream")
    p.add_argument("--config", help="watch: the cameras file (default cameras.json)")
    p.add_argument("--profile", choices=list(ai.PROFILES), help="what to watch for: " + ", ".join(ai.PROFILES))
    p.add_argument("--camera", help="camera name shown in the dashboard and alerts")
    p.add_argument("--start", type=float, default=0, help="files: start at this second")
    p.add_argument("--end", type=float, help="files: stop at this second")
    p.add_argument("--loop", action="store_true", help="files: loop the part between --start and --end")
    p.add_argument("--headless", action="store_true", help="no dashboard, watch and alert only")
    p.add_argument("--no-open", action="store_true", help="don't open the browser")
    p.add_argument("--port", type=int, default=config.PORT)
    p.add_argument("--trigger", type=float, help="motion trigger in %% of the picture (default MOTION_TRIGGER)")
    p.add_argument("--seconds", type=float, default=8, help="once: how long to look")
    p.add_argument("--force", action="store_true", help="once: ask Nemotron even if nothing moved")
    p.add_argument("--verbose", action="store_true", help="once: also print when all is quiet")
    p.add_argument("--send", action="store_true", help="once: also deliver the alert itself (ALERTS)")
    p.add_argument("--input", choices=["video", "frames"], help="override AI_INPUT for this run")
    p.add_argument("--yes", action="store_true", help="smoke: don't ask before using a call")
    p.add_argument("--wait", type=float, default=120, help="telegram: seconds to wait for your message")
    args = p.parse_args()
    if args.input:
        config.AI_INPUT = args.input
    if args.profile:
        ai.set_profile(args.profile)
    if args.camera:
        config.CAMERA = args.camera
    if args.brand:
        if not args.ip:
            p.error("--brand needs --ip")
        args.source = build_source(args.brand, args.ip, hd=args.hd)
    commands = {"serve": serve, "watch": cmd_watch, "cameras": cmd_cameras, "probe": cmd_probe, "discover": cmd_discover,
                "fetch": cmd_fetch, "find": cmd_find, "once": cmd_once, "report": cmd_report, "check": cmd_check,
                "telegram": cmd_telegram, "smoke": cmd_smoke, "hermes": cmd_hermes}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
