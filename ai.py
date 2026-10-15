"""Ask Nemotron 3.5 Super VL (free on OpenRouter) whether a clip shows an incident, within a daily budget."""
import base64
import json
import re
import threading
import time
from datetime import datetime, timezone

import config
from net import error_text, http

OPENROUTER = "https://openrouter.ai/api/v1"

# Scenario packs: the same camera loop, different things to watch for. rule -> (title, what counts)
PROFILES = {
    "home": {
        "camera": "Living Room",
        "context": "You watch a camera inside a family home, where an elderly parent may be alone, and decide whether "
                   "the family must be alerted.",
        "rules": {
            "person_fell": ("Fall detected", "a person fell and stays down, or is lying on the floor unable to get up"),
            "object_fell": ("Something fell", "something fell, broke or shattered"),
            "liquid_on_floor": ("Spill on the floor", "liquid is spilled or spreading on the floor (slip risk)"),
            "smoke_or_fire": ("Smoke or fire", "smoke or flames are visible"),
        },
        "normal": "Walking, sitting, cooking, cleaning, and lying down on a bed or sofa to rest are not incidents.",
    },
    "shop": {
        "camera": "Aisle 3",
        "context": "You watch a CCTV camera in a small shop, supermarket or warehouse and decide whether the owner "
                   "must be alerted.",
        "rules": {
            "object_fell": ("Item broke", "a product, glass, bottle or jar fell, broke or shattered, or a shelf or stack fell"),
            "liquid_on_floor": ("Spill on the floor", "liquid is spilled or spreading on the floor (slip risk)"),
            "person_fell": ("Person fell", "a person fell and stays down"),
            "robbery_or_theft": ("Possible theft", "someone hides goods in clothes or a bag, grabs and runs, or forces a till or door"),
            "fight": ("Fight", "people are fighting"),
            "vandalism": ("Damage", "glass, a door or fixtures are being damaged on purpose"),
        },
        "normal": "Shopping, picking up and putting back items, walking, restocking and cleaning are not incidents.",
    },
    "exam": {
        "camera": "Exam Hall · Room 101",
        "context": "You watch a CCTV camera in an exam hall during an exam and flag behaviour for an invigilator to "
                   "check. You never decide that anyone cheated; you only point out what to look at.",
        "rules": {
            "phone_use": ("Phone in exam", "a student is looking at or using a phone or another device"),
            "copying": ("Possible copying", "a student keeps looking at a neighbour's answer sheet, or students pass a paper or note"),
            "hidden_notes": ("Hidden notes", "a student reads a hidden paper, notes on the hand, or notes under the desk"),
            "talking": ("Talking", "students are talking to each other"),
        },
        "normal": "Writing, thinking, stretching, glancing up or around briefly, raising a hand and talking to the "
                  "invigilator are normal.",
    },
    "traffic": {
        "camera": "MG Road Junction",
        "context": "You watch a traffic camera at a road junction and flag violations for a traffic officer to review.",
        "rules": {
            "red_light_jump": ("Red light jump", "a vehicle crosses the stop line or the junction while its signal is red"),
            "no_helmet": ("No helmet", "a rider or pillion on a two-wheeler is not wearing a helmet"),
            "triple_riding": ("Triple riding", "three or more people ride one two-wheeler"),
            "wrong_side": ("Wrong-side driving", "a vehicle drives against the flow of traffic"),
            "accident": ("Accident", "vehicles collide, or a vehicle or person falls on the road"),
        },
        "normal": "Vehicles moving with traffic, stopping at signals, and people crossing at a crossing are normal. "
                  "Do not read out number plates.",
    },
}

PROFILE_NAME, PROFILE, RULES, TITLES, PROMPTS, SCHEMA = None, None, {}, {}, {}, {}


def set_profile(name):
    """Switch the scenario pack: the rules in the prompt, the JSON schema, and the alert titles."""
    global PROFILE_NAME, PROFILE, RULES, TITLES, PROMPTS, SCHEMA
    if name not in PROFILES:
        raise SystemExit(f"Unknown profile {name!r}. Pick one of: {', '.join(PROFILES)}")
    PROFILE_NAME, PROFILE = name, PROFILES[name]
    if not config.env("CAMERA_NAME"):
        config.CAMERA = PROFILE["camera"]
    rules = PROFILE["rules"]
    TITLES = {k: title for k, (title, _) in rules.items()}
    TITLES["other"] = "Something's wrong"
    RULES = {k: t.upper() for k, t in TITLES.items() if k != "other"}
    rules_text = "\n".join(f"- {k}: {what}" for k, (_, what) in rules.items())
    body = (f"Alert only for these, and only when it is clearly visible:\n{rules_text}\n\n{PROFILE['normal']}\n"
            "Never identify or describe a person beyond the action itself: no clothing, age, gender, ethnicity or names.\n\n"
            "Reply with JSON only:\n"
            '{"alert": true or false, "rule": "<one rule name above, or none>", "t": <the t= time where it first '
            'shows, or 0>, "confidence": <0 to 1>, "why": "<at most 12 words on what happened>"}')
    PROMPTS = {
        "video": f"{PROFILE['context']}\n\nYou get a short video clip from one camera. Every frame shows its time as "
                 f"t=...s in the top-left corner.\n\n{body}",
        "frames": f"{PROFILE['context']}\n\nYou get one image: a 2x2 contact sheet of consecutive frames from one "
                  f"camera, read left to right, then top to bottom. Each frame shows its time as t=...s.\n\n{body}",
    }
    SCHEMA = {
        "type": "object",
        "properties": {
            "alert": {"type": "boolean"},
            "rule": {"type": "string", "enum": [*rules, "none"]},
            "t": {"type": "number"},
            "confidence": {"type": "number"},
            "why": {"type": "string"},
        },
        "required": ["alert", "rule", "t", "confidence", "why"],
        "additionalProperties": False,
    }


