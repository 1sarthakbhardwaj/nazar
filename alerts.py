"""Send an alert to Telegram (with the frames) and/or to a Hermes Agent webhook."""
import hashlib
import hmac
import html
import json
import time
import uuid
from datetime import datetime

import config
import ai
from net import error_text, http, multipart


def telegram(method, fields, file=None, timeout=30):
    """file: (field, filename, bytes, mime) for sendPhoto / sendVideo."""
    if not config.TELEGRAM_BOT_TOKEN:
        return False, "TELEGRAM_BOT_TOKEN is missing in .env"
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    if file:
        body, ctype = multipart(fields, *file)
        status, resp, _ = http("POST", url, body=body, headers={"Content-Type": ctype}, timeout=timeout)
    else:
        status, resp, _ = http("POST", url, payload=fields, timeout=timeout)
    if status == 200 and resp.get("ok"):
        return True, resp.get("result")
    return False, error_text(resp, status)


def lines(a, rich):
    """a: dict with rule, why, after, camera. Short and human: what, where, when."""
    esc = html.escape if rich else (lambda s: s)
    bold = (lambda s: f"<b>{s}</b>") if rich else (lambda s: s)
    out = [f"🚨 {bold(ai.TITLES.get(a['rule'], ai.TITLES['other']))} · {esc(a['camera'])}"]
    if a["why"]:
        out.append(f"“{esc(a['why'])}”")
    now = datetime.now().strftime("%I:%M %p").lstrip("0")
    out.append(f"{now} · alert sent {a['after']:.0f}s after it happened")
    return "\n".join(out)


def to_telegram(a, sheet_path, clip_path):
    if not config.TELEGRAM_CHAT_ID:
        return False, "TELEGRAM_CHAT_ID missing (run: python3 app.py telegram)"
    fields = {"chat_id": config.TELEGRAM_CHAT_ID, "parse_mode": "HTML", "caption": lines(a, rich=True)}
    if config.ALERT_MEDIA == "video" and clip_path:
        ok, info = telegram("sendVideo", {**fields, "supports_streaming": "true"},
                            ("video", "incident.mp4", clip_path.read_bytes(), "video/mp4"))
        if ok:
            return ok, info
    return telegram("sendPhoto", fields, ("photo", "incident.jpg", sheet_path.read_bytes(), "image/jpeg"))


def to_hermes(a, sheet_path):
    """POST to a Hermes webhook route with deliver_only, so Hermes forwards it without an LLM call."""
    if not config.HERMES_WEBHOOK_SECRET:
        return False, "HERMES_WEBHOOK_SECRET missing in .env"
    text = lines(a, rich=False)
    if config.HERMES_MEDIA:
        text += f"\nMEDIA:{sheet_path.resolve()}"
    body = json.dumps({"event": "nazar_alert", "text": text, "rule": a["rule"], "why": a["why"],
                       "confidence": a["confidence"], "camera": a["camera"], "image": str(sheet_path.resolve())})
    ts = str(int(time.time()))
    sig = hmac.new(config.HERMES_WEBHOOK_SECRET.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    status, resp, _ = http("POST", config.HERMES_WEBHOOK_URL, body=body.encode(), timeout=15, headers={
        "Content-Type": "application/json", "X-Webhook-Timestamp": ts, "X-Webhook-Signature-V2": sig,
        "X-Request-ID": str(uuid.uuid4())})
    if status in (200, 202):
        return True, resp
    return False, error_text(resp, status) if status else f"Hermes not reachable at {config.HERMES_WEBHOOK_URL}"


def send(a, sheet_path, clip_path=None):
    """Deliver one alert on every channel in ALERTS. Returns [{"channel", "ok", "error"}]."""
    results = []
    for channel in config.ALERTS:
        if channel == "telegram":
            ok, info = to_telegram(a, sheet_path, clip_path)
        elif channel == "hermes":
            ok, info = to_hermes(a, sheet_path)
        else:
            ok, info = False, f"unknown channel {channel!r}"
        results.append({"channel": channel, "ok": ok, "error": None if ok else str(info)})
    return results
