"""Settings, read once from .env (and the environment)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ENV_FILES = [ROOT / ".env.local", ROOT / ".env"]  # .env.local wins over .env
ENV_PATH = next((f for f in ENV_FILES if f.exists()), ROOT / ".env")


def load_env():
    for path in ENV_FILES:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def write_env(key, value):
    """Save one setting in the env file you use (.env.local if you have one, else .env)."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


load_env()
env = os.environ.get

# the model: OpenRouter (hosted, free tier) or Ollama (fully local, nothing leaves the machine)
PROVIDER = env("PROVIDER", "openrouter")       # openrouter | ollama
OLLAMA_URL = env("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = env("OLLAMA_MODEL", "nemotron-3.5-super")
OPENROUTER_KEY = env("OPENROUTER_API_KEY", "")
MODEL = env("MODEL", "nvidia/nemotron-3.5-super-vl:free")
MODEL_LABEL = "Nemotron 3.5 Super VL" if "nemotron-3.5-super-vl" in MODEL else MODEL
AI_INPUT = env("AI_INPUT", "video")            # video: send a short mp4 clip | frames: send a 2x2 contact sheet
REASONING = env("REASONING", "off")            # off | default
STRUCTURED = env("STRUCTURED", "1") != "0"     # ask for strict JSON output
DAILY_CAP = int(env("DAILY_CAP", "45"))        # OpenRouter allows 50 free calls a day; keep 5 spare
MIN_CONF = float(env("ALERT_MIN_CONFIDENCE", "0.5"))
if PROVIDER == "ollama":
    AI_INPUT = "frames"                        # Ollama takes images, not video: send the 4-frame sheet
    MODEL_LABEL = "Nemotron 3.5 Super (local)" if OLLAMA_MODEL.startswith("nemotron-3.5-super") else OLLAMA_MODEL

# the camera
SOURCE = env("SOURCE", "0")                    # file path, webcam index (0) or stream URL (rtsp://...)
APP_NAME = env("APP_NAME", "Nazar")
PROFILE = env("PROFILE", "home")               # home | shop | exam | traffic: what the camera watches for
CAMERA = env("CAMERA_NAME", "")                # empty = the profile's default name (Living Room, Aisle 3, ...)
PORT = int(env("PORT", "8765"))

# detection
MOTION_TRIGGER = float(env("MOTION_TRIGGER", "0.8"))   # % of the picture that changed in the last second
PRE_ROLL = float(env("PRE_ROLL", "2"))               # seconds before the motion that go into the clip
POST_ROLL = float(env("POST_ROLL", "4"))             # seconds after the motion (the aftermath matters)
CLIP_FPS = float(env("CLIP_FPS", "6"))               # frames per second in the clip sent to the model
MAX_PER_ARM = int(env("MAX_PER_ARM", "6"))           # calls per arming in the dashboard, then it disarms
COOLDOWN_ALERT = float(env("COOLDOWN_ALERT", "45"))   # quiet time after an alert, so one incident is one alert
COOLDOWN_CLEAR = float(env("COOLDOWN_CLEAR", "4"))
MIN_CHECK_GAP = float(env("MIN_CHECK_GAP", "20"))    # always-on cameras: least seconds between two checks

# alerts
ALERTS = [a.strip() for a in env("ALERTS", "telegram").split(",") if a.strip()]  # telegram, hermes
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID", "")
ALERT_MEDIA = env("ALERT_MEDIA", "photo")            # photo: the 4-frame sheet | video: the clip
HERMES_WEBHOOK_URL = env("HERMES_WEBHOOK_URL", "http://localhost:8644/webhooks/nazar")
HERMES_WEBHOOK_SECRET = env("HERMES_WEBHOOK_SECRET", "")
HERMES_MEDIA = env("HERMES_MEDIA", "1") != "0"       # add a MEDIA:/path line so Hermes attaches the frames
