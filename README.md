# Nazar (ShopCam Alert)

An AI camera that watches over your home or shop and messages you on Telegram when something goes wrong: a person falls, something falls or breaks, liquid spreads on the floor, a robbery, a fight, vandalism.

It runs on a laptop for $0. The motion check runs locally, so the model is only asked when something moves. Then a 6-second clip around the motion goes to **NVIDIA Nemotron 3.5 Super VL**, free on OpenRouter, and an alert with the frames lands in Telegram, directly or through **Hermes Agent**.

```
camera / RTSP / clip ─► motion check (local, free) ─► 6s clip ─► Nemotron 3.5 Super VL ─► Telegram / Hermes
```

## Quick start

```bash
pip install -r requirements.txt        # opencv-python, numpy; also needs ffmpeg (brew install ffmpeg)
cp .env.example .env                   # add OPENROUTER_API_KEY and TELEGRAM_BOT_TOKEN (.env.local works too)
python3 app.py telegram                # press Start on your bot; saves your chat id
python3 app.py check                   # keys, free quota, Telegram (no model calls)
```

Try it on real footage. These are real recorded falls with labelled times, CC BY 4.0:

```bash
python3 app.py fetch                                                  # 4 clips from GMDCSA-24, ~35 MB
python3 app.py --source datasets/gmdcsa24/s2-fall-12.mp4 --start 2.9  # dashboard opens; press Space to arm
```

Watch a real camera:

```bash
python3 app.py --source 0                                             # webcam
python3 app.py --source "rtsp://user:pass@192.168.1.20:554/stream1"   # CCTV / NVR stream
python3 app.py --source "rtsp://..." --headless                       # no dashboard, alerts only (server)
```

## Scenario packs

One camera loop, four things it can watch for. Pick with `--profile` (or `PROFILE=` in `.env`):

| Profile | Watches for | Example |
|---|---|---|
| `home` (default) | Falls, things breaking, spills, smoke or fire | Parents living alone |
| `shop` | Items breaking or falling, spills, falls, theft, fights, damage | A store aisle or warehouse |
| `exam` | Phone use, copying, hidden notes, talking (flags for the invigilator, never decides guilt) | An exam hall |
| `traffic` | Red light jumps, no helmet, triple riding, wrong-side driving, accidents | A junction camera |

```bash
python3 app.py --profile shop --source store-cam.mp4
python3 app.py --profile traffic --camera "Silk Board Junction" --source "rtsp://..."
```

## Staying inside the free quota

- OpenRouter gives 50 free calls a day without credits. ShopCam stops at `DAILY_CAP=45` and counts every call, including failed ones.
- The motion check decides when to call. Each call covers a 6-second clip.
- In the dashboard, each arming allows `MAX_PER_ARM=6` calls, then it disarms itself.
- `python3 app.py find --source clip.mp4` lists the moments with motion in a video, and the `--start` to demo each one, without any AI calls.

## Hermes Agent

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash   # official installer
hermes model && hermes gateway setup                                 # pick a model, connect Telegram
python3 app.py hermes                                                # installs the skill and cron script
```

- **Alerts through Hermes:** `ALERTS=hermes` posts each alert to a Hermes webhook route with `deliver_only`, so Hermes forwards it to Telegram without an LLM call.
- **Ask on Telegram:** the `shopcam` skill answers "check the shop camera" or "any alerts today?".
- **Scheduled checks:** `hermes cron create "every 15m" --no-agent --script shopcam_check.py --deliver telegram` takes a short look every 15 minutes and stays silent unless there's an incident.

## Commands

| Command | What it does |
|---|---|
| `python3 app.py --source X` | Dashboard: camera, status, alerts. Space turns monitoring on/off, V gives a 9:16 layout, H hides the cursor, D opens the developer drawer (trigger, check now, replay, logs) |
| `python3 app.py fetch` | Downloads the demo clips (GMDCSA-24) and their fall times |
| `python3 app.py find --source clip.mp4` | Moments with motion and the command to demo each one (no AI calls) |
| `python3 app.py once --source X` | One short look; prints only on an incident (used by Hermes cron) |
| `python3 app.py report` | Today's checks and alerts (used by the Hermes skill) |
| `python3 app.py smoke --source clip.mp4` | One real Nemotron call on a few seconds of a clip |
| `python3 app.py check` | Checks keys, quota, Telegram and Hermes |

Settings live in `.env`; see `.env.example`. Clips, sheets and logs go to `data/`: `calls.jsonl` for every model call, `verdicts.jsonl` for every check.

## Privacy

Free endpoints may log prompts. Only send footage you're allowed to share, and never real customers without consent. The prompt tells the model never to describe a person beyond the action itself.

## Credits

Demo footage: GMDCSA-24, E. Alam, A. Sufian, P. Dutta, M. Leo, I. A. Hameed, *Data in Brief* (2024), CC BY 4.0, [doi:10.5281/zenodo.13354453](https://doi.org/10.5281/zenodo.13354453).

Built by Sarthak ([@nomadev.ai](https://instagram.com/nomadev.ai)).
