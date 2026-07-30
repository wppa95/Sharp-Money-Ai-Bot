"""
Tests for engine/ranking.py and engine/backtesting.py.

Run from workspace root:
    python3 -m pytest bot/tests/test_ranking.py -v

Or directly:
    cd /home/runner/workspace && python3 bot/tests/test_ranking.py
"""

from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock
from engine.ranking import (
    RankingTier, RankingDecision, HistoricalStats,
    compute_ranking, MIN_SAMPLE_SIZE,
)
from engine.backtesting import (
    BacktestEngine, BacktestRecord, BacktestReport,
    DimensionStats, run_backtest, RankingTier as BT_Tier,
)

SEP = "=" * 64

def _pass():
    print("  ✓ PASS")

def _fail(msg):
    raise AssertionError(msg)


# ── Helper: minimal compute_ranking call ─────────────────────────────────────

def _rank(
    steam=75, ev=5.0, fp=0.55, n_books=3, sharp=1,
    agree=0.85, speed=1.0, liq=60, minutes=360.0,
    overall=None, market=None, sport=None,
):
    return compute_ranking(
        steam_score=steam, ev_edge_pct=ev, fair_probability=fp,
        n_books_moving=n_books, sharp_book_count=sharp,
        market_agreement=agree, movement_speed=speed,
        liquidity_score=liq, minutes_to_game=minutes,
        overall_history=overall, market_history=market,
        sport_history=sport,
    )


# ── Helper: mock EVRecord ─────────────────────────────────────────────────────

def _ev_record(ai_confidence, result, clv=None, sport="NFL",
               market_type="Spread", ev=3.5):
    r = MagicMock()
    r.ai_confidence = ai_confidence
    r.result        = result
    r.clv           = clv
    r.sport         = sport
    r.market_type   = market_type
    r.expected_value = ev
    r.event         = "Team A @ Team B"
    r.selection     = "Team A -3"
    return r


# ══════════════════════════════════════════════════════════════
print(SEP)
print("  TEST 1 — Tier boundaries (S/A/B/Pass at 95/85/75/<75)")
print(SEP)

BOUNDARY_CASES = [
    (100, RankingTier.S),
    (95,  RankingTier.S),
    (94,  RankingTier.A),
    (85,  RankingTier.A),
    (84,  RankingTier.B),
    (75,  RankingTier.B),
    (74,  RankingTier.PASS),
    (0,   RankingTier.PASS),
]
for score, expected in BOUNDARY_CASES:
    got = RankingTier.from_score(score)
    ok  = got == expected
    print(f"  score={score:>3}  expected={expected.value:<5}  got={got.value:<5}  {'✓' if ok else '✗'}")
    assert ok, f"Tier boundary failed at {score}"
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 2 — No history: pure signal ranking")
print(SEP)

r_high = _rank(steam=85, ev=7.0, n_books=5, sharp=3, agree=1.0,
               speed=2.0, liq=80, minutes=60*30)
print(f"  High-signal: score={r_high.score}  tier={r_high.tier.value}  "
      f"decision={r_high.decision.value}")
assert r_high.tier in (RankingTier.S, RankingTier.A, RankingTier.B), \
    f"Got {r_high.tier}"
assert r_high.historical_breakdown.total == 0, "No history → adj must be 0"
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 3 — Historical boost: 63% win rate, +3% avg CLV → higher tier")
print(SEP)

h_good = HistoricalStats(sample_size=50, win_rate=0.63, avg_clv=3.2, roi=4.0)
r_base = _rank(steam=72, ev=4.5, n_books=3, sharp=1, agree=0.80,
               speed=0.8, liq=55, minutes=360)
r_boost = _rank(steam=72, ev=4.5, n_books=3, sharp=1, agree=0.80,
                speed=0.8, liq=55, minutes=360, overall=h_good)

print(f"  Base score : {r_base.score}  tier={r_base.tier.value}")
print(f"  Boost score: {r_boost.score}  tier={r_boost.tier.value}")
print(f"  Hist adj   : {r_boost.historical_breakdown.total:+d}")

