"""
Attribute model & position-suitability scoring.

Loads a player's full attribute row from SQLite into a tidy Python dict,
then computes:
  - per-position "Current Ability" (CA) scores (GK / DEF / MID / ATT)
  - a position-suitability score for any specific (role, side) slot
  - a per-player match-form roll (using Cons/Temp/Pres/Imp M)

All attribute weights are explicit and tweakable; nothing is hidden in
spaghetti code.  Everything in here is pure logic -- no I/O except a single
SQLite read.
"""
from __future__ import annotations

import os
import random
import sqlite3
from typing import Any

# data/ is a sibling of engine/ -- compute DB path relative to this file
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
DB_PATH = os.path.join(_PROJECT_ROOT, "data", "football.db")

# Canonical attribute key.  Matches the columns written by ingest.py.
# Note: "NatFit" is the renamed "Nat.1" (natural fitness).
ATTR_KEYS = [
    # Technical
    "Cor", "Cro", "Dri", "Fin", "Fir", "Fre", "Hea", "Lon", "L Th", "Mar",
    "Pas", "Pen", "Tck", "Tec",
    # Mental
    "Agg", "Ant", "Bra", "Cmp", "Cnt", "Dec", "Det", "Fla", "Ldr", "OtB",
    "Pos", "Tea", "Vis", "Wor",
    # Physical
    "Acc", "Agi", "Bal", "Jum", "Pac", "Sta", "Str",
    # Goalkeeping
    "Aer", "Cmd", "Com", "Ecc", "Han", "Kic", "1v1", "Pun", "Ref", "TRO", "Thr",
    # Hidden / personality
    "Ada", "Amb", "Cons", "Dirt", "Imp M", "Inj Pr", "Loy", "Pres",
    "Prof", "Spor", "Temp", "Vers",
    # Natural fitness (renamed)
    "NatFit",
]


# ---------------------------------------------------------------------------
# Weighted CA formulas per role family.
# Each entry: {attr: weight}.  Weights sum to ~1.0 for transparency.
# Averaged value * 20 = approximate CA on the 1..20 FM scale.
# ---------------------------------------------------------------------------

CA_WEIGHTS = {
    "GK": {
        "Ref": 0.16, "Han": 0.14, "1v1": 0.12, "Aer": 0.10, "Cmd": 0.10,
        "Com": 0.08, "Kic": 0.06, "Pos": 0.06, "Ant": 0.06, "Dec": 0.06,
        "Cmp": 0.06,
    },
    "DEF": {
        "Tck": 0.14, "Mar": 0.12, "Pos": 0.10, "Ant": 0.10, "Hea": 0.08,
        "Str": 0.08, "Jum": 0.06, "Dec": 0.06, "Cnt": 0.06, "Acc": 0.05,
        "Pac": 0.05, "Tea": 0.05, "Cmp": 0.05,
    },
    "MID": {
        "Pas": 0.14, "Tec": 0.10, "Vis": 0.08, "Dec": 0.08, "Cmp": 0.08,
        "Ant": 0.07, "Tck": 0.07, "Sta": 0.06, "Wor": 0.06, "Dri": 0.06,
        "OtB": 0.06, "Fir": 0.05, "Tea": 0.05,
    },
    "ATT": {
        "Fin": 0.16, "Dri": 0.12, "Tec": 0.10, "OtB": 0.10, "Pac": 0.10,
        "Acc": 0.08, "Cmp": 0.08, "Agi": 0.06, "Fla": 0.06, "Ant": 0.06,
        "Dec": 0.04, "Str": 0.04,
    },
}

# Per-side bonus/penalty multipliers when computing suitability for a
# specific (role, side) slot.  Left-footed players get a small bonus on the
# left, right-footed on the right; "Either" gets a small bonus on both.
SIDE_FOOT_BONUS = {
    ("L", "Left"):       0.05,
    ("L", "Left Only"):  0.08,
    ("L", "Either"):     0.02,
    ("R", "Right"):      0.05,
    ("R", "Right Only"): 0.08,
    ("R", "Either"):     0.02,
    ("C", "Either"):     0.03,
    ("C", "Right"):      0.01,
    ("C", "Left"):       0.01,
}


def load_player_attrs(conn: sqlite3.Connection, uid: int) -> dict[str, Any]:
    """Return {attr: int|None} for one player, plus 'uid' and basic info."""
    cur = conn.execute("SELECT * FROM player_attributes WHERE uid = ?", (uid,))
    row = cur.fetchone()
    if row is None:
        return {}
    cols = [d[0] for d in cur.description]
    return {c: v for c, v in zip(cols, row)}


