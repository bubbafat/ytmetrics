from __future__ import annotations

import logging

from ytmetrics.logging_setup import RedactionFilter, redact, register_secret, setup_logging


def test_redacts_known_secret():
    register_secret("1//super-secret-refresh-token-value")
    out = redact("token is 1//super-secret-refresh-token-value here")
    assert "super-secret" not in out
    assert "***REDACTED***" in out


def test_redacts_token_patterns():
    assert "ya29." not in redact("Authorization used ya29.AbCdEf_12345 today")
    assert redact('{"refresh_token": "1//xyzXYZ"}').count("***REDACTED***") >= 1
    # The whole "Bearer <token>" is scrubbed — the token value must not survive.
    assert "abc.def.ghi" not in redact("Bearer abc.def.ghi")


def test_filter_scrubs_record_args():
    f = RedactionFilter()
    rec = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "secret=%s", ("ya29.SHOULD_VANISH",), None
    )
    assert f.filter(rec) is True
    assert "SHOULD_VANISH" not in rec.getMessage()


def test_log_file_has_no_secret(tmp_path):
    logger = setup_logging(tmp_path / "logs", level="DEBUG", verbose=False)
    register_secret("1//leakydleaky-token")
    logger.info("using refresh token 1//leakydleaky-token for channel main")
    for h in logger.handlers:
        h.flush()
    content = (tmp_path / "logs" / "ytmetrics.log").read_text()
    assert "leakydleaky" not in content
    assert "***REDACTED***" in content
