---
name: Scope filter config access bug
description: Module-versus-instance config access pattern for alert scope filters
---

`alert_scope_filter.py` must access the Config instance rather than the
`config.py` module:

```python
import config as _cfg_mod
sport_val = sport.value if hasattr(sport, "value") else str(sport)
return sport_val in _cfg_mod.config.ud_tier1_sports
```

Never use `import config; config.<property>`; the module and its
`config = Config()` instance share a name. A broad exception fallback can
silently block every sport, so scope failures must remain visible.