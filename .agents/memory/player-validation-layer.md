---
name: Player Prop Validation Layer
description: How the validation gate works for Underdog immediate alerts and what data is stored per snapshot.
---

## Rule

Before any immediate individual Telegram alert fires for an Underdog prop, the prop must pass the player validation gate: `n_history >= config.UD_VALIDATION_MIN_SAMPLES` (default 5). Props that fail this gate go to the digest only — they are still stored and still appear in the cycle summary.

**Why:** A 0.5 HR prop should not alert just because the number is low. There must be enough historical evidence to support the signal.

## Consequence for new props

`is_new_prop=True` → `get_ud_prop_history()` returns `[]` on first appearance → `n_history=0` → `has_supporting_data=False` → `np_immediate=False` always. New props NEVER get immediate alerts on first appearance. They build history over subsequent cycles and eventually qualify via the line-change path's score gate.

## How to apply

- To test immediate alert behaviour, provide `prop_history=_fake_history(6)` to `_make_db()`; empty history means no immediate alert even for 0.5+priority stats.
- `_fake_history` must set `prev_line=None` and `removed=False` on each record — the scoring model (`_score_consistency`, `_score_stability`) accesses both.
- `config.UD_VALIDATION_MIN_SAMPLES` controls the threshold (env: `UD_VALIDATION_MIN_SAMPLES`).

## Storage

`UnderdogSnapshotRecord.validation_json` (TEXT, nullable) stores a compact JSON blob per snapshot:
```json
{"n":8,"l5":0.8,"l10":0.7,"l20":0.65,"l30":null,"avg":0.5,"min":0.5,"rate_below":0.9,"season":null,"h2h":null,"has_data":true}
```
- `season` and `h2h` are always `null` until game-result tracking is added.
- `l5/l10/l20/l30` are line-move rates (fraction of records where `line_moved=True`), **not** actual hit rates against prop lines. They are market-proxy signals only.

## Files

- `bot/engine/player_validator.py` — `PlayerPropValidation` dataclass + `validate_player_prop()` function
- `bot/market_engine.py` — validation called in both `is_new_prop` and line-change branches; result stored in record
- `bot/alerts.py` / `bot/alerts_multiplatform.py` — `validation=` kwarg added; history block shown in alerts when `has_supporting_data=True`
