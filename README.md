# Nazar

**Gone are the days of training a custom YOLO for every use case.**

Nazar is an AI camera that watches over a home, shop, gate, exam hall, junction, pet or worksite, and messages you on Telegram when something goes wrong. There is no dataset to label and no model to train: each use case is a few lines of plain English, and one vision model handles all 43 alert types.

It runs on a laptop for $0. A motion check runs locally, so the AI is only asked when something moves. Then a 6-second clip around the motion goes to **NVIDIA Nemotron 3.5 Super VL**, free on OpenRouter, or to Nemotron 3.5 Super on your own machine with Ollama. If it sees an incident, the alert and the frames land in Telegram, directly or through **Hermes Agent**.

```
home camera / RTSP / webcam / clip ─► motion check (local, free) ─► 6s clip ─► Nemotron ─► Telegram / Hermes
```

## Scenario packs

One camera loop, seven things it can watch for. Pick one with `--profile` (or `PROFILE=` in `.env`).

| Pack | Watches for |
|---|---|
| `home` (default) | Fall detected, needs help, something fell, spill on the floor, smoke or fire |
| `shop` | Item broke, spill, person fell, possible shoplifting, robbery, fight, damage, long queue, smoke or fire |
| `parking` | Possible car theft, checking car doors, possible theft, break-in attempt, damage, accident, smoke or fire |
| `exam` | Phone in exam, possible copying, hidden notes, talking, left seat. Flags for the invigilator, never decides guilt |
| `traffic` | Red light jump, no helmet, triple riding, wrong-side driving, phone while driving, stopped on the crossing, accident |
| `pets` | On the sofa again, on the counter, chewing something, knocked something over, made a mess |
| `site` | No hard hat, no safety vest, danger zone, person fell, smoke or fire |

Each pack sets what the model is told to look for, the alert titles and a default camera name. Adding a pack is one entry in `PROFILES` in `ai.py`.

## Quick start

```bash
pip install -r requirements.txt   # opencv-python, numpy; also needs ffmpeg (brew install ffmpeg)
cp .env.example .env              # add OPENROUTER_API_KEY and TELEGRAM_BOT_TOKEN (.env.local works too)
python3 app.py telegram           # press Start on your bot; saves your chat id
python3 app.py check              # keys, free quota, Telegram (no model calls)
```

Try it on real footage: recorded falls with labelled times, CC BY 4.0.

```bash
python3 app.py fetch                                                  # 4 clips from GMDCSA-24, about 35 MB
python3 app.py --source datasets/gmdcsa24/s2-fall-12.mp4              # dashboard opens; press Space
```

Watch your webcam: `python3 app.py --source 0`. For a home or shop camera, see the next section.

## Connect your camera

Nazar reads any camera that gives an RTSP or HTTP stream, which covers most home and shop cameras.

```bash
python3 app.py cameras                                # stream addresses for common brands
python3 app.py discover                               # find ONVIF cameras on your network
python3 app.py probe --brand tapo --ip 192.168.1.20   # check the stream works, saves one frame (no AI calls)
python3 app.py --brand tapo --ip 192.168.1.20         # the dashboard on that camera
```