assert r_boost.score >= r_base.score, "Good history must not lower score"
assert r_boost.historical_breakdown.total > 0, "Good history must give positive adj"
assert r_boost.historical_breakdown.overall_adj == 5, \
    f"63% WR → +5 expected, got {r_boost.historical_breakdown.overall_adj}"
assert r_boost.historical_breakdown.clv_adj == 3, \
    f"3.2% CLV → +3 expected, got {r_boost.historical_breakdown.clv_adj}"
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 4 — Historical penalty: 38% win rate, -5% avg CLV → lower tier")
print(SEP)

h_bad = HistoricalStats(sample_size=30, win_rate=0.38, avg_clv=-7.0, roi=-3.0)
r_penalty = _rank(steam=82, ev=6.0, n_books=4, sharp=2, agree=0.90,
                  speed=1.5, liq=70, minutes=720, overall=h_bad)

print(f"  With penalty: score={r_penalty.score}  tier={r_penalty.tier.value}")
print(f"  Hist adj   : {r_penalty.historical_breakdown.total:+d}")
assert r_penalty.historical_breakdown.overall_adj == -5, \
    f"38% WR → -5 expected, got {r_penalty.historical_breakdown.overall_adj}"
# -7.0% CLV is below the -6.0 threshold → -3
assert r_penalty.historical_breakdown.clv_adj == -3, \
    f"-7.0% CLV → -3 expected, got {r_penalty.historical_breakdown.clv_adj}"
assert r_penalty.historical_breakdown.total == -8
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 5 — Below-minimum samples → zero adjustment")
print(SEP)

h_small = HistoricalStats(sample_size=MIN_SAMPLE_SIZE - 1, win_rate=0.70, avg_clv=5.0)
r_small = _rank(steam=75, ev=5.0, n_books=3, sharp=1, agree=0.80,
                speed=1.0, liq=60, minutes=360, overall=h_small)
assert r_small.historical_breakdown.total == 0, \
    f"< MIN_SAMPLE_SIZE → adj must be 0, got {r_small.historical_breakdown.total}"
print(f"  sample_size={h_small.sample_size} < {MIN_SAMPLE_SIZE} → adj={r_small.historical_breakdown.total}")
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 6 — Market-type history adjustment (±2)")
print(SEP)

good_mkt  = HistoricalStats(sample_size=20, win_rate=0.60)
bad_mkt   = HistoricalStats(sample_size=20, win_rate=0.40)

r_good_m = _rank(market=good_mkt)
r_bad_m  = _rank(market=bad_mkt)

print(f"  Good market adj: {r_good_m.historical_breakdown.market_adj:+d}")
print(f"  Bad  market adj: {r_bad_m.historical_breakdown.market_adj:+d}")
assert r_good_m.historical_breakdown.market_adj == 2, \
    f"60% market WR → +2 expected"
assert r_bad_m.historical_breakdown.market_adj == -2, \
    f"40% market WR → -2 expected"
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 7 — TAKE / PASS decision gates")
print(SEP)

# B-tier signal, no HIGH warnings → TAKE
r_take = _rank(steam=80, ev=6.0, n_books=4, sharp=2, agree=0.90,
               speed=1.5, liq=70, minutes=480)
print(f"  B/A tier signal: score={r_take.score}  tier={r_take.tier.value}  "
      f"decision={r_take.decision.value}")

# PASS tier → PASS regardless
r_pass_tier = _rank(steam=20, ev=0.5, n_books=0, sharp=0, agree=0.4,
                    speed=0.0, liq=5, minutes=10)
print(f"  PASS tier signal: score={r_pass_tier.score}  "
      f"tier={r_pass_tier.tier.value}  decision={r_pass_tier.decision.value}")
assert r_pass_tier.decision == RankingDecision.PASS

# HIGH severity warning (single book) blocks TAKE even on A-tier score
r_single = _rank(steam=88, ev=7.0, n_books=1, sharp=1, agree=1.0,
                 speed=3.0, liq=80, minutes=720)
print(f"  A-tier + SINGLE_BOOK: score={r_single.score}  "
      f"tier={r_single.tier.value}  decision={r_single.decision.value}")
from engine.confidence import RiskWarning
assert RiskWarning.SINGLE_BOOK in r_single.confidence_result.risk_warnings
assert r_single.decision == RankingDecision.PASS, \
    "SINGLE_BOOK (HIGH severity) must block TAKE"
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 8 — Score clamping (0 floor, 100 ceiling)")
print(SEP)

