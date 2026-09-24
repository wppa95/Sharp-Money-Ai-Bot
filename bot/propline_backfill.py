"""Manual, SQLite-backed historical import from PropLine's Pro CSV export.

Run from ``bot/`` with an authorized Pro account, for example:
``python propline_backfill.py --sport basketball_nba --since 2026-07-01T00:00:00Z``

This is intentionally an operator-invoked backfill, not a scheduled job.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from engine.sport_policy import is_football_related_sport
from providers.player_history import propline_result_rows
from providers.propline import (
    PropLineClient,
    PropLineResultEvidence,
    _OFFICIAL_TO_CANONICAL_SPORT,
    _normalize_direction,
    _normalize_stat,
    normalize_propline_result_state,
)

_WRITE_BATCH_SIZE = 500


def _field_name(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def _row_value(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(_field_name(name))
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _validate_export_range(since: str, until: str) -> None:
    now = datetime.utcnow()
    earliest = now - timedelta(days=90)
    start = _parse_datetime(since) if since else earliest
    end = _parse_datetime(until) if until else now
    if (
        start is None
        or end is None
        or start < earliest
        or start > end
        or end > now
    ):
        raise ValueError("Resolved export range must be valid and within 90 days")


def _number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _stable_export_identity(
    row: dict[str, Any],
    *,
    event_id: str,
    player_id: str,
    player_name: str,
    market: str,
    bookmaker: str,
    direction: str,
    line_value: float | None,
) -> str:
    outcome_id = _row_value(row, "outcome_id")
    if outcome_id:
        return outcome_id
    identity = [
        event_id,
        player_id or player_name.casefold(),
        market,
        bookmaker,
        direction,
        line_value,
    ]
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return f"fallback:{digest}"


def _result_evidence(
    row: dict[str, Any],
    *,
    canonical_sport: str,
    fetched_at: datetime,
) -> PropLineResultEvidence:
    resolution = _row_value(
        row, "resolution", "resolution_state", "result", "outcome_result"
    )
    market = _normalize_stat(
        _row_value(row, "market", "market_key", "stat_type")
    )
    bookmaker_key = _row_value(row, "bookmaker_key", "bookmaker", "provider")
    direction = _normalize_direction(
        _row_value(row, "direction", "outcome_name", "selection")
    )
    return PropLineResultEvidence(
        observed_at=fetched_at,
        resolution_state=normalize_propline_result_state(resolution),
        source_resolution=resolution,
        actual_value=_number(
            _row_value(row, "actual_value", "actual", "stat_value")
        ),
        resolved_at=_parse_datetime(
            _row_value(row, "resolved_at", "settled_at")
        ),
        provider=(
            _row_value(row, "bookmaker_title", "provider")
            or bookmaker_key
        ),
        player_name=_row_value(row, "player_name", "player"),
        sport=canonical_sport,
        stat_type=market,
        line_value=_number(
            _row_value(row, "line_value", "line", "point", "projection")
        ),
        direction=direction,
        event_id=_row_value(row, "event_id", "game_id", "match_id"),
        player_id=_row_value(row, "player_id", "athlete_id"),
        market_key=_row_value(row, "market_key", "market"),
        market_description=_row_value(
            row, "market_description", "description"
        ),
        bookmaker_key=bookmaker_key,
        bookmaker_title=_row_value(row, "bookmaker_title", "bookmaker"),
        provider_status=_row_value(
            row, "provider_status", "settlement_status", "event_status"
        ),
        dfs_odds_type=_row_value(row, "dfs_odds_type"),
        supplemental_source="PropLine",
        response_complete=True,
        commence_time=_parse_datetime(
            _row_value(row, "commence_time", "start_time", "game_time")
        ),
    )


async def persist_resolved_export_csv(
    db,
    csv_text: str,
    *,
    official_sport: str,
    fetched_at: datetime | None = None,
) -> dict[str, int]:
    """Persist a complete CSV response in bounded evidence/result batches."""
    sport_key = str(official_sport or "").strip()
    canonical_sport = _OFFICIAL_TO_CANONICAL_SPORT.get(sport_key.lower(), "")
    if not canonical_sport:
        raise ValueError("Resolved export requires a mapped official sport key")
    if is_football_related_sport(canonical_sport):
        raise ValueError("Resolved export is disabled for football-related sports")
    if not isinstance(csv_text, str):
        raise TypeError("Resolved export payload must be CSV text")

    evidence_writer = getattr(db, "upsert_propline_evidence_batch", None)
    game_writer = getattr(db, "upsert_player_results_bulk", None)
    if not callable(evidence_writer) or not callable(game_writer):
        raise RuntimeError("Database does not support PropLine export persistence")

    fetched = _parse_datetime(fetched_at) or datetime.utcnow()
    evidence_batch: list[dict[str, Any]] = []
    game_batch: list[Any] = []
    persisted_rows = game_rows = skipped_rows = rows_seen = 0

    async def flush() -> None:
        nonlocal persisted_rows, game_rows
        if evidence_batch:
            persisted_rows += await evidence_writer(evidence_batch)
            evidence_batch.clear()
        if game_batch:
            game_rows += await game_writer(game_batch)
            game_batch.clear()

    reader = csv.DictReader(io.StringIO(csv_text))
    for raw_row in reader:
        rows_seen += 1
        row = {
            _field_name(key): value
            for key, value in (raw_row or {}).items()
            if key is not None
        }
        player_name = _row_value(row, "player_name", "player")
        if player_name.startswith("(Watermark)"):
            skipped_rows += 1
            continue

        event_id = _row_value(row, "event_id", "game_id", "match_id")
        player_id = _row_value(row, "player_id", "athlete_id")
        market = _normalize_stat(
            _row_value(row, "market", "market_key", "stat_type")
        )
        bookmaker = _row_value(row, "bookmaker_key", "bookmaker", "provider")
        direction = _normalize_direction(
            _row_value(row, "direction", "outcome_name", "selection")
        )
        line_value = _number(
            _row_value(row, "line_value", "line", "point", "projection")
        )
        outcome_id = _row_value(row, "outcome_id")
        if not any((outcome_id, event_id, player_id, player_name, market)):
            skipped_rows += 1
            continue
        target_key = _stable_export_identity(
            row,
            event_id=event_id,
            player_id=player_id,
            player_name=player_name,
            market=market,
            bookmaker=bookmaker,
            direction=direction,
            line_value=line_value,
        )
        payload = {
            key: value
            for key, value in row.items()
            if key != "customer_token"
        }
        evidence_batch.append(
            {
                "family": "resolved_props_export",
                "target_key": target_key,
                "official_sport": sport_key,
                "canonical_sport": canonical_sport,
                "event_id": event_id or None,
                "player_id": player_id or None,
                "player_name": player_name or None,
                "market": market or None,
                "outcome_id": outcome_id or None,
                "bookmaker": bookmaker or None,
                "source": "PropLine resolved-props export",
                "fetched_at": fetched,
                "expires_at": None,
                "complete": True,
                "redacted": False,
                "payload": payload,
            }
        )

        result = _result_evidence(
            row, canonical_sport=canonical_sport, fetched_at=fetched
        )
        # Without the event's actual commence time, resolved_at can be later
        # than game day. Keep the raw export row, but do not invent a game date.
        if (
            result.is_settled
            and result.commence_time is not None
            and result.resolution_state != "VOID"
        ):
            game_batch.extend(propline_result_rows([result]))
        if len(evidence_batch) >= _WRITE_BATCH_SIZE:
            await flush()
    await flush()
    return {
        "rows_read": rows_seen,
        "evidence_rows": persisted_rows,
        "game_rows": game_rows,
        "skipped_rows": skipped_rows,
    }


async def backfill_propline_resolved_props(
    db,
    client: PropLineClient,
    *,
    sport: str,
    market: str = "",
    bookmaker: str = "",
    since: str = "",
    until: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Fetch one documented Pro export and idempotently persist its rows."""
    official_sport = str(sport or "").strip()
    canonical_sport = _OFFICIAL_TO_CANONICAL_SPORT.get(
        official_sport.lower(), ""
    )
    if not canonical_sport:
        raise ValueError("Use a discovered, mapped official PropLine sport key")
    if is_football_related_sport(canonical_sport):
        raise ValueError("Resolved export is disabled for football-related sports")
    _validate_export_range(since, until)

    filters = [official_sport, market.strip(), bookmaker.strip(), since.strip(), until.strip()]
    request_hash = hashlib.sha256(
        json.dumps(filters, separators=(",", ":")).encode()
    ).hexdigest()
    request_key = f"resolved-export:{request_hash}"
    if not force:
        reader = getattr(db, "get_propline_evidence", None)
        if callable(reader):
            previous = await reader(
                "resolved_export_request",
                request_key,
                official_sport,
                fresh_only=True,
            )
            if previous:
                return {
                    "status": "already_imported",
                    **(previous.get("payload") or {}),
                }

    if not getattr(client, "is_configured", False):
        raise RuntimeError("PropLine is not configured")
    result = await client.fetch_resolved_props_export(
        sport=official_sport,
        market=market,
        bookmaker=bookmaker,
        since=since,
        until=until,
    )
    if (
        not getattr(result, "succeeded", False)
        or not getattr(result, "complete", False)
        or getattr(result, "redacted", False)
    ):
        failure = getattr(result, "failure_kind", "") or "incomplete_response"
        raise RuntimeError(f"PropLine resolved export unavailable ({failure})")

    summary = await persist_resolved_export_csv(
        db,
        result.payload,
        official_sport=official_sport,
        fetched_at=getattr(result, "fetched_at", None),
    )
    marker_writer = getattr(db, "upsert_propline_evidence_batch", None)
    if not callable(marker_writer):
        raise RuntimeError("Database cannot store the export request marker")
    await marker_writer(
        [
            {
                "family": "resolved_export_request",
                "target_key": request_key,
                "official_sport": official_sport,
                "canonical_sport": canonical_sport,
                "source": "PropLine resolved-props export",
                "fetched_at": _parse_datetime(
                    getattr(result, "fetched_at", None)
                )
                or datetime.utcnow(),
                "expires_at": None,
                "complete": True,
                "redacted": False,
                "payload": summary,
            }
        ]
    )
    return {"status": "imported", **summary}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sport",
        action="append",
        required=True,
        help="Official discovered PropLine sport key; repeat for another sport.",
    )
    parser.add_argument("--market", default="")
    parser.add_argument("--bookmaker", default="")
    parser.add_argument("--since", default="", help="ISO datetime lower bound.")
    parser.add_argument("--until", default="", help="ISO datetime upper bound.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Repeat an identical export request already imported into SQLite.",
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    from config import config
    from database import Database

    db = Database(config.DATABASE_URL)
    await db.init()
    client = PropLineClient()
    try:
        for sport in dict.fromkeys(args.sport):
            report = await backfill_propline_resolved_props(
                db,
                client,
                sport=sport,
                market=args.market,
                bookmaker=args.bookmaker,
                since=args.since,
                until=args.until,
                force=args.force,
            )
            print(
                f"{sport}: {report['status']} "
                f"evidence_rows={report.get('evidence_rows', 0)} "
                f"game_rows={report.get('game_rows', 0)} "
                f"skipped_rows={report.get('skipped_rows', 0)}"
            )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            await close()
        await db.close()
    return 0


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())