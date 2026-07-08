"""
Tactical AI for Phase 4A: manager personalities + in-match adaptation.

Each non-user-controlled club has a `manager_profile` that sets default
mentality, team instructions, and preferred formation. The in-match
adaptation loop evaluates the scoreline every ~10 minutes and adjusts
mentality + makes substitutions.
"""
from __future__ import annotations

import random
from typing import Any


# ===========================================================================
# Manager personality profiles
# ===========================================================================

MANAGER_PROFILES = {
    "possession": {
        "label": "Possession",
        "default_mentality": "balanced",
        "default_instructions": {
            "pressing": "standard",
            "tempo": "slow",
            "width": "standard",
            "defensive_line": "high",
        },
        "preferred_formations": ["4-3-3", "4-2-3-1"],
        "adaptation_style": "patient",  # doesn't panic when losing
    },
    "counter_attack": {
        "label": "Counter-Attack",
        "default_mentality": "defensive",
        "default_instructions": {
            "pressing": "low",
            "tempo": "fast",
            "width": "wide",
            "defensive_line": "deep",
        },
        "preferred_formations": ["4-4-2", "5-4-1"],
        "adaptation_style": "opportunistic",  # stays defensive even when losing slightly
    },
    "high_press": {
        "label": "High Press",
        "default_mentality": "attacking",
        "default_instructions": {
            "pressing": "high",
            "tempo": "fast",
            "width": "standard",
            "defensive_line": "high",
        },
        "preferred_formations": ["4-3-3", "3-5-2"],
        "adaptation_style": "aggressive",  # doubles down when losing
    },
    "defensive_block": {
        "label": "Defensive Block",
        "default_mentality": "defensive",
        "default_instructions": {
            "pressing": "low",
            "tempo": "slow",
            "width": "narrow",
            "defensive_line": "deep",
        },
        "preferred_formations": ["5-4-1", "4-4-2"],
        "adaptation_style": "cautious",  # rarely goes attacking even when losing
    },
}


def derive_manager_profile(club_name: str, division_quality: float) -> str:
    """Derive a manager profile deterministically from club name + division quality.

    Stronger clubs bias toward possession/high_press.
    Weaker clubs bias toward defensive_block/counter_attack.
    """
    rng = random.Random(hash(club_name))
    if division_quality >= 70:
        # strong club: 40% possession, 30% high_press, 20% counter, 10% defensive
        profiles = ["possession"] * 4 + ["high_press"] * 3 + ["counter_attack"] * 2 + ["defensive_block"]
    elif division_quality >= 55:
        # mid club: balanced
        profiles = ["possession"] * 2 + ["high_press"] * 2 + ["counter_attack"] * 3 + ["defensive_block"] * 2
    else:
        # weak club: defensive + counter
        profiles = ["possession"] + ["high_press"] + ["counter_attack"] * 4 + ["defensive_block"] * 4
    return rng.choice(profiles)


def get_profile_defaults(profile: str) -> dict:
    """Return default settings for a manager profile."""
    p = MANAGER_PROFILES.get(profile, MANAGER_PROFILES["possession"])
    return {
        "mentality": p["default_mentality"],
        "instructions": p["default_instructions"].copy(),
        "formation": p["preferred_formations"][0],
        "adaptation_style": p["adaptation_style"],
    }


# ===========================================================================
# In-match tactical adaptation
# ===========================================================================

MENTALITY_ORDER = ["defensive", "balanced", "attacking"]


def adapt_tactics(
    current_mentality: str,
    score_for: int,
    score_against: int,
    minute: int,
    profile: str = "possession",
    yellow_card_uids: set[int] | None = None,
) -> dict:
    """Evaluate whether to adapt mentality or make substitutions.

    Called every ~10 minutes of sim time. Returns a dict of changes:
    {
        "new_mentality": str | None,
        "substitution": {"type": "attacking"|"defensive", "reason": str} | None,
        "yellow_caution_uids": list[int],  # players to reduce Agg for
        "event_text": str | None,  # match event to log
    }
    """
    result = {
        "new_mentality": None,
        "substitution": None,
        "yellow_caution_uids": [],
        "event_text": None,
    }

    goal_diff = score_for - score_against
    style = MANAGER_PROFILES.get(profile, {}).get("adaptation_style", "patient")

    # Determine adaptation based on scoreline + minute + style
    if minute >= 45 and goal_diff <= -1:
        # Losing at halftime or later
        if style in ("aggressive", "patient"):
            # push for an equalizer
            idx = MENTALITY_ORDER.index(current_mentality)
            if idx < 2:
                result["new_mentality"] = MENTALITY_ORDER[idx + 1]
                result["event_text"] = f"{minute}'  Manager switches to {result['new_mentality']} mentality — chasing the game"
        elif style == "opportunistic" and goal_diff <= -2:
            # only react if losing by 2+
            idx = MENTALITY_ORDER.index(current_mentality)
            if idx < 2:
                result["new_mentality"] = MENTALITY_ORDER[idx + 1]
                result["event_text"] = f"{minute}'  Manager switches to {result['new_mentality']} mentality"
        # defensive_block: rarely changes

    elif minute >= 75 and goal_diff >= 1:
        # Winning late — shut up shop
        if style in ("cautious", "opportunistic", "patient"):
            idx = MENTALITY_ORDER.index(current_mentality)
            if idx > 0:
                result["new_mentality"] = MENTALITY_ORDER[idx - 1]
                result["event_text"] = f"{minute}'  Manager switches to {result['new_mentality']} mentality — protecting the lead"

    # Substitution suggestions
    if minute >= 60 and minute <= 75:
        if goal_diff <= -1 and style != "cautious":
            # losing → attacking sub
            result["substitution"] = {
                "type": "attacking",
                "reason": "chasing the game",
            }
        elif goal_diff >= 1 and minute >= 75 and style != "aggressive":
            # winning → defensive sub
            result["substitution"] = {
                "type": "defensive",
                "reason": "protecting the lead",
            }

    # Yellow card caution: reduce Agg for booked players
    if yellow_card_uids:
        result["yellow_caution_uids"] = list(yellow_card_uids)

    return result
