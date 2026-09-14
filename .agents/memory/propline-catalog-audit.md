---
name: PropLine catalog audit boundary
description: Durable handling of provider catalog identities that are not yet covered by canonical mappings
---

Catalog identities are operational evidence, not player-prop evidence. Unknown provider-native sport and market keys may be retained with canonical context and sample counts, but they must not create request targets, scoring inputs, or delivery candidates. A later complete catalog audit can resolve the same identity to mapped without treating the catalog row as a player result.

**Why:** Provider catalog drift needs incident history, but using an unmapped identity as a fallback would silently expand the monitored universe and could produce unsupported bets.

**How to apply:** Persist catalog findings in their own audit surface, ignore incomplete/unavailable discovery for persistence, and keep the normal scoring/delivery tables sourced only from established mappings.