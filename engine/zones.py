"""
Pitch zone model for Phase 4A spatial engine.

Divides the pitch into a 3×3 grid:
  - Rows: defensive third (0), middle third (1), attacking third (2)
  - Cols: left channel (0), center channel (1), right channel (2)

Each zone has a transition probability table keyed by (formation, zone, instructions).
The model is O(1) per phase — no full spatial physics, just weighted transitions.
"""
from __future__ import annotations

from typing import Any
from dataclasses import dataclass


# Zone indices: (row, col) where row 0=defensive, 1=middle, 2=attacking
# col 0=left, 1=center, 2=right
DEF_THIRD, MID_THIRD, ATT_THIRD = 0, 1, 2
LEFT, CENTER, RIGHT = 0, 1, 2

# All 9 zones
ZONES = [(r, c) for r in range(3) for c in range(3)]
ZONE_NAMES = {
    (0, 0): "Defensive Left", (0, 1): "Defensive Center", (0, 2): "Defensive Right",
    (1, 0): "Middle Left", (1, 1): "Middle Center", (1, 2): "Middle Right",
    (2, 0): "Attacking Left", (2, 1): "Attacking Center", (2, 2): "Attacking Right",
}


@dataclass
class ZoneTransition:
    """A single zone transition during a phase."""
    from_zone: tuple[int, int]
    to_zone: tuple[int, int]
    success: bool
    ball_lost_to: str | None = None  # 'home' or 'away' if possession changed


def get_formation_zone_strength(formation_slots: list[dict], family_weights: dict[str, float]) -> dict[tuple[int, int], float]:
    """Compute attacking strength per zone based on which players occupy nearby slots.

    This is a simplified model: each formation slot maps to a zone, and the
    zone's strength is the average CA of players in slots that map to it.
    """
    # Map formation slots to zones (simplified — based on slot family + side)
    slot_to_zone = {
        ("GK", "C"): (0, 1),
        ("DEF", "L"): (0, 0), ("DEF", "C"): (0, 1), ("DEF", "R"): (0, 2),
        ("MID", "L"): (1, 0), ("MID", "C"): (1, 1), ("MID", "R"): (1, 2),
        ("ATT", "L"): (2, 0), ("ATT", "C"): (2, 1), ("ATT", "R"): (2, 2),
    }

    zone_strengths: dict[tuple[int, int], list[float]] = {z: [] for z in ZONES}

    for slot in formation_slots:
        family = slot.get("family", "MID")
        side = slot.get("side", "C")
        ca = slot.get("score", 50) or 50
        zone_key = (family, side)
        zone = slot_to_zone.get(zone_key, (1, 1))
        zone_strengths[zone].append(ca)

    # average
    result = {}
    for zone, vals in zone_strengths.items():
        result[zone] = sum(vals) / len(vals) if vals else 40.0
    return result


def compute_transition_probabilities(
    current_zone: tuple[int, int],
    att_strength: dict[tuple[int, int], float],
    def_strength: dict[tuple[int, int], float],
    instructions: dict[str, str],
) -> dict[tuple[int, int], float]:
    """Compute probabilities of transitioning to each zone from current_zone.

    Returns a dict {zone: probability} summing to ~1.0.
    The model considers:
      - Forward progression bias (teams try to move toward attacking third)
      - Channel width (wide instructions favor L/R channels)
      - Zone strength differential (stronger zone = more likely to progress)
      - Ball loss probability (defensive pressure in target zone)
    """
    r, c = current_zone
    probs: dict[tuple[int, int], float] = {}

    # Width instruction affects channel preference
    width = instructions.get("width", "standard")
    channel_mult = {"narrow": {0: 0.7, 1: 1.4, 2: 0.7},
                    "standard": {0: 1.0, 1: 1.0, 2: 1.0},
                    "wide": {0: 1.3, 1: 0.8, 2: 1.3}}[width]

    # Tempo affects forward bias
    tempo = instructions.get("tempo", "standard")
    forward_bias = {"slow": 0.6, "standard": 1.0, "fast": 1.3}[tempo]

    # Possible next zones: same, forward, sideways, backward
    for nr, nc in ZONES:
        # base probability
        dr = nr - r  # forward = positive
        dc = nc - c
        prob = 0.0

        if dr == 0 and dc == 0:
            # stay in same zone
            prob = 0.15
        elif dr > 0:
            # forward progression
            prob = 0.35 * forward_bias
            # stronger attacking zone = more likely to progress there
            zone_str = att_strength.get((nr, nc), 50)
            prob *= (zone_str / 50.0)
        elif dr < 0:
            # backward (retaining possession)
            prob = 0.05
        else:
            # sideways
            prob = 0.10

        # apply channel multiplier
        prob *= channel_mult.get(nc, 1.0)

        # defensive pressure reduces probability of keeping possession there
        def_press = def_strength.get((nr, nc), 50)
        prob *= (1.0 - (def_press - 50) / 200.0)  # ±25% adjustment

        probs[(nr, nc)] = max(0.01, prob)

    # normalize
    total = sum(probs.values())
    if total > 0:
        probs = {z: p / total for z, p in probs.items()}
    return probs


def get_shot_zone_bonus(zone: tuple[int, int]) -> float:
    """Bonus to shot quality based on zone (attacking center = best).
    Phase 4A tuning: reduced bonuses to keep goals in 1.8-2.5 band."""
    r, c = zone
    if r == ATT_THIRD and c == CENTER:
        return 1.15  # central attacking = best shooting position
    elif r == ATT_THIRD:
        return 1.05  # wide attacking = good but angle is harder
    elif r == MID_THIRD:
        return 0.85  # long shot
    else:
        return 0.5  # defensive third = basically no shot


def get_xg_for_shot(zone: tuple[int, int], shot_quality: float, pressure: float) -> float:
    """Compute expected goals (xG) for a shot.

    xG = base * zone_bonus * quality_factor * (1 - pressure_factor)
    Returns a value 0.0 - 0.95.
    """
    base = 0.10  # 10% base conversion
    zone_bonus = get_shot_zone_bonus(zone)
    quality_factor = max(0.3, min(2.0, shot_quality / 50.0))
    pressure_factor = min(0.5, pressure / 100.0)

    xg = base * zone_bonus * quality_factor * (1.0 - pressure_factor * 0.5)
    return max(0.01, min(0.95, xg))
