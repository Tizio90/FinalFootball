"""Engine package -- pure match simulation logic."""
from .match import MatchEngine, MatchResult, MatchEvent, play_match
from .attributes import load_full_player, ca_for_family
from .lineup import (
    FORMATION_433, load_club_squad, pick_best_xi, starting_xi_strength,
)

__all__ = [
    "MatchEngine", "MatchResult", "MatchEvent", "play_match",
    "load_full_player", "ca_for_family",
    "FORMATION_433", "load_club_squad", "pick_best_xi", "starting_xi_strength",
]
