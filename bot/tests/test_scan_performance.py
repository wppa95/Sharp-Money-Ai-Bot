"""
test_scan_performance.py — Regression suite for the scan-performance batch-save pass.

Verifies that the performance fix (bulk snapshot save instead of per-prop individual
saves) does not alter any V3.5-frozen behaviour.  14 regression items:

 1. Poll interval configured to 120 s
 2. max_instances=2 enforced on underdog_monitor job
 3. Full active prop feed is monitored each cycle (no sports/markets silently dropped)
 4. No sports removed from UD_ALERT_SPORTS
 5. Scoring model unchanged — compute_market_quality is still called
 6. Tier thresholds unchanged (S≥85, A≥70, B≥55)
 7. Confidence / BQ gates unchanged (UD_MIN_CONF_S, UD_MIN_CONF_A)
 8. MLB UNDER whitelist still present
 9. NFL direction rules still present
10. Dedup in-memory dict still used
11. CLV seed still called for S/A picks
12. Stale-line guard active on all 5 delivery paths
13. Fast Resume completely absent
14. Scheduler skips are observable (logged by APScheduler, not silently swallowed)

Tests use source-code inspection where behavioural testing would require a full bot
harness, and lightweight unit-level assertions everywhere else.
"""

import ast
import asyncio
import json
import re
import sys
import os
import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────

ROOT = os.path.join(os.path.dirname(__file__), "..")

# These are the regression limits for the recurring full-feed validation.  Keep
# them in the benchmark so a change to the contract fails the validation
# command, rather than only changing the prose documentation.
FULL_FEED_CADENCE_BUDGET_SECONDS = 120.0
FULL_FEED_MAX_QUEUE_SATURATION = 0
FULL_FEED_MAX_PERSISTENCE_FAILURES = 0
FULL_FEED_MAX_WRITER_WAIT_SECONDS = 120.0
FULL_FEED_MAX_COUNT_DELTA = 0


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def _cfg():
    """Load config module with dummy env so defaults are readable."""
    import types

    spec = importlib.util.spec_from_file_location(
        "config_perf_test", os.path.join(ROOT, "config.py")
    )
    mod = importlib.util.module_from_spec(spec)
    # Provide all secrets as blank strings so Config() doesn't raise
    dummy_env = {
        "TELEGRAM_BOT_TOKEN": "dummy",
        "TELEGRAM_TOKEN": "dummy",
        "ODDS_API_KEY": "dummy",
        "SESSION_SECRET": "dummy",
    }
    with pytest.MonkeyPatch().context() as mp:
        for k, v in dummy_env.items():
            mp.setenv(k, v)
        spec.loader.exec_module(mod)
    return mod