set_profile(config.PROFILE)


def auth():
    return {"Authorization": f"Bearer {config.OPENROUTER_KEY}", "X-Title": "ShopCam Alert"}


# ---------------------------------------------------------------- daily budget

_lock = threading.Lock()
USAGE = config.DATA / "usage.json"
_free = {"at": 0.0, "value": None}


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")  # OpenRouter's free counter resets at 00:00 UTC


def used_today():
    try:
        return json.loads(USAGE.read_text()).get(today(), 0)
    except (OSError, ValueError):
        return 0


def count_call():
    with _lock:
        try:
            counts = json.loads(USAGE.read_text())
        except (OSError, ValueError):
            counts = {}
        counts[today()] = counts.get(today(), 0) + 1
        config.DATA.mkdir(exist_ok=True)
        USAGE.write_text(json.dumps(counts, indent=1))
        return counts[today()]


def free_quota(max_age=60):
    """OpenRouter's own count of today's free-model calls: {"used", "limit", "remaining"}, cached."""
    if not config.OPENROUTER_KEY:
        return None
    if time.time() - _free["at"] > max_age:
        status, body, _ = http("GET", f"{OPENROUTER}/key", headers=auth(), timeout=6)
        _free["at"] = time.time()
        if status == 200:
            _free["value"] = (body.get("data") or {}).get("free_model_daily_requests")
    return _free["value"]


# ---------------------------------------------------------------- the call

def ask(media, kind, times, camera):
    """kind: "video" (mp4 bytes) or "frames" (a JPEG contact sheet). Returns (status, body, headers, seconds)."""
    span = f"t = {times[0]:.1f} to {times[-1]:.1f} s" if times else "unknown times"
    if kind == "video":
        part = {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(media).decode()}}
    else:
        part = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(media).decode()}}
    payload = {
        "model": config.MODEL,
        "messages": [
            {"role": "system", "content": PROMPTS[kind]},
            {"role": "user", "content": [{"type": "text", "text": f"Camera: {camera}. Footage {span}. Is there an incident?"}, part]},
        ],
        "temperature": 0.1,
        "max_tokens": 300,
    }
    if config.REASONING == "off":
        payload["reasoning"] = {"effort": "none"}
    if config.STRUCTURED:
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "shop_alert", "strict": True, "schema": SCHEMA}}
    started = time.time()
    status, body, headers = http("POST", f"{OPENROUTER}/chat/completions", payload, auth(), timeout=120)
    return status, body, headers, time.time() - started


def parse_verdict(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        v = json.loads(match.group(0)) if match else None
    except ValueError:
        v = None
    if not isinstance(v, dict):
        return None

    def num(x, default=0.0):
        try:
            return float(x)
        except (TypeError, ValueError):
            return default

    alert = bool(v.get("alert"))
    rule = str(v.get("rule") or "none")
    if rule not in RULES:
        rule = "other" if alert else "none"
    return {"alert": alert, "rule": rule, "t": num(v.get("t")),
            "confidence": max(0.0, min(1.0, num(v.get("confidence"), 0.5))), "why": str(v.get("why") or "")[:140]}


def check(media, kind, times, camera):
    """One budgeted Nemotron check. Returns {"ok": True, "verdict", "latency", "call"} or {"ok": False, ...}."""
    if not config.OPENROUTER_KEY:
        return {"ok": False, "error": "no_key", "message": "OPENROUTER_API_KEY is missing in .env"}
    if used_today() >= config.DAILY_CAP:
        return {"ok": False, "error": "daily_cap", "message": f"Daily budget used ({config.DAILY_CAP} calls). It resets at 05:30 IST."}
    quota = free_quota()
    if quota and quota.get("remaining") is not None and quota["remaining"] <= 0:
        return {"ok": False, "error": "daily_cap", "message": "OpenRouter says today's free calls are used up."}
    call_no = count_call()  # counted before the call: a failed call can still use quota
    status, body, headers, latency = ask(media, kind, times, camera)
    _free["at"] = 0  # re-read OpenRouter's count next time
    config.DATA.mkdir(exist_ok=True)
    with open(config.DATA / "calls.jsonl", "a") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "model": config.MODEL, "input": kind,
                            "bytes": len(media), "times": times, "latency": round(latency, 2), "status": status,
                            "usage": body.get("usage"), "error": None if status == 200 else error_text(body, status)}) + "\n")
    choice = (body.get("choices") or [{}])[0]
    if status != 200 or "error" in body or not choice:
        return {"ok": False, "error": "rate_limited" if status == 429 else "model_error", "status": status,
                "message": error_text(body, status), "retry_after": headers.get("Retry-After"), "call": call_no}
    content = (choice.get("message") or {}).get("content")
    verdict = parse_verdict(content)
    if verdict is None:
        return {"ok": False, "error": "bad_json", "message": f"Couldn't read the answer: {str(content)[:160]!r}", "call": call_no}
    return {"ok": True, "verdict": verdict, "latency": round(latency, 2), "call": call_no, "usage": body.get("usage")}
