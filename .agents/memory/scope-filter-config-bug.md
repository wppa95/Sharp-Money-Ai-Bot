---
name: Odds API scope filter config access bug
description: alert_scope_filter.py had silent module-vs-instance naming collision that blocked all sports; fix pattern and related invariants.
---

## The Bug

`alert_scope_filter.py` used `import config; config.ud_tier1_sports` — this accesses the MODULE (`config.py`) not the Config instance. The module does not have `ud_tier1_sports`; it is on the `config = Config()` instance inside the module.

The `except Exception: return False` fallback made this silent — every sport was blocked.

## Fix Pattern

```python
import config as _cfg_mod
sport_val = sport.value if hasattr(sport, "value") else str(sport)
return sport_val in _cfg_mod.config.ud_tier1_sports
```

Same pattern for `check()`: `_sport_val = obj.sport.value if hasattr(obj.sport, "value") else str(obj.sport)`.

**Never** do `import config; config.<property>` — always `config.config.<property>`.

## Related Invariants

- `_poll_odds_job` in main.py iterates `config.ud_tier1_sports` (NOT `config.active_sports`) to decide which sports to fetch Odds API lines for. Tests that mock `main.config` must set both `mock_cfg.ud_tier1_sports` and `mock_cfg.active_sports`.
- `config.active_sports` parses `ACTIVE_SPORTS_RAW` — contains Underdog scanner sport strings (TENNIS, CS, etc.), NOT Sport enum values. These are separate namespaces.
- Tier-2 (MLB, NBA, NFL) is blocked from all Odds API EV pipeline paths. `is_ev_line_in_scope` correctly returns False for these.

**Why:** The `config` Python module and the `config = Config()` instance share the same name — accessing `module.ud_tier1_sports` always raised AttributeError silently.