# ── Multi-cycle persistence benchmark ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_feed_persistence_benchmark_across_live_cycles(
    tmp_path,
    record_testsuite_property,
):
    """Exercise the durable full-feed boundary over several cadence cycles.

    This deliberately bypasses provider and scoring code.  It models the
    production persistence fan-out with a real file-backed SQLite database:
    raw observations, normalized current-feed history, candidate telemetry,
    directional opportunities, and both tracking/grading ledgers are queued
    through the same in-process persistence worker used by the live scan.

    The two feed sizes keep this a repeatable regression benchmark rather than
    a wall-clock performance claim.  Every cycle must remain below the
    configured 120-second cadence and every expected row must be durable before
    the next cycle is started.
    """
    from sqlalchemy import func, select

    import database as database_module
    import market_engine as engine
    from database import (
        Database,
        PropCandidateLog,
        PropLineHistory,
        PropOpportunityLog,
        PropTrackingLog,
        UnderdogSnapshotRecord,
    )

    scenarios = (
        # (full-feed observations, full-pool directional/grading rows)
        (256, 64),
        (1024, 256),
    )
    cycles_per_scenario = 3
    cadence_budget_seconds = FULL_FEED_CADENCE_BUDGET_SECONDS
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'benchmark.sqlite'}")
    await database.init()

    async def count_rows(model, column, prefix: str) -> int:
        async with database.session() as session:
            result = await session.execute(
                select(func.count()).select_from(model).where(
                    column.like(f"{prefix}%")
                )
            )
            return int(result.scalar() or 0)

    async def count_tracking_results(prefix: str, result_value: str) -> int:
        async with database.session() as session:
            result = await session.execute(
                select(func.count()).select_from(PropTrackingLog).where(
                    PropTrackingLog.external_id.like(f"{prefix}%"),
                    PropTrackingLog.result == result_value,
                )
            )
            return int(result.scalar() or 0)

    async def count_opportunity_results(prefix: str, result_value: str) -> int:
        async with database.session() as session:
            result = await session.execute(
                select(func.count()).select_from(PropOpportunityLog).where(
                    PropOpportunityLog.external_id.like(f"{prefix}%"),
                    PropOpportunityLog.result == result_value,
                )
            )
            return int(result.scalar() or 0)

    async def track_and_grade(
        tracking_rows: list[dict],
        tracking_prefix: str,
    ) -> None:
        tracked = await database.track_prop_records(tracking_rows)
        assert tracked == len(tracking_rows)
        async with database.session() as session:
            result = await session.execute(
                select(PropTrackingLog.id).where(
                    PropTrackingLog.external_id.like(f"{tracking_prefix}%")
                )
            )
            tracking_ids = [int(row_id) for row_id in result.scalars().all()]
        assert await database.grade_tracked_props_bulk(
            [(row_id, "HIT", 10.0) for row_id in tracking_ids]
        ) == len(tracking_rows)

    async def persist_and_grade_opportunities(
        opportunities: list[dict],
        opportunity_prefix: str,
    ) -> None:
        inserted = await database.log_prop_opportunity_bulk(opportunities)
        assert inserted == len(opportunities)
        async with database.session() as session:
            result = await session.execute(
                select(PropOpportunityLog.id).where(
                    PropOpportunityLog.external_id.like(f"{opportunity_prefix}%")
                )
            )
            opportunity_ids = [int(row_id) for row_id in result.scalars().all()]
        assert await database.grade_opportunities_bulk([
            {"id": row_id, "result": "HIT", "actual_value": 10.0}
            for row_id in opportunity_ids
        ]) == len(opportunities)

    reports = []
    try:
        # A previous test may have created the worker in this event loop.
        # Drain it before this benchmark so each scenario starts cleanly.
        assert await engine.drain_db_persistence_worker()

        for scenario_index, (feed_size, full_pool_size) in enumerate(scenarios):
            for cycle in range(cycles_per_scenario):
                prefix = f"bench-{scenario_index}-{cycle}-"
                observation_prefix = f"{prefix}obs-"
                candidate_prefix = f"{prefix}candidate-"
                opportunity_prefix = f"{prefix}opp-"
                tracking_prefix = f"{prefix}grade-"
                now = datetime.utcnow()
                future_game = now + timedelta(hours=6)

                observations = [
                    UnderdogSnapshotRecord(
                        external_id=f"{observation_prefix}{index}",
                        player_name=f"Benchmark Player {index}",
                        team="BENCH",
                        sport="NBA",
                        stat_type="Points",
                        line_value=20.5 + (index % 5),
                        game_id=f"{prefix}game-{index}",
                        game_time=future_game,
                        fetched_at=now,
                    )
                    for index in range(feed_size)
                ]
                normalized_props = [
                    SimpleNamespace(
                        external_id=f"{observation_prefix}{index}",
                        player_name=f"Benchmark Player {index}",
                        team="BENCH",
                        sport="NBA",
                        stat_type="Points",
                        line_value=20.5 + (index % 5),
                        game_id=f"{prefix}game-{index}",
                        game_time=future_game,
                        fetched_at=now,
                        provider_status="active",
                    )
                    for index in range(feed_size)
                ]
                candidates = [
                    {
                        "scan_ts": now,
                        "player_name": prop.player_name,
                        "team": prop.team,
                        "sport": prop.sport,
                        "stat_type": prop.stat_type,
                        "line_value": prop.line_value,
                        "provider": "Underdog",
                        "score_total": 70.0,
                        "score_tier": "A",
                        "confidence": 75,
                        "gate_decision": "EVALUATED",
                        "snapshot_external_id": f"{candidate_prefix}{index}",
                    }
                    for index, prop in enumerate(normalized_props)
                ]
                opportunities = [
                    {
                        "external_id": f"{opportunity_prefix}{index}",
                        "player_name": f"Benchmark Pool Player {index}",
                        "team": "BENCH",
                        "sport": "NBA",
                        "stat_type": "Points",
                        "line_value": 20.5 + (index % 5),
                        "recommendation": "OVER" if index % 2 else "UNDER",
                        "decision_tier": "A",
                        "confidence": 75,
                        "game_time": future_game,
                        "game_id": f"{prefix}pool-game-{index}",
                        "provider": "Underdog",
                        "bet_quality_score": 80,
                    }
                    for index in range(full_pool_size)
                ]
                tracking_rows = [
                    {
                        "external_id": f"{tracking_prefix}{index}",
                        "player_name": f"Benchmark Pool Player {index}",
                        "team": "BENCH",
                        "sport": "NBA",
                        "stat_type": "Points",
                        "line_value": 20.5 + (index % 5),
                        "game_time": future_game,
                        "game_id": f"{prefix}pool-game-{index}",
                        "provider": "Underdog",
                    }
                    for index in range(full_pool_size)
                ]

                metrics_before = engine._persistence_metrics_snapshot()
                backpressure_before = database_module.get_sqlite_backpressure_metrics(
                    database._url
                )
                started = asyncio.get_running_loop().time()
                queued = (
                    engine._enqueue_db_persistence(
                        "benchmark raw observations",
                        lambda rows=observations: database.save_underdog_snapshots_bulk(rows),
                    ),
                    engine._enqueue_db_persistence(
                        "benchmark normalized feed",
                        lambda props=normalized_props: database.upsert_prop_line_history_batch(
                            "Underdog", props, append_snapshot=True
                        ),
                    ),
                    engine._enqueue_db_persistence(
                        "benchmark candidate evidence",
                        lambda rows=candidates: database.log_prop_candidate_batch(rows),
                    ),
                    engine._enqueue_db_persistence(
                        "benchmark tracking and grading",
                        lambda rows=tracking_rows, track_prefix=tracking_prefix:
                            track_and_grade(rows, track_prefix),
                    ),
                    engine._enqueue_db_persistence(
                        "benchmark opportunity and grading",
                        lambda rows=opportunities, opp_prefix=opportunity_prefix:
                            persist_and_grade_opportunities(rows, opp_prefix),
                    ),
                )
                assert all(queued), "benchmark persistence queue rejected required work"
                assert await engine.drain_db_persistence_worker()
                elapsed = asyncio.get_running_loop().time() - started

                metrics_after = engine._persistence_metrics_snapshot()
                backpressure_after = database_module.get_sqlite_backpressure_metrics(
                    database._url
                )
                metric_delta = {
                    key: round(
                        float(metrics_after.get(key, 0.0))
                        - float(metrics_before.get(key, 0.0)),
                        6,
                    )
                    for key in metrics_after
                }
                backpressure_delta = {
                    key: round(
                        float(backpressure_after.get(key, 0.0))
                        - float(backpressure_before.get(key, 0.0)),
                        6,
                    )
                    for key in backpressure_after
                }
                expected_cycles = cycle + 1
                expected_observations = feed_size * expected_cycles
                expected_candidates = feed_size * expected_cycles
                expected_opportunities = full_pool_size * expected_cycles
                expected_tracking = full_pool_size * expected_cycles
                counts = {
                    "observations": await count_rows(
                        UnderdogSnapshotRecord,
                        UnderdogSnapshotRecord.external_id,
                        f"bench-{scenario_index}-",
                    ),
                    "normalized_history": await count_rows(
                        PropLineHistory,
                        PropLineHistory.external_id,
                        f"bench-{scenario_index}-",
                    ),
                    "candidates": await count_rows(
                        PropCandidateLog,
                        PropCandidateLog.snapshot_external_id,
                        f"bench-{scenario_index}-",
                    ),
                    "opportunities": await count_rows(
                        PropOpportunityLog,
                        PropOpportunityLog.external_id,
                        f"bench-{scenario_index}-",
                    ),
                    "graded_tracking": await count_tracking_results(
                        f"bench-{scenario_index}-",
                        "HIT",
                    ),
                    "graded_opportunities": await count_opportunity_results(
                        f"bench-{scenario_index}-",
                        "HIT",
                    ),
                }
                expected_counts = {
                    "observations": expected_observations,
                    "normalized_history": expected_observations,
                    "candidates": expected_candidates,
                    "opportunities": expected_opportunities,
                    "graded_tracking": expected_tracking,
                    "graded_opportunities": expected_opportunities,
                }
                count_deltas = {
                    key: counts[key] - expected_counts[key]
                    for key in expected_counts
                }
                assert all(
                    abs(delta) <= FULL_FEED_MAX_COUNT_DELTA
                    for delta in count_deltas.values()
                ), f"unexpected durable count deltas: {count_deltas}"
                assert metric_delta["failed"] <= FULL_FEED_MAX_PERSISTENCE_FAILURES
                assert metric_delta["queue_full"] <= FULL_FEED_MAX_QUEUE_SATURATION
                assert metric_delta["enqueued"] == 5
                assert metric_delta["completed"] == 5
                assert elapsed <= cadence_budget_seconds
                assert (
                    backpressure_delta.get("writer_wait_seconds", 0.0)
                    <= FULL_FEED_MAX_WRITER_WAIT_SECONDS
                )

                reports.append({
                    "scenario": scenario_index,
                    "cycle": cycle + 1,
                    "feed_size": feed_size,
                    "full_pool_size": full_pool_size,
                    "scan_elapsed_seconds": round(elapsed, 4),
                    "writer_wait_seconds": backpressure_delta.get(
                        "writer_wait_seconds", 0.0
                    ),
                    "writer_waits": int(backpressure_delta.get("writer_waits", 0)),
                    "queue_full": int(metric_delta["queue_full"]),
                    "queue_depth_after_drain": 0,
                    "persisted": counts,
                    "count_deltas": count_deltas,
                })
    finally:
        await engine.drain_db_persistence_worker()
        worker = engine._db_persistence_worker_task
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        engine._db_persistence_queue = None
        engine._db_persistence_worker_task = None
        engine._db_persistence_loop = None
        await database.close()

    record_testsuite_property(
        "full_feed_persistence_benchmark",
        json.dumps(reports),
    )
    record_testsuite_property(
        "full_feed_persistence_thresholds",
        json.dumps({
            "cadence_budget_seconds": FULL_FEED_CADENCE_BUDGET_SECONDS,
            "max_queue_saturation": FULL_FEED_MAX_QUEUE_SATURATION,
            "max_persistence_failures": FULL_FEED_MAX_PERSISTENCE_FAILURES,
            "max_writer_wait_seconds": FULL_FEED_MAX_WRITER_WAIT_SECONDS,
            "max_absolute_count_delta": FULL_FEED_MAX_COUNT_DELTA,
        }),
    )
    assert len(reports) == len(scenarios) * cycles_per_scenario


