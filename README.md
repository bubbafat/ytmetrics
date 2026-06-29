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
