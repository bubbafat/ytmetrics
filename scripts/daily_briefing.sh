#!/usr/bin/env bash
# Daily digest: pull the FRESHEST available days (through *yesterday* — a YouTube estimate
# that will still revise) and email the mobile-first plain-text digest. Studio-style: you
# see tentative recent numbers rather than waiting ~2 days for them to finalize. The
# merge-upsert + revision_log correct these days automatically on later pulls.
# Driven by scheduling/com.ytmetrics.daily-digest.plist.example.
#
# SMTP/Gmail app password: $YTMETRICS_SMTP_PASSWORD, or the gitignored secrets/smtp_password.
set -euo pipefail
cd "$(dirname "$0")/.."

UV="${UV_BIN:-/opt/homebrew/bin/uv}"

# Fetch an 8-day window ending YESTERDAY (macOS `date`). Default pulls stop at today-2;
# the digest wants the tentative recent days, so we reach one day closer to now.
END="$(date -v-1d +%F)"
START="$(date -v-8d +%F)"
"$UV" run ytmetrics pull --start "$START" --end "$END"
"$UV" run ytmetrics daily --email
