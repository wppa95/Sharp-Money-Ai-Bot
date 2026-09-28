---
name: PropLine zero-observation diagnosis
description: How to distinguish an empty PrizePicks sport slate from an ingestion regression
---

The authorized PropLine feed uses a bookmaker/player-prop event envelope:
events contain `bookmakers`, the PrizePicks bookmaker contains `player_*`
markets, and each outcome carries the player in `description`, direction in
`name`, and the line in `point`. The normalizer accepts this shape.

**Why:** A successful request can legitimately return an empty event list for
one active catalog sport while another sport has valid player props. Reporting
zero normalized observations without the raw-record count makes upstream
availability look like a parser failure.

**How to apply:** Keep the one-sport-per-cycle pacing and treat
`raw_records=0, normalized_props=0` as an empty upstream slate. Treat raw
records with zero normalized props as a normalization regression, and verify
that valid normalized props reach both append-only history and universal
tracking before investigating scoring or delivery.