"""The camera loop. Reads any video source, checks motion locally (free), and only when something moves
cuts a short clip around it and asks Nemotron. Used by the dashboard and by headless runs on a server."""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2  # noqa: E402
import numpy as np  # noqa: E402

import ai  # noqa: E402
import alerts  # noqa: E402
import config  # noqa: E402

SETTLE = 1.5      # seconds after arming where motion is ignored
STREAM_FPS = 20   # frame rate of the dashboard view
PIXEL_DELTA = 18  # colour change (0-255) that counts a pixel as changed


def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None


class Source:
    """A video file, a webcam index ("0") or a stream URL (rtsp://, http://, https://)."""

    def __init__(self, spec, start=0.0, end=None, loop=False):
        self.spec = str(spec)
        self.start, self.end, self.loop = float(start or 0), (float(end) if end else None), loop
        if self.spec.isdigit():
            self.kind, self.name = "camera", f"webcam {self.spec}"
        elif "://" in self.spec:
            scheme, rest = self.spec.split("://", 1)
            self.kind, self.name = "stream", f"{scheme}://{rest.split('@')[-1]}"  # never show the password
        else:
            path = Path(self.spec).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"no such video: {self.spec}")
            self.kind, self.name, self.spec = "file", path.name, str(path)
        self.cap, self.fps, self.duration = None, 25.0, None
        self.open()

    @property
    def live(self):
        return self.kind != "file"

    def open(self):
        if self.cap is not None:
            self.cap.release()
        if self.kind == "camera":
            self.cap = cv2.VideoCapture(int(self.spec), cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        else:
            self.cap = cv2.VideoCapture(self.spec, cv2.CAP_FFMPEG)
        if not self.cap.isOpened():
            raise RuntimeError(f"couldn't open {self.name}")
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if 1 <= fps <= 120 else 25.0
        if self.kind == "file":
            frames = self.cap.get(cv2.CAP_PROP_FRAME_COUNT)
            self.duration = frames / self.fps if frames > 0 else None
            if self.end and self.duration:
                self.end = min(self.end, self.duration)
            self.seek(self.start)
        self.t0, self.origin = time.monotonic(), time.time()

    def seek(self, t):
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(round(t * self.fps))))

    def read(self):
        """-> (ok, frame, t): t is seconds into the file, or seconds since a live source opened."""
        ok, frame = self.cap.read()
        if self.kind == "file":
            t = max(0.0, (self.cap.get(cv2.CAP_PROP_POS_FRAMES) - 1) / self.fps)
            if ok and self.end is not None and t > self.end:
                ok = False
        else:
            t = time.monotonic() - self.t0
        return ok, frame, t

    def release(self):
        if self.cap is not None:
            self.cap.release()


# ------------------------------------------------------------------ frames, clips and sheets

def fit(frame, long_side):
    """Resize so the long side is at most `long_side`, with even width and height (H.264 needs them)."""
    h, w = frame.shape[:2]
    k = min(1.0, long_side / max(h, w))
    size = (max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2))
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)


def stamp(frame, t):
    """Burn the video time into the corner so the model can say when something happened."""
    label = f"t={t:.1f}s"
    scale = min(frame.shape[:2]) / 360 * 0.7
    (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    cv2.rectangle(frame, (6, 6), (18 + tw, 16 + th + base), (0, 0, 0), -1)
    cv2.putText(frame, label, (12, 11 + th), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2, cv2.LINE_AA)
    return frame


def motion_between(small, ref):
    """% of the picture that changed: a pixel counts when any colour channel moved more than PIXEL_DELTA.
    Colour matters: a spreading puddle changes the colour of the floor more than its brightness."""
    changed = cv2.absdiff(small, ref).max(axis=2) > PIXEL_DELTA
    return float(np.count_nonzero(changed)) / changed.size * 100


def tiny_grey(frame):
    """A small, slightly blurred colour copy for the motion check (averaging away sensor noise)."""
    return cv2.GaussianBlur(cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA), (3, 3), 0)


