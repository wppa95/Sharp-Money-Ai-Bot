"""
engine/player_block.py — Player Reliability / Block System (Framework v3.0 Layer 11).

Manages player-level blocks that prevent alerting on unreliable props.

Block policy
─────────────
Block ONLY for true reliability issues:
  INJURY                 — injury confirmed to affect usage
  MINUTES_RESTRICTION    — official minutes cap by team / medical staff
  TENNIS_RETIREMENT      — retired from the match/tournament
  AVAILABILITY           — repeated availability problems (missing games)

Do NOT block for:
  • Normal statistical variance
  • One bad game
  • Line movement
  • App-side scratches (platform artefacts)

Block types
───────────
  TEMPORARY — expires at a fixed datetime; auto-clears when expires_at passes
  PERMANENT — requires manual removal; for chronic reliability issues

Design constraints
──────────────────
• is_blocked() is a pure function — takes a list of blocks, returns result.
• No DB calls in this module — DB layer (database.py) owns persistence.
• Blocks must have a reason_code from BLOCKABLE_REASONS.
• Non-blockable reasons are rejected by validate_reason_code().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Reason code registry ───────────────────────────────────────────────────────

#: Reason codes that may trigger a player block.
BLOCKABLE_REASONS: frozenset = frozenset({
    "INJURY",
    "MINUTES_RESTRICTION",
    "TENNIS_RETIREMENT",
    "AVAILABILITY",
})

#: Reason codes that are explicitly NOT blockable (referenced for clarity).
NON_BLOCKABLE_REASONS: frozenset = frozenset({
    "NORMAL_VARIANCE",
    "ONE_BAD_GAME",
    "LINE_MOVEMENT",
    "APP_SCRATCH",
})

#: All known reason codes (union of both sets).
ALL_REASON_CODES: frozenset = BLOCKABLE_REASONS | NON_BLOCKABLE_REASONS


# ── Block dataclass ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlayerBlock:
    """
    A player reliability block — prevents alerting on unreliable props.

    Fields
    ------
    player_key        : Canonical player key (from engine.identity.player_key).
    player_name       : Display name for Telegram output.
    sport             : Sport string (e.g. "NBA", "MLB").  An empty string ("") 
                        means the block applies across all sports for this player.
    reason_code       : One of BLOCKABLE_REASONS.
    description       : Human-readable explanation stored for audit trail.
    block_type        : "TEMPORARY" | "PERMANENT"
    expires_at        : UTC datetime when a TEMPORARY block expires.
                        None for PERMANENT blocks.
    created_at        : UTC datetime when the block was created.
    review_date       : Optional reminder date for reviewing a PERMANENT block.
    created_by        : Identifier of who created the block ("system" | user ID).
    """
    player_key:   str
    player_name:  str
    sport:        str               # "" = all sports
    reason_code:  str               # one of BLOCKABLE_REASONS
    description:  str
    block_type:   str               # "TEMPORARY" | "PERMANENT"
    expires_at:   Optional[datetime] = None
    created_at:   datetime           = field(default_factory=datetime.utcnow)
    review_date:  Optional[datetime] = None
    created_by:   str                = "system"

    def __post_init__(self) -> None:  # called after __init__ in frozen dataclass
        # Validate reason_code
        if self.reason_code not in BLOCKABLE_REASONS:
            raise ValueError(
                f"reason_code {self.reason_code!r} is not blockable. "
                f"Use one of: {sorted(BLOCKABLE_REASONS)}"
            )
        # Validate block_type
        if self.block_type not in ("TEMPORARY", "PERMANENT"):
            raise ValueError(
                f"block_type must be 'TEMPORARY' or 'PERMANENT', got {self.block_type!r}"
            )
        # TEMPORARY blocks must have an expiry
        if self.block_type == "TEMPORARY" and self.expires_at is None:
            raise ValueError("TEMPORARY blocks must have an expires_at datetime.")

    @property
    def is_active(self) -> bool:
        """
        True when the block is currently in effect.

        A PERMANENT block is always active.
        A TEMPORARY block is active until expires_at passes (in UTC).
        """
        if self.block_type == "PERMANENT":
            return True
        if self.expires_at is None:
            return False
        return datetime.utcnow() < self.expires_at

    @property
    def reason_label(self) -> str:
        return {
            "INJURY":               "Injury",
            "MINUTES_RESTRICTION":  "Minutes restriction",
            "TENNIS_RETIREMENT":    "Tournament retirement",
            "AVAILABILITY":         "Repeated availability issues",
        }.get(self.reason_code, self.reason_code.replace("_", " ").title())

    def to_dict(self) -> dict:
        return {
            "player_key":   self.player_key,
            "player_name":  self.player_name,
            "sport":        self.sport,
            "reason_code":  self.reason_code,
            "reason_label": self.reason_label,
            "description":  self.description,
            "block_type":   self.block_type,
            "expires_at":   self.expires_at.isoformat() if self.expires_at else None,
            "created_at":   self.created_at.isoformat(),
            "review_date":  self.review_date.isoformat() if self.review_date else None,
            "is_active":    self.is_active,
        }

    def to_telegram(self) -> str:
        """Format the block for Telegram HTML display."""
        import html
        exp = (
            f"Expires: {self.expires_at.strftime('%Y-%m-%d %H:%M UTC')}"
            if self.expires_at else "Permanent"
        )
        review = (
            f"  Review: {self.review_date.strftime('%Y-%m-%d')}"
            if self.review_date else ""
        )
        sport_str = self.sport or "ALL SPORTS"
        return (
            f"🚫 <b>{html.escape(self.player_name)}</b> "
            f"[{html.escape(sport_str)}]\n"
            f"  Reason: {html.escape(self.reason_label)}\n"
            f"  {html.escape(exp)}{review}\n"
            f"  <i>{html.escape(self.description[:120])}</i>"
        )


# ── Pure block-checking functions ─────────────────────────────────────────────

def is_blocked(
    player_key: str,
    sport:      str,
    blocks:     list[PlayerBlock],
) -> Optional[PlayerBlock]:
    """
    Check whether a player is currently blocked for a given sport.

    Returns the first active block that matches, or None if the player
    is clear to alert.

    A block matches when:
      - block.player_key == player_key, AND
      - block.sport == "" (all sports) OR block.sport == sport, AND
      - block.is_active is True.

    Parameters
    ----------
    player_key : Canonical player key (from engine.identity.player_key).
    sport      : Sport string (e.g. "NBA").
    blocks     : List of PlayerBlock objects to check against.
    """
    sport_upper = (sport or "").upper()
    for blk in blocks:
        if blk.player_key != player_key:
            continue
        if blk.sport and blk.sport.upper() != sport_upper:
            continue
        if blk.is_active:
            return blk
    return None


def filter_blocked(
    candidates: list,
    blocks:     list[PlayerBlock],
) -> tuple[list, list[tuple]]:
    """
    Separate a list of Candidates into (allowed, blocked_pairs).

    Returns
    -------
    allowed       : Candidates that are not blocked.
    blocked_pairs : List of (Candidate, PlayerBlock) for blocked candidates.
    """
    allowed = []
    blocked_pairs: list[tuple] = []
    for c in candidates:
        blk = is_blocked(c.player_key, c.sport, blocks)
        if blk:
            blocked_pairs.append((c, blk))
        else:
            allowed.append(c)
    return allowed, blocked_pairs


def validate_reason_code(code: str) -> bool:
    """Return True if *code* is a valid blockable reason code."""
    return code in BLOCKABLE_REASONS


def reason_code_explanation(code: str) -> str:
    """Return a human-readable description of a reason code."""
    _EXPLANATIONS = {
        "INJURY": (
            "Player has a confirmed injury that affects their usage or "
            "ability to participate in the game."
        ),
        "MINUTES_RESTRICTION": (
            "Player is on an official minutes cap enforced by the team "
            "or medical staff — prop projections are unreliable."
        ),
        "TENNIS_RETIREMENT": (
            "Player has retired from the current match or tournament — "
            "no further stats will accumulate."
        ),
        "AVAILABILITY": (
            "Player has a documented pattern of missing games or "
            "appearing on the injury/availability report regularly."
        ),
        "NORMAL_VARIANCE": (
            "NOT blockable — one bad game or statistical variation is "
            "expected and does not indicate a reliability issue."
        ),
        "ONE_BAD_GAME": "NOT blockable — see NORMAL_VARIANCE.",
        "LINE_MOVEMENT": (
            "NOT blockable — market line movement is normal and does "
            "not indicate a player reliability issue."
        ),
        "APP_SCRATCH": (
            "NOT blockable — platform-side scratches (removed props on "
            "the fantasy app) are artefacts, not player issues."
        ),
    }
    return _EXPLANATIONS.get(code, f"Unknown reason code: {code!r}")


def blocks_summary_telegram(blocks: list[PlayerBlock]) -> str:
    """Return a Telegram HTML summary of all active blocks."""
    active = [b for b in blocks if b.is_active]
    if not active:
        return "✅ <b>No active player blocks.</b>"

    lines = [f"🚫 <b>Active Player Blocks ({len(active)})</b>", ""]
    for blk in sorted(active, key=lambda b: b.player_name):
        sport = blk.sport or "ALL"
        exp = (
            blk.expires_at.strftime("%m/%d") if blk.expires_at else "Permanent"
        )
        import html
        lines.append(
            f"  • <b>{html.escape(blk.player_name)}</b> [{sport}]"
            f" — {blk.reason_label} ({exp})"
        )
    return "\n".join(lines)
