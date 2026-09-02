"""Regression tests for credential-safe workflow logging."""

from __future__ import annotations

import logging

from logging_utils import CredentialRedactionFilter


def _record(message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


def test_telegram_bot_path_token_is_redacted_but_request_diagnostics_remain():
    record = _record(
        "HTTP Request: %s %s %s",
        "POST",
        "https://api.telegram.org/bot123456:secret-token/getUpdates",
        "HTTP/1.1 200 OK",
    )

    assert CredentialRedactionFilter().filter(record)

    message = record.getMessage()
    assert "secret-token" not in message
    assert "/bot[REDACTED]/getUpdates" in message
    assert "POST" in message
    assert "200 OK" in message


def test_configured_credential_is_redacted_even_outside_a_bot_url():
    record = _record(
        "request failed for credential=%s",
        "configured-telegram-secret",
    )

    CredentialRedactionFilter(secrets=("configured-telegram-secret",)).filter(record)

    assert "configured-telegram-secret" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_sensitive_query_values_and_bearer_credentials_are_redacted():
    record = _record(
        "url=%s auth=%s",
        "https://example.test/check?access_token=query-secret&ok=1",
        "Bearer header-secret",
    )

    CredentialRedactionFilter().filter(record)

    message = record.getMessage()
    assert "query-secret" not in message
    assert "header-secret" not in message
    assert "access_token=[REDACTED]" in message
    assert "Bearer [REDACTED]" in message


def test_plain_bot_words_are_not_treated_as_credentials():
    record = _record("Sharp Money Bot is ready")

    CredentialRedactionFilter().filter(record)

    assert record.getMessage() == "Sharp Money Bot is ready"


def test_exception_text_is_redacted_before_formatter_reads_it():
    try:
        raise RuntimeError(
            "request failed: https://api.telegram.org/bot123456:secret-token/getMe"
        )
    except RuntimeError:
        record = logging.LogRecord(
            name="httpx",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Telegram request failed",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )

    CredentialRedactionFilter().filter(record)

    assert record.exc_text is not None
    assert "secret-token" not in record.exc_text
    assert "/bot[REDACTED]/getMe" in record.exc_text