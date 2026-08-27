---
name: OddsAPI error pacing
description: Permanent invalid-market responses must not delay valid quota polling.
---

An OddsAPI 422 INVALID_MARKET response is a permanent request-shape or market-availability error, not a transient quota/provider failure. It must not trigger adaptive backoff, and each failed response should be classified exactly once so retryable statuses retain their intended backoff.

**Why:** The player-prop path can receive 422 responses while the useful game-line poller shares the same cache/pacer. Treating them as transient delayed valid requests and double-counted retry state.

**How to apply:** Keep invalid-market handling non-blocking in the shared OddsAPI cache; pass the actual response status to one centralized pacer failure handler.