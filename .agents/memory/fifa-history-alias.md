---
name: FIFA history alias
description: Constraint for reusing the soccer evidence provider for Underdog FIFA props
---

FIFA may reuse the existing soccer history provider, but every returned `RawGameResult` must preserve `sport="FIFA"` so stored evidence cannot mismatch the Underdog sport key. Empty or unsupported provider results remain N/A.

**Why:** The provider abstraction is shared, while downstream history queries are keyed by the requested Underdog sport. Relabeling FIFA results as SOCCER would make valid rows invisible or attach evidence to the wrong sport.

**How to apply:** Keep FIFA as a narrow routing alias only. Do not invent FIFA event data or broaden the soccer provider's competition/stat coverage without separate validation.