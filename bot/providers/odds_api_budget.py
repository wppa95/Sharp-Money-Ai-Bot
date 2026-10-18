"""Persistent, UTC-period Odds API credit accounting and request pacing."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - the bot is deployed on Linux
    fcntl = None


UTC = timezone.utc
ODDS_API_MONTHLY_TARGET = 450
ODDS_API_MONTHLY_HARD_LIMIT = 500
_DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "odds_api_pacing.json"
)
_ABANDONED_RESERVATION_SECONDS = 180


def utc_month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return this provider quota period and its next UTC reset."""
    current = _as_utc(now or datetime.now(UTC))
    start = datetime(current.year, current.month, 1, tzinfo=UTC)
    if current.month == 12:
        end = datetime(current.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(current.year, current.month + 1, 1, tzinfo=UTC)
    return start, end


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _as_utc(parsed)
    except (TypeError, ValueError):
        return None


class OddsApiCreditBudget:
    """One durable credit ledger shared by every Odds API endpoint.

    Legacy ``date``/``daily_count`` fields are preserved for compatibility.
    New requests are charged as provider credits and the only rollover is the
    documented first-of-month UTC reset.
    """

    def __init__(self, state_path: str = _DEFAULT_STATE_PATH) -> None:
        self.state_path = os.path.abspath(state_path)
        self.lock_path = self.state_path + ".lock"
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with self._thread_lock:
            with open(self.lock_path, "a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield self._load_unlocked()
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            with open(self.state_path, encoding="utf-8") as state_file:
                state = json.load(state_file)
            if not isinstance(state, dict):
                raise ValueError("state must be an object")
        except FileNotFoundError:
            state = {}
        except (OSError, ValueError, json.JSONDecodeError):
            # A corrupt ledger must not grant another month's quota.
            state = {
                "version": 2,
                "period_start": None,
                "credits_used": ODDS_API_MONTHLY_HARD_LIMIT,
                "ledger_error": True,
            }

        state.setdefault("version", 2)
        state.setdefault("reservations", {})
        state.setdefault("caches", {})
        state.setdefault("rotation_cursor", 0)
        state.setdefault("last_request", None)
        state.setdefault("cooldown_until", None)
        state.setdefault("next_paid_request_at", None)
        state.setdefault("reported_used", 0)
        state.setdefault("reported_remaining", None)
        return state

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        directory = os.path.dirname(self.state_path)
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".odds-api-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
                json.dump(state, temp_file, separators=(",", ":"), sort_keys=True)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self.state_path)
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass

    def _ensure_period(self, state: dict[str, Any], now: datetime) -> None:
        period_start, _ = utc_month_bounds(now)
        current_key = period_start.isoformat()
        stored_start = _parse_datetime(state.get("period_start"))
        if stored_start is None:
            legacy_date = str(state.get("date") or "")
            legacy_time = _parse_datetime(state.get("last_request_at"))
            legacy_in_current_period = (
                legacy_date[:7] == period_start.strftime("%Y-%m")
                or (legacy_time is not None and legacy_time >= period_start)
            )
            if legacy_in_current_period:
                # The old field counted calls, not credits, so it cannot be
                # safely converted into the new credit ledger.
                state["credits_used"] = ODDS_API_MONTHLY_HARD_LIMIT
                state["ledger_error"] = True
            else:
                state.setdefault("credits_used", 0)
            state["period_start"] = current_key
            state["reported_used"] = int(state.get("reported_used") or 0)
            return

        if stored_start > period_start:
            # A clock rollback must not roll the account budget back.
            state["ledger_error"] = True
            state["credits_used"] = ODDS_API_MONTHLY_HARD_LIMIT
            return
        if stored_start < period_start:
            history = state.setdefault("period_history", {})
            history[stored_start.isoformat()] = {
                "credits_used": int(state.get("credits_used") or 0),
                "reported_used": int(state.get("reported_used") or 0),
            }
            state["period_start"] = current_key
            state["credits_used"] = 0
            state["reported_used"] = 0
            state["reported_remaining"] = None
            state["reservations"] = {}
            state["cooldown_until"] = None
            state["next_paid_request_at"] = None
            state.pop("ledger_error", None)

    def _effective_used(self, state: dict[str, Any]) -> int:
        return max(
            int(state.get("credits_used") or 0),
            int(state.get("reported_used") or 0),
            ODDS_API_MONTHLY_HARD_LIMIT
            - max(
                0,
                int(state["reported_remaining"]),
            )
            if state.get("reported_remaining") is not None
            else 0,
        )

    def _recover_stale_reservations(self, state: dict[str, Any], now: datetime) -> None:
        reservations = state.setdefault("reservations", {})
        stale_ids = []
        for reservation_id, reservation in reservations.items():
            created = _parse_datetime(reservation.get("created_at"))
            if created is None or (now - created).total_seconds() >= _ABANDONED_RESERVATION_SECONDS:
                stale_ids.append(reservation_id)
        for reservation_id in stale_ids:
            reservation = reservations.pop(reservation_id, {})
            state["credits_used"] = int(state.get("credits_used") or 0) + int(
                reservation.get("max_cost") or 0
            )
            state["last_request"] = {
                "at": reservation.get("created_at"),
                "endpoint": reservation.get("endpoint", "unknown"),
                "credit_cost": int(reservation.get("max_cost") or 0),
                "outcome": "unknown_after_interruption",
            }

    def initialize_schedule(self, now: datetime | None = None) -> datetime:
        """Persist a first request time in the future; never burst on startup."""
        current = _as_utc(now or datetime.now(UTC))
        _, reset_at = utc_month_bounds(current)
        with self._locked_state() as state:
            self._ensure_period(state, current)
            next_at = _parse_datetime(state.get("next_paid_request_at"))
            if next_at is None or next_at <= current:
                available = max(
                    1,
                    ODDS_API_MONTHLY_TARGET - self._effective_used(state),
                )
                spacing = max(
                    60.0,
                    (reset_at - current).total_seconds() / available,
                )
                next_at = current + timedelta(seconds=spacing)
                state["next_paid_request_at"] = next_at.isoformat()
            self._save_unlocked(state)
            return next_at

    def reserve(
        self,
        *,
        endpoint: str,
        params: dict[str, Any],
        max_cost: int,
        now: datetime | None = None,
    ) -> tuple[str | None, str]:
        """Reserve the maximum documented cost before a quota-consuming call."""
        current = _as_utc(now or datetime.now(UTC))
        if max_cost < 0:
            return None, "invalid_cost"
        with self._locked_state() as state:
            self._ensure_period(state, current)
            self._recover_stale_reservations(state, current)
            if state.get("ledger_error"):
                self._save_unlocked(state)
                return None, "budget_state_unknown"
            cooldown = _parse_datetime(state.get("cooldown_until"))
            if cooldown and current < cooldown:
                self._save_unlocked(state)
                return None, "provider_cooldown"
            next_at = _parse_datetime(state.get("next_paid_request_at"))
            if next_at and current < next_at:
                self._save_unlocked(state)
                return None, "paced"

            reserved = sum(
                int(value.get("max_cost") or 0)
                for value in state.get("reservations", {}).values()
            )
            effective = self._effective_used(state)
            if effective + reserved + max_cost > ODDS_API_MONTHLY_TARGET:
                self._save_unlocked(state)
                return None, "working_target_reached"
            if effective + reserved + max_cost > ODDS_API_MONTHLY_HARD_LIMIT:
                self._save_unlocked(state)
                return None, "hard_limit_reached"

            reservation_id = uuid.uuid4().hex
            state["reservations"][reservation_id] = {
                "endpoint": endpoint,
                "params": _safe_params(params),
                "max_cost": int(max_cost),
                "created_at": current.isoformat(),
            }
            if max_cost:
                _, reset_at = utc_month_bounds(current)
                remaining_target = max(1, ODDS_API_MONTHLY_TARGET - effective)
                spacing = max(
                    60.0,
                    (reset_at - current).total_seconds() / remaining_target,
                )
                reserved_slot = current + timedelta(seconds=spacing * max_cost)
                prior_slot = _parse_datetime(state.get("next_paid_request_at"))
                if prior_slot and prior_slot > reserved_slot:
                    reserved_slot = prior_slot
                state["next_paid_request_at"] = reserved_slot.isoformat()
            self._save_unlocked(state)
            return reservation_id, ""

    def complete(
        self,
        reservation_id: str,
        *,
        actual_cost: int | None,
        outcome: str,
        now: datetime | None = None,
        reported_used: int | None = None,
        reported_remaining: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Commit actual cost or conservatively charge the full reservation."""
        current = _as_utc(now or datetime.now(UTC))
        with self._locked_state() as state:
            self._ensure_period(state, current)
            reservation = state.setdefault("reservations", {}).pop(
                reservation_id, None
            )
            if reservation is None:
                return
            max_cost = int(reservation.get("max_cost") or 0)
            charge = max_cost if actual_cost is None else max(0, int(actual_cost))
            # An unexpectedly high provider charge is recorded, not hidden.
            state["credits_used"] = int(state.get("credits_used") or 0) + charge
            if reported_used is not None:
                state["reported_used"] = max(
                    int(state.get("reported_used") or 0),
                    max(0, int(reported_used)),
                )
            if reported_remaining is not None:
                state["reported_remaining"] = max(
                    0,
                    int(reported_remaining),
                )
            effective = self._effective_used(state)
            state["last_request"] = {
                "at": current.isoformat(),
                "endpoint": reservation.get("endpoint", "unknown"),
                "params": reservation.get("params", {}),
                "credit_cost": charge,
                "outcome": outcome[:48],
                "retry_after_seconds": (
                    max(0.0, float(retry_after_seconds))
                    if retry_after_seconds is not None else None
                ),
            }
            if retry_after_seconds is not None and retry_after_seconds > 0:
                state["cooldown_until"] = (
                    current + timedelta(seconds=float(retry_after_seconds))
                ).isoformat()
            if charge > 0:
                _, reset_at = utc_month_bounds(current)
                remaining_target = max(1, ODDS_API_MONTHLY_TARGET - effective)
                spacing = max(
                    60.0,
                    (reset_at - current).total_seconds() / remaining_target,
                )
                next_paid_at = current + timedelta(seconds=spacing * charge)
                previous_next = _parse_datetime(
                    state.get("next_paid_request_at")
                )
                if previous_next and previous_next > next_paid_at:
                    next_paid_at = previous_next
                cooldown = _parse_datetime(state.get("cooldown_until"))
                if cooldown and cooldown > next_paid_at:
                    next_paid_at = cooldown
                state["next_paid_request_at"] = next_paid_at.isoformat()
            self._save_unlocked(state)

    def record_free_request(
        self,
        *,
        endpoint: str,
        params: dict[str, Any],
        outcome: str,
        now: datetime | None = None,
        reported_used: int | None = None,
        reported_remaining: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Record a documented zero-credit call and reconcile provider headers."""
        current = _as_utc(now or datetime.now(UTC))
        with self._locked_state() as state:
            self._ensure_period(state, current)
            if reported_used is not None:
                state["reported_used"] = max(
                    int(state.get("reported_used") or 0),
                    max(0, int(reported_used)),
                )
            if reported_remaining is not None:
                state["reported_remaining"] = max(
                    0,
                    int(reported_remaining),
                )
            state["last_request"] = {
                "at": current.isoformat(),
                "endpoint": endpoint,
                "params": _safe_params(params),
                "credit_cost": 0,
                "outcome": outcome[:48],
                "retry_after_seconds": (
                    max(0.0, float(retry_after_seconds))
                    if retry_after_seconds is not None else None
                ),
            }
            if retry_after_seconds is not None and retry_after_seconds > 0:
                state["cooldown_until"] = (
                    current + timedelta(seconds=float(retry_after_seconds))
                ).isoformat()
            self._save_unlocked(state)

    def can_make_free_request(self, now: datetime | None = None) -> tuple[bool, str]:
        current = _as_utc(now or datetime.now(UTC))
        with self._locked_state() as state:
            self._ensure_period(state, current)
            cooldown = _parse_datetime(state.get("cooldown_until"))
            if cooldown and current < cooldown:
                return False, "provider_cooldown"
            return True, ""

    def next_paid_request_at(self, now: datetime | None = None) -> datetime:
        return self.initialize_schedule(now)

    def advance_rotation_cursor(self, count: int, now: datetime | None = None) -> int:
        current = _as_utc(now or datetime.now(UTC))
        with self._locked_state() as state:
            self._ensure_period(state, current)
            cursor = int(state.get("rotation_cursor") or 0)
            next_cursor = (cursor + 1) % max(1, int(count))
            state["rotation_cursor"] = next_cursor
            self._save_unlocked(state)
            return cursor % max(1, int(count))

    def get_discovery_last_sport(self) -> str:
        """Return the last attempted free-discovery sport, if recorded."""
        with self._locked_state() as state:
            value = state.get("discovery_last_sport_key")
            return str(value).strip() if isinstance(value, str) else ""

    def set_discovery_last_sport(self, sport_key: str) -> None:
        """Persist discovery position without changing paid-credit accounting."""
        key = str(sport_key or "").strip()
        if not key:
            return
        with self._locked_state() as state:
            state["discovery_last_sport_key"] = key
            self._save_unlocked(state)

    def cache_get(self, key: str, now: datetime | None = None) -> Any | None:
        current = _as_utc(now or datetime.now(UTC))
        with self._locked_state() as state:
            entry = state.setdefault("caches", {}).get(key)
            if not isinstance(entry, dict):
                return None
            expires = _parse_datetime(entry.get("expires_at"))
            if expires is None or current >= expires:
                return None
            return entry.get("value")

    def cache_set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> None:
        current = _as_utc(now or datetime.now(UTC))
        with self._locked_state() as state:
            self._ensure_period(state, current)
            caches = state.setdefault("caches", {})
            caches[key] = {
                "expires_at": (
                    current + timedelta(seconds=max(0.0, ttl_seconds))
                ).isoformat(),
                "value": value,
            }
            self._save_unlocked(state)

    def diagnostics(self, now: datetime | None = None) -> dict[str, Any]:
        current = _as_utc(now or datetime.now(UTC))
        period_start, reset_at = utc_month_bounds(current)
        with self._locked_state() as state:
            self._ensure_period(state, current)
            pending = sum(
                int(value.get("max_cost") or 0)
                for value in state.get("reservations", {}).values()
            )
            used = self._effective_used(state)
            return {
                "quota_period_start_utc": period_start.isoformat(),
                "next_reset_utc": reset_at.isoformat(),
                "credits_used": used,
                "credits_reserved": pending,
                "credits_target": ODDS_API_MONTHLY_TARGET,
                "credits_hard_limit": ODDS_API_MONTHLY_HARD_LIMIT,
                "credits_remaining_to_target": max(
                    0, ODDS_API_MONTHLY_TARGET - used - pending
                ),
                "last_request": dict(state.get("last_request") or {}),
                "next_paid_request_at": state.get("next_paid_request_at"),
                "cooldown_until": state.get("cooldown_until"),
                "rotation_cursor": int(state.get("rotation_cursor") or 0),
                "budget_state_unknown": bool(state.get("ledger_error")),
            }


def _safe_params(params: dict[str, Any]) -> dict[str, Any]:
    """Persist only request-shaping parameters, never credentials."""
    return {
        str(key): value
        for key, value in params.items()
        if str(key).casefold() not in {"apikey", "api_key", "key", "token"}
    }