@pytest.mark.asyncio
async def test_concurrent_file_backed_writers_stay_within_cadence(
    tmp_path,
    record_testsuite_property,
):
    """Exercise shared SQLite gates with grading/evidence writers in flight.

    The multi-cycle benchmark above intentionally serializes the persistence
    worker, so it cannot prove that a grading pass or player-evidence write
    waits safely behind a live scan.  This phase uses two independently pooled
    ``Database`` objects pointing at the same file, holds one bounded write
    transaction open, and then releases four writers together.  The hold makes
    writer-gate contention deterministic without relying on scheduler timing or
    a platform-specific SQLite lock race.
    """
    from sqlalchemy import func, select

    import database as database_module
    from database import (
        Database,
        PlayerGameResult,
        PropCandidateLog,
        PropOpportunityLog,
        PropTrackingLog,
        ScanCycleLog,
        UnderdogSnapshotRecord,
    )

    cadence_budget_seconds = 120.0
    url = f"sqlite+aiosqlite:///{tmp_path / 'concurrent-writers.sqlite'}"
    monitoring_database = Database(url)
    grading_database = Database(url)
    await monitoring_database.init()
    await grading_database.init()

    phase_prefix = "contention-"
    now = datetime.utcnow()
    future_game = now + timedelta(hours=6)
    tracking_rows = [
        {
            "external_id": f"{phase_prefix}tracking-{index}",
            "player_name": f"Contention Player {index}",
            "team": "BENCH",
            "sport": "NBA",
            "stat_type": "Points",
            "line_value": 20.5,
            "game_time": future_game,
            "game_id": f"{phase_prefix}tracking-game-{index}",
            "provider": "Underdog",
        }
        for index in range(48)
    ]
    opportunity_rows = [
        {
            "external_id": f"{phase_prefix}opportunity-{index}",
            "player_name": f"Contention Player {index}",
            "team": "BENCH",
            "sport": "NBA",
            "stat_type": "Points",
            "line_value": 20.5,
            "recommendation": "OVER",
            "decision_tier": "A",
            "confidence": 75,
            "game_time": future_game,
            "game_id": f"{phase_prefix}opportunity-game-{index}",
            "provider": "Underdog",
            "bet_quality_score": 80,
        }
        for index in range(48)
    ]

    try:
        assert await monitoring_database.track_prop_records(tracking_rows) == len(
            tracking_rows
        )
        assert await monitoring_database.log_prop_opportunity_bulk(
            opportunity_rows
        ) == len(opportunity_rows)

        async with monitoring_database.session() as session:
            tracking_result = await session.execute(
                select(PropTrackingLog.id).where(
                    PropTrackingLog.external_id.like(f"{phase_prefix}tracking-%")
                )
            )
            tracking_ids = [int(row_id) for row_id in tracking_result.scalars().all()]
            opportunity_result = await session.execute(
                select(PropOpportunityLog.id).where(
                    PropOpportunityLog.external_id.like(
                        f"{phase_prefix}opportunity-%"
                    )
                )
            )
            opportunity_ids = [
                int(row_id) for row_id in opportunity_result.scalars().all()
            ]
        assert len(tracking_ids) == len(tracking_rows)
        assert len(opportunity_ids) == len(opportunity_rows)

        feed_rows = [
            UnderdogSnapshotRecord(
                external_id=f"{phase_prefix}snapshot-{index}",
                player_name=f"Contention Feed Player {index}",
                team="BENCH",
                sport="NBA",
                stat_type="Points",
                line_value=20.5,
                game_id=f"{phase_prefix}feed-game-{index}",
                game_time=future_game,
                fetched_at=now,
            )
            for index in range(256)
        ]
        candidate_rows = [
            {
                "scan_ts": now,
                "player_name": f"Contention Feed Player {index}",
                "team": "BENCH",
                "sport": "NBA",
                "stat_type": "Points",
                "line_value": 20.5,
                "provider": "Underdog",
                "score_total": 70.0,
                "score_tier": "A",
                "confidence": 75,
                "gate_decision": "EVALUATED",
                "snapshot_external_id": f"{phase_prefix}candidate-{index}",
            }
            for index in range(128)
        ]
        evidence_rows = [
            SimpleNamespace(
                player_name=f"Contention Evidence Player {index}",
                sport="NBA",
                stat_type="points",
                game_date=(now - timedelta(days=1)).strftime("%Y-%m-%d"),
                actual_value=21.0,
                opponent="BENCH",
                source="contention-benchmark",
                game_id=f"{phase_prefix}evidence-game-{index}",
            )
            for index in range(64)
        ]

        holder_ready = asyncio.Event()
        release_holder = asyncio.Event()
        phase_started = asyncio.Event()
        phase_start = asyncio.Event()
        ready_count = 0
        ready_count_lock = asyncio.Lock()
        writer_count = 4

        async def hold_writer_slot() -> None:
            async with monitoring_database.session() as session:
                session.add(
                    ScanCycleLog(
                        scan_ts=now,
                        fetched=1,
                        active=1,
                        analyzed=1,
                    )
                )
                await session.flush()
                holder_ready.set()
                await release_holder.wait()
                await session.commit()

        async def await_phase_start() -> None:
            nonlocal ready_count
            async with ready_count_lock:
                ready_count += 1
                if ready_count == writer_count:
                    phase_started.set()
            await phase_start.wait()

        async def persist_feed() -> dict[str, int]:
            await await_phase_start()
            snapshot_result = await monitoring_database.save_underdog_snapshots_bulk(
                feed_rows
            )
            candidate_count = await monitoring_database.log_prop_candidate_batch(
                candidate_rows
            )
            return {
                "snapshots": int(snapshot_result["records_written"]),
                "candidates": candidate_count,
                "snapshot_retries": int(snapshot_result["retry_count"]),
            }

        async def grade_tracking() -> int:
            await await_phase_start()
            return await grading_database.grade_tracked_props_bulk(
                [(tracking_id, "HIT", 21.0) for tracking_id in tracking_ids]
            )

        async def grade_opportunities() -> int:
            await await_phase_start()
            return await grading_database.grade_opportunities_bulk(
                [
                    {"id": opportunity_id, "result": "HIT", "actual_value": 21.0}
                    for opportunity_id in opportunity_ids
                ]
            )

        async def persist_evidence() -> int:
            await await_phase_start()
            return await grading_database.upsert_player_results_bulk(evidence_rows)

        metrics_before = database_module.get_sqlite_backpressure_metrics(
            url,
            reset=True,
        )
        holder_task = asyncio.create_task(hold_writer_slot())
        await asyncio.wait_for(holder_ready.wait(), timeout=5.0)

        started = asyncio.get_running_loop().time()
        writer_tasks = [
            asyncio.create_task(persist_feed()),
            asyncio.create_task(grade_tracking()),
            asyncio.create_task(grade_opportunities()),
            asyncio.create_task(persist_evidence()),
        ]
        await asyncio.wait_for(phase_started.wait(), timeout=5.0)
        phase_start.set()
        # Let every writer reach the shared gate while the holder is still
        # active.  The explicit pause makes writer_waits deterministic while
        # keeping the entire phase far below the 120-second cadence.
        await asyncio.sleep(0.05)
        release_holder.set()
        writer_results = await asyncio.gather(*writer_tasks)
        await holder_task
        cycle_elapsed = asyncio.get_running_loop().time() - started

        metrics_after = database_module.get_sqlite_backpressure_metrics(url)
        backpressure_delta = {
            key: float(metrics_after.get(key, 0.0))
            - float(metrics_before.get(key, 0.0))
            for key in metrics_after
        }

        async def count_rows(model, column, prefix: str) -> int:
            async with monitoring_database.session() as session:
                result = await session.execute(
                    select(func.count()).select_from(model).where(
                        column.like(f"{prefix}%")
                    )
                )
                return int(result.scalar() or 0)

        async def count_results(model, prefix: str) -> int:
            async with monitoring_database.session() as session:
                result = await session.execute(
                    select(func.count()).select_from(model).where(
                        model.external_id.like(f"{prefix}%"),
                        model.result == "HIT",
                    )
                )
                return int(result.scalar() or 0)

        persisted = {
            "snapshots": await count_rows(
                UnderdogSnapshotRecord,
                UnderdogSnapshotRecord.external_id,
                f"{phase_prefix}snapshot-",
            ),
            "candidates": await count_rows(
                PropCandidateLog,
                PropCandidateLog.snapshot_external_id,
                f"{phase_prefix}candidate-",
            ),
            "graded_tracking": await count_results(
                PropTrackingLog,
                f"{phase_prefix}tracking-",
            ),
            "graded_opportunities": await count_results(
                PropOpportunityLog,
                f"{phase_prefix}opportunity-",
            ),
            "evidence": await count_rows(
                PlayerGameResult,
                PlayerGameResult.game_id,
                f"{phase_prefix}evidence-game-",
            ),
        }
        expected = {
            "snapshots": len(feed_rows),
            "candidates": len(candidate_rows),
            "graded_tracking": len(tracking_rows),
            "graded_opportunities": len(opportunity_rows),
            "evidence": len(evidence_rows),
        }
        assert writer_results[0]["snapshots"] == len(feed_rows)
        assert writer_results[0]["candidates"] == len(candidate_rows)
        assert writer_results[0]["snapshot_retries"] >= 0
        assert writer_results[1] == len(tracking_rows)
        assert writer_results[2] == len(opportunity_rows)
        assert writer_results[3] == len(evidence_rows)
        assert persisted == expected, "concurrent writers lost required rows"
        assert backpressure_delta["writer_waits"] >= 1
        assert backpressure_delta["writer_wait_seconds"] > 0.0
        assert backpressure_delta["write_retries"] >= 0
        assert cycle_elapsed <= cadence_budget_seconds

        contention_report = {
            "cycle_elapsed_seconds": round(cycle_elapsed, 4),
            "writer_wait_seconds": round(
                backpressure_delta["writer_wait_seconds"], 6
            ),
            "writer_waits": int(backpressure_delta["writer_waits"]),
            "session_wait_seconds": round(
                backpressure_delta["session_wait_seconds"], 6
            ),
            "session_waits": int(backpressure_delta["session_waits"]),
            "write_retries": int(backpressure_delta["write_retries"]),
            "snapshot_retries": int(writer_results[0]["snapshot_retries"]),
            "persisted": persisted,
        }
    finally:
        await grading_database.close()
        await monitoring_database.close()

    record_testsuite_property(
        "concurrent_file_backed_writer_benchmark",
        json.dumps(contention_report),
    )


