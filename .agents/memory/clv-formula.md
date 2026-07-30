---
name: CLV formula direction
description: The correct direction for CLV% using fair probability comparison
---

## Rule
CLV% = `(fp_close / fp_bet - 1) × 100`

NOT `(fp_bet / fp_close - 1) × 100`

**Why:** As a bettor you want LOWER implied probability at your bet price (less juice). When the market closes tighter (higher implied probability), it means the market got more confident AFTER you bet — that's positive CLV.

Example: bet -110 (fp≈0.524), closed -130 (fp≈0.565).
- Correct: 0.565/0.524 - 1 = +7.8% CLV ✅  
- Wrong:   0.524/0.565 - 1 = -7.3% CLV ❌

**How to apply:** Whenever computing CLV from fair probabilities, use fp_close in the numerator. The raw proxy `bet_odds - closing_odds` gives the same sign correctly (+20 when you beat the close).
