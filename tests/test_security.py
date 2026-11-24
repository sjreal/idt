"""Tests for the self-contained request security helpers."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from llm_app.security import (
    MAX_BODY_BYTES,
    SECURITY_HEADERS,
    TokenBucketLimiter,
    check_auth,
    constant_time_equals,
    enforce_body_limit,
    rate_limit_key,
)


def test_constant_time_equals_handles_matches_mismatches_and_empty_values() -> None:
    assert constant_time_equals("secret", "secret") is True
    assert constant_time_equals("secret", "different") is False
    assert constant_time_equals("", "") is True
    assert constant_time_equals("", "secret") is False
    assert constant_time_equals("sécret", "sécret") is True
    assert constant_time_equals(None, "secret") is False
    assert constant_time_equals(None, None) is True


def test_check_auth_is_a_no_op_when_no_token_is_configured() -> None:
    check_auth(None, None)
    check_auth("anything", "Bearer wrong-token")
    check_auth("wrong-token", "Basic unrelated")


def test_check_auth_accepts_bearer_and_raw_token_forms() -> None:
    check_auth(None, "Bearer correct-token", "correct-token")
    check_auth(None, "bearer correct-token", "correct-token")
    check_auth(None, "correct-token", "correct-token")
    check_auth("correct-token", None, "correct-token")
    check_auth("Bearer correct-token", None, "correct-token")


def test_check_auth_rejects_missing_and_wrong_tokens_without_echoing_them() -> None:
    for token, header in ((None, None), (None, "Bearer wrong-token"), ("wrong-token", None)):
        with pytest.raises(HTTPException) as exc_info:
            check_auth(token, header, "correct-token")
        assert exc_info.value.status_code == 401
        assert "correct-token" not in str(exc_info.value.detail)
        assert "wrong-token" not in str(exc_info.value.detail)
        assert str(exc_info.value.detail) == "Invalid or missing API token"


def test_token_bucket_allows_the_burst_then_rejects_with_retry_after() -> None:
    limiter = TokenBucketLimiter(per_minute=2)
    assert limiter.allow("client", now=0.0) == (True, 0)
    assert limiter.allow("client", now=0.0) == (True, 0)
    allowed, retry_after = limiter.allow("client", now=0.0)
    assert allowed is False
    assert retry_after >= 1


def test_token_bucket_refills_linearly_over_the_window() -> None:
    limiter = TokenBucketLimiter(per_minute=2)
    assert limiter.allow("client", now=0.0) == (True, 0)
    assert limiter.allow("client", now=0.0) == (True, 0)
    assert limiter.allow("client", now=0.0)[0] is False
    assert limiter.allow("client", now=30.0) == (True, 0)
    assert limiter.allow("client", now=30.0)[0] is False
    assert limiter.allow("client", now=60.0) == (True, 0)


def test_token_bucket_uses_custom_burst_and_independent_keys() -> None:
    limiter = TokenBucketLimiter(per_minute=1, burst=2)
    assert limiter.allow("first", now=0.0) == (True, 0)
    assert limiter.allow("second", now=0.0) == (True, 0)
    assert limiter.allow("first", now=0.0) == (True, 0)
    assert limiter.allow("first", now=0.0)[0] is False
    assert limiter.allow("second", now=60.0) == (True, 0)


def test_token_bucket_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="per_minute"):
        TokenBucketLimiter(0)
    with pytest.raises(ValueError, match="burst"):
        TokenBucketLimiter(5, burst=0)


def test_token_bucket_enforces_capacity_under_concurrent_calls() -> None:
    limiter = TokenBucketLimiter(per_minute=5)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.allow("shared", now=0.0), range(20)))
    assert sum(allowed for allowed, _ in results) == 5


def test_rate_limit_key_is_bounded_deterministic_and_content_free() -> None:
    long_host = "host-" + "x" * 500
    long_session = "session-" + "y" * 500
    key = rate_limit_key(long_host, long_session)
    assert key == rate_limit_key(long_host, long_session)
    assert len(key) <= 200
    assert "x" * 100 not in key
    assert "y" * 100 not in key
    assert key != rate_limit_key(long_host, "other-session")
    assert rate_limit_key("1.2.3.4", "session-1") != rate_limit_key("session-1", "1.2.3.4")
    assert isinstance(rate_limit_key(None, None), str)


def test_security_headers_match_the_local_http_defaults() -> None:
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"
    assert SECURITY_HEADERS["Content-Security-Policy"] == (
        "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
    )
    assert "Strict-Transport-Security" not in SECURITY_HEADERS


def test_enforce_body_limit_allows_exact_boundary_and_rejects_above_it() -> None:
    enforce_body_limit(MAX_BODY_BYTES)
    enforce_body_limit(MAX_BODY_BYTES - 1)
    enforce_body_limit(MAX_BODY_BYTES + 1, max_bytes=MAX_BODY_BYTES + 1)
    enforce_body_limit(None)
    with pytest.raises(HTTPException) as exc_info:
        enforce_body_limit(MAX_BODY_BYTES + 1)
    assert exc_info.value.status_code == 413
    assert "body" in str(exc_info.value.detail).lower()
