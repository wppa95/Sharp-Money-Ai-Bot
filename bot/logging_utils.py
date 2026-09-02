"""Logging safety helpers for external-service credentials."""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterable
from typing import Any


class CredentialRedactionFilter:
    """Redact credentials from log messages and exception text.

    The filter is applied at record creation and the stdout handler rather than
    changing provider or HTTP client behavior. This keeps request diagnostics
    available while ensuring captured workflow logs never contain credentials.
    """

    _BOT_PATH_RE = re.compile(r"(?i)(/bot)[^/?\s\"']+")
    _SENSITIVE_QUERY_RE = re.compile(
        r"(?i)([?&](?:token|api[_-]?key|access[_-]?token|authorization)=)[^&\s\"']+"
    )
    _BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[^\s\"']+")
    _REDACTED = "[REDACTED]"

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    @classmethod
    def redact(cls, value: str, secrets: Iterable[str] = ()) -> str:
        """Return *value* with URL and credential material redacted."""
        redacted = cls._BOT_PATH_RE.sub(r"\1" + cls._REDACTED, value)
        redacted = cls._SENSITIVE_QUERY_RE.sub(r"\1" + cls._REDACTED, redacted)
        redacted = cls._BEARER_RE.sub(r"\1" + cls._REDACTED, redacted)
        for secret in secrets:
            redacted = redacted.replace(secret, cls._REDACTED)
        return redacted

    @classmethod
    def _redact_value(cls, value: Any, secrets: Iterable[str]) -> Any:
        if isinstance(value, str):
            return cls.redact(value, secrets)
        if isinstance(value, tuple):
            return tuple(cls._redact_value(item, secrets) for item in value)
        if isinstance(value, list):
            return [cls._redact_value(item, secrets) for item in value]
        if isinstance(value, dict):
            return {
                key: cls._redact_value(item, secrets)
                for key, item in value.items()
            }
        return value

    def filter(self, record: Any) -> bool:
        """Redact a LogRecord in place and allow it to be emitted."""
        record.msg = self._redact_value(record.msg, self._secrets)
        record.args = self._redact_value(record.args, self._secrets)
        if record.exc_info:
            record.exc_text = self.redact(
                "".join(traceback.format_exception(*record.exc_info)),
                self._secrets,
            )
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info, self._secrets)
        return True


def install_credential_redaction(secrets: Iterable[str] = ()) -> CredentialRedactionFilter:
    """Install record-level redaction before any logger handler sees a record."""
    redaction_filter = CredentialRedactionFilter(secrets)
    previous_factory = logging.getLogRecordFactory()

    def redacting_factory(*args: Any, **kwargs: Any) -> Any:
        record = previous_factory(*args, **kwargs)
        redaction_filter.filter(record)
        return record

    logging.setLogRecordFactory(redacting_factory)
    return redaction_filter