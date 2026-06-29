#!/usr/bin/env bash
# Monday-morning pipeline: refresh data, refresh windowed insights, then build + email
# the weekly briefing PDF. Driven by scheduling/com.ytmetrics.briefing.plist.example.
#
# The SMTP/Gmail app password comes from $YTMETRICS_SMTP_PASSWORD or, if unset, the
# gitignored file secrets/smtp_password (see config.example.toml [email]).
set -euo pipefail
cd "$(dirname "$0")/.."

UV="${UV_BIN:-/opt/homebrew/bin/uv}"

"$UV" run ytmetrics pull --days 7
"$UV" run ytmetrics insights --days 90
"$UV" run ytmetrics briefing --email
