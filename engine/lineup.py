"""
Lineup selection: pick the best XI for a chosen formation using the
Hungarian algorithm (scipy.optimize.linear_sum_assignment).

Formations supported (Phase 2):
  - 4-3-3 (default)
  - 4-4-2
  - 4-2-3-1
  - 3-5-2

Algorithm:
  1. For each club, load all its players with their attrs + CA per family.
  2. Build a (players × slots) suitability matrix.
  3. Solve with linear_sum_assignment to maximize total suitability across
     all 11 slots. This avoids the greedy trap where the best RB is also
     the best RCB — the global optimum is found.
  4. Bench: fill remaining slots (up to 7) with the next-best unused players,
     ensuring at least one backup GK, one DEF, one MID, one ATT if possible.

Auto-formation selection:
  - Count players per family in the squad.
  - If squad has many DEF (>= 5 natural defenders) and few ATT (< 3), prefer
    3-5-2 or 4-4-2 (formations with fewer pure attackers).
  - If squad has many ATT (>= 4) and wingers, prefer 4-3-3 or 4-2-3-1.
  - Default: 4-3-3.

A 'manual override' entry point accepts a user-chosen XI + bench (Phase 2B
will use this for manager mode).
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

from .attributes import (
    DB_PATH,
    load_player_attrs,
    ca_for_family,
    suitability_for_slot,
    foot_strength_score,
    SIDE_FOOT_BONUS,
)

# ---------------------------------------------------------------------------
# Formation definitions: each slot is (slot_name, family, side)
# ---------------------------------------------------------------------------

FORMATION_433 = [
    ("GK",   "GK",  "C"),
    ("RB",   "DEF", "R"),
    ("RCB",  "DEF", "C"),
    ("LCB",  "DEF", "C"),
    ("LB",   "DEF", "L"),
    ("RCM",  "MID", "R"),
    ("CM",   "MID", "C"),
    ("LCM",  "MID", "L"),
    ("RW",   "ATT", "R"),
    ("ST",   "ATT", "C"),
    ("LW",   "ATT", "L"),
]

FORMATION_442 = [
    ("GK",   "GK",  "C"),
    ("RB",   "DEF", "R"),
    ("RCB",  "DEF", "C"),
    ("LCB",  "DEF", "C"),
    ("LB",   "DEF", "L"),
    ("RM",   "MID", "R"),
    ("RCM",  "MID", "C"),
    ("LCM",  "MID", "C"),
    ("LM",   "MID", "L"),
    ("RST",  "ATT", "C"),
    ("LST",  "ATT", "C"),
]

FORMATION_4231 = [
    ("GK",   "GK",  "C"),
    ("RB",   "DEF", "R"),
    ("RCB",  "DEF", "C"),
    ("LCB",  "DEF", "C"),
    ("LB",   "DEF", "L"),
    ("RDM",  "MID", "C"),
    ("LDM",  "MID", "C"),
    ("RAM",  "MID", "R"),
    ("CAM",  "MID", "C"),
    ("LAM",  "MID", "L"),
    ("ST",   "ATT", "C"),
]

FORMATION_352 = [
    ("GK",   "GK",  "C"),
    ("RCB",  "DEF", "C"),
    ("CB",   "DEF", "C"),
    ("LCB",  "DEF", "C"),
    ("RWB",  "MID", "R"),
    ("RCM",  "MID", "C"),
    ("CM",   "MID", "C"),
    ("LCM",  "MID", "C"),
    ("LWB",  "MID", "L"),
    ("RST",  "ATT", "C"),
    ("LST",  "ATT", "C"),
]

# 5-4-1: defensive formation for the manager-mode sanity test (2B-1)
FORMATION_541 = [
    ("GK",   "GK",  "C"),
    ("RB",   "DEF", "R"),
    ("RCB",  "DEF", "C"),
    ("CB",   "DEF", "C"),
    ("LCB",  "DEF", "C"),
    ("LB",   "DEF", "L"),
    ("RM",   "MID", "R"),
    ("RCM",  "MID", "C"),
    ("LCM",  "MID", "C"),
    ("LM",   "MID", "L"),
    ("ST",   "ATT", "C"),
]

ALL_FORMATIONS = {
    "4-3-3":   FORMATION_433,
    "4-4-2":   FORMATION_442,
    "4-2-3-1": FORMATION_4231,
    "3-5-2":   FORMATION_352,
    "5-4-1":   FORMATION_541,
}

# Default (used when no formation is specified)
DEFAULT_FORMATION = "4-3-3"


# ---------------------------------------------------------------------------
# Squad loading
# ---------------------------------------------------------------------------

def load_club_squad(conn: sqlite3.Connection, club: str) -> list[dict[str, Any]]:
    """Return all players for one club with their attrs + CA per family."""
    cur = conn.execute(
        "SELECT * FROM players WHERE club = ? ORDER BY uid",
        (club,),
    )
    rows = cur.fetchall()
    squad = []
    for r in rows:
        attrs = load_player_attrs(conn, r["uid"])
        ca = {
            fam: (ca_for_family(attrs, fam) or 0.0)
            for fam in ("GK", "DEF", "MID", "ATT")
        }
        squad.append({
            "uid": r["uid"],
            "name": r["name"],
            "primary_role": r["primary_role"],
            "primary_family": r["primary_family"],
            "preferred_foot": r["preferred_foot"] or "Either",
            "age": r["age"],
            "attrs": attrs,
            "ca": ca,
            "best_ca": max(ca.values()),
            "best_family": max(ca, key=ca.get),
        })
    return squad


# ---------------------------------------------------------------------------
# Auto-formation selection based on squad composition
# ---------------------------------------------------------------------------

def pick_formation_for_squad(squad: list[dict[str, Any]]) -> str:
    """Pick the best-fitting formation key based on squad composition.

    Heuristics:
      - Need >= 1 GK (always)
      - Need >= 4 DEF for 4-def formations, >= 3 for 3-def
      - Need >= 3 ATT for 3-att formations, >= 2 for 2-att
      - Prefer 3-5-2 if squad has many DEF and MID but few ATT
      - Prefer 4-2-3-1 if squad has 1 elite ST + 3 AMs
      - Prefer 4-4-2 if squad has 2 STs but few wingers
      - Default: 4-3-3
    """
    family_counts = {"GK": 0, "DEF": 0, "MID": 0, "ATT": 0}
    for p in squad:
        fam = p.get("primary_family")
        if fam in family_counts:
            family_counts[fam] += 1

    # need at least 1 GK
    if family_counts["GK"] < 1:
        return DEFAULT_FORMATION  # fallback

    # count wingers (ATT or AM on L/R side) — check positions table? for now,
    # use ATT count as a proxy for winger depth
    n_def = family_counts["DEF"]
    n_mid = family_counts["MID"]
    n_att = family_counts["ATT"]

    # 3-5-2 needs 3 natural CBs + 5 MID + 2 ST; prefer if ATT are scarce
    if n_def >= 5 and n_mid >= 6 and n_att <= 3:
        return "3-5-2"
    # 4-4-2 needs 4 DEF + 4 MID + 2 ST; prefer if few wingers (ATT <= 3)
    if n_def >= 5 and n_att <= 3 and n_mid >= 6:
        return "4-4-2"
    # 4-2-3-1 needs 4 DEF + 6 MID + 1 ST; prefer if MID-heavy
    if n_def >= 5 and n_mid >= 7 and n_att >= 2:
        return "4-2-3-1"
    # default: 4-3-3 (needs 4 DEF + 3 MID + 3 ATT)
    return "4-3-3"


# ---------------------------------------------------------------------------
# Hungarian-algorithm best-XI picker
# ---------------------------------------------------------------------------

def pick_best_xi(
    squad: list[dict[str, Any]],
    conn: sqlite3.Connection,
    formation: list[tuple[str, str, str]] | str = FORMATION_433,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (starting_xi, bench).

    Uses scipy.optimize.linear_sum_assignment to find the global optimum
    assignment of players to slots (maximizing total suitability).

    `formation` can be either a list of (slot_name, family, side) tuples or
    a string key like "4-3-3".
    """
    if isinstance(formation, str):
        formation = ALL_FORMATIONS.get(formation, FORMATION_433)

    n_players = len(squad)
    n_slots = len(formation)

    if n_players == 0:
        return ([{
            "slot": s[0], "family": s[1], "side": s[2],
            "uid": None, "name": "(empty)", "score": 0.0,
            "attrs": {}, "preferred_foot": "Either",
            "ca": {"GK": 0, "DEF": 0, "MID": 0, "ATT": 0},
            "age": None, "primary_family": s[1],
        } for s in formation], [])

    # Pre-load player_positions for the entire squad to avoid N×M DB queries.
    # This is the critical performance fix for large squads (70+ players).
    uid_set = {p["uid"] for p in squad if p.get("uid")}
    positions_by_uid: dict[int, set[str]] = {}
    if uid_set:
        placeholders = ",".join("?" * len(uid_set))
        cur = conn.execute(
            f"SELECT uid, family FROM player_positions WHERE uid IN ({placeholders})",
            list(uid_set),
        )
        for row in cur:
            positions_by_uid.setdefault(row["uid"], set()).add(row["family"])

    # Build (players × slots) cost matrix. linear_sum_assignment MINIMIZES
    # cost, so we use negative suitability.
    cost = np.full((n_players, n_slots), 1e6, dtype=float)
    for i, p in enumerate(squad):
        attrs = p["attrs"]
        uid = p["uid"]
        player_families = positions_by_uid.get(uid, set())
        for j, (slot_name, family, side) in enumerate(formation):
            # Inline suitability calculation (avoids per-slot DB query)
            ca = ca_for_family(attrs, family)
            if ca is None:
                cost[i, j] = 0.0
                continue
            # Out-of-position penalty
            if family not in player_families:
                vers = attrs.get("Vers") or 25
                penalty = max(0.05, 0.20 * (1.0 - vers / 100.0))
                ca = ca * (1.0 - penalty)
            # Side / foot bonus
            foot = p.get("preferred_foot", "Either")
            bonus = SIDE_FOOT_BONUS.get((side, foot), 0.0)
            ca = ca * (1.0 + bonus)
            cost[i, j] = -ca  # negate for minimization

    # Solve. If fewer slots than players, that's fine; if fewer players than
    # slots, linear_sum_assignment still works (unassigned slots get nothing).
    row_ind, col_ind = linear_sum_assignment(cost)

    # Map slot -> player
    slot_to_player: dict[int, int] = {}
    for r, c in zip(row_ind, col_ind):
        slot_to_player[c] = r

    starting: list[dict[str, Any]] = []
    used_uids: set[int] = set()
    for j, (slot_name, family, side) in enumerate(formation):
        if j in slot_to_player:
            p = squad[slot_to_player[j]]
            score = -cost[slot_to_player[j], j]
            used_uids.add(p["uid"])
            starting.append({
                "slot": slot_name, "family": family, "side": side,
                "uid": p["uid"], "name": p["name"],
                "score": score,
                "attrs": p["attrs"],
                "preferred_foot": p["preferred_foot"],
                "ca": p["ca"],
                "age": p["age"],
                "primary_family": p["primary_family"],
            })
        else:
            starting.append({
                "slot": slot_name, "family": family, "side": side,
                "uid": None, "name": "(empty)", "score": 0.0,
                "attrs": {}, "preferred_foot": "Either",
                "ca": {"GK": 0, "DEF": 0, "MID": 0, "ATT": 0},
                "age": None, "primary_family": family,
            })

    # ---- Bench (same logic as before) ----
    remaining = [p for p in squad if p["uid"] not in used_uids]
    remaining.sort(key=lambda p: p["best_ca"], reverse=True)

    bench_picks: list[dict[str, Any]] = []
    bench_uids: set[int] = set()

    gk_backup = next((p for p in remaining
                      if p["primary_family"] == "GK" and p["uid"] not in bench_uids), None)
    if gk_backup:
        bench_picks.append(_bench_entry(gk_backup, "GK"))
        bench_uids.add(gk_backup["uid"])

    needed_families = {"DEF", "MID", "ATT"}
    for fam in list(needed_families):
        cand = next((p for p in remaining
                     if p["primary_family"] == fam and p["uid"] not in bench_uids), None)
        if cand:
            bench_picks.append(_bench_entry(cand, fam))
            bench_uids.add(cand["uid"])

    for p in remaining:
        if len(bench_picks) >= 7:
            break
        if p["uid"] in bench_uids:
            continue
        bench_picks.append(_bench_entry(p, p["best_family"]))
        bench_uids.add(p["uid"])

    return starting, bench_picks


