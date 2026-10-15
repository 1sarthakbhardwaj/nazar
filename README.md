# Nazar

An AI camera that watches over a home, shop, gate, exam hall, junction, pet or worksite, and messages you on Telegram when something goes wrong.

It runs on a laptop for $0. A motion check runs locally, so the AI is only asked when something moves. Then a 6-second clip around the motion goes to **NVIDIA Nemotron 3.5 Super VL**, free on OpenRouter. If it sees an incident, the alert and the frames land in Telegram, directly or through **Hermes Agent**.

```
camera / RTSP / clip ─► motion check (local, free) ─► 6s clip ─► Nemotron 3.5 Super VL ─► Telegram / Hermes
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

Watch a real camera:

```bash
python3 app.py --source 0                                             # webcam
python3 app.py --profile shop --source "rtsp://user:pass@192.168.1.20:554/stream1"
python3 app.py --profile parking --source "rtsp://..." --headless     # no dashboard, alerts only
```

## The dashboard

The camera, a status chip ("Monitoring", "Reviewing", "Fall detected"), an alert card with the time it took to alert, and a plain-words timeline of the day.

- **Space** turns monitoring on or off.
- **R** starts a fresh take: rewinds the clip and clears the screen.
- **V** switches to a 9:16 layout for recording reels.
- **H** hides the cursor.
- **D** opens the developer drawer: motion trigger, check now, model, calls used and logs.

## Staying inside the free quota

- OpenRouter gives 50 free calls a day without credits. Nazar stops at `DAILY_CAP=45` and counts every call, including failed ones.
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
| `python3 app.py fetch` | Downloads the demo clips (GMDCSA-24) and their fall times |
| `python3 app.py find --source clip.mp4` | Moments with motion and the command to demo each one (no AI calls) |
| `python3 app.py once --source X` | One short look; prints only on an incident (used by Hermes cron) |
| `python3 app.py report` | Today's checks and alerts (used by the Hermes skill) |
| `python3 app.py smoke --source clip.mp4` | One real Nemotron call on a few seconds of a clip |
| `python3 app.py check` | Checks keys, quota, Telegram and Hermes |
| `python3 app.py hermes` | Installs the Hermes skill and cron script |

Settings live in `.env` (see `.env.example`). Clips, frames and logs go to `data/`: `calls.jsonl` logs every model call and `verdicts.jsonl` every check.

## Files

| File | Role |
|---|---|
| `app.py` | Command line, dashboard server and setup commands |
| `engine.py` | Video sources, motion check, clips, the detection loop |
| `ai.py` | Scenario packs, prompts, Nemotron calls and the daily budget |
| `alerts.py` | Telegram and Hermes delivery |
| `config.py`, `net.py` | Settings and a small HTTP client |
| `web/` | The dashboard |
| `hermes/` | Hermes Agent skill and cron script templates |

## Privacy

Free endpoints may log prompts. Only send footage you're allowed to share, and never real customers or passers-by without consent. The model is told never to describe a person beyond the action, and the traffic pack never reads number plates.

## Credits

Demo footage: GMDCSA-24, E. Alam, A. Sufian, P. Dutta, M. Leo, I. A. Hameed, *Data in Brief* (2024), CC BY 4.0, [doi:10.5281/zenodo.13354453](https://doi.org/10.5281/zenodo.13354453).

Built by Sarthak ([@nomadev.ai](https://instagram.com/nomadev.ai)).