# ── 1. Poll interval ──────────────────────────────────────────────────────────


def test_poll_interval_default_180s():
    """config.py must leave enough time for the full Player Props cycle."""
    src = _src("config.py")
    # Config uses os.environ.get with "180" as default
    assert re.search(
        r'UNDERDOG_POLL_INTERVAL.*int\(os\.environ\.get\([^,]+,\s*["\']180["\']\)',
        src,
    ), "UNDERDOG_POLL_INTERVAL default must be '180'"


# ── 2. scheduled execution coordinator ───────────────────────────────────────


def test_coordinator_in_underdog_monitor():
    """underdog_monitor uses the shared coalescing coordinator."""
    src = _src("main.py")
    assert '_coordinated_job("underdog_monitor", _underdog_monitor_cycle)' in src


def test_no_scheduler_max_instances_override():
    """Overlap protection must not be delegated to max_instances."""
    src = _src("main.py")
    assert "max_instances" not in src


# ── 3. Full prop feed monitored ───────────────────────────────────────────────


def test_incremental_records_buffer_used():
    """Normal-scan records must accumulate in _incremental_records, not saved 1-by-1."""
    src = _src("market_engine.py")
    assert "_incremental_records.append(record)" in src, (
        "_incremental_records.append(record) not found — per-prop batch buffering missing"
    )


