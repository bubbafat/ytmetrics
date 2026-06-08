"""Pluggable on-failure hook: run a command or POST a webhook. Never raises."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.request

_TIMEOUT = 10


def notify_failure(
    on_failure: dict[str, str] | None, message: str, logger: logging.Logger | None = None
) -> None:
    log = logger or logging.getLogger("ytmetrics")
    if not on_failure:
        return
    try:
        if cmd := on_failure.get("command"):
            env = {**os.environ, "YTMETRICS_STATUS": message}
            subprocess.run(cmd, shell=True, env=env, timeout=_TIMEOUT, check=False)
        elif url := on_failure.get("webhook"):
            data = json.dumps({"text": message}).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - user-configured url
    except Exception as exc:  # notification must never break the run
        log.warning("on_failure hook failed: %s", exc)
