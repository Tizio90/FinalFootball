"""
Match engine: turns two lineups into a scoreline + event log via a
possession/phase model with opposed-attribute duels.

Design (Phase 2):
  - Match is simulated as ~60 discrete "phases" (attacking possessions),
    distributed across the 90 minutes proportional to each side's midfield
    share + attacking intent.
  - Each phase flows through a chain of duels:
      build-up pass (MID vs MID pressure)
        -> if lost, transition ends (no event, foul, or counter-attack chance)
        -> if won, progress to final-third duel (ATT vs DEF)
            -> if lost, defender clears (no event, foul, or counter chance)
            -> if won, shot opportunity
                -> shot quality (shooter Fin/Tec/Cmp + pressure)
                    -> shot on target? (Tec/Cmp/Dec)
                    -> saved? (GK Ref/1v1/Pos + shot quality)
                    -> GOAL or save or miss
  - Every event is timestamped with a match minute derived from phase index.
  - Scoreline is the COUNT of goal events -- never computed separately.
  - Fatigue: encapsulated in `_fatigue_multiplier()` (single source of truth).
  - Form: per-player per-match roll (Cons/Temp/Pres/Imp M).
  - Subs: at 60/70/75min, replace most-fatigued starter at each family.
  - Injuries: tiny per-phase probability driven by `Inj Pr` + opponent Agg.
  - Fouls: lost-by-wide-margin tackles can be fouls; cards driven by Agg/Dirt
    vs. per-match referee strictness. Build-up fouls just stop play; final-
    third fouls yield a free kick (no chaining back to corners).
  - Set pieces: corners (25% chance of a header shot, no further corner);
    free kicks (Fre-driven, no chaining); penalties (Pen vs 1v1, 75% base).
  - Events sorted by minute at the end; jitter ±1.5 minutes.
  - Mentality (defensive/balanced/attacking) modifies phase count, shot
    threshold, and defensive line height.

Pure logic -- no I/O.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MatchEvent:
    minute: int
    type: str        # 'goal','shot','save','miss','chance','card','sub','info',
                     # 'foul','corner','free_kick','penalty','injury','offside'
    side: str        # 'home' / 'away' / 'neutral'
    text: str
    player_uids: list[int] = field(default_factory=list)
    player_names: list[str] = field(default_factory=list)


@dataclass
class MatchStats:
    possession: dict[str, float] = field(default_factory=lambda: {"home": 0.5, "away": 0.5})
    shots: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    shots_on_target: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    shots_off_target: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    corners: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    free_kicks: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    penalties: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    fouls: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    offsides: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    passes_attempted: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    passes_completed: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    cards: dict[str, dict[str, int]] = field(
        default_factory=lambda: {"home": {"y": 0, "r": 0}, "away": {"y": 0, "r": 0}})
    # Phase 4A: xG tracking
    xg: dict[str, float] = field(default_factory=lambda: {"home": 0.0, "away": 0.0})


@dataclass
class MatchResult:
    home_club: str
    away_club: str
    home_score: int
    away_score: int
    events: list[MatchEvent]
    stats: MatchStats
    home_xi: list[dict[str, Any]]
    away_xi: list[dict[str, Any]]
    home_bench: list[dict[str, Any]]
    away_bench: list[dict[str, Any]]
    seed: int
    motm_uid: int | None = None
    motm_name: str | None = None
    player_ratings: dict[int, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Engine config
# ---------------------------------------------------------------------------
# Tuning knobs (see prompt_2.md §0 "Engine tuning observations"):
#   PHASES_PER_MATCH      : 60 -> ~1.5-2.5 goals/match. Phase 1 used 80;
#                           Phase 2 uses 60 to leave headroom for the added
#                           set-piece + foul shot opportunities.
#   on_target_threshold   : 42.5 -> shots above this test the keeper (0-100 scale).
#   miss_threshold        : 35.0 -> shots below this are blocked/wide.
#   keeper +5.0           : GK positional advantage on saves.
#   pressure * 0.30       : how much defensive pressure reduces shot quality.
#   COUNTER_CHANCE        : 0.10 -> 10% of lost possessions become counters.
PHASES_PER_MATCH = 48
MATCH_MINUTES = 92

# Fatigue
FATIGUE_DECAY_PER_PHASE = 0.004
FATIGUE_RECOVERY_HIGH_STA = 0.5
FATIGUE_MIN_MULTIPLIER = 0.55

# Counter-attacks
COUNTER_CHANCE = 0.10

# Fouls / cards — Phase 3 tuning fix: cards were way too rare
# (was 0.76 yellows/match vs real ~3.5; raised bases + multipliers)
FOUL_MARGIN_THRESHOLD = 15.0   # 0-100 scale
YELLOW_CARD_BASE = 0.22
RED_CARD_BASE = 0.012

# Injuries — Phase 3 tuning fix: were too rare (0.18/match vs real ~0.5)
INJURY_BASE_PROB = 0.00035
INJURY_OPP_AGG_MULT = 0.4

# Set pieces
CORNER_SHOT_CHANCE = 0.25
PENALTY_CONVERSION_BASE = 0.75

# Mentality
MENTALITY_PHASE_MOD = {"defensive": 0.85, "balanced": 1.0, "attacking": 1.15}
MENTALITY_SHOT_THRESHOLD_MOD = {"defensive": +2.5, "balanced": 0.0, "attacking": -2.5}  # 0-100 scale
MENTALITY_LINE_HEIGHT = {"defensive": 0.7, "balanced": 1.0, "attacking": 1.3}

# Referee
REFEREE_STRICTNESS_MIN = 5
REFEREE_STRICTNESS_MAX = 18


class MatchEngine:
    def __init__(self, home_club: str, away_club: str,
                 home_xi: list[dict[str, Any]],
                 away_xi: list[dict[str, Any]],
                 home_bench: list[dict[str, Any]] = None,
                 away_bench: list[dict[str, Any]] = None,
                 seed: Optional[int] = None,
                 home_advantage: float = 1.10,
                 home_mentality: str = "balanced",
                 away_mentality: str = "balanced",
                 home_instructions: dict | None = None,
                 away_instructions: dict | None = None,
                 home_profile: str = "possession",
                 away_profile: str = "possession"):
        self.home_club = home_club
        self.away_club = away_club
        # Deep-copy XI and bench so simulations never mutate the caller's
        # lineup dicts.  Without this, running 500 matches with the same XI
        # reference would propagate substitutions across matches.
        self.home_xi = copy.deepcopy(home_xi)
        self.away_xi = copy.deepcopy(away_xi)
        self.home_bench = copy.deepcopy(home_bench or [])
        self.away_bench = copy.deepcopy(away_bench or [])
        self.home_advantage = home_advantage
        self.home_mentality = home_mentality
        self.away_mentality = away_mentality
        # Phase 4A: team instructions + manager profiles
        self.home_instructions = home_instructions or {}
        self.away_instructions = away_instructions or {}
        self.home_profile = home_profile
        self.away_profile = away_profile
        self.rng = random.Random(seed)

        self.form_roll: dict[int, float] = {}
        self.fatigue: dict[int, float] = {}
        self.used_bench_home: set[int] = set()
        self.used_bench_away: set[int] = set()
        self.injured_home: set[int] = set()
        self.injured_away: set[int] = set()
        self.player_actions: dict[int, dict[str, int]] = {}

        self.events: list[MatchEvent] = []
        self.stats = MatchStats()
        self.home_score = 0
        self.away_score = 0

        self.referee_strictness = self.rng.uniform(
            REFEREE_STRICTNESS_MIN, REFEREE_STRICTNESS_MAX)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _init_player_state(self) -> None:
        for p in self.home_xi + self.away_xi + self.home_bench + self.away_bench:
            if p is None or p.get("uid") is None:
                continue
            uid = p["uid"]
            attrs = p.get("attrs", {})
            self.form_roll[uid] = self._form_roll(attrs)
            self.fatigue[uid] = 0.0
            self.player_actions[uid] = {
                "goals": 0, "assists": 0, "shots": 0, "shots_on_target": 0,
                "passes_attempted": 0, "passes_completed": 0,
                "tackles_won": 0, "tackles_lost": 0, "saves": 0,
                "fouls_committed": 0, "fouls_won": 0,
            }

    def _form_roll(self, attrs: dict[str, Any]) -> float:
        cons  = attrs.get("Cons")  or 50
        temp  = attrs.get("Temp")  or 50
        pres  = attrs.get("Pres")  or 50
        imp_m = attrs.get("Imp M") or 50
        stability = (cons + temp + pres + imp_m) / 4.0  # 0-100 scale
        sigma = 0.20 - (stability / 100.0) * 0.15
        sigma = max(0.03, sigma)
        mult = self.rng.gauss(1.0, sigma)
        return max(0.70, min(1.30, mult))

    def _fatigue_multiplier(self, p: dict[str, Any]) -> float:
        """Single source of truth for fatigue effect. Returns 0..1 multiplier.

        A player at fatigue=0 returns 1.0; the multiplier decays linearly
        with phase count, modulated by Stamina (high-Sta players recover
        more of the decay). Never drops below FATIGUE_MIN_MULTIPLIER.

        Examples (PHASES_PER_MATCH=80, FATIGUE_DECAY_PER_PHASE=0.004,
        FATIGUE_RECOVERY_HIGH_STA=0.5):
          Sta=1  after 80 phases: 1 - 0.004*80*(1-0.025) = 1 - 0.31  = 0.69
          Sta=10 after 80 phases: 1 - 0.004*80*(1-0.25)  = 1 - 0.24  = 0.76
          Sta=20 after 80 phases: 1 - 0.004*80*(1-0.50)  = 1 - 0.16  = 0.84
        """
        uid = p.get("uid")
        if uid is None:
            return 1.0
        attrs = p.get("attrs", {})
        fat = self.fatigue.get(uid, 0.0)
        if fat <= 0:
            return 1.0
        sta = attrs.get("Sta") or 50
        recovery = (sta / 100.0) * FATIGUE_RECOVERY_HIGH_STA
        decay = FATIGUE_DECAY_PER_PHASE * fat * (1.0 - recovery)
        # decay is already a fraction (e.g. 0.16 for Sta=20 after 80 phases);
        # subtract directly from 1.0 (no magic *100 multiplier -- that was a
        # Phase 1 bug that clamped everyone to FATIGUE_MIN_MULTIPLIER).
        mult = 1.0 - decay
        return max(FATIGUE_MIN_MULTIPLIER, mult)

    def _eff(self, p: dict[str, Any], attr: str) -> float:
        """Effective attribute value, accounting for form + fatigue."""
        attrs = p.get("attrs", {})
        base = attrs.get(attr)
        if base is None:
            return 0.0
        uid = p.get("uid")
        form = self.form_roll.get(uid, 1.0) if uid else 1.0
        physical = {"Acc", "Agi", "Pac", "Sta", "Str", "Jum", "Bal", "NatFit"}
        if attr in physical:
            return base * form * self._fatigue_multiplier(p)
        return base * form

    # ------------------------------------------------------------------
    # Team-level helpers
    # ------------------------------------------------------------------
    def _team_strength(self, xi: list[dict[str, Any]]) -> dict[str, float]:
        out = {"GK": [], "DEF": [], "MID": [], "ATT": []}
        for s in xi:
            if s.get("uid") is None:
                continue
            fam = s["family"]
            ca = s.get("ca", {}).get(fam, 0.0) or 0.0
            form = self.form_roll.get(s["uid"], 1.0)
            ca = ca * form * self._fatigue_multiplier(s)
            out[fam].append(ca)
        return {fam: (sum(v) / len(v)) if v else 0.0 for fam, v in out.items()}

    def _possession_split(self, home_str: dict[str, float],
                          away_str: dict[str, float]) -> tuple[float, float]:
        """Possession split driven by midfield + mentality + home edge.

        NOTE: Phase 2 originally added a 'losing side attacks more' modifier
        here, but it created a bimodal score distribution (most matches 0-0,
        some matches 6+ goals) because the +10% midfield boost amplified
        comebacks into runaway scorelines. Removed for stability.
        """
        h_mid = home_str["MID"] * self.home_advantage * MENTALITY_PHASE_MOD[self.home_mentality]
        a_mid = away_str["MID"] * MENTALITY_PHASE_MOD[self.away_mentality]
        total = h_mid + a_mid
        if total <= 0:
            return 0.5, 0.5
        h = h_mid / total
        return h, 1.0 - h

    # ------------------------------------------------------------------
    # Phase simulation
    # ------------------------------------------------------------------
    def simulate(self) -> MatchResult:
        self._init_player_state()

        self._emit(0, "info", "neutral",
                   f"Kick-off: {self.home_club} vs {self.away_club}")

        home_str = self._team_strength(self.home_xi)
        away_str = self._team_strength(self.away_xi)
        h_pos, a_pos = self._possession_split(home_str, away_str)
        self.stats.possession = {"home": round(h_pos, 3), "away": round(a_pos, 3)}

        # Phase 4A: compute zone strengths for both teams
        from engine.zones import get_formation_zone_strength, compute_transition_probabilities, get_xg_for_shot, ATT_THIRD
        home_zone_str = get_formation_zone_strength(self.home_xi, {})
        away_zone_str = get_formation_zone_strength(self.away_xi, {})

        sides: list[str] = []
        for _ in range(PHASES_PER_MATCH):
            sides.append("home" if self.rng.random() < h_pos else "away")

        # Phase 4A: track current zone for each side (start in defensive third)
        home_zone = (0, 1)  # defensive center
        away_zone = (0, 1)

        # Phase 4A: track yellow-carded players for AI caution
        home_yellows: set[int] = set()
        away_yellows: set[int] = set()

        for i, side in enumerate(sides):
            minute = self._phase_to_minute(i, PHASES_PER_MATCH)
            self._tick_fatigue()

            # Phase 4A: AI tactical adaptation every ~10 minutes
            if minute > 0 and minute % 10 == 0:
                self._adapt_tactics(minute, home_yellows, away_yellows)

            if minute in (60, 70, 75):
                self._maybe_substitute(minute)
            self._maybe_injury(side, minute)

            # Phase 4A: zone-based phase simulation
            if side == "home":
                current_zone = home_zone
                att_str = home_zone_str
                def_str = away_zone_str
                instructions = self.home_instructions
            else:
                current_zone = away_zone
                att_str = away_zone_str
                def_str = home_zone_str
                instructions = self.away_instructions

            # compute zone transition
            probs = compute_transition_probabilities(current_zone, att_str, def_str, instructions)
            zones = list(probs.keys())
            weights = list(probs.values())
            new_zone = self.rng.choices(zones, weights=weights, k=1)[0]

            # update zone for this side
            if side == "home":
                home_zone = new_zone
            else:
                away_zone = new_zone

            # simulate the phase with zone context
            self._simulate_phase(side, minute, new_zone)

        self._emit(90, "info", "neutral",
                   f"Full time: {self.home_club} {self.home_score} - "
                   f"{self.away_score} {self.away_club}")

        # §0.10: sort events by minute (stable) so equal-minute events keep order
        self.events.sort(key=lambda e: e.minute)

        ratings = self._compute_player_ratings()
        motm_uid, motm_name = self._pick_motm(ratings)

        return MatchResult(
            home_club=self.home_club, away_club=self.away_club,
            home_score=self.home_score, away_score=self.away_score,
            events=self.events, stats=self.stats,
            home_xi=self.home_xi, away_xi=self.away_xi,
            home_bench=self.home_bench, away_bench=self.away_bench,
            seed=self.rng.randrange(0, 2**31),
            motm_uid=motm_uid, motm_name=motm_name,
            player_ratings=ratings,
        )

    def _phase_to_minute(self, idx: int, total: int) -> int:
        """§0.10: wider jitter (±1.5, was ±0.4) so events don't cluster."""
        base = (idx / total) * 90
        jitter = self.rng.uniform(-1.5, 1.5)
        m = int(round(base + jitter))
        return max(0, min(90, m))

    def _tick_fatigue(self) -> None:
        for p in self.home_xi + self.away_xi:
            uid = p.get("uid")
            if uid is None:
                continue
            self.fatigue[uid] = self.fatigue.get(uid, 0.0) + 1.0

    # ------------------------------------------------------------------
    # Phase 4A: AI tactical adaptation
    # ------------------------------------------------------------------
    def _adapt_tactics(self, minute: int, home_yellows: set, away_yellows: set) -> None:
        """Evaluate AI tactical adaptation for both sides every ~10 minutes."""
        from engine.tactics_ai import adapt_tactics

        # Home side adaptation (if not user-controlled — user controls via UI)
        # For now, both sides adapt (simplified — no user/AI distinction in engine)
        home_result = adapt_tactics(
            self.home_mentality, self.home_score, self.away_score,
            minute, self.home_profile, home_yellows,
        )
        if home_result["new_mentality"] and home_result["new_mentality"] != self.home_mentality:
            self.home_mentality = home_result["new_mentality"]
            if home_result["event_text"]:
                self._emit(minute, "info", "home", home_result["event_text"])

        # Away side adaptation
        away_result = adapt_tactics(
            self.away_mentality, self.away_score, self.home_score,
            minute, self.away_profile, away_yellows,
        )
        if away_result["new_mentality"] and away_result["new_mentality"] != self.away_mentality:
            self.away_mentality = away_result["new_mentality"]
            if away_result["event_text"]:
                self._emit(minute, "info", "away", away_result["event_text"])

    # ------------------------------------------------------------------
    # Phase chains
    # ------------------------------------------------------------------
    def _simulate_phase(self, attacker: str, minute: int,
                        zone: tuple[int, int] = (1, 1)) -> None:
        att_xi = self.home_xi if attacker == "home" else self.away_xi
        def_xi = self.away_xi if attacker == "home" else self.home_xi
        def_side = "away" if attacker == "home" else "home"

        # ---- Step 1: build-up (MID vs MID pressure) ----
        att_mids = [s for s in att_xi if s.get("family") == "MID" and s.get("uid")]
        def_mids = [s for s in def_xi if s.get("family") == "MID" and s.get("uid")]
        if not att_mids or not def_mids:
            return

        passer = self.rng.choice(att_mids)
        presser = self.rng.choice(def_mids)

        pass_score = (
            self._eff(passer, "Pas") * 0.30 +
            self._eff(passer, "Tec") * 0.20 +
            self._eff(passer, "Vis") * 0.15 +
            self._eff(passer, "Cmp") * 0.15 +
            self._eff(passer, "Dec") * 0.20
        )
        press_score = (
            self._eff(presser, "Ant") * 0.30 +
            self._eff(presser, "Pos") * 0.25 +
            self._eff(presser, "Tck") * 0.20 +
            self._eff(presser, "Agg") * 0.10 +
            self._eff(presser, "Wor") * 0.15
        )

        pass_roll = pass_score + self.rng.gauss(0, 10.0)
        press_roll = press_score + self.rng.gauss(0, 10.0)

        # track pass attempt
        self.player_actions[passer["uid"]]["passes_attempted"] += 1
        self.stats.passes_attempted[attacker] += 1

        if pass_roll < press_roll:
            # pass lost -- maybe a foul, else counter
            margin = press_roll - pass_roll
            if self._maybe_foul(presser, passer, margin, minute, def_side):
                # build-up fouls just stop play (no free kick shot from midfield)
                return
            self._maybe_counter(attacker, minute, presser)
            return

        # pass completed
        self.player_actions[passer["uid"]]["passes_completed"] += 1
        self.stats.passes_completed[attacker] += 1

        # ---- Step 2: final-third progression (ATT vs DEF) ----
        att_atts = [s for s in att_xi if s.get("family") == "ATT" and s.get("uid")]
        def_defs = [s for s in def_xi if s.get("family") == "DEF" and s.get("uid")]
        if not att_atts or not def_defs:
            return

        attacker_p = self.rng.choice(att_atts)
        marker = self.rng.choice(def_defs)

        att_score = (
            self._eff(attacker_p, "Dri") * 0.25 +
            self._eff(attacker_p, "Agi") * 0.15 +
            self._eff(attacker_p, "Fla") * 0.15 +
            self._eff(attacker_p, "OtB") * 0.15 +
            self._eff(attacker_p, "Bal") * 0.15 +
            self._eff(attacker_p, "Tec") * 0.15
        )
        def_score = (
            self._eff(marker, "Tck") * 0.25 +
            self._eff(marker, "Ant") * 0.20 +
            self._eff(marker, "Pos") * 0.20 +
            self._eff(marker, "Str") * 0.15 +
            self._eff(marker, "Mar") * 0.20
        )
        att_roll = att_score + self.rng.gauss(0, 10.0)
        def_roll = def_score + self.rng.gauss(0, 10.0)

        if att_roll < def_roll:
            # duel lost -- maybe a foul, else clean tackle
            margin = def_roll - att_roll
            self.player_actions[marker["uid"]]["tackles_won"] += 1
            if self._maybe_foul(marker, attacker_p, margin, minute, def_side):
                # final-third foul -> free kick (which is a separate shot path,
                # does NOT chain back into corners or further set pieces)
                self._attempt_free_kick(att_xi, def_xi, minute, attacker)
                return
            self._emit(minute, "chance", attacker,
                       f"{minute}'  {marker['name']} wins it back from "
                       f"{attacker_p['name']} with a clean tackle",
                       player_uids=[marker["uid"], attacker_p["uid"]],
                       player_names=[marker["name"], attacker_p["name"]])
            self._maybe_counter(attacker, minute, marker)
            return

        # ---- Step 3: shot opportunity (with passer as assist candidate) ----
        # occasional offside
        if self.rng.random() < 0.05:
            self.stats.offsides[attacker] += 1
            self._emit(minute, "offside", attacker,
                       f"{minute}'  Offside against {attacker_p['name']}",
                       player_uids=[attacker_p["uid"]],
                       player_names=[attacker_p["name"]])
            return

        self._attempt_shot(attacker_p, marker, def_xi, minute, attacker,
                           assist_candidate=passer, zone=zone)

    def _attempt_shot(self, shooter: dict[str, Any],
                      nearest_def: dict[str, Any],
                      def_xi: list[dict[str, Any]],
                      minute: int, attacker: str,
                      assist_candidate: dict[str, Any] | None = None,
                      allow_corner: bool = True,
                      zone: tuple[int, int] = (1, 1)) -> None:
        """§0.2: assist_candidate threaded through for goal attribution.
        Phase 4A: zone parameter affects shot quality + xG tracking."""
        # mentality affects defensive line height of the DEFENDING team.
        defending_mentality = self.away_mentality if attacker == "home" else self.home_mentality
        line_height = MENTALITY_LINE_HEIGHT[defending_mentality]
        pressure_mult = 2.0 - line_height
        pressure = (
            self._eff(nearest_def, "Tck") * 0.4 +
            self._eff(nearest_def, "Pos") * 0.3 +
            self._eff(nearest_def, "Mar") * 0.3
        ) * pressure_mult

        shot_quality = (
            self._eff(shooter, "Fin") * 0.30 +
            self._eff(shooter, "Tec") * 0.20 +
            self._eff(shooter, "Cmp") * 0.20 +
            self._eff(shooter, "OtB") * 0.10 +
            self._eff(shooter, "Dec") * 0.10 +
            self._eff(shooter, "Lon") * 0.10
        )
        shot_quality -= pressure * 0.30

        # Phase 4A: zone bonus to shot quality
        from engine.zones import get_shot_zone_bonus, get_xg_for_shot
        zone_bonus = get_shot_zone_bonus(zone)
        shot_quality *= zone_bonus

        shot_roll = shot_quality + self.rng.gauss(0, 12.5)
        self.stats.shots[attacker] += 1
        self.player_actions[shooter["uid"]]["shots"] += 1

        # Phase 4A: compute + accumulate xG for this shot
        xg = get_xg_for_shot(zone, shot_quality, pressure)
        self.stats.xg[attacker] += xg

        mentality = self.home_mentality if attacker == "home" else self.away_mentality
        on_target_threshold = 42.5 + MENTALITY_SHOT_THRESHOLD_MOD[mentality]
        miss_threshold = 35.0

        if shot_roll < miss_threshold:
            self.stats.shots_off_target[attacker] += 1
            self._emit(minute, "miss", attacker,
                       f"{minute}'  {shooter['name']} drags a shot wide under "
                       f"pressure from {nearest_def['name']}",
                       player_uids=[shooter["uid"], nearest_def["uid"]],
                       player_names=[shooter["name"], nearest_def["name"]])
            return

        if shot_roll < on_target_threshold:
            self.stats.shots_off_target[attacker] += 1
            self._emit(minute, "miss", attacker,
                       f"{minute}'  {shooter['name']} shoots but it's off target",
                       player_uids=[shooter["uid"]],
                       player_names=[shooter["name"]])
            return

        self.stats.shots_on_target[attacker] += 1
        self.player_actions[shooter["uid"]]["shots_on_target"] += 1

        # ---- Goalkeeper save ----
        gks = [s for s in def_xi if s.get("family") == "GK" and s.get("uid")]
        if not gks:
            self._score_goal(shooter, nearest_def, minute, attacker,
                             assist=assist_candidate)
            return
        keeper = gks[0]

        keeper_quality = (
            self._eff(keeper, "Ref") * 0.30 +
            self._eff(keeper, "1v1") * 0.20 +
            self._eff(keeper, "Pos") * 0.20 +
            self._eff(keeper, "Han") * 0.15 +
            self._eff(keeper, "Cmd") * 0.15
        )

        # save chance: small keeper advantage (+3.0) on 0-100 scale.
        # Was 0 early in 0-100 tuning but that gave too many goals for
        # mid-quality clubs (CA ~67). +3.0 gives a slight edge that keeps
        # avg goals in the 2.0-3.0 band across club quality levels.
        # Phase 3 fix: reduced from +3.0 to +1.5 (0-0 frequency was too high at 22%)
        save_roll = keeper_quality + 1.5 + self.rng.gauss(0, 7.5)
        shot_final = shot_roll + self.rng.gauss(0, 5.0)

        if save_roll >= shot_final:
            self.player_actions[keeper["uid"]]["saves"] += 1
            self._emit(minute, "save", attacker,
                       f"{minute}'  Great save by {keeper['name']}! denies "
                       f"{shooter['name']}'s shot",
                       player_uids=[keeper["uid"], shooter["uid"]],
                       player_names=[keeper["name"], shooter["name"]])
            # corner only on the ORIGINAL shot (no recursion)
            if allow_corner and self.rng.random() < 0.35:
                self.stats.corners[attacker] += 1
                corner_att_xi = self.home_xi if attacker == "home" else self.away_xi
                self._attempt_corner(corner_att_xi, def_xi, minute, attacker)
            return

        # GOAL!
        self._score_goal(shooter, nearest_def, minute, attacker,
                         assist=assist_candidate)

    def _score_goal(self, scorer: dict[str, Any],
                    nearest_def: dict[str, Any],
                    minute: int, attacker: str,
                    assist: dict[str, Any] | None = None) -> None:
        """§0.2: records assist if a valid assist candidate was threaded through."""
        if attacker == "home":
            self.home_score += 1
        else:
            self.away_score += 1
        self.player_actions[scorer["uid"]]["goals"] += 1
        has_assist = (assist and assist.get("uid")
                      and assist["uid"] != scorer["uid"])
        if has_assist:
            self.player_actions[assist["uid"]]["assists"] += 1

        text = f"{minute}'  GOAL!  {scorer['name']} scores for {self.home_club if attacker == 'home' else self.away_club}!"
        if has_assist:
            text += f"  Assist: {assist['name']}"
        uids = [scorer["uid"]] + ([assist["uid"]] if has_assist else [])
        names = [scorer["name"]] + ([assist["name"]] if has_assist else [])
        self._emit(minute, "goal", attacker, text, player_uids=uids, player_names=names)

    # ------------------------------------------------------------------
    # Fouls + cards (§0.3, §0.4)
    # ------------------------------------------------------------------
    def _maybe_foul(self, tackler: dict[str, Any], victim: dict[str, Any],
                    margin: float, minute: int, tackler_side: str) -> bool:
        """Return True if a foul is called.  Cards driven by Agg+Dirt vs ref."""
        if margin < FOUL_MARGIN_THRESHOLD:
            return False
        agg = self._eff(tackler, "Agg")
        foul_chance = 0.30 + (agg / 100.0) * 0.40
        if self.rng.random() > foul_chance:
            return False
        self.stats.fouls[tackler_side] += 1
        self.player_actions[tackler["uid"]]["fouls_committed"] += 1
        self.player_actions[victim["uid"]]["fouls_won"] += 1
        self._emit(minute, "foul", tackler_side,
                   f"{minute}'  Foul by {tackler['name']} on {victim['name']}",
                   player_uids=[tackler["uid"], victim["uid"]],
                   player_names=[tackler["name"], victim["name"]])

        dirt = self._eff(tackler, "Dirt") or 50
        card_factor = (agg + dirt) / 200.0
        strict_factor = self.referee_strictness / 20.0  # referee is 5-18, not 0-100
        # Phase 3 fix: raised multiplier from 0.20 to 0.35 for yellows, 0.01 to 0.02 for reds
        yellow_chance = YELLOW_CARD_BASE + card_factor * strict_factor * 0.35
        red_chance = RED_CARD_BASE + card_factor * strict_factor * 0.02

        roll = self.rng.random()
        if roll < red_chance:
            self.stats.cards[tackler_side]["r"] += 1
            self._emit(minute, "card", tackler_side,
                       f"{minute}'  RED CARD!  {tackler['name']} is sent off!",
                       player_uids=[tackler["uid"]],
                       player_names=[tackler["name"]])
            self._remove_player(tackler, tackler_side)
        elif roll < red_chance + yellow_chance:
            self.stats.cards[tackler_side]["y"] += 1
            self._emit(minute, "card", tackler_side,
                       f"{minute}'  Yellow card for {tackler['name']}",
                       player_uids=[tackler["uid"]],
                       player_names=[tackler["name"]])
        return True

    def _remove_player(self, player: dict[str, Any], side: str) -> None:
        xi = self.home_xi if side == "home" else self.away_xi
        uid = player.get("uid")
        for i, p in enumerate(xi):
            if p.get("uid") == uid:
                xi[i] = {
                    "slot": p.get("slot", "?"), "family": p.get("family", "MID"),
                    "side": p.get("side", "C"),
                    "uid": None, "name": "(sent off)", "score": 0.0,
                    "attrs": {}, "preferred_foot": "Either",
                    "ca": {"GK": 0, "DEF": 0, "MID": 0, "ATT": 0},
                    "age": None, "primary_family": p.get("primary_family", "MID"),
                }
                return

    # ------------------------------------------------------------------
    # Set pieces (no chaining — each is a one-shot opportunity)
    # ------------------------------------------------------------------
    def _attempt_corner(self, att_xi: list[dict[str, Any]],
                        def_xi: list[dict[str, Any]],
                        minute: int, attacker: str) -> None:
        if self.rng.random() > CORNER_SHOT_CHANCE:
            return
        att_atts = [s for s in att_xi if s.get("family") in ("ATT", "MID") and s.get("uid")]
        if not att_atts:
            return
        attacker_p = max(att_atts, key=lambda p: (
            self._eff(p, "Hea") + self._eff(p, "Jum") + self._eff(p, "Str")
        ) / 3)
        def_defs = [s for s in def_xi if s.get("family") in ("DEF", "MID") and s.get("uid")]
        if not def_defs:
            return
        marker = max(def_defs, key=lambda p: (
            self._eff(p, "Hea") + self._eff(p, "Jum") + self._eff(p, "Pos")
        ) / 3)
        att_aerial = (self._eff(attacker_p, "Hea") * 0.4 +
                      self._eff(attacker_p, "Jum") * 0.4 +
                      self._eff(attacker_p, "Str") * 0.2)
        def_aerial = (self._eff(marker, "Hea") * 0.4 +
                      self._eff(marker, "Jum") * 0.4 +
                      self._eff(marker, "Pos") * 0.2)
        att_roll = att_aerial + self.rng.gauss(0, 10.0)
        def_roll = def_aerial + self.rng.gauss(0, 10.0)
        if att_roll > def_roll:
            self._emit(minute, "chance", attacker,
                       f"{minute}'  {attacker_p['name']} gets his head to the corner!",
                       player_uids=[attacker_p["uid"]],
                       player_names=[attacker_p["name"]])
            # corner shot does NOT yield further corners (allow_corner=False)
            self._attempt_shot(attacker_p, marker, def_xi, minute, attacker,
                               assist_candidate=None, allow_corner=False)

    def _attempt_free_kick(self, att_xi: list[dict[str, Any]],
                           def_xi: list[dict[str, Any]],
                           minute: int, attacker: str) -> None:
        """Direct free kick: Fre-driven, wall reduces, no chaining."""
        self.stats.free_kicks[attacker] += 1
        candidates = [s for s in att_xi if s.get("family") in ("MID", "ATT") and s.get("uid")]
        if not candidates:
            return
        taker = max(candidates, key=lambda p: self._eff(p, "Fre"))
        fk_quality = (
            self._eff(taker, "Fre") * 0.40 +
            self._eff(taker, "Tec") * 0.30 +
            self._eff(taker, "Cmp") * 0.20 +
            self._eff(taker, "Dec") * 0.10
        )
        def_defs = [s for s in def_xi if s.get("family") == "DEF" and s.get("uid")]
        if def_defs:
            wall_block = sum(self._eff(d, "Jum") for d in def_defs[:3]) / max(1, len(def_defs[:3]))
            fk_quality -= wall_block * 0.25

        fk_roll = fk_quality + self.rng.gauss(0, 10.0)
        if fk_roll < 55.0:
            self._emit(minute, "free_kick", attacker,
                       f"{minute}'  Free kick: {taker['name']} curls it over the wall but it's off target",
                       player_uids=[taker["uid"]],
                       player_names=[taker["name"]])
            return
        gks = [s for s in def_xi if s.get("family") == "GK" and s.get("uid")]
        if not gks:
            self._score_goal(taker, def_defs[0] if def_defs else taker, minute,
                             attacker, assist=None)
            return
        keeper = gks[0]
        keeper_quality = (
            self._eff(keeper, "Ref") * 0.4 +
            self._eff(keeper, "Pos") * 0.3 +
            self._eff(keeper, "Han") * 0.3
        )
        save_roll = keeper_quality + 10.0 + self.rng.gauss(0, 7.5)
        fk_final = fk_roll + self.rng.gauss(0, 5.0)
        self.stats.shots[attacker] += 1
        self.stats.shots_on_target[attacker] += 1
        self.player_actions[taker["uid"]]["shots"] += 1
        self.player_actions[taker["uid"]]["shots_on_target"] += 1
        if save_roll >= fk_final:
            self.player_actions[keeper["uid"]]["saves"] += 1
            self._emit(minute, "save", attacker,
                       f"{minute}'  Free kick save!  {keeper['name']} denies {taker['name']}",
                       player_uids=[keeper["uid"], taker["uid"]],
                       player_names=[keeper["name"], taker["name"]])
            return
        self._score_goal(taker, def_defs[0] if def_defs else taker, minute,
                         attacker, assist=None)

    def _attempt_penalty(self, shooter: dict[str, Any], keeper: dict[str, Any],
                         minute: int, attacker: str) -> None:
        self.stats.penalties[attacker] += 1
        pen_score = self._eff(shooter, "Pen")
        gk_score = self._eff(keeper, "1v1")
        conversion = (PENALTY_CONVERSION_BASE
                      + (pen_score - 50) * 0.003
                      - (gk_score - 50) * 0.002)
        conversion = max(0.40, min(0.95, conversion))
        self.stats.shots[attacker] += 1
        self.player_actions[shooter["uid"]]["shots"] += 1
        if self.rng.random() < conversion:
            self._score_goal(shooter, keeper, minute, attacker, assist=None)
        else:
            self.player_actions[keeper["uid"]]["saves"] += 1
            self._emit(minute, "save", attacker,
                       f"{minute}'  PENALTY SAVED!  {keeper['name']} denies {shooter['name']} from the spot!",
                       player_uids=[keeper["uid"], shooter["uid"]],
                       player_names=[keeper["name"], shooter["name"]])

    # ------------------------------------------------------------------
    # Injuries (§0.9)
    # ------------------------------------------------------------------
    def _maybe_injury(self, current_side: str, minute: int) -> None:
        for side in ("home", "away"):
            xi = self.home_xi if side == "home" else self.away_xi
            opp_xi = self.away_xi if side == "home" else self.home_xi
            opp_aggs = [self._eff(s, "Agg") for s in opp_xi if s.get("uid")]
            opp_agg = (sum(opp_aggs) / len(opp_aggs)) if opp_aggs else 10
            for p in list(xi):
                uid = p.get("uid")
                if uid is None or uid in self.injured_home or uid in self.injured_away:
                    continue
                attrs = p.get("attrs", {})
                inj_pr = attrs.get("Inj Pr") or 50
                prob = INJURY_BASE_PROB * (inj_pr / 50.0) * (1.0 + INJURY_OPP_AGG_MULT * (opp_agg / 100.0))
                if self.rng.random() < prob:
                    self._emit(minute, "injury", side,
                               f"{minute}'  Injury: {p['name']} goes down and needs treatment",
                               player_uids=[uid],
                               player_names=[p["name"]])
                    if side == "home":
                        self.injured_home.add(uid)
                    else:
                        self.injured_away.add(uid)
                    if not self._sub_injured(p, side, minute):
                        self._remove_player(p, side)
                        self._emit(minute, "info", side,
                                   f"{minute}'  {p['name']} can't continue; {self.home_club if side == 'home' else self.away_club} down to 10 men",
                                   player_uids=[uid],
                                   player_names=[p["name"]])

    def _sub_injured(self, player: dict[str, Any], side: str, minute: int) -> bool:
        xi = self.home_xi if side == "home" else self.away_xi
        bench = self.home_bench if side == "home" else self.away_bench
        used = self.used_bench_home if side == "home" else self.used_bench_away
        if len(used) >= 3 or not bench:
            return False
        fam = player.get("family", "MID")
        cand = None
        for b in bench:
            if b.get("uid") in used:
                continue
            if b.get("primary_family") == fam or b.get("best_family") == fam:
                cand = b
                break
        if cand is None:
            for b in bench:
                if b.get("uid") not in used:
                    cand = b
                    break
        if cand is None:
            return False
        for i, p in enumerate(xi):
            if p.get("uid") == player.get("uid"):
                xi[i] = {
                    "slot": p.get("slot", "?"), "family": fam, "side": p.get("side", "C"),
                    "uid": cand["uid"], "name": cand["name"],
                    "score": cand.get("best_ca", 0.0),
                    "attrs": cand.get("attrs", {}),
                    "preferred_foot": cand.get("preferred_foot", "Either"),
                    "ca": cand.get("ca", {}),
                    "age": cand.get("age"),
                    "primary_family": cand.get("primary_family", fam),
                }
                used.add(cand["uid"])
                self.form_roll[cand["uid"]] = self._form_roll(cand.get("attrs", {}))
                self.fatigue[cand["uid"]] = 0.0
                self._emit(minute, "sub", side,
                           f"{minute}'  Substitution ({self.home_club if side == 'home' else self.away_club}):  "
                           f"{cand['name']} replaces injured {player['name']}",
                           player_uids=[cand["uid"]],
                           player_names=[cand["name"], player["name"]])
                return True
        return False

    # ------------------------------------------------------------------
    # Counter-attacks
    # ------------------------------------------------------------------
    def _maybe_counter(self, original_attacker: str, minute: int,
                       interceptor: dict[str, Any]) -> None:
        if self.rng.random() > COUNTER_CHANCE:
            return
        counter_side = "away" if original_attacker == "home" else "home"
        counter_xi = self.home_xi if counter_side == "home" else self.away_xi
        att_atts = [s for s in counter_xi if s.get("family") == "ATT" and s.get("uid")]
        if not att_atts:
            return
        runner = self.rng.choice(att_atts)
        self._emit(minute, "chance", counter_side,
                   f"{minute}'  Counter-attack! {interceptor['name']} picks out "
                   f"{runner['name']} on the break...",
                   player_uids=[interceptor["uid"], runner["uid"]],
                   player_names=[interceptor["name"], runner["name"]])
        def_xi = self.away_xi if counter_side == "home" else self.home_xi
        def_defs = [s for s in def_xi if s.get("family") == "DEF" and s.get("uid")]
        marker = self.rng.choice(def_defs) if def_defs else interceptor
        self._attempt_shot(runner, marker, def_xi, minute, counter_side,
                           assist_candidate=interceptor)

    # ------------------------------------------------------------------
    # Tactical substitutions (unchanged from Phase 1)
    # ------------------------------------------------------------------
    def _maybe_substitute(self, minute: int) -> None:
        self._sub_for_side("home", minute)
        self._sub_for_side("away", minute)

    def _sub_for_side(self, side: str, minute: int) -> None:
        xi = self.home_xi if side == "home" else self.away_xi
        bench = self.home_bench if side == "home" else self.away_bench
        used = self.used_bench_home if side == "home" else self.used_bench_away
        if len(used) >= 3 or not xi or not bench:
            return
        starters_with_fat = [(i, s, self.fatigue.get(s.get("uid"), 0.0))
                              for i, s in enumerate(xi) if s.get("uid")]
        if not starters_with_fat:
            return
        starters_with_fat.sort(key=lambda t: t[2], reverse=True)
        for idx, starter, fat in starters_with_fat:
            fam = starter["family"]
            cand = None
            for b in bench:
                if b.get("uid") in used:
                    continue
                if b.get("primary_family") == fam or b.get("best_family") == fam:
                    cand = b
                    break
            if cand is None:
                continue
            old_name = starter["name"]
            xi[idx] = {
                "slot": starter["slot"], "family": fam, "side": starter["side"],
                "uid": cand["uid"], "name": cand["name"],
                "score": cand.get("best_ca", 0.0),
                "attrs": cand.get("attrs", {}),
                "preferred_foot": cand.get("preferred_foot", "Either"),
                "ca": cand.get("ca", {}),
                "age": cand.get("age"),
                "primary_family": cand.get("primary_family", fam),
            }
            used.add(cand["uid"])
            self.form_roll[cand["uid"]] = self._form_roll(cand.get("attrs", {}))
            self.fatigue[cand["uid"]] = 0.0
            self._emit(minute, "sub", side,
                       f"{minute}'  Substitution ({self.home_club if side == 'home' else self.away_club}):  "
                       f"{cand['name']} replaces {old_name}",
                       player_uids=[cand["uid"]],
                       player_names=[cand["name"], old_name])
            return

    # ------------------------------------------------------------------
    # Player ratings + MOTM
    # ------------------------------------------------------------------
    def _compute_player_ratings(self) -> dict[int, float]:
        ratings = {}
        for uid, a in self.player_actions.items():
            r = 6.0
            r += a["goals"] * 1.5
            r += a["assists"] * 1.0
            r += a["shots_on_target"] * 0.2
            r += a["saves"] * 0.5
            r += a["tackles_won"] * 0.15
            if a["passes_attempted"] > 0:
                completion = a["passes_completed"] / a["passes_attempted"]
                r += (completion - 0.7) * 2.0
            r -= a["fouls_committed"] * 0.1
            r = max(3.0, min(10.0, r))
            ratings[uid] = round(r, 2)
        return ratings

    def _pick_motm(self, ratings: dict[int, float]) -> tuple[int | None, str | None]:
        if not ratings:
            return None, None
        home_uids = {p["uid"] for p in self.home_xi if p.get("uid")}
        away_uids = {p["uid"] for p in self.away_xi if p.get("uid")}
        if self.home_score > self.away_score:
            candidates = {u: r for u, r in ratings.items() if u in home_uids}
        elif self.away_score > self.home_score:
            candidates = {u: r for u, r in ratings.items() if u in away_uids}
        else:
            candidates = ratings
        if not candidates:
            candidates = ratings
        best_uid = max(candidates, key=candidates.get)
        name = None
        for p in self.home_xi + self.away_xi + self.home_bench + self.away_bench:
            if p.get("uid") == best_uid:
                name = p.get("name")
                break
        return best_uid, name

    # ------------------------------------------------------------------
    # Event emit
    # ------------------------------------------------------------------
    def _emit(self, minute: int, type_: str, side: str, text: str,
              player_uids: list[int] = None,
              player_names: list[str] = None) -> None:
        self.events.append(MatchEvent(
            minute=minute, type=type_, side=side, text=text,
            player_uids=player_uids or [],
            player_names=player_names or [],
        ))


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------

def play_match(home_club: str, away_club: str,
               home_xi: list[dict[str, Any]],
               away_xi: list[dict[str, Any]],
               home_bench: list[dict[str, Any]] = None,
               away_bench: list[dict[str, Any]] = None,
               seed: int | None = None,
               home_mentality: str = "balanced",
               away_mentality: str = "balanced",
               home_advantage: float = 1.10,
               home_instructions: dict | None = None,
               away_instructions: dict | None = None,
               home_profile: str = "possession",
               away_profile: str = "possession") -> MatchResult:
    engine = MatchEngine(
        home_club=home_club, away_club=away_club,
        home_xi=home_xi, away_xi=away_xi,
        home_bench=home_bench or [], away_bench=away_bench or [],
        seed=seed,
        home_mentality=home_mentality, away_mentality=away_mentality,
        home_advantage=home_advantage,
        home_instructions=home_instructions, away_instructions=away_instructions,
        home_profile=home_profile, away_profile=away_profile,
    )
    return engine.simulate()