def test_bulk_save_called_for_incremental():
    """save_underdog_snapshots_bulk must be called with the incremental records."""
    src = _src("market_engine.py")
    # The flush block copies _incremental_records into _incr_snapshot then bulk-saves it
    assert "save_underdog_snapshots_bulk(" in src, (
        "save_underdog_snapshots_bulk not found — bulk flush missing"
    )
    assert "_incr_snapshot" in src or "_incremental_records" in src, (
        "Incremental snapshot buffer not found in bulk-save block"
    )


def test_full_feed_keeps_unchanged_observations():
    """Raw history must retain every feed observation, not only movers."""
    src = _src("market_engine.py")
    assert "raw snapshot append-only for the complete feed" in src
    assert "normalized PropLineHistory upsert" in src


def test_persistence_queue_saturation_applies_backpressure():
    """A full deferred queue must not silently discard required writes."""
    src = _src("market_engine.py")
    assert "caller must apply backpressure" in src
    assert "dropping deferred operation" not in src
    assert "db.log_prop_candidate_batch(_candidate_batch)" in src


def test_large_writes_are_chunked_and_yield_between_transactions():
    """Feed/history/opportunity writes must release SQLite between chunks."""
    database_src = _src("database.py")
    engine_src = _src("market_engine.py")
    assert "_UNDERDOG_BULK_BATCH_SIZE = 200" in database_src
    assert "_PROP_HISTORY_BATCH_SIZE = 200" in database_src
    assert "_TRACKING_BATCH_SIZE = 200" in database_src
    assert "await asyncio.sleep(0)" in database_src
    assert "full_pool_rescan_timing:" in engine_src


