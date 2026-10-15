#!/usr/bin/env python3
"""Hermes cron job (no-agent mode): one short look at the shop camera.

Prints the alert (and a MEDIA: line with the frames) only when Nemotron sees an incident. Empty output is a
silent tick, so Hermes sends nothing. Installed by `python3 app.py hermes`; schedule it with:

  hermes cron create "every 15m" --no-agent --script shopcam_check.py --deliver telegram --name shopcam
"""
import subprocess
import sys

done = subprocess.run(["{{PYTHON}}", "{{APP}}", "once", "--source", "{{SOURCE}}"],
                      capture_output=True, text=True, timeout=300)
sys.stdout.write(done.stdout)
if done.returncode != 0:
    sys.stderr.write(done.stderr)
    sys.exit(done.returncode)