| `--brand` | Cameras |
|---|---|
| `tapo` | TP-Link Tapo (make a camera account in the Tapo app first) |
| `hikvision` | Hikvision cameras and NVRs |
| `dahua`, `cpplus`, `amcrest` | Dahua and its OEMs, CP Plus, Amcrest, Lorex |
| `reolink` | Reolink (switch on RTSP in its network settings) |
| `ezviz` | EZVIZ (password = the code on the camera's sticker) |
| `ipwebcam` | An old Android phone running the IP Webcam app |
| `go2rtc` | go2rtc, Frigate or Home Assistant restreams: the way in for Ring, Nest, Wyze and other cloud cameras |

The camera user and password come from `CAMERA_USER` and `CAMERA_PASS` in `.env`. Nazar uses the lighter sub-stream by default (`--hd` for the main one), since the AI only needs 640px. Any other camera works with its full address: `--source "rtsp://user:pass@IP:554/your/path"`.

**Several cameras at once:**

```bash
cp cameras.example.json cameras.json   # name, brand or source, pack and options per camera
python3 app.py watch                   # watches them all, no dashboard; alerts go to Telegram / Hermes
```

Each camera gets its own pack: the living room on `home`, the gate on `parking`, the counter on `shop`. Passwords stay in `.env` and are referenced as `${NAME}`, so `cameras.json` never holds one (it's git-ignored anyway). Per camera you can also set:

- `zone`: only watch part of the picture, as `[left, top, right, bottom]` from 0 to 1 (skip the road, watch the driveway).
- `trigger`: how much has to move before Nazar looks, in % of the picture.
- `min_gap`: least seconds between two checks.

**Running 24/7.** A busy camera can move all day. On live cameras Nazar waits at least `MIN_CHECK_GAP` (20s) between checks, and stops at `DAILY_CAP` calls a day. The free tier is 50 calls a day, or 1,000 once your OpenRouter account has $10 of credits. For an always-on camera with no daily limit, run the model locally with Ollama (below).

Only connect cameras you own or are allowed to use, and follow your local rules on recording people.

## Run fully local with Ollama

Nemotron 3.5 Super is also on [Ollama](https://ollama.com/library/nemotron-3.5-super). Run it on your own machine and the footage never leaves it: no API key, no daily limit.

```bash
ollama pull nemotron-3.5-super
```

Then in `.env`:

```bash
PROVIDER=ollama
OLLAMA_URL=http://localhost:11434   # or the machine that runs Ollama on your network
OLLAMA_MODEL=nemotron-3.5-super
```

`python3 app.py check` confirms Ollama is up and the model is pulled. The local model takes images rather than video, so Nazar sends a sheet of 4 frames from the same 6 seconds. It's a 120B model (12B active) and needs around 90 GB of memory, so a laptop can point `OLLAMA_URL` at a bigger box on the network.

## The dashboard

The camera, a status chip ("Monitoring", "Reviewing", "Fall detected"), an alert card with the time it took to alert, and a plain-words timeline of the day.

- **Space** turns monitoring on or off.
- **R** starts a fresh take: rewinds the clip and clears the screen.
- **V** switches to a 9:16 layout for recording reels.
- **H** hides the cursor.
- **D** opens the developer drawer: motion trigger, check now, model, calls used and logs.

## Staying inside the free quota

- OpenRouter gives 50 free calls a day without credits (1,000 with $10 of credits). Nazar stops at `DAILY_CAP=45` and counts every call, including failed ones.
- The local motion check decides when to call. Each call covers a 6-second clip. Motion during a check gets its own follow-up check, so nothing slips through.
- After an alert, Nazar stays quiet for 45 seconds, so one incident sends one alert.
- In the dashboard, each round of monitoring allows `MAX_PER_ARM=6` calls.
- `python3 app.py find --source clip.mp4` lists the moments with motion in a video, and the `--start` to demo each one, without any AI calls.

## Hermes Agent

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash   # official installer
hermes setup                                                         # your model key and Telegram bot
python3 app.py hermes --profile home --source 0                      # installs the skill and the cron script
```

- **Alerts through Hermes:** `ALERTS=hermes` posts each alert to a Hermes webhook route with `deliver_only`, so Hermes forwards it to Telegram without an LLM call.
- **Ask on Telegram:** the `nazar` skill answers "check the camera" and "any alerts today?".
- **Scheduled looks:** `hermes cron create "every 15m" --no-agent --script nazar_check.py --deliver telegram --name nazar` takes a short look every 15 minutes and stays silent unless there's an incident.

## Commands

| Command | What it does |
|---|---|
| `python3 app.py --source X [--profile P]` | The dashboard on a file, webcam or stream |
| `python3 app.py --brand B --ip IP` | The dashboard on a home or shop camera |
| `python3 app.py watch` | Every camera in `cameras.json` at once, alerts only |
| `python3 app.py cameras` | Stream addresses for common camera brands |
| `python3 app.py discover` | Finds ONVIF cameras on your network |
| `python3 app.py probe --source X` | Checks a camera or stream delivers frames (no AI calls) |
| `python3 app.py fetch` | Downloads the demo clips (GMDCSA-24) and their fall times |
| `python3 app.py find --source clip.mp4` | Moments with motion and the command to demo each one (no AI calls) |
| `python3 app.py once --source X` | One short look; prints only on an incident (used by Hermes cron) |
| `python3 app.py report` | Today's checks and alerts (used by the Hermes skill) |
| `python3 app.py smoke --source clip.mp4` | One real Nemotron call on a few seconds of a clip |
| `python3 app.py check` | Checks keys, quota (or Ollama), Telegram and Hermes |
| `python3 app.py hermes` | Installs the Hermes skill and cron script |

Settings live in `.env` (see `.env.example`). Clips, frames and logs go to `data/`: `calls.jsonl` logs every model call and `verdicts.jsonl` every check.

## Files

| File | Role |
|---|---|
| `app.py` | Command line, dashboard server and setup commands |
| `engine.py` | Video sources, motion check, clips, the detection loop |
| `ai.py` | Scenario packs, prompts, Nemotron calls (OpenRouter or Ollama) and the daily budget |
| `alerts.py` | Telegram and Hermes delivery |
| `config.py`, `net.py` | Settings and a small HTTP client |
| `web/` | The dashboard |
| `hermes/` | Hermes Agent skill and cron script templates |
| `cameras.example.json` | Example multi-camera setup for `watch` |

## Privacy

Free endpoints may log prompts. Only send footage you're allowed to share, and never real customers or passers-by without consent. With `PROVIDER=ollama` the footage stays on your machine. The model is told never to describe a person beyond the action, and the traffic pack never reads number plates.

## Credits

Demo footage: GMDCSA-24, E. Alam, A. Sufian, P. Dutta, M. Leo, I. A. Hameed, *Data in Brief* (2024), CC BY 4.0, [doi:10.5281/zenodo.13354453](https://doi.org/10.5281/zenodo.13354453).

Built by Sarthak ([@nomadev.ai](https://instagram.com/nomadev.ai)).