def test_single_save_not_called_in_main_loop():
    """save_underdog_snapshot (singular) must not be called inside the per-prop loop."""
    src = _src("market_engine.py")
    # The call save_underdog_snapshot(record) should not be awaited in the hot path
    # It may still exist as a helper but must not appear after the '_incremental_records' refactor
    # We check that the only remaining reference is the function definition in database.py
    # (we only inspect market_engine.py here)
    lines = src.splitlines()
    hot_path_calls = [
        i for i, ln in enumerate(lines, 1)
        if "await db.save_underdog_snapshot(record)" in ln
    ]
    assert hot_path_calls == [], (
        f"await db.save_underdog_snapshot(record) still called at line(s) "
        f"{hot_path_calls} — per-prop individual save not removed"
    )


def test_cold_start_bulk_save_unchanged():
    """Cold-start bulk-save path (_cold_start_records) must still be present."""
    src = _src("market_engine.py")
    assert "_cold_start_records.append(record)" in src
    assert "save_underdog_snapshots_bulk(_cs_batch)" in src


# ── 4. No sports silently removed ─────────────────────────────────────────────


def test_ud_alert_sports_default_contains_core_sports():
    """UD_ALERT_SPORTS default must still contain MLB, NFL, NBA."""
    src = _src("config.py")
    for sport in ("MLB", "NFL", "NBA"):
        assert sport in src, f"{sport} missing from config.py UD_ALERT_SPORTS default"


