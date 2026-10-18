---
name: PropLine complete-feed observation ledger
description: Complete dynamically discovered multi-sport ingestion and immutable normalized observation storage for the shared PropLine feed.
---

The internal PropLine ingestion pass must cover all discovered sports monitored by V4, not just basketball or non-football. It persists each successful sport to current-state history and an append-only normalized observation ledger, and withholds evaluation when a required feed or write is incomplete. Stored rows are classified as the shared `Player Props` product; PrizePicks and Underdog remain distinct source provenance.

**Why:** The user intends PropLine to supplement most V4-monitored sports. A one-sport or sport-family-limited fetch loses provider observations, while mutable current-state rows cannot prove what a provider returned at a specific fetch. Evaluating a partial multi-sport pass also makes incomplete evidence look successful.

**How to apply:** Discover sports from the current catalog and use the shared bounded request budget; do not skip a monitored sport solely by sport family. Keep unresolved live observations in the observation/history path only. Completed-game grading must use explicit settled results, and a failed sport or persistence barrier must remain visible as an incomplete feed rather than triggering scoring or delivery. Use product classification for shared prop logic and provider fields for source reconciliation.