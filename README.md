# ytmetrics

Pull a YouTube channel's analytics into a local **SQLite** database you own — the system
of record — at daily granularity, on demand now and on an automatic daily schedule later.
The `.db` is a single portable file you (or Claude) can query with SQL, fully offline.
Google Sheets / CSV / Parquet export can be layered on later without changing the store.

- **Channel + per-video daily** metrics, split **Shorts vs long-form** (native
  `creatorContentType`), with engagement, discovery (thumbnail/card CTR), traffic sources,
  and optional **revenue** (graceful-degrading).
- **Non-destructive merge-upsert** with a revision log — re-pulling corrects YouTube's
  recent-day revisions without ever erasing captured data.
- **Offline-capable**: `--source replay` runs the whole pipeline from fixtures, no network.
- **Safe to automate**: per-channel transactions, pre-pull db snapshots + reversible
  restore, resilient backoff, a runaway API-call guard, redacted logs, freshness/gap
  warnings, and a pluggable failure hook.

## Install

```bash
uv venv && uv sync          # add --extra keychain for the macOS Keychain token backend
cp config.example.toml config.toml
```

## One-time Google setup (manual — the tool can't click these for you)

You'll create a free Google Cloud project, enable the YouTube Analytics + Data APIs, make a
**Desktop OAuth client**, and authorize **as the account that owns the channel**. Then:

```bash
uv run ytmetrics list-channels    # authorizes in a browser, prints your channel id
uv run ytmetrics doctor --live    # confirms the whole chain works
```

No billing is required (these APIs are free), and the scopes are read-only:
`yt-analytics.readonly`, `youtube.readonly`, and `yt-analytics-monetary.readonly` only when
`include_revenue = true`.

**→ Full click-by-click walkthrough (with the exact links): [SETUP.md](SETUP.md).** It
covers which Google account to use, the consent-screen/test-user steps, downloading the
client JSON, and troubleshooting.

## Usage

```bash
uv run ytmetrics pull --days 7              # trailing 7-day window into ytmetrics.db
uv run ytmetrics pull --start 2024-06-01 --end 2026-06-01   # backfill (chunked by month)
uv run ytmetrics pull --dry-run            # show the plan, write nothing
uv run ytmetrics info                      # row counts, coverage, freshness
uv run ytmetrics doctor                    # preflight checks (add --live for auth/API)
uv run ytmetrics backups                   # list db snapshots
uv run ytmetrics restore --latest          # reversible rollback (.prerestore kept)

# Cross-video / Short→long-form attribution (windowed, on demand):
uv run ytmetrics referrers --video <id> --window 2026-06-01:2026-06-30
```

`endDate` defaults to today − 2 (Pacific) because YouTube revises recent days for ~2-3
days. Re-running a trailing window each day keeps things correct and is idempotent.

Analyze the result directly:
```bash
sqlite3 ytmetrics.db 'SELECT * FROM v_channel_daily_totals ORDER BY date DESC LIMIT 14;'
```
See [SCHEMA.md](SCHEMA.md) for tables, views, and analysis recipes.

## Offline / no credentials

```bash
uv run ytmetrics pull --source replay --start 2026-06-01 --end 2026-06-03
```
runs the full pipeline from `fixtures/`, writing a real database with no network. The
committed `sample/ytmetrics.sample.db` lets you explore the schema immediately:
```bash
sqlite3 sample/ytmetrics.sample.db '.tables'
```

## Automate the daily refresh (local macOS / launchd)

`scheduling/com.ytmetrics.daily.plist.example` runs `ytmetrics pull --days 7` daily. Copy
it to `~/Library/LaunchAgents/com.ytmetrics.daily.plist`, edit the paths, then:
```bash
launchctl load ~/Library/LaunchAgents/com.ytmetrics.daily.plist
launchctl start com.ytmetrics.daily        # run once now to verify
```
It runs headless from the stored refresh token (no browser after the first
`list-channels`). stdout/stderr are captured to `logs/launchd.out` / `logs/launchd.err`.

The daily job covers the daily tables (including `subscribed_status_daily` and the
`subscriber_count` anchor). The **windowed insights** (retention, demographics, geography,
devices, search terms) are *not* part of `pull` — they run via the separate `insights`
command. Schedule them weekly with `scheduling/com.ytmetrics.weekly.plist.example`, which
runs `ytmetrics insights --days 90` (a rolling 90-day window; retention is one API call per
video, so weekly — not daily — is the right cadence):
```bash
launchctl load ~/Library/LaunchAgents/com.ytmetrics.weekly.plist
launchctl start com.ytmetrics.weekly       # run once now to verify
```
Each insights run appends a fresh snapshot, so the windowed tables grow by one snapshot per
run. `insights` prunes snapshots older than `insights_retention_weeks` (default 26; `0`
keeps all) after each run, leaving the daily history untouched.