def contact_sheet(frames):
    """4 frames spread across the clip as a 2x2 JPEG (the dashboard view and the Telegram photo)."""
    n = len(frames)
    pick = [frames[round(i * (n - 1) / 3)] for i in range(4)] if n >= 4 else (frames * 4)[:4]
    imgs = [f.copy() for _, f in pick]
    h, w = imgs[0].shape[:2]
    sheet = np.vstack([np.hstack(imgs[:2]), np.hstack(imgs[2:])])
    sheet[:, w - 2:w + 2] = 0
    sheet[h - 2:h + 2, :] = 0
    return cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def encode_mp4(frames, fps, path):
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg not found (brew install ffmpeg, or pip install imageio-ffmpeg)")
    h, w = frames[0][1].shape[:2]
    cmd = [exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps:g}",
           "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(path)]
    done = subprocess.run(cmd, input=b"".join(f.tobytes() for _, f in frames if f.shape[:2] == (h, w)),
                          capture_output=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.decode(errors="replace")[-300:])
    return path.read_bytes()


def prepare(frames):
    """Save a clean 2x2 sheet (for people: dashboard and Telegram) and, for the model, an mp4 clip or a sheet
    with the video time burned into every frame."""
    name = datetime.now().strftime("%Y%m%d-%H%M%S")
    for sub in ("sheets", "clips"):
        (config.DATA / sub).mkdir(parents=True, exist_ok=True)
    sheet_path = config.DATA / "sheets" / f"{name}.jpg"
    sheet_path.write_bytes(contact_sheet(frames))
    stamped = [(t, stamp(f.copy(), t)) for t, f in frames]
    clip, clip_path = None, None
    if config.AI_INPUT == "video" or config.ALERT_MEDIA == "video":
        clip_path = config.DATA / "clips" / f"{name}.mp4"
        try:
            clip = encode_mp4(stamped, config.CLIP_FPS, clip_path)
        except Exception as e:  # fall back to the still frames
            print(f"clip encoding failed, sending frames instead: {e}", file=sys.stderr)
            clip_path = None
    kind = "video" if config.AI_INPUT == "video" and clip else "frames"
    return {"model_sheet": contact_sheet(stamped) if kind == "frames" else None, "sheet_path": sheet_path,
            "clip": clip, "clip_path": clip_path, "kind": kind,
            "times": [round(frames[0][0], 1), round(frames[-1][0], 1)]}


def ask_model(p, camera):
    return ai.check(p["clip"] if p["kind"] == "video" else p["model_sheet"], p["kind"], p["times"], camera)


def judge(frames, camera):
    """Prepare and ask in one go. -> (result, kind, sheet_path, clip_path)."""
    p = prepare(frames)
    return ask_model(p, camera), p["kind"], p["sheet_path"], p["clip_path"]


