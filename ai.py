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
FIRE = ("Smoke or fire", "smoke or flames are visible")
PROFILES = {
    "home": {
        "camera": "Living Room",
        "context": "You watch a camera inside a family home, where an elderly parent may be alone, and decide whether "
                   "the family must be alerted.",
        "rules": {
            "person_fell": ("Fall detected", "a person fell and stays down, or is lying on the floor unable to get up"),
            "needs_help": ("Needs help", "a person waves for help, clutches their chest or head in pain, or cannot "
                           "get up from a chair or bed"),
            "object_fell": ("Something fell", "something fell, broke or shattered"),
            "liquid_on_floor": ("Spill on the floor", "liquid is spilled or spreading on the floor (slip risk)"),
            "smoke_or_fire": FIRE,
        },
        "normal": "Walking, sitting, cooking, cleaning, exercising, and lying down on a bed or sofa to rest are not "
                  "incidents.",
    },
    "shop": {
        "camera": "Aisle 3",
        "context": "You watch a CCTV camera in a small shop, supermarket or warehouse and decide whether the owner "
                   "must be alerted.",
        "rules": {
            "object_fell": ("Item broke", "a product, glass, bottle or jar fell, broke or shattered, or a shelf or "
                            "stack fell"),
            "liquid_on_floor": ("Spill on the floor", "liquid is spilled or spreading on the floor (slip risk)"),
            "person_fell": ("Person fell", "a person fell and stays down"),
            "shoplifting": ("Possible shoplifting", "someone hides goods in clothes or a bag instead of a basket, or "
                            "walks out with unpaid goods"),
            "robbery": ("Robbery", "someone threatens staff, forces the till, or grabs goods and runs"),
            "fight": ("Fight", "people are fighting"),
            "vandalism": ("Damage", "glass, a door, shelves or fixtures are damaged on purpose"),
            "long_queue": ("Long queue", "more than five people are waiting in line at the counter"),
            "smoke_or_fire": FIRE,
        },
        "normal": "Shopping, picking items up and putting them back or in a basket, walking, restocking and cleaning "
                  "are not incidents.",
    },
    "parking": {
        "camera": "Gate Camera",
        "context": "You watch a security camera outside a home or shop that covers the gate, the street and parked "
                   "vehicles, and decide whether the owner must be alerted.",
        "rules": {
            "vehicle_theft": ("Possible car theft", "someone opens the bonnet, door or boot of a parked vehicle and "
                              "works on it or removes parts, or breaks into it"),
            "door_checking": ("Checking car doors", "someone tries the doors or peers into the windows of parked cars "
                              "one after another"),
            "theft": ("Possible theft", "someone takes something that isn't theirs (a bag, parcel, bike, plant or "
                      "goods) and leaves"),
            "break_in": ("Break-in attempt", "someone forces or climbs a gate, door, window or lock"),
            "vandalism": ("Damage", "someone damages a vehicle or property on purpose"),
            "accident": ("Accident", "a vehicle hits something or someone"),
            "smoke_or_fire": FIRE,
        },
        "normal": "People walking past, cars parking or driving away, deliveries, and people getting into or out of "
                  "their own car are normal. Two people arriving together, one keeping watch while another works on "
                  "a parked car they did not arrive in, is suspicious.",
    },
    "exam": {
        "camera": "Exam Hall · Room 101",
        "context": "You watch a CCTV camera in an exam hall during an exam and flag behaviour for an invigilator to "
                   "check. You never decide that anyone cheated; you only point out what to look at.",
        "rules": {
            "phone_use": ("Phone in exam", "a student is looking at or using a phone or another device"),
            "copying": ("Possible copying", "a student keeps looking at a neighbour's answer sheet, or students pass "
                        "a paper or note"),
            "hidden_notes": ("Hidden notes", "a student reads a hidden paper, notes on the hand, or notes under the desk"),
            "talking": ("Talking", "students are talking to each other"),
            "left_seat": ("Left seat", "a student leaves their seat or walks around the hall without the invigilator"),
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
            "phone_driving": ("Phone while driving", "a driver or rider uses a phone while moving"),
            "on_crossing": ("Stopped on the crossing", "a vehicle stops on the zebra crossing"),
            "accident": ("Accident", "vehicles collide, or a vehicle or person falls on the road"),
        },
        "normal": "Vehicles moving with traffic, stopping at signals, and people crossing at a crossing are normal. "
                  "Do not read out number plates.",
    },
    "pets": {
        "camera": "Living Room",
        "context": "You watch a home camera that keeps an eye on a pet while the owner is out, and tell the owner "
                   "when the pet does something they would want to know about.",
        "rules": {
            "on_furniture": ("On the sofa again", "the pet climbs or lies on a sofa, bed or chair"),
            "on_counter": ("On the counter", "the pet climbs onto a kitchen counter, dining table or shelf"),
            "chewing": ("Chewing something", "the pet chews shoes, cables, furniture or anything that is not its toy"),
            "knocked_over": ("Knocked something over", "the pet knocks something off a table or shelf, or tips "
                             "something over"),
            "mess": ("Made a mess", "the pet pees or poops indoors, or tears up cushions, paper or plants"),
        },
        "normal": "Walking around, playing with its own toys, eating or drinking from its bowl, and sleeping in its "
                  "own bed are normal.",
    },
    "site": {
        "camera": "Site Gate",
        "context": "You watch a camera on a construction site or factory floor and flag safety problems for the "
                   "supervisor.",
        "rules": {
            "no_hard_hat": ("No hard hat", "a worker on site is not wearing a hard hat"),
            "no_vest": ("No safety vest", "a worker is not wearing a high-visibility vest"),
            "danger_zone": ("Danger zone", "a person is very close to moving machinery or a vehicle, or under a "
                            "suspended load"),
            "person_fell": ("Person fell", "a worker falls, or is lying on the ground"),
            "smoke_or_fire": FIRE,
        },
        "normal": "Workers walking, carrying material, and operating machines while wearing protective gear are normal.",
    },
}