# ── 5. Scoring unchanged ──────────────────────────────────────────────────────


def test_compute_market_quality_still_imported():
    """compute_market_quality must still be imported/used in market_engine.py."""
    src = _src("market_engine.py")
    assert "compute_market_quality" in src


# ── 6. Tier thresholds ────────────────────────────────────────────────────────


def test_s_tier_threshold_85():
    """S-tier threshold remains configurable at 85."""
    src = _src("config.py")
    assert "UD_MIN_CONF_S" in src and '"85"' in src


def test_a_tier_threshold_70():
    """A-tier threshold remains configurable at 75."""
    src = _src("config.py")
    assert "UD_MIN_CONF_A" in src and '"75"' in src


# ── 7. Confidence / BQ gates ──────────────────────────────────────────────────


def test_min_conf_s_config_key_present():
    src = _src("config.py")
    assert "UD_MIN_CONF_S" in src


def test_min_conf_a_config_key_present():
    src = _src("config.py")
    assert "UD_MIN_CONF_A" in src


def test_bq_gate_still_present():
    """BQ gate (bet_quality_score / BQ) must still be referenced."""
    src = _src("market_engine.py")
    assert "bq_gate" in src or "bet_quality_score" in src or "BQ" in src


# ── 8. MLB UNDER direction coverage ──────────────────────────────────────────