h_max = HistoricalStats(sample_size=200, win_rate=0.70, avg_clv=8.0)
r_max = _rank(steam=100, ev=15.0, n_books=10, sharp=5, agree=1.0,
              speed=10.0, liq=100, minutes=60*72, overall=h_max)
print(f"  Max inputs + great history: score={r_max.score}  tier={r_max.tier.value}")
assert r_max.score == 100
assert r_max.tier == RankingTier.S

h_min = HistoricalStats(sample_size=50, win_rate=0.30, avg_clv=-8.0)
r_min = _rank(steam=0, ev=-10.0, n_books=0, sharp=0, agree=0.0,
              speed=0.0, liq=0, minutes=None, overall=h_min)
print(f"  Zero inputs + terrible history: score={r_min.score}  tier={r_min.tier.value}")
assert r_min.score == 0
assert r_min.tier == RankingTier.PASS
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 9 — Key factors are extracted correctly")
print(SEP)

h_proven = HistoricalStats(sample_size=40, win_rate=0.62, avg_clv=2.5)
r_factors = _rank(steam=80, ev=6.0, n_books=4, sharp=2, agree=0.90,
                  speed=1.5, liq=70, minutes=720, overall=h_proven)
print(f"  Key factors: {r_factors.key_factors}")
# Should include historical factors (win rate, CLV)
assert len(r_factors.key_factors) > 0
# Should include sharp book factor (2 sharp books)
factor_text = " ".join(r_factors.key_factors).lower()
assert any(kw in factor_text for kw in ["sharp", "win rate", "clv", "steam"]), \
    f"Unexpected factors: {r_factors.key_factors}"
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 10 — Telegram block renders without error")
print(SEP)

h_ctx = HistoricalStats(sample_size=25, win_rate=0.58, avg_clv=1.8)
r_tg = _rank(steam=85, ev=6.5, n_books=4, sharp=2, agree=0.92,
             speed=2.0, liq=75, minutes=60*20, overall=h_ctx)
block = r_tg.to_telegram_block()
assert "<b>AI Decision" in block
assert str(r_tg.score) in block
print(block[:300] + ("..." if len(block) > 300 else ""))
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 11 — Backtesting: basic accuracy calculation")
print(SEP)

# 10 records: 7 TAKE (score≥75), 3 PASS (score<75)
# Among TAKE: 5 WIN, 2 LOSS → win rate = 71.4%
records = [
    _ev_record(90, "WIN",  clv=2.5),
    _ev_record(85, "WIN",  clv=1.8),
    _ev_record(80, "WIN",  clv=3.1),
    _ev_record(78, "WIN",  clv=0.5),
    _ev_record(76, "WIN",  clv=1.2),
    _ev_record(82, "LOSS", clv=-1.5),
    _ev_record(88, "LOSS", clv=-2.0),
    _ev_record(60, "WIN",  clv=0.8),   # PASS
    _ev_record(50, "LOSS", clv=-0.5),  # PASS
    _ev_record(40, "PUSH", clv=0.0),   # PASS
]

engine = BacktestEngine()
report = engine.run(records)

print(f"  total={report.total_evaluated}  take={report.total_take}  pass={report.total_pass}")
print(f"  wins={report.overall.wins}  losses={report.overall.losses}")
print(f"  win_rate={report.overall.win_rate}")

assert report.total_evaluated == 10
assert report.total_take == 7
assert report.total_pass == 3
assert report.overall.wins == 5
assert report.overall.losses == 2
wr = report.overall.win_rate
assert wr is not None and abs(wr - 5/7) < 0.001, f"Expected 5/7 ≈ 0.714, got {wr}"
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 12 — Backtesting: CLV calculation")
print(SEP)

# TAKE records have CLV: 2.5, 1.8, 3.1, 0.5, 1.2, -1.5, -2.0 → avg ≈ 0.8
avg_clv_expected = (2.5 + 1.8 + 3.1 + 0.5 + 1.2 + (-1.5) + (-2.0)) / 7
avg_clv_got = report.overall.avg_clv
print(f"  Expected avg CLV: {avg_clv_expected:.2f}%  Got: {avg_clv_got}")
assert avg_clv_got is not None and abs(avg_clv_got - avg_clv_expected) < 0.01
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 13 — Backtesting: breakdown by tier")
print(SEP)