_PACKS = {}


def pack(name):
    """Everything one scenario pack needs: its rules, alert titles, prompts and JSON schema (cached)."""
    if name not in PROFILES:
        raise SystemExit(f"Unknown profile {name!r}. Pick one of: {', '.join(PROFILES)}")
    if name not in _PACKS:
        prof = PROFILES[name]
        rules = prof["rules"]
        titles = {k: title for k, (title, _) in rules.items()}
        titles["other"] = "Something's wrong"
        rules_text = "\n".join(f"- {k}: {what}" for k, (_, what) in rules.items())
        body = (f"Alert only for these, and only when it is clearly visible:\n{rules_text}\n\n{prof['normal']}\n"
                "Never identify or describe a person beyond the action itself: no clothing, age, gender, ethnicity or names.\n\n"
                "Reply with JSON only:\n"
                '{"alert": true or false, "rule": "<one rule name above, or none>", "t": <the t= time where it first '
                'shows, or 0>, "confidence": <0 to 1>, "why": "<at most 12 words on what happened>"}')
        _PACKS[name] = {
            "name": name, "camera": prof["camera"], "rules": rules, "titles": titles,
            "labels": {k: t.upper() for k, t in titles.items() if k != "other"},
            "prompts": {
                "video": f"{prof['context']}\n\nYou get a short video clip from one camera. Every frame shows its time "
                         f"as t=...s in the top-left corner.\n\n{body}",
                "frames": f"{prof['context']}\n\nYou get one image: a 2x2 contact sheet of consecutive frames from one "
                          f"camera, read left to right, then top to bottom. Each frame shows its time as t=...s.\n\n{body}",
            },
            "schema": {
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
            },
        }
    return _PACKS[name]


# the default pack for single-camera runs (PROFILE in .env or --profile)
PROFILE_NAME, PROFILE, RULES, TITLES = None, None, {}, {}


def set_profile(name):
    """Switch the default scenario pack: the rules in the prompt, the JSON schema, and the alert titles."""
    global PROFILE_NAME, PROFILE, RULES, TITLES
    pk = pack(name)
    PROFILE_NAME, PROFILE, RULES, TITLES = name, PROFILES[name], pk["labels"], pk["titles"]
    if not config.env("CAMERA_NAME"):
        config.CAMERA = pk["camera"]


set_profile(config.PROFILE)


def ready():
    """Can we ask the model? A local Ollama, or an OpenRouter key."""
    return config.PROVIDER == "ollama" or bool(config.OPENROUTER_KEY)


def auth():
    return {"Authorization": f"Bearer {config.OPENROUTER_KEY}", "X-Title": "Nazar"}


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
    if config.PROVIDER == "ollama" or not config.OPENROUTER_KEY:
        return None
    if time.time() - _free["at"] > max_age:
        status, body, _ = http("GET", f"{OPENROUTER}/key", headers=auth(), timeout=6)
        _free["at"] = time.time()
        if status == 200:
            _free["value"] = (body.get("data") or {}).get("free_model_daily_requests")
    return _free["value"]


# ---------------------------------------------------------------- the call