def _weighted_avg(attrs: dict[str, Any], weights: dict[str, float]) -> float | None:
    num = 0.0
    den = 0.0
    for k, w in weights.items():
        v = attrs.get(k)
        if v is None:
            continue
        num += v * w
        den += w
    if den == 0:
        return None
    return num / den


def ca_for_family(attrs: dict[str, Any], family: str) -> float | None:
    """Compute Current Ability for a role family (GK/DEF/MID/ATT). 0..100 scale."""
    weights = CA_WEIGHTS.get(family)
    if not weights:
        return None
    return _weighted_avg(attrs, weights)


def foot_strength_score(player_row: sqlite3.Row) -> str:
    """Return 'Left' / 'Right' / 'Either' / etc. from the players row."""
    return player_row["preferred_foot"] or "Either"


def suitability_for_slot(
    conn: sqlite3.Connection,
    player_row: sqlite3.Row,
    attrs: dict[str, Any],
    family: str,
    side: str,
) -> float:
    """How well this player fits a (family, side) slot, 0..100 scale.

    Combines:
      - CA at that family (weighted attribute average)
      - Side/foot bonus
      - Out-of-position penalty: if the player has no row in
        player_positions matching this family, apply up to 20% penalty
        (mitigated by Versatility)
    """
    ca = ca_for_family(attrs, family)
    if ca is None:
        return 0.0

    # Out-of-position check
    uid = player_row["uid"]
    cur = conn.execute(
        "SELECT 1 FROM player_positions WHERE uid = ? AND family = ? LIMIT 1",
        (uid, family),
    )
    in_family = cur.fetchone() is not None
    if not in_family:
        # Versatility (0-100) mitigates the penalty: vers=0 -> 20% penalty,
        # vers=100 -> 0% penalty
        vers = attrs.get("Vers") or 25
        penalty = 0.20 * (1.0 - vers / 100.0)
        penalty = max(0.05, penalty)
        ca = ca * (1.0 - penalty)

    # Side / foot bonus
    foot = foot_strength_score(player_row)
    bonus = SIDE_FOOT_BONUS.get((side, foot), 0.0)
    ca = ca * (1.0 + bonus)

    return ca


# ---------------------------------------------------------------------------
# Match-form roll -- per-player per-match variance
# ---------------------------------------------------------------------------

def match_form_roll(attrs: dict[str, Any], rng: random.Random) -> float:
    """Return a multiplier ~0.85..1.15 driven by Consistency / Temperament /
    Pressure / Important Matches.  Higher hidden attributes -> tighter range
    around 1.0; lower -> wider swings.  Attributes are 0-100 scale."""
    cons  = attrs.get("Cons")  or 50
    temp  = attrs.get("Temp")  or 50
    pres  = attrs.get("Pres")  or 50
    imp_m = attrs.get("Imp M") or 50

    # Average "stability" -- higher is more consistent (0-100 scale)
    stability = (cons + temp + pres + imp_m) / 4.0
    # Map stability 0..100 -> sigma 0.20..0.05
    sigma = 0.20 - (stability / 100.0) * 0.15
    sigma = max(0.03, sigma)


    mult = rng.gauss(1.0, sigma)
    # Clamp to a sane range so a single bad roll can't make a star useless
    return max(0.70, min(1.30, mult))


# ---------------------------------------------------------------------------
# Convenience: load one player's full record (basic info + attrs)
# ---------------------------------------------------------------------------

def load_full_player(conn: sqlite3.Connection, uid: int) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM players WHERE uid = ?", (uid,))
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    out = {c: row[c] for c in cols}
    out["attrs"] = load_player_attrs(conn, uid)
    out["ca"] = {
        fam: ca_for_family(out["attrs"], fam)
        for fam in ("GK", "DEF", "MID", "ATT")
    }
    return out


if __name__ == "__main__":
    # Quick smoke test
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT uid, name, club FROM players LIMIT 5")
    for r in cur:
        p = load_full_player(conn, r["uid"])
        print(f"{p['name']:30s}  GK={p['ca']['GK']:.1f}  DEF={p['ca']['DEF']:.1f}  "
              f"MID={p['ca']['MID']:.1f}  ATT={p['ca']['ATT']:.1f}")
    conn.close()