# Tier S (≥95): none in our set
# Tier A (85-94): scores 90, 85, 88 → 3 records (2 WIN, 1 LOSS)
# Tier B (75-84): scores 80, 78, 76, 82 → 4 records (3 WIN, 1 LOSS)
tier_a = report.by_tier.get("A")
tier_b = report.by_tier.get("B")

print(f"  Tier A: n={tier_a.total if tier_a else 0}  "
      f"W={tier_a.wins if tier_a else 0}  L={tier_a.losses if tier_a else 0}")
print(f"  Tier B: n={tier_b.total if tier_b else 0}  "
      f"W={tier_b.wins if tier_b else 0}  L={tier_b.losses if tier_b else 0}")

assert tier_a is not None and tier_a.total == 3
assert tier_b is not None and tier_b.total == 4
assert tier_a.wins == 2 and tier_a.losses == 1
assert tier_b.wins == 3 and tier_b.losses == 1
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 14 — Backtesting: PENDING records excluded from win_rate")
print(SEP)

# Mix of resolved and pending
pending_records = [
    _ev_record(85, "WIN"),
    _ev_record(80, "LOSS"),
    _ev_record(90, "PENDING"),  # excluded from win rate
    _ev_record(78, None),       # excluded from win rate
]
rpt_p = run_backtest(pending_records)
ov_p  = rpt_p.overall

print(f"  total={rpt_p.total_take}  wins={ov_p.wins}  losses={ov_p.losses}")
assert rpt_p.total_take == 4                 # all ≥ 75 → TAKE
assert ov_p.wins   == 1 and ov_p.losses == 1  # only WIN/LOSS count
wr_p = ov_p.win_rate
# only 2 resolved → < MIN_SAMPLE_SIZE (5) → win_rate returns None
assert wr_p is None, f"Expected None (< 5 samples), got {wr_p}"
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 15 — Backtesting: Telegram report renders without error")
print(SEP)

tg = report.to_telegram()
assert "<b>Backtest Report</b>" in tg
assert "By Tier" in tg
assert "By Market" in tg
print(tg[:400] + "...")
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 16 — RankingResult.to_console() renders")
print(SEP)

r_console = _rank(steam=82, ev=6.0, n_books=4, sharp=2, agree=0.90,
                  speed=1.5, liq=70, minutes=480)
line = r_console.to_console()
assert "[Ranking]" in line
assert str(r_console.score) in line
print(f"  {line}")
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 17 — HistoricalStats.has_signal gating")
print(SEP)

for n, expected in [(0, False), (4, False), (5, True), (100, True)]:
    h = HistoricalStats(sample_size=n, win_rate=0.55)
    ok = h.has_signal == expected
    print(f"  sample_size={n}  has_signal={h.has_signal}  {'✓' if ok else '✗'}")
    assert ok
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  TEST 18 — Backtest: by-sport and by-market breakdowns")
print(SEP)

mixed = [
    _ev_record(85, "WIN",  sport="NFL", market_type="Spread"),
    _ev_record(80, "WIN",  sport="NFL", market_type="Moneyline"),
    _ev_record(88, "LOSS", sport="NBA", market_type="Total (O/U)"),
    _ev_record(76, "WIN",  sport="NBA", market_type="Spread"),
    _ev_record(82, "LOSS", sport="MLB", market_type="Moneyline"),
]
rpt_m = run_backtest(mixed)

print(f"  Sports:  {list(rpt_m.by_sport.keys())}")
print(f"  Markets: {list(rpt_m.by_market.keys())}")
assert "NFL" in rpt_m.by_sport
assert "NBA" in rpt_m.by_sport
assert "MLB" in rpt_m.by_sport
assert rpt_m.by_sport["NFL"].wins == 2
assert rpt_m.by_sport["NBA"].wins == 1 and rpt_m.by_sport["NBA"].losses == 1
_pass()


# ══════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  ALL 18 TESTS PASSED ✓")
print(SEP)
