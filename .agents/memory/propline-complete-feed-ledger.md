---
name: PropLine complete-feed observation ledger
description: Complete active-sport ingestion and immutable normalized observation storage for the internal PrizePicks feed.
---

The internal PropLine/PrizePicks ingestion pass fetches every currently discovered non-football sport, persists each successful sport to both current-state history and an append-only normalized observation ledger, and withholds evaluation when any sport or required write is incomplete. Stored rows are classified as the shared `Player Props` product; `PrizePicks` remains source provenance only.

**Why:** A one-sport rotating fetch left active PrizePicks coverage delayed, while mutable current-state rows could not prove what a provider returned at a specific fetch. Evaluating a partial multi-sport pass also made incomplete evidence look successful.

**How to apply:** Keep unresolved live observations in the observation/history path only. Completed-game grading must continue to use explicit settled results, and a failed sport or persistence barrier must remain visible as an incomplete feed rather than triggering scoring or delivery. Use the product classification for shared prop logic and the provider field only for source reconciliation.