## Weekly briefing PDF (opt-in)

`ytmetrics briefing` renders a brand-styled, 8-page channel-intelligence PDF straight from
the db — scoreboard, latest-video performance, what's working, audience, monetization, a
search/topic radar, and a data-derived top-3 action plan. Internal-momentum only (no web
calls). Needs the `briefing` extra:
```bash
uv sync --extra briefing
uv run ytmetrics briefing                       # -> reports/empty-besters-briefing-<date>.pdf
uv run ytmetrics briefing --out ~/Desktop/eb.pdf --weeks 1
```
### Email it automatically (Mondays 6am)

`scripts/weekly_briefing.sh` (pull → insights → `briefing --email`) plus
`scheduling/com.ytmetrics.briefing.plist.example` deliver the PDF to your inbox every Monday
at 06:00 local. One-time setup:
1. Add an `[email]` section to `config.toml` (see `config.example.toml`).
2. Create a Gmail **App Password** (Google Account → Security → 2-Step Verification → App
   passwords) and store it without committing it:
   `echo 'app-password' > secrets/smtp_password` (`secrets/` is gitignored), or export
   `YTMETRICS_SMTP_PASSWORD`.
3. `cp scheduling/com.ytmetrics.briefing.plist.example ~/Library/LaunchAgents/com.ytmetrics.briefing.plist`,
   edit paths, `launchctl load` it, then `launchctl start com.ytmetrics.briefing` to test.

Send on demand any time with `ytmetrics briefing --email` (or `--to someone@example.com`).
The Mac must be awake at 06:00 Monday; if asleep, launchd runs it on the next wake.

## Daily digest email

`ytmetrics daily` renders a calm, **mobile-first plain-text** digest you can read on your
phone instead of compulsively opening YouTube Studio. The **verdict lives in the subject
line** (`✅` normal vs `⚠️` + a one-line headline) so you can triage from the lock screen
without opening the mail. When emailed, the message embeds an **inline 7-day trend chart**
(views in coral, revenue in sky) whenever matplotlib (the `briefing` extra) is installed,
and falls back to the text sparkline otherwise — the stdout path stays plain text.

It shows the **freshest day available** (`max(date)` in `channel_daily`) — Studio-style, so
you see tentative recent numbers rather than waiting ~2 days for them to finalize. YouTube
revises roughly the last 2-3 days, so any day that recent is marked **`(est.)`** and a
`RECENT DAYS` table lists the last few days with the same marker — what'll change vs what's
settled. (To get those tentative days into the db, the daily job pulls through *yesterday*;
the merge-upsert + revision log correct them automatically later.) Most days read "✅ Normal
day — nothing needs you"; it only escalates when an anomaly clears a fixed threshold
(views/revenue ±20% vs the 7-day average, a net-subscriber loss or spike, a traffic-source
surge, or the latest video breaking out). Sections: the latest day's stats with both
day-over-day and vs-7-day deltas, the recent-days table, what moved it (top videos), the
latest video in one line, signals, and 7-day sparklines.

```bash
uv run ytmetrics daily                          # print subject + body to stdout
uv run ytmetrics daily --email                  # email it (uses [email] 'to')
uv run ytmetrics daily --to someone@example.com # override the recipient
```

### Email it automatically (daily 6:35am)

`scripts/daily_briefing.sh` (`pull` through yesterday → `daily --email`) plus
`scheduling/com.ytmetrics.daily-digest.plist.example` deliver the digest every day at 06:35
local. It both pulls and digests, so it can run alongside the bare daily pull job
(`com.ytmetrics.daily`, 06:30) or replace it. Same one-time `[email]` / Gmail App Password
setup as the weekly briefing above, then `cp` the plist into `~/Library/LaunchAgents/`,
`launchctl load` it, and `launchctl start com.ytmetrics.daily-digest` to test.

## Deploy elsewhere (later)

Because everything is config + token files and the entrypoint is non-interactive, the same
package runs in GitHub Actions or a GCP job. Set `secret_backend = "env"` for a channel and
provide the token JSON via the env var `YTMETRICS_TOKEN_<NAME>` (e.g. a CI secret) — nothing
sensitive lands on disk. No code change to move hosts.

## Security & cost

- Read-only scopes: a leaked token can read analytics, not delete videos or spend money.
- Refresh tokens and client secrets are gitignored, written `chmod 600`, and **redacted
  from all logs**. Revoke a token at Google Account → Security → third-party access.
- Free, quota-limited APIs + no billing on the project ⇒ no runaway-cost risk;
  `max_api_calls_per_run` aborts a misbehaving run.

## Development

```bash
uv run pytest          # offline tests (ReplaySource fixtures, no network/credentials)
uv run ruff check .
uv run mypy
pre-commit install     # secret scanning + ruff + mypy on every commit
```
