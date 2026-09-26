from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_evidence_snapshot_column_exists_before_v2_reads_legacy_audit_rows(
    tmp_path,
):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from database import Database

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy-audit.sqlite'}"
    legacy_engine = create_async_engine(database_url)
    async with legacy_engine.begin() as connection:
        await connection.execute(text("""
            CREATE TABLE telegram_delivery_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider VARCHAR(32) NOT NULL,
                alert_kind VARCHAR(64) NOT NULL,
                delivery_path VARCHAR(64) NOT NULL,
                player_name VARCHAR(128),
                sport VARCHAR(32),
                stat_type VARCHAR(64),
                external_id VARCHAR(128),
                event_id VARCHAR(128),
                delivery_key VARCHAR(256),
                line_value FLOAT,
                outcome VARCHAR(24) NOT NULL,
                recipients_sent INTEGER NOT NULL DEFAULT 0,
                recipients_failed INTEGER NOT NULL DEFAULT 0,
                attempted_at DATETIME NOT NULL
            )
        """))
        await connection.execute(text("""
            INSERT INTO telegram_delivery_audit (
                provider, alert_kind, delivery_path, player_name, sport,
                stat_type, external_id, event_id, delivery_key, line_value,
                outcome, recipients_sent, recipients_failed, attempted_at
            ) VALUES (
                'Underdog', 'ACTIONABLE_PICK', 'underdog', 'Legacy Player',
                'MLB', 'Hits', 'legacy-source', 'legacy-event', NULL, 0.5,
                'sent', 1, 0, '2026-09-25 12:00:00'
            )
        """))
    await legacy_engine.dispose()

    database = Database(database_url)
    await database.init()
    try:
        async with database._engine.connect() as connection:
            columns = await connection.execute(
                text("PRAGMA table_info(telegram_delivery_audit)")
            )
            column_names = {row[1] for row in columns.fetchall()}
        assert "evidence_snapshot_json" in column_names
    finally:
        await database.close()