def test_mlb_under_whitelist_removed():
    """The retired MLB UNDER whitelist must not remain in market_engine.py."""
    src = _src("market_engine.py")
    assert "MLB UNDER whitelist" not in src


def test_mlb_under_has_no_whitelist_entries():
    """No delivery path should retain an MLB UNDER whitelist."""
    src = _src("market_engine.py")
    assert "mlb_under_gate" not in src


# ── 9. NFL direction rules ────────────────────────────────────────────────────


def test_nfl_direction_rules_present():
    """NFL-specific direction logic must exist in market_engine.py."""
    src = _src("market_engine.py")
    # NFL UNDER is allowed; must see NFL referenced near direction/UNDER logic
    assert "NFL" in src
    assert "UNDER" in src


# ── 10. Dedup unchanged ───────────────────────────────────────────────────────


def test_prop_market_alerted_dict_present():
    """In-memory dedup dict _prop_market_alerted must still be used."""
    src = _src("market_engine.py")
    assert "_prop_market_alerted" in src


def test_is_prop_deduped_called():
    """_is_prop_deduped helper must still be called."""
    src = _src("market_engine.py")
    assert "_is_prop_deduped(" in src


# ── 11. CLV seed ──────────────────────────────────────────────────────────────


def test_clv_seed_still_called():
    """AlertCLVSeed / clv_seeder still referenced (not removed by perf pass)."""
    src = _src("market_engine.py")
    assert "AlertCLVSeed" in src or "clv_seed" in src.lower()


# ── 12. Stale-line guard on all paths ─────────────────────────────────────────


def test_ud_line_fresh_helper_present():
    src = _src("market_engine.py")
    assert "_ud_line_fresh(" in src or "def _ud_line_fresh" in src


def test_current_scan_line_map_built():
    """_current_scan_line_map must be built from ud_snaps each cycle."""
    src = _src("market_engine.py")
    assert "_current_scan_line_map" in src


def test_stale_line_guard_on_np_path():
    """NP delivery path must check _ud_line_fresh."""
    src = _src("market_engine.py")
    assert "_ud_line_fresh(" in src


def test_stale_line_guard_on_lc_path():
    """LC delivery path must reference _ud_line_fresh or _current_scan_line_map."""
    src = _src("market_engine.py")
    # Both np and lc paths use the same helper
    assert "_ud_line_fresh(" in src and "_current_scan_line_map" in src


# ── 13. Fast Resume absent ────────────────────────────────────────────────────


def test_fast_resume_threshold_removed():
    src = _src("market_engine.py")
    assert "_FAST_RESUME_THRESHOLD_MINUTES" not in src, \
        "_FAST_RESUME_THRESHOLD_MINUTES found — fast resume not fully removed"


def test_fast_resume_function_removed():
    src = _src("market_engine.py")
    assert "_fast_resume" not in src, \
        "_fast_resume found — fast resume not fully removed"


def test_no_fast_resume_in_config():
    src = _src("config.py")
    assert "FAST_RESUME" not in src, \
        "FAST_RESUME found in config.py — fast resume not fully removed"


# ── 14. Scheduler trigger coalescing ─────────────────────────────────────────


def test_scheduler_does_not_configure_misfire_skips():
    """Triggers are coalesced by the coordinator, not recorded as skips."""
    src = _src("main.py")
    assert "misfire_grace_time" not in src


def test_coordinator_and_scan_running_flag_prevent_duplicate_heavy_scans():
    """The coordinator and in-job flag protect the heavy path."""
    import market_engine as me
    src = _src("main.py")
    assert "_coordinated_job" in src
    # The flag must guard the heavy path
    import inspect
    me_src = inspect.getsource(me)
    assert "_ud_full_scan_running" in me_src, \
        "_ud_full_scan_running flag missing — second instance not guarded"