def ask(media, kind, times, camera, pk):
    """kind: "video" (mp4 bytes) or "frames" (a JPEG contact sheet). Returns (status, body, headers, seconds)."""
    span = f"t = {times[0]:.1f} to {times[-1]:.1f} s" if times else "unknown times"
    if kind == "video":
        part = {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(media).decode()}}
    else:
        part = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(media).decode()}}
    payload = {
        "model": config.MODEL,
        "messages": [
            {"role": "system", "content": pk["prompts"][kind]},
            {"role": "user", "content": [{"type": "text", "text": f"Camera: {camera}. Footage {span}. Is there an incident?"}, part]},
        ],
        "temperature": 0.1,
        "max_tokens": 300,
    }
    if config.REASONING == "off":
        payload["reasoning"] = {"effort": "none"}
    if config.STRUCTURED:
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "camera_alert", "strict": True, "schema": pk["schema"]}}
    started = time.time()
    status, body, headers = http("POST", f"{OPENROUTER}/chat/completions", payload, auth(), timeout=120)
    return status, body, headers, time.time() - started


def ask_ollama(media, kind, times, camera, pk):
    """The same question to a local Ollama (native /api/chat), with the JSON schema as the output format."""
    span = f"t = {times[0]:.1f} to {times[-1]:.1f} s" if times else "unknown times"
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": pk["prompts"]["frames"]},
            {"role": "user", "content": f"Camera: {camera}. Footage {span}. Is there an incident?",
             "images": [base64.b64encode(media).decode()]},
        ],
        "stream": False,
        "format": pk["schema"] if config.STRUCTURED else "json",
        "options": {"temperature": 0.1},
    }
    if config.REASONING == "off":
        payload["think"] = False
    started = time.time()
    status, body, headers = http("POST", f"{config.OLLAMA_URL}/api/chat", payload, timeout=600)
    if status == 200 and isinstance(body.get("message"), dict):  # reshape like an OpenAI-style answer
        body = {"choices": [{"message": {"content": body["message"].get("content")}}],
                "usage": {"prompt_tokens": body.get("prompt_eval_count"), "completion_tokens": body.get("eval_count")}}
    elif status == 0:
        body = {"error": {"message": f"Ollama isn't reachable at {config.OLLAMA_URL} (is `ollama serve` running?)"}}
    return status, body, headers, time.time() - started


def parse_verdict(text, rules):
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
    if rule not in rules:
        rule = "other" if alert else "none"
    return {"alert": alert, "rule": rule, "t": num(v.get("t")),
            "confidence": max(0.0, min(1.0, num(v.get("confidence"), 0.5))), "why": str(v.get("why") or "")[:140]}


def check(media, kind, times, camera, profile=None):
    """One budgeted Nemotron check. Returns {"ok": True, "verdict", "latency", "call"} or {"ok": False, ...}."""
    pk = pack(profile or PROFILE_NAME)
    local = config.PROVIDER == "ollama"
    if not local:  # the hosted free tier has a daily budget; a local model doesn't
        if not config.OPENROUTER_KEY:
            return {"ok": False, "error": "no_key", "message": "OPENROUTER_API_KEY is missing in .env"}
        if used_today() >= config.DAILY_CAP:
            return {"ok": False, "error": "daily_cap", "message": f"Daily budget used ({config.DAILY_CAP} calls). It resets at 05:30 IST."}
        quota = free_quota()
        if quota and quota.get("remaining") is not None and quota["remaining"] <= 0:
            return {"ok": False, "error": "daily_cap", "message": "OpenRouter says today's free calls are used up."}
    call_no = None if local else count_call()  # counted before the call: a failed call can still use quota
    status, body, headers, latency = (ask_ollama if local else ask)(media, kind, times, camera, pk)
    _free["at"] = 0  # re-read OpenRouter's count next time
    config.DATA.mkdir(exist_ok=True)
    with open(config.DATA / "calls.jsonl", "a") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                            "model": config.OLLAMA_MODEL if local else config.MODEL, "input": kind,
                            "bytes": len(media), "times": times, "latency": round(latency, 2), "status": status,
                            "usage": body.get("usage"), "error": None if status == 200 else error_text(body, status)}) + "\n")
    choice = (body.get("choices") or [{}])[0]
    if status != 200 or "error" in body or not choice:
        return {"ok": False, "error": "rate_limited" if status == 429 else "model_error", "status": status,
                "message": error_text(body, status), "retry_after": headers.get("Retry-After"), "call": call_no}
    content = (choice.get("message") or {}).get("content")
    verdict = parse_verdict(content, pk["rules"])
    if verdict is None:
        return {"ok": False, "error": "bad_json", "message": f"Couldn't read the answer: {str(content)[:160]!r}", "call": call_no}
    return {"ok": True, "verdict": verdict, "latency": round(latency, 2), "call": call_no, "usage": body.get("usage")}
