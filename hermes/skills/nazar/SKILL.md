---
name: nazar
description: Look at the camera ({{CAMERA}}) with NVIDIA Nemotron and report what Nazar watches for there ({{RULES}}). Use when the user asks about the camera, CCTV, alerts, or what happened today.
version: 1.1.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [cctv, camera, security, nemotron]
    category: home
---

# Nazar camera: {{CAMERA}}

Nazar watches this camera for: {{RULES}}. A look records a few seconds and, if something moved, asks Nemotron 3.5 Super VL about the clip. Each look that reaches Nemotron uses one of the free OpenRouter calls for the day, the same pool this chat uses, so only look when the user asks.

## When to use

- "Check the camera", "anything happening?", "is everything ok?" → look now.
- "What happened today?", "any alerts?" → today's report.

## Procedure

1. Look now: run `{{PYTHON}} {{APP}} once --profile {{PROFILE}} --source "{{SOURCE}}" --force --verbose`
2. Today's report: run `{{PYTHON}} {{APP}} report --profile {{PROFILE}}`
3. Answer in one or two short lines, using only what the output says.
4. If the output has a line that starts with `MEDIA:`, put that exact line at the end of your reply, unchanged, so the frames are attached.

## Pitfalls

- Never describe a person beyond the action: no clothing, age, gender or ethnicity.
- If the output says the daily budget is used up, say so and don't retry.
- Don't run a look on a schedule from chat; the cron job `nazar` already does that.

## Verification

Your reply quotes the incident type and the time from the output, or says all is quiet.
