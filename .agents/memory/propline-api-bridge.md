---
name: PropLine analytics API bridge
description: Boundary for exposing PropLine analytics through the separate TypeScript API artifact
---

The API artifact must transport requests to a persistent Python worker that owns `PropLineAnalytics`. It must not duplicate PropLine HTTP calls, scoring, alerting, or source validation in TypeScript.

**Why:** The authorized provider client keeps the official-host boundary, endpoint cache, daily budget, and rolling-hour pacing in Python. A one-shot subprocess would recreate those controls for every request and make external dashboard traffic burst independently.

**How to apply:** Keep the bridge line-oriented and serialized, reuse the existing analytics bundle payloads, and surface provider failure envelopes as read-only JSON rather than converting them into alerts or synthetic evidence. A worker crash must clear the child handle so the next request starts a fresh worker; do not permanently poison the API process after one failed request.