def _bench_entry(p: dict[str, Any], family: str) -> dict[str, Any]:
    return {
        "uid": p["uid"], "name": p["name"],
        "family": family,
        "preferred_foot": p["preferred_foot"],
        "age": p["age"],
        "attrs": p["attrs"],
        "ca": p["ca"],
        "best_ca": p["best_ca"],
        "primary_family": p["primary_family"],
    }


class _row_proxy:
    """sqlite.Row-like shim so suitability_for_slot can read row['uid']."""
    def __init__(self, p: dict[str, Any]):
        self._d = p
    def __getitem__(self, key):
        return self._d.get(key)


# ---------------------------------------------------------------------------
# Manual override (for Phase 2B manager mode)
# ---------------------------------------------------------------------------

def pick_manual_xi(
    squad: list[dict[str, Any]],
    conn: sqlite3.Connection,
    formation: list[tuple[str, str, str]] | str,
    chosen_uids: list[int],
    bench_uids: list[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build a starting XI from a user-chosen list of UIDs.

    Each UID is assigned to the next slot in the formation. The caller is
    responsible for providing UIDs in a sensible order (or for letting the
    Hungarian algorithm fill in any unassigned slots).
    """
    if isinstance(formation, str):
        formation = ALL_FORMATIONS.get(formation, FORMATION_433)
    squad_by_uid = {p["uid"]: p for p in squad}
    starting: list[dict[str, Any]] = []
    for i, (slot_name, family, side) in enumerate(formation):
        if i < len(chosen_uids) and chosen_uids[i] in squad_by_uid:
            p = squad_by_uid[chosen_uids[i]]
            score = suitability_for_slot(conn, _row_proxy(p), p["attrs"], family, side)
            starting.append({
                "slot": slot_name, "family": family, "side": side,
                "uid": p["uid"], "name": p["name"],
                "score": score,
                "attrs": p["attrs"],
                "preferred_foot": p["preferred_foot"],
                "ca": p["ca"],
                "age": p["age"],
                "primary_family": p["primary_family"],
            })
        else:
            starting.append({
                "slot": slot_name, "family": family, "side": side,
                "uid": None, "name": "(empty)", "score": 0.0,
                "attrs": {}, "preferred_foot": "Either",
                "ca": {"GK": 0, "DEF": 0, "MID": 0, "ATT": 0},
                "age": None, "primary_family": family,
            })
    # bench
    bench = []
    if bench_uids:
        for uid in bench_uids:
            if uid in squad_by_uid:
                p = squad_by_uid[uid]
                bench.append(_bench_entry(p, p["best_family"]))
    return starting, bench


# ---------------------------------------------------------------------------
# Strength aggregation (unchanged from Phase 1)
# ---------------------------------------------------------------------------

def starting_xi_strength(starting: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate strength per family for the starting XI."""
    out = {"GK": 0.0, "DEF": 0.0, "MID": 0.0, "ATT": 0.0, "OVERALL": 0.0}
    counts = {"GK": 0, "DEF": 0, "MID": 0, "ATT": 0}
    for s in starting:
        if s["uid"] is None:
            continue
        fam = s["family"]
        ca = s["ca"].get(fam, 0.0) or 0.0
        out[fam] += ca
        counts[fam] += 1
        out["OVERALL"] += ca
    for fam in ("GK", "DEF", "MID", "ATT"):
        if counts[fam] > 0:
            out[fam] = out[fam] / counts[fam]
    n = sum(counts.values())
    if n > 0:
        out["OVERALL"] = out["OVERALL"] / n
    return out


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT club, COUNT(*) AS n FROM players GROUP BY club ORDER BY n DESC LIMIT 3")
    for r in cur:
        club = r["club"]
        print(f"\n=== {club}  ({r['n']} players) ===")
        squad = load_club_squad(conn, club)
        auto = pick_formation_for_squad(squad)
        print(f"  Auto-picked formation: {auto}")
        starting, bench = pick_best_xi(squad, conn, formation=auto)
        for s in starting:
            print(f"  {s['slot']:4s}  {s['name']:30s}  score={s['score']:.2f}")
        print(f"  Bench: {[b['name'] for b in bench]}")
        print(f"  Strength: {starting_xi_strength(starting)}")
    conn.close()