def log_verdict(record):
    config.DATA.mkdir(exist_ok=True)
    with open(config.DATA / "verdicts.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


def say(text, kind=""):
    colors = {"alert": "31;1", "error": "31", "clear": "32", "ask": "35", "motion": "33", "tg": "36"}
    stamp_ = datetime.now().strftime("%H:%M:%S")
    c = colors.get(kind)
    print(f"\033[{c}m{stamp_}  {text}\033[0m" if c else f"{stamp_}  {text}", flush=True)


# ------------------------------------------------------------------ the live loop

class Engine(threading.Thread):
    def __init__(self, source, headless=False):
        super().__init__(daemon=True)
        self.src, self.headless = source, headless
        self.trigger = config.MOTION_TRIGGER
        self.max_per_arm = 10 ** 9 if headless else config.MAX_PER_ARM
        self.armed = headless                      # a headless run watches from the start
        self.phase = "watching" if headless else "ready"
        self.playing = source.live or headless     # in the dashboard a clip waits for ARM
        self.ended, self.calls = False, 0
        self.cooldown_until = SETTLE
        self.t, self.motion = source.start, 0.0
        self.small = deque()                       # (t, tiny grey frame) for the motion check
        self.clip = deque()                        # (t, 640px frame) for clips and sheets
        self.next_motion_t = self.next_clip_t = None
        self.event, self.wall0, self.vt0 = None, None, 0.0
        self.pending_t = None                      # motion seen while Nemotron was busy, checked next
        self.frame_wall = time.time()              # wall-clock time the latest frame was shown
        self.cmds = queue.Queue()
        self.listeners, self.listeners_lock = [], threading.Lock()
        self.jpeg, self.jpeg_seq, self.jpeg_cond, self.last_pub = None, 0, threading.Condition(), 0.0

    # ---- talking to the dashboard

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.listeners_lock:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q):
        with self.listeners_lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def emit(self, event_type, /, **data):
        msg = json.dumps({"type": event_type, **data})
        with self.listeners_lock:
            for q in self.listeners:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    def log(self, text, kind=""):
        say(text, kind)
        self.emit("log", text=text, kind=kind)

    def status(self):
        s = self.src
        return {"type": "status", "app": config.APP_NAME, "model": config.MODEL_LABEL, "profile": ai.PROFILE_NAME,
                "camera": config.CAMERA, "input": config.AI_INPUT, "alerts": config.ALERTS,
                "source": {"kind": s.kind, "name": s.name, "start": s.start, "end": s.end},
                "origin": s.origin if s.live else None, "used": ai.used_today(), "cap": config.DAILY_CAP,
                "free": ai._free["value"], "trigger": self.trigger,
                "max_per_arm": config.MAX_PER_ARM, "armed": self.armed, "phase": self.phase, "playing": self.playing,
                "key_set": bool(config.OPENROUTER_KEY)}

    def command(self, name, *args):
        self.cmds.put((name, *args))

    # ---- the loop

    def run(self):
        threading.Thread(target=self._quota_loop, daemon=True).start()
        if not self.playing:
            self._show_first_frame()
        while True:
            self._run_commands()
            if not self.playing:
                time.sleep(0.03)
                continue
            ok, frame, t = self.src.read()
            if not ok:
                self._source_ended()
                continue
            self._pace(t)
            self.t, self.frame_wall = t, time.time()
            self._publish(frame)
            self._sample(frame, t)

    def emit_status(self):
        self.emit("status", **{k: v for k, v in self.status().items() if k != "type"})

    def _quota_loop(self):
        while True:
            ai.free_quota(max_age=50)
            self.emit_status()
            time.sleep(60)

    def _show_first_frame(self):
        ok, frame, t = self.src.read()
        if ok:
            self.t = t
            self._publish(frame, force=True)
        if self.src.kind == "file":
            self.src.seek(self.src.start)

    def _source_ended(self):
        if self.src.kind == "file":
            if self.src.loop:
                return self._restart_file()
            self.playing, self.ended = False, True
            if self.phase == "collecting":
                self._dispatch()
            self.log("Clip ended. Press R to play it again.")
            self._set_phase(self.phase)
            return
        self.log(f"Lost {self.src.name}, reconnecting…", "error")
        time.sleep(2)
        try:
            self.src.open()
        except Exception as e:
            self.log(f"Reconnect failed: {e}", "error")
            time.sleep(3)

    def _pace(self, t):
        """Play files in real time, like a camera."""
        if self.src.live:
            return
        now = time.monotonic()
        if self.wall0 is None or t < self.vt0:
            self.wall0, self.vt0 = now, t
            return
        delay = self.wall0 + (t - self.vt0) - now
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.5:
            self.wall0, self.vt0 = now, t

    def _publish(self, frame, force=False):
        now = time.monotonic()
        if not force and now - self.last_pub < 1 / STREAM_FPS:
            return
        self.last_pub = now
        ok, jpg = cv2.imencode(".jpg", fit(frame, 1280), [cv2.IMWRITE_JPEG_QUALITY, 82])
        if ok:
            with self.jpeg_cond:
                self.jpeg, self.jpeg_seq = jpg.tobytes(), self.jpeg_seq + 1
                self.jpeg_cond.notify_all()

    def _sample(self, frame, t):
        if self.next_motion_t is None or t >= self.next_motion_t:
            self.next_motion_t = t + 0.25
            grey = tiny_grey(frame)
            self.small.append((t, grey))
            while self.small and self.small[0][0] < t - 4:
                self.small.popleft()
            ref = None
            for ft, g in self.small:
                if ft <= t - 1:
                    ref = g
            self.motion = motion_between(grey, ref) if ref is not None else 0.0
            self.emit("motion", value=round(self.motion, 1), t=round(t, 2))
            if self.phase == "watching" and self.armed and t >= self.cooldown_until and self.motion >= self.trigger:
                self._start_event(t, "motion")
            elif (self.phase == "analyzing" and self.armed and self.pending_t is None and self.event
                  and t > self.event["ready_at"] and self.motion >= self.trigger):
                self.pending_t = t  # no blind spot: this gets its own check when the current one finishes
        if self.next_clip_t is None or t >= self.next_clip_t:
            self.next_clip_t = t + 1 / config.CLIP_FPS
            self.clip.append((t, fit(frame, 640)))
            while self.clip and self.clip[0][0] < t - 15:
                self.clip.popleft()
        if self.phase == "collecting" and t >= self.event["ready_at"]:
            self._dispatch()

    def _start_event(self, t, trigger):
        if trigger == "manual":
            self.event = {"trigger": trigger, "t0": t, "from": t - config.PRE_ROLL - config.POST_ROLL, "ready_at": t}
        else:
            self.event = {"trigger": trigger, "t0": t, "from": t - config.PRE_ROLL, "ready_at": t + config.POST_ROLL}
            self.log(f"Motion {self.motion:.0f}%: recording the next {config.POST_ROLL:g} seconds", "motion")
        self._set_phase("collecting")
        if trigger == "manual":
            self._dispatch()

    def _dispatch(self):
        ev = self.event
        frames = [(ft, f) for ft, f in self.clip if ev["from"] - 0.05 <= ft <= ev["ready_at"] + 0.05]
        if len(frames) < 2:
            frames = list(self.clip)[-int(config.CLIP_FPS * (config.PRE_ROLL + config.POST_ROLL)):]
        if not frames:
            self.event = None
            return self._set_phase("watching" if self.armed else "ready")
        self.calls += 1
        self._set_phase("analyzing")
        threading.Thread(target=self._analyze, args=(ev, frames), daemon=True).start()

    def _analyze(self, ev, frames):
        """Runs on a worker thread, so the video keeps playing while Nemotron thinks."""
        seconds = frames[-1][0] - frames[0][0]
        p = prepare(frames)
        kind, sheet_path, clip_path = p["kind"], p["sheet_path"], p["clip_path"]
        what = f"a {seconds:.0f}s clip" if kind == "video" else "4 frames"
        self.emit("sheet", url=f"/data/sheets/{sheet_path.name}", kind=kind, seconds=round(seconds, 1))
        self.log(f"Sent {what} to Nemotron", "ask")
        res = ask_model(p, config.CAMERA)
        record = {"at": datetime.now().isoformat(timespec="seconds"), "source": self.src.name, "trigger": ev["trigger"],
                  "from": round(frames[0][0], 1), "to": round(frames[-1][0], 1), "input": kind,
                  "sheet": str(sheet_path), "clip": str(clip_path) if clip_path else None}
        cooldown, disarm_reason = config.COOLDOWN_CLEAR, None
        if not res["ok"]:
            self.log(res["message"], "error")
            self.emit("verdict", ok=False, error=res["error"], message=res["message"])
            record.update(ok=False, error=res["error"], message=res["message"])
            retry = str(res.get("retry_after") or "")
            if retry.isdigit():
                cooldown = float(retry)
            if res["error"] in ("daily_cap", "no_key"):
                disarm_reason = "Disarmed: no Nemotron calls left today."
        else:
            v = res["verdict"]
            incident_t = v["t"] if frames[0][0] - 1 <= v["t"] <= frames[-1][0] + 1 else ev["t0"]
            # real seconds from the incident being on screen to now (works after a clip has ended, too)
            after = max(0.0, time.time() - (self.frame_wall - (self.t - incident_t)))
            is_alert = v["alert"] and v["confidence"] >= config.MIN_CONF
            label = ai.RULES.get(v["rule"], "INCIDENT")
            record.update(ok=True, verdict=v, alert=is_alert, latency=res["latency"], after=round(after, 1),
                          profile=ai.PROFILE_NAME, title=ai.TITLES.get(v["rule"], ai.TITLES["other"]))
            if is_alert:
                a = {**v, "t": incident_t, "origin": self.src.origin if self.src.live else None, "after": after,
                     "camera": config.CAMERA, "call": res["call"]}
                deliveries = alerts.send(a, sheet_path, clip_path)
                record["deliveries"] = deliveries
                self.emit("alert", rule=v["rule"], label=label, title=ai.TITLES.get(v["rule"], ai.TITLES["other"]),
                          why=v["why"], confidence=v["confidence"], after=round(after, 1), latency=res["latency"],
                          deliveries=deliveries, sheet=f"/data/sheets/{sheet_path.name}")
                self.log(f"🚨 {label} · {round(v['confidence'] * 100)}% · “{v['why']}” ({res['latency']:.1f}s)", "alert")
                for d in deliveries:
                    name_ = d["channel"].title()
                    self.log(f"Alert sent to {name_}" if d["ok"] else f"{name_} failed: {d['error']}", "tg" if d["ok"] else "error")
                cooldown = config.COOLDOWN_ALERT
            else:
                unsure = f" ({label}, only {round(v['confidence'] * 100)}% sure, so no alert)" if v["alert"] else ""
                self.emit("verdict", ok=True, alert=False, why=v["why"], latency=res["latency"])
                self.log(f"No incident{unsure}: “{v['why']}” ({res['latency']:.1f}s)", "clear")
        log_verdict(record)
        self.command("finish", cooldown, disarm_reason, bool(record.get("alert")))

    # ---- commands from the dashboard, run on the engine thread

    def _run_commands(self):
        while True:
            try:
                name, *args = self.cmds.get_nowait()
            except queue.Empty:
                return
            getattr(self, "_cmd_" + name)(*args)

    def _cmd_arm(self):
        if self.armed:
            return
        if not config.OPENROUTER_KEY:
            return self.log("Add OPENROUTER_API_KEY to .env first", "error")
        if self.src.kind == "file":
            if self.ended:
                self._restart_file()
            self.playing, self.wall0 = True, None
        self.armed, self.calls, self.pending_t = True, 0, None
        self.cooldown_until = self.t + SETTLE
        if self.phase == "ready":
            self._set_phase("watching")
        else:
            self._set_phase(self.phase)
        self.log(f"Armed: watching {config.CAMERA}")

    def _cmd_disarm(self):
        self.armed, self.pending_t = False, None
        if self.phase in ("watching", "collecting"):
            self.event = None
            self._set_phase("ready")
        else:
            self._set_phase(self.phase)
        self.log("Disarmed")

    def _cmd_check(self):
        if self.phase in ("analyzing", "collecting"):
            return
        if not config.OPENROUTER_KEY:
            return self.log("Add OPENROUTER_API_KEY to .env first", "error")
        if not self.clip:
            return self.log("Nothing recorded yet. Play the video first.", "error")
        self._start_event(self.t, "manual")

    def _cmd_restart(self):
        if self.src.kind != "file":
            return
        self._restart_file()
        if not self.armed:
            self.playing = False
            self._show_first_frame()
        self._set_phase(self.phase)

    def _restart_file(self):
        self.src.seek(self.src.start)
        self.small.clear()
        self.clip.clear()
        self.next_motion_t = self.next_clip_t = None
        self.ended, self.wall0, self.t, self.pending_t = False, None, self.src.start, None
        if self.phase == "collecting":
            self.event = None
            self.phase = "watching" if self.armed else "ready"
        self.emit("restart")

    def _cmd_pause(self):
        if self.src.kind != "file":
            return
        if self.ended:
            self._restart_file()
        self.playing, self.wall0 = not self.playing, None
        self._set_phase(self.phase)

    def _cmd_trigger(self, value):
        self.trigger = max(0.1, min(50.0, float(value)))

    def _cmd_finish(self, cooldown, disarm_reason=None, alerted=False):
        self.event = None
        pending, self.pending_t = self.pending_t, None
        if pending is not None and self.armed and not alerted and not disarm_reason and self.calls < self.max_per_arm:
            self.event = {"trigger": "motion", "t0": pending, "from": pending - config.PRE_ROLL,
                          "ready_at": pending + config.POST_ROLL}
            self.log(f"More motion at {pending:.1f}s while Nemotron was busy, checking that too", "motion")
            self._set_phase("collecting")
            if self.t >= self.event["ready_at"] or self.ended:
                self._dispatch()
            return
        if disarm_reason:
            self.armed = False
            self.log(disarm_reason)
        if self.armed and self.calls >= self.max_per_arm:
            self.armed = False
            self.log(f"{self.calls} calls used this round, disarmed to save quota. Press Space to arm again.")
        if self.armed:
            self.cooldown_until = self.t + cooldown
        self._set_phase("watching" if self.armed else "ready")
        self.emit_status()

    def _set_phase(self, phase):
        self.phase = phase
        self.emit("phase", phase=phase, armed=self.armed, playing=self.playing)
