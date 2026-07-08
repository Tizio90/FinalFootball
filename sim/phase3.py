"""
Phase 3 systems: player development, youth intake, staff, training,
finances, scouting, press conferences, player concerns, news feed.

All functions take a sqlite3.Connection and are pure logic — no Flask.
"""
from __future__ import annotations

import json
import random
import sqlite3
import datetime as dt
from typing import Any


# ===========================================================================
# Schema migrations for Phase 3 tables
# ===========================================================================

PHASE3_MIGRATIONS = [
    # version 4 -> 5: player development tracking + youth intake
    """CREATE TABLE IF NOT EXISTS player_development (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_uid INTEGER NOT NULL,
        season_id TEXT,
        career_id TEXT,
        ca_before REAL,
        ca_after REAL,
        delta REAL,
        reason TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS youth_intake (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        season_id TEXT,
        player_uid INTEGER NOT NULL,
        club TEXT NOT NULL,
        intake_year INTEGER,
        created_at TEXT NOT NULL
    )""",
    # version 5 -> 6: staff + training
    """CREATE TABLE IF NOT EXISTS staff (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        rating INTEGER DEFAULT 50,
        specialty TEXT,
        wage INTEGER DEFAULT 50000,
        hired_at TEXT NOT NULL
    )""",
    # version 6 -> 7: finances + board expectations + scouting
    """CREATE TABLE IF NOT EXISTS finances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        season_id TEXT,
        round INTEGER,
        income_matchday INTEGER DEFAULT 0,
        income_tv INTEGER DEFAULT 0,
        income_transfers INTEGER DEFAULT 0,
        income_prize INTEGER DEFAULT 0,
        expenses_wages INTEGER DEFAULT 0,
        expenses_transfers INTEGER DEFAULT 0,
        expenses_staff INTEGER DEFAULT 0,
        balance_after INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS board_expectations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        season_id TEXT,
        expectation TEXT,
        target_position INTEGER,
        current_confidence INTEGER DEFAULT 75,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS scouting_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        player_uid INTEGER NOT NULL,
        scout_id INTEGER,
        accuracy INTEGER DEFAULT 50,
        scouted_at TEXT NOT NULL,
        expires_at TEXT
    )""",
    # version 7 -> 8: press conferences + player concerns + news
    """CREATE TABLE IF NOT EXISTS press_conferences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        season_id TEXT,
        round INTEGER,
        match_home TEXT,
        match_away TEXT,
        questions_json TEXT,
        answers_json TEXT,
        morale_effect INTEGER DEFAULT 0,
        confidence_effect INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS player_concerns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        season_id TEXT,
        player_uid INTEGER NOT NULL,
        player_name TEXT,
        concern_type TEXT,
        concern_text TEXT,
        rounds_active INTEGER DEFAULT 0,
        resolved INTEGER DEFAULT 0,
        resolution TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS news_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        season_id TEXT,
        round INTEGER,
        category TEXT,
        headline TEXT,
        body TEXT,
        created_at TEXT NOT NULL
    )""",
]


def run_phase3_migrations(conn: sqlite3.Connection) -> None:
    """Apply all Phase 3 migrations."""
    cur = conn.cursor()
    cur.execute("PRAGMA user_version")
    current_version = cur.fetchone()[0] or 0

    for i, migration_sql in enumerate(PHASE3_MIGRATIONS):
        target_version = i + 5  # Phase 3 migrations start at version 5
        if current_version < target_version:
            try:
                cur.execute(migration_sql)
                conn.commit()
                cur.execute(f"PRAGMA user_version = {target_version}")
                conn.commit()
            except sqlite3.OperationalError as e:
                if "already exists" in str(e):
                    cur.execute(f"PRAGMA user_version = {target_version}")
                    conn.commit()
                else:
                    raise

    # Safety net: force-create all Phase 3 tables
    for sql in PHASE3_MIGRATIONS:
        try:
            cur.execute(sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()


# ===========================================================================
# 3A.2: Player attribute progression
# ===========================================================================

def compute_development_rate(attrs: dict, age: int) -> float:
    """Compute a player's development rate (0.0 - 2.0+).

    Based on:
      - Professionalism (Prof): higher = faster development
      - Ambition (Amb): higher = faster development
      - Age: 16-24 = improving, 25-29 = peak, 30+ = declining
    Returns a multiplier: 1.0 = average, >1 = fast developer, <1 = slow.
    """
    prof = attrs.get("Prof") or 50
    amb = attrs.get("Amb") or 50
    # base rate from personality (0.5 - 1.5)
    personality_rate = 0.5 + (prof + amb) / 200.0  # 0.5 - 1.5

    # age factor
    if age <= 21:
        age_factor = 1.5  # fast development
    elif age <= 24:
        age_factor = 1.2
    elif age <= 28:
        age_factor = 0.8  # peak, slow improvement
    elif age <= 31:
        age_factor = -0.5  # slow decline
    else:
        age_factor = -1.0  # fast decline

    return personality_rate * age_factor


def progress_player(conn: sqlite3.Connection, uid: int, season_id: str,
                    career_id: str, training_focus: str = "general",
                    training_intensity: str = "standard",
                    staff_bonus: float = 1.0) -> dict:
    """Progress a player's attributes by one season.

    Returns {ca_before, ca_after, delta, changes: {attr: delta}}.
    """
    # load player + attrs
    player = conn.execute("SELECT * FROM players WHERE uid = ?", (uid,)).fetchone()
    if player is None:
        return {"ca_before": 0, "ca_after": 0, "delta": 0, "changes": {}}

    attrs = dict(conn.execute("SELECT * FROM player_attributes WHERE uid = ?", (uid,)).fetchone())
    age = player["age"] or 25

    dev_rate = compute_development_rate(attrs, age)
    # training + staff bonus
    intensity_mult = {"light": 0.7, "standard": 1.0, "intensive": 1.3}.get(training_intensity, 1.0)
    focus_mult = _training_focus_multiplier(training_focus)
    total_mult = dev_rate * intensity_mult * staff_bonus

    from engine.attributes import ca_for_family
    ca_before = ca_for_family(attrs, player["primary_family"] or "MID") or 50

    changes = {}
    rng = random.Random(uid + hash(season_id))

    # determine which attributes to change
    attr_keys = [k for k in attrs if k != "uid" and attrs[k] is not None]
    # each attribute changes by ±1-3 based on dev_rate
    for attr in attr_keys:
        base_val = attrs[attr]
        # training focus affects which attrs change more
        focus_bonus = focus_mult.get(attr, 1.0)
        # random component
        change = rng.gauss(total_mult * focus_bonus, 1.5)
        # round to int, clamp to [-5, +5] per season
        change = max(-5, min(5, round(change)))
        if change != 0:
            new_val = max(0, min(100, base_val + change))
            attrs[attr] = new_val
            changes[attr] = change

    # write back attrs
    set_clauses = ", ".join(f'"{k}" = ?' for k in changes)
    if set_clauses:
        params = [changes[k] for k in changes] + [uid]
        conn.execute(f'UPDATE player_attributes SET {set_clauses} WHERE uid = ?', params)

    ca_after = ca_for_family(attrs, player["primary_family"] or "MID") or 50
    delta = ca_after - ca_before

    # record development
    now = dt.datetime.now().isoformat()
    conn.execute(
        "INSERT INTO player_development (player_uid, season_id, career_id, "
        "ca_before, ca_after, delta, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, season_id, career_id, ca_before, ca_after, delta,
         f"training={training_focus}/{training_intensity}", now),
    )

    return {"ca_before": ca_before, "ca_after": ca_after, "delta": delta, "changes": changes}


def _training_focus_multiplier(focus: str) -> dict:
    """Return per-attribute multipliers based on training focus."""
    multipliers = {
        "general": {},
        "attacking": {
            # boost attacking attrs
            "Fin": 1.3, "Dri": 1.3, "Tec": 1.2, "Fla": 1.2, "OtB": 1.2, "Pas": 1.1,
            # slight defensive decline
            "Tck": 0.8, "Mar": 0.8, "Pos": 0.9,
        },
        "defending": {
            "Tck": 1.3, "Mar": 1.3, "Pos": 1.2, "Ant": 1.2, "Hea": 1.2, "Str": 1.1,
            "Fin": 0.8, "Dri": 0.8, "Fla": 0.9,
        },
        "fitness": {
            "Acc": 1.3, "Pac": 1.3, "Sta": 1.3, "Str": 1.2, "Agi": 1.2, "Jum": 1.1,
            "Tec": 0.9, "Pas": 0.9,
        },
        "technical": {
            "Tec": 1.3, "Pas": 1.3, "Dri": 1.2, "Fin": 1.2, "Cro": 1.2, "Fir": 1.2,
            "Str": 0.9, "Sta": 0.9,
        },
    }
    return multipliers.get(focus, {})


def progress_squad(conn: sqlite3.Connection, club: str, season_id: str,
                   career_id: str, training_focus: str = "general",
                   training_intensity: str = "standard",
                   staff_bonus: float = 1.0) -> list[dict]:
    """Progress all players in a club's squad by one season."""
    from engine.lineup import load_club_squad
    squad = load_club_squad(conn, club)
    results = []
    for p in squad:
        r = progress_player(conn, p["uid"], season_id, career_id,
                           training_focus, training_intensity, staff_bonus)
        results.append({"uid": p["uid"], "name": p["name"], **r})
    conn.commit()
    return results


# ===========================================================================
# 3A.3: Youth intake
# ===========================================================================

# Name pools by nationality (simplified — real FM has comprehensive databases)
NAME_POOLS = {
    "ENG": {
        "first": ["Jack", "Harry", "Tom", "James", "Charlie", "George", "Oliver", "Jacob",
                  "Kyle", "Liam", "Mason", "Ryan", "Joe", "Ben", "Sam", "Dan", "Alex", "Max"],
        "last": ["Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson", "Davies",
                 "Robinson", "Wright", "Walker", "Hall", "Green", "Harris", "Clarke", "Patel"],
    },
    "ESP": {
        "first": ["Carlos", "Diego", "Pablo", "Javier", "Sergio", "Marc", "Adrián", "Hugo",
                  "Álvaro", "Iker", "Nico", "Mario", "Raúl", "Bruno", "Martín"],
        "last": ["García", "Rodríguez", "Martínez", "López", "Sánchez", "Pérez", "Gómez",
                 "Fernández", "Ruiz", "Moreno", "Jiménez", "Torres", "Romero", "Navarro"],
    },
    "ITA": {
        "first": ["Marco", "Luca", "Andrea", "Matteo", "Francesco", "Alessandro", "Lorenzo",
                  "Davide", "Federico", "Simone", "Giuseppe", "Antonio", "Giovanni", "Stefano"],
        "last": ["Rossi", "Russo", "Ferrari", "Esposito", "Bianchi", "Romano", "Colombo",
                 "Ricci", "Marino", "Greco", "Bruno", "Gallo", "Conti", "De Luca"],
    },
    "GER": {
        "first": ["Lukas", "Felix", "Maximilian", "Jonas", "Paul", "Tim", "Niklas", "Tobias",
                  "Florian", "Sebastian", "Julian", "Philipp", "Marco", "Daniel"],
        "last": ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner",
                 "Becker", "Schulz", "Hoffmann", "Schäfer", "Koch", "Bauer", "Richter"],
    },
    "FRA": {
        "first": ["Lucas", "Hugo", "Léo", "Gabriel", "Louis", "Jules", "Arthur", "Nathan",
                  "Tom", "Maxime", "Théo", "Enzo", "Raphaël", "Noah"],
        "last": ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit",
                 "Durand", "Leroy", "Moreau", "Simon", "Laurent", "Lefebvre", "Michel"],
    },
    "BRA": {
        "first": ["João", "Pedro", "Lucas", "Gabriel", "Matheus", "Rafael", "Bruno", "Felipe",
                  "Vinícius", "André", "Carlos", "Eduardo", "Thiago", "Diego"],
        "last": ["Silva", "Santos", "Oliveira", "Souza", "Rodrigues", "Ferreira", "Alves",
                 "Pereira", "Lima", "Gomes", "Costa", "Ribeiro", "Martins", "Carvalho"],
    },
    "ARG": {
        "first": ["Mateo", "Benjamín", "Juan", "Santiago", "Valentín", "Joaquín", "Tomás",
                  "Ignacio", "Lautaro", "Bruno", "Franco", "Nicolás", "Agustín", "Exequiel"],
        "last": ["González", "Rodríguez", "Fernández", "López", "Martínez", "Pérez", "Gómez",
                 "Sánchez", "Romero", "Sosa", "Álvarez", "Torres", "Ruiz", "Acosta"],
    },
    "DEFAULT": {
        "first": ["Alex", "Sam", "Chris", "Jordan", "Taylor", "Morgan", "Casey", "Riley"],
        "last": ["Smith", "Brown", "Lee", "Patel", "Kim", "Nguyen", "Garcia", "Müller"],
    },
}


def generate_newgen(conn: sqlite3.Connection, club: str, division_quality: float = 60,
                    nationality: str = None) -> dict:
    """Generate a single youth player (newgen).

    division_quality (0-100) affects the average CA of the generated player.
    Returns a dict with all fields needed to INSERT into players + player_attributes.
    """
    rng = random.Random()

    # nationality
    if nationality is None:
        # use club's nation
        club_row = conn.execute(
            "SELECT n.code FROM clubs c JOIN nations n ON c.nation_code = n.code WHERE c.name = ?",
            (club,)
        ).fetchone()
        nationality = club_row["code"] if club_row else "ENG"

    # name
    pool = NAME_POOLS.get(nationality, NAME_POOLS["DEFAULT"])
    name = f"{rng.choice(pool['first'])} {rng.choice(pool['last'])}"

    # age: 15-17
    age = rng.randint(15, 17)

    # position: random, weighted toward outfield
    positions = ["GK"] * 1 + ["D (C)"] * 4 + ["DM"] * 2 + ["M (C)"] * 4 + ["AM (C)"] * 2 + ["ST (C)"] * 3
    position = rng.choice(positions)

    # generate attributes: base around division_quality, with variance
    # youth players are weaker than seniors
    youth_penalty = 20  # youth players start ~20 below senior level
    base = max(20, division_quality - youth_penalty)

    from data.ingest import ATTR_COLUMNS, scale_attr
    attrs = {}
    uid = rng.randint(2000000000, 2999999999)  # newgen UIDs start with 2

    for attr in ATTR_COLUMNS:
        # each attribute: base ± 15
        val = max(1, min(20, rng.gauss(base / 5, 3)))  # convert back to 1-20 for scaling
        val = max(1, min(20, round(val)))
        attrs[attr] = scale_attr(val, uid, attr)

    # preferred foot
    foot = rng.choices(["Right", "Left", "Either"], weights=[60, 30, 10])[0]

    # height/weight: random realistic
    height_cm = round(rng.gauss(178, 8), 1)
    weight_kg = round(rng.gauss(73, 7), 1)

    # role family
    from data.ingest import ROLE_FAMILY, parse_position
    pos_list = parse_position(position)
    primary_role = pos_list[0][0] if pos_list else "M"
    primary_family = ROLE_FAMILY.get(primary_role, "MID")

    return {
        "uid": uid,
        "name": name,
        "age": age,
        "club": club,
        "nationality": nationality,
        "position": position,
        "primary_role": primary_role,
        "primary_family": primary_family,
        "preferred_foot": foot,
        "height_cm": height_cm,
        "weight_kg": weight_kg,
        "attrs": attrs,
        "transfer_value": rng.randint(100_000, 500_000),
    }


def generate_youth_intake(conn: sqlite3.Connection, club: str, season_id: str,
                          career_id: str, intake_year: int,
                          division_quality: float = 60) -> list[dict]:
    """Generate a youth intake for a club (3-8 newgens)."""
    rng = random.Random()
    n = rng.randint(3, 8)
    newgens = []
    now = dt.datetime.now().isoformat()

    for _ in range(n):
        ng = generate_newgen(conn, club, division_quality)
        # insert into players + player_attributes
        conn.execute(
            "INSERT INTO players (uid, name, age, club, nationality, position_raw, "
            "primary_role, primary_family, preferred_foot, height_cm, weight_kg, "
            "transfer_value, morale, inf) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 60, 'Ctr')",
            (ng["uid"], ng["name"], ng["age"], ng["club"], ng["nationality"],
             ng["position"], ng["primary_role"], ng["primary_family"],
             ng["preferred_foot"], ng["height_cm"], ng["weight_kg"],
             ng["transfer_value"]),
        )
        # insert attributes
        from data.ingest import ATTR_COLUMNS
        placeholders = ", ".join("?" for _ in ATTR_COLUMNS)
        col_names = ", ".join(f'"{c}"' for c in ATTR_COLUMNS)
        conn.execute(
            f'INSERT INTO player_attributes (uid, {col_names}) VALUES (?, {placeholders})',
            [ng["uid"]] + [ng["attrs"].get(c) for c in ATTR_COLUMNS],
        )
        # insert positions
        from data.ingest import ROLE_FAMILY, parse_position
        for role, side in parse_position(ng["position"]):
            family = ROLE_FAMILY.get(role, "MID")
            conn.execute(
                "INSERT OR IGNORE INTO player_positions (uid, role, side, family) VALUES (?, ?, ?, ?)",
                (ng["uid"], role, side, family),
            )
        # record in youth_intake
        conn.execute(
            "INSERT INTO youth_intake (career_id, season_id, player_uid, club, intake_year, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (career_id, season_id, ng["uid"], club, intake_year, now),
        )
        newgens.append(ng)

    conn.commit()
    return newgens


# ===========================================================================
# 3A.4: Player roles
# ===========================================================================

PLAYER_ROLES = {
    "GK": {
        "none": {},
        "sweeper_keeper": {
            "label": "Sweeper Keeper",
            "bonus": {"Ref": 5, "Kic": 10, "TRO": 10, "1v1": 5},
            "penalty": {"Pos": -5, "Cmd": -5},
        },
    },
    "DEF": {
        "none": {},
        "ball_playing_defender": {
            "label": "Ball-Playing Defender",
            "bonus": {"Pas": 10, "Tec": 8, "Vis": 8, "Cmp": 5},
            "penalty": {"Tck": -3, "Mar": -3},
        },
        "no_nonsense_defender": {
            "label": "No-Nonsense Defender",
            "bonus": {"Tck": 8, "Mar": 8, "Hea": 5, "Str": 5},
            "penalty": {"Pas": -8, "Tec": -5, "Vis": -5},
        },
    },
    "MID": {
        "none": {},
        "box_to_box": {
            "label": "Box-to-Box",
            "bonus": {"Sta": 10, "Wor": 8, "Tck": 5, "OtB": 5},
            "penalty": {"Pos": -3},
        },
        "deep_lying_playmaker": {
            "label": "Deep-Lying Playmaker",
            "bonus": {"Pas": 10, "Vis": 8, "Cmp": 8, "Tec": 5},
            "penalty": {"Tck": -5, "Sta": -3},
        },
        "ball_winning_mid": {
            "label": "Ball-Winning Midfielder",
            "bonus": {"Tck": 10, "Wor": 8, "Ant": 5, "Agg": 5},
            "penalty": {"Pas": -5, "Tec": -3, "Vis": -3},
        },
    },
    "ATT": {
        "none": {},
        "poacher": {
            "label": "Poacher",
            "bonus": {"Fin": 10, "Cmp": 8, "OtB": 5, "Acc": 5},
            "penalty": {"Wor": -8, "Tck": -5, "Pas": -3},
        },
        "target_man": {
            "label": "Target Man",
            "bonus": {"Hea": 10, "Str": 10, "Jum": 8, "Bal": 5},
            "penalty": {"Pac": -5, "Agi": -5, "Dri": -3},
        },
        "inside_forward": {
            "label": "Inside Forward",
            "bonus": {"Dri": 10, "Cmp": 5, "Fin": 5, "Agi": 5},
            "penalty": {"Cro": -5, "Wor": -3},
        },
    },
}


def get_roles_for_family(family: str) -> list[dict]:
    """Return available roles for a position family."""
    roles = PLAYER_ROLES.get(family, {})
    return [{"key": k, "label": v.get("label", k)} for k, v in roles.items()]


def apply_role_modifiers(attrs: dict, family: str, role: str) -> dict:
    """Return a copy of attrs with role bonuses/penalties applied."""
    if not role or role == "none":
        return attrs.copy()
    role_data = PLAYER_ROLES.get(family, {}).get(role, {})
    if not role_data:
        return attrs.copy()
    modified = attrs.copy()
    for attr, bonus in role_data.get("bonus", {}).items():
        if attr in modified and modified[attr] is not None:
            modified[attr] = max(0, min(100, modified[attr] + bonus))
    for attr, penalty in role_data.get("penalty", {}).items():
        if attr in modified and modified[attr] is not None:
            modified[attr] = max(0, min(100, modified[attr] + penalty))
    return modified


# ===========================================================================
# 3A.5: Team instructions
# ===========================================================================

TEAM_INSTRUCTIONS = {
    "pressing": {
        "low": {"label": "Low Pressing", "opponent_pass_bonus": 1.05, "fatigue_mult": 0.9},
        "standard": {"label": "Standard Pressing", "opponent_pass_bonus": 1.0, "fatigue_mult": 1.0},
        "high": {"label": "High Pressing", "opponent_pass_bonus": 0.90, "fatigue_mult": 1.15},
    },
    "tempo": {
        "slow": {"label": "Slow Tempo", "shot_mult": 0.9, "turnover_mult": 0.9},
        "standard": {"label": "Standard Tempo", "shot_mult": 1.0, "turnover_mult": 1.0},
        "fast": {"label": "Fast Tempo", "shot_mult": 1.15, "turnover_mult": 1.1},
    },
    "width": {
        "narrow": {"label": "Narrow", "cross_mult": 0.8, "through_ball_mult": 1.15},
        "standard": {"label": "Standard", "cross_mult": 1.0, "through_ball_mult": 1.0},
        "wide": {"label": "Wide", "cross_mult": 1.2, "through_ball_mult": 0.9},
    },
    "defensive_line": {
        "deep": {"label": "Deep Line", "pressure_mult": 1.15, "through_ball_risk_mult": 0.85},
        "standard": {"label": "Standard Line", "pressure_mult": 1.0, "through_ball_risk_mult": 1.0},
        "high": {"label": "High Line", "pressure_mult": 0.85, "through_ball_risk_mult": 1.15},
    },
}


def get_team_instructions_defaults() -> dict:
    return {
        "pressing": "standard",
        "tempo": "standard",
        "width": "standard",
        "defensive_line": "standard",
    }


# ===========================================================================
# 3B.1: Staff system
# ===========================================================================

STAFF_ROLES = {
    "assistant_manager": {
        "label": "Assistant Manager",
        "effects": {"training_efficiency": 0.15, "lineup_suggestion": True},
        "wage_range": (80_000, 200_000),
    },
    "coach_def": {
        "label": "Defensive Coach",
        "effects": {"def_training_bonus": 0.20},
        "wage_range": (40_000, 100_000),
    },
    "coach_mid": {
        "label": "Midfield Coach",
        "effects": {"mid_training_bonus": 0.20},
        "wage_range": (40_000, 100_000),
    },
    "coach_att": {
        "label": "Attacking Coach",
        "effects": {"att_training_bonus": 0.20},
        "wage_range": (40_000, 100_000),
    },
    "coach_gk": {
        "label": "Goalkeeping Coach",
        "effects": {"gk_training_bonus": 0.20},
        "wage_range": (40_000, 100_000),
    },
    "scout": {
        "label": "Scout",
        "effects": {"scout_accuracy": 20},
        "wage_range": (30_000, 80_000),
    },
    "physio": {
        "label": "Physio",
        "effects": {"injury_reduction": 0.15, "recovery_bonus": 0.10},
        "wage_range": (35_000, 90_000),
    },
    "sports_scientist": {
        "label": "Sports Scientist",
        "effects": {"fatigue_reduction": 0.15},
        "wage_range": (35_000, 90_000),
    },
}

STAFF_NAME_POOL = {
    "first": ["Rob", "Steve", "Mike", "Dave", "Paul", "Chris", "Mark", "Neil", "Andy", "Kevin",
              "Tony", "Graham", "Stuart", "Phil", "Ian", "Martin", "Peter", "David"],
    "last": ["Wilson", "Taylor", "Anderson", "Thompson", "Walker", "White", "Roberts",
             "Clarke", "Hughes", "Murphy", "Carter", "Phillips", "Evans", "Stewart"],
}


def generate_staff_candidate(role: str, rng: random.Random = None) -> dict:
    """Generate a random staff candidate for hiring."""
    if rng is None:
        rng = random.Random()
    role_data = STAFF_ROLES.get(role, {})
    name = f"{rng.choice(STAFF_NAME_POOL['first'])} {rng.choice(STAFF_NAME_POOL['last'])}"
    rating = rng.randint(40, 95)
    wage_min, wage_max = role_data.get("wage_range", (30_000, 80_000))
    wage = int(rng.uniform(wage_min, wage_max) * (0.5 + rating / 100.0))
    return {
        "name": name, "role": role, "rating": rating, "wage": wage,
        "label": role_data.get("label", role),
    }


def generate_staff_candidates(n: int = 5) -> list[dict]:
    """Generate n random staff candidates across all roles."""
    rng = random.Random()
    candidates = []
    roles = list(STAFF_ROLES.keys())
    for _ in range(n):
        role = rng.choice(roles)
        candidates.append(generate_staff_candidate(role, rng))
    return candidates


def hire_staff(conn: sqlite3.Connection, career_id: str, name: str, role: str,
               rating: int, wage: int) -> int:
    """Hire a staff member. Returns the staff ID."""
    now = dt.datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO staff (career_id, name, role, rating, wage, hired_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (career_id, name, role, rating, wage, now),
    )
    conn.commit()
    return cur.lastrowid


def fire_staff(conn: sqlite3.Connection, staff_id: int) -> None:
    """Fire a staff member."""
    conn.execute("DELETE FROM staff WHERE id = ?", (staff_id,))
    conn.commit()


def get_club_staff(conn: sqlite3.Connection, career_id: str) -> list[dict]:
    """Return all staff hired by a career."""
    cur = conn.execute(
        "SELECT * FROM staff WHERE career_id = ? ORDER BY rating DESC",
        (career_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def get_staff_bonus(conn: sqlite3.Connection, career_id: str, family: str = None) -> float:
    """Compute the training bonus from staff for a given family (or overall)."""
    staff = get_club_staff(conn, career_id)
    bonus = 1.0
    for s in staff:
        role_data = STAFF_ROLES.get(s["role"], {})
        effects = role_data.get("effects", {})
        # assistant manager gives flat bonus
        if "training_efficiency" in effects:
            bonus += effects["training_efficiency"] * (s["rating"] / 100.0)
        # family-specific coaches
        if family:
            key = f"{family.lower()}_training_bonus"
            if key in effects:
                bonus += effects[key] * (s["rating"] / 100.0)
    return bonus


# ===========================================================================
# 3B.2: Training system
# ===========================================================================

TRAINING_FOCI = {
    "general": "General (balanced)",
    "attacking": "Attacking (boost ATT/MID, slight DEF decline)",
    "defending": "Defending (boost DEF/GK, slight ATT decline)",
    "fitness": "Fitness (boost physical, slight technical decline)",
    "technical": "Technical (boost technical, slight physical decline)",
}

TRAINING_INTENSITIES = {
    "light": "Light (slower development, low injury risk)",
    "standard": "Standard (balanced)",
    "intensive": "Intensive (faster development, higher injury risk + fatigue)",
}


# ===========================================================================
# 3B.3: Finances
# ===========================================================================

def compute_player_wage(attrs: dict, age: int, ca: float) -> int:
    """Compute a player's weekly wage based on CA + age."""
    # base: CA * 1000
    base = ca * 1000
    # age factor: peak earners are 24-30
    if 24 <= age <= 30:
        age_mult = 1.0
    elif age < 24:
        age_mult = 0.6 + (age - 16) * 0.05  # young players earn less
    else:
        age_mult = max(0.4, 1.0 - (age - 30) * 0.08)  # decline
    return int(base * age_mult)


def compute_club_wage_bill(conn: sqlite3.Connection, club: str) -> int:
    """Compute total weekly wage bill for a club."""
    from engine.lineup import load_club_squad
    from engine.attributes import ca_for_family
    squad = load_club_squad(conn, club)
    total = 0
    for p in squad:
        ca = p["ca"].get(p["primary_family"], 50) if p.get("ca") else 50
        wage = compute_player_wage(p["attrs"], p["age"] or 25, ca)
        total += wage
    return total


def compute_matchday_income(conn: sqlite3.Connection, club: str, is_home: bool) -> int:
    """Compute matchday income for a single match."""
    if not is_home:
        return 0
    # base attendance by division quality
    club_row = conn.execute(
        "SELECT c.player_count, d.player_count as div_player_count FROM clubs c "
        "LEFT JOIN divisions d ON c.division_id = d.id WHERE c.name = ?",
        (club,)
    ).fetchone()
    if club_row:
        # use division player count as proxy for league quality
        div_quality = min(100, (club_row["div_player_count"] or 500) // 20)
        attendance = 10000 + div_quality * 500  # 10k-60k
        ticket_price = 40  # average
        return attendance * ticket_price
    return 200_000  # default


def compute_tv_money(conn: sqlite3.Connection, club: str) -> int:
    """Compute seasonal TV money."""
    club_row = conn.execute(
        "SELECT d.player_count FROM clubs c "
        "LEFT JOIN divisions d ON c.division_id = d.id WHERE c.name = ?",
        (club,)
    ).fetchone()
    if club_row:
        div_quality = min(100, (club_row["player_count"] or 500) // 20)
        return 5_000_000 + div_quality * 2_000_000  # 5M-205M
    return 10_000_000


def compute_prize_money(position: int, total_clubs: int) -> int:
    """Compute prize money based on final league position."""
    # top position gets most, linear decline
    base = 50_000_000  # winner
    decline = base / total_clubs
    return int(base - (position - 1) * decline)


def compute_finances(conn: sqlite3.Connection, career_id: str, season_id: str,
                     club: str, final_position: int = None,
                     total_clubs: int = 20) -> dict:
    """Compute a season's finances for a club."""
    # income
    tv_money = compute_tv_money(conn, club)
    # matchday: ~half home matches
    matchday_income = compute_matchday_income(conn, club, True) * (total_clubs - 1)
    prize = 0
    if final_position:
        prize = compute_prize_money(final_position, total_clubs)

    # expenses
    wage_bill = compute_club_wage_bill(conn, club) * 38  # weekly * 38 rounds
    staff_wages = sum(s["wage"] for s in get_club_staff(conn, career_id)) * 38

    total_income = tv_money + matchday_income + prize
    total_expenses = wage_bill + staff_wages
    balance = total_income - total_expenses

    return {
        "income_tv": tv_money,
        "income_matchday": matchday_income,
        "income_prize": prize,
        "income_total": total_income,
        "expenses_wages": wage_bill,
        "expenses_staff": staff_wages,
        "expenses_total": total_expenses,
        "balance": balance,
    }


# ===========================================================================
# 3B.4: Board expectations
# ===========================================================================

def set_board_expectation(conn: sqlite3.Connection, career_id: str, season_id: str,
                          club: str, total_clubs: int) -> dict:
    """Set board expectations for the season based on club strength."""
    from engine.lineup import load_club_squad, pick_best_xi, starting_xi_strength
    squad = load_club_squad(conn, club)
    xi, _ = pick_best_xi(squad, conn, formation="4-3-3")
    strength = starting_xi_strength(xi)["OVERALL"]

    # determine expectation based on strength
    if strength >= 80:
        expectation = "Win the league"
        target = 1
    elif strength >= 75:
        expectation = "Finish in the top 3"
        target = 3
    elif strength >= 70:
        expectation = "Finish in the top half"
        target = total_clubs // 2
    else:
        expectation = "Avoid relegation"
        target = total_clubs - 3

    now = dt.datetime.now().isoformat()
    conn.execute(
        "INSERT INTO board_expectations (career_id, season_id, expectation, "
        "target_position, current_confidence, created_at) "
        "VALUES (?, ?, ?, ?, 75, ?)",
        (career_id, season_id, expectation, target, now),
    )
    conn.commit()
    return {"expectation": expectation, "target_position": target}


def get_board_confidence(conn: sqlite3.Connection, career_id: str,
                         season_id: str) -> int:
    """Get current board confidence (0-100)."""
    row = conn.execute(
        "SELECT current_confidence FROM board_expectations "
        "WHERE career_id = ? AND season_id = ? ORDER BY id DESC LIMIT 1",
        (career_id, season_id),
    ).fetchone()
    return row["current_confidence"] if row else 75


def update_board_confidence(conn: sqlite3.Connection, career_id: str,
                            season_id: str, delta: int, reason: str = None) -> int:
    """Adjust board confidence by delta. Returns new value."""
    row = conn.execute(
        "SELECT id, current_confidence FROM board_expectations "
        "WHERE career_id = ? AND season_id = ? ORDER BY id DESC LIMIT 1",
        (career_id, season_id),
    ).fetchone()
    if row:
        new_val = max(0, min(100, row["current_confidence"] + delta))
        conn.execute(
            "UPDATE board_expectations SET current_confidence = ? WHERE id = ?",
            (new_val, row["id"]),
        )
        conn.commit()
        return new_val
    return 75


# ===========================================================================
# 3B.5: Scouting
# ===========================================================================

def scout_player(conn: sqlite3.Connection, career_id: str, player_uid: int,
                 scout_rating: int = 50) -> dict:
    """Scout a player, creating a scouting report.

    Higher scout rating = more accurate attribute visibility.
    Returns the scouting report dict.
    """
    now = dt.datetime.now().isoformat()
    # accuracy: scout rating determines how close the shown values are to real
    # accuracy 90 = ±3, accuracy 50 = ±15, accuracy 10 = ±25
    accuracy = max(5, min(95, scout_rating))

    conn.execute(
        "INSERT INTO scouting_reports (career_id, player_uid, scout_id, accuracy, "
        "scouted_at, expires_at) VALUES (?, ?, NULL, ?, ?, ?)",
        (career_id, player_uid, accuracy, now,
         (dt.datetime.now() + dt.timedelta(days=90)).isoformat()),
    )
    conn.commit()
    return {"player_uid": player_uid, "accuracy": accuracy, "scouted_at": now}


def is_player_scouted(conn: sqlite3.Connection, career_id: str, player_uid: int) -> bool:
    """Check if a player has been scouted by this career."""
    row = conn.execute(
        "SELECT 1 FROM scouting_reports WHERE career_id = ? AND player_uid = ? "
        "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
        (career_id, player_uid, dt.datetime.now().isoformat()),
    ).fetchone()
    return row is not None


def get_scouting_accuracy(conn: sqlite3.Connection, career_id: str,
                          player_uid: int) -> int | None:
    """Get the scouting accuracy for a player (None = unscouted)."""
    row = conn.execute(
        "SELECT accuracy FROM scouting_reports WHERE career_id = ? AND player_uid = ? "
        "AND (expires_at IS NULL OR expires_at > ?) ORDER BY id DESC LIMIT 1",
        (career_id, player_uid, dt.datetime.now().isoformat()),
    ).fetchone()
    return row["accuracy"] if row else None


def get_scouted_players(conn: sqlite3.Connection, career_id: str) -> list[dict]:
    """Return all players scouted by this career."""
    cur = conn.execute(
        "SELECT sr.player_uid, sr.accuracy, sr.scouted_at, p.name, p.club, p.age, "
        "p.position_raw, p.primary_family, p.transfer_value "
        "FROM scouting_reports sr JOIN players p ON sr.player_uid = p.uid "
        "WHERE sr.career_id = ? AND (sr.expires_at IS NULL OR sr.expires_at > ?) "
        "ORDER BY sr.scouted_at DESC",
        (career_id, dt.datetime.now().isoformat()),
    )
    return [dict(r) for r in cur.fetchall()]


# ===========================================================================
# 3C.1: Press conferences
# ===========================================================================

PRESS_QUESTIONS = [
    {
        "id": "match_preview",
        "question": "How do you feel about the upcoming match against {opponent}?",
        "options": [
            {"text": "We're confident and expect to win", "morale": +5, "confidence": +3,
             "risk": "Adds pressure — morale -10 if you lose"},
            {"text": "It'll be a tough game, but we're prepared", "morale": +2, "confidence": 0,
             "risk": "Balanced response"},
            {"text": "We're the underdogs, let's see what happens", "morale": -2, "confidence": -2,
             "risk": "Lowers expectations but hurts morale"},
        ],
    },
    {
        "id": "recent_form",
        "question": "Your team has {form_description}. How do you explain this?",
        "options": [
            {"text": "The players have been excellent, full credit to them", "morale": +5, "confidence": +2,
             "risk": "Boosts morale regardless of form"},
            {"text": "We need to work harder in training", "morale": -3, "confidence": 0,
             "risk": "Honest but hurts morale"},
            {"text": "The referee decisions have gone against us", "morale": +1, "confidence": -3,
             "risk": "Deflects blame, media may criticize"},
        ],
    },
    {
        "id": "title_race",
        "question": "Are you still in the title race?",
        "options": [
            {"text": "Absolutely, we're fighting for the title", "morale": +3, "confidence": +5,
             "risk": "High expectations — big confidence hit if you fail"},
            {"text": "We're taking it one game at a time", "morale": 0, "confidence": 0,
             "risk": "Safe, neutral response"},
            {"text": "Our target is just a top-half finish", "morale": -2, "confidence": -3,
             "risk": "Lowers expectations"},
        ],
    },
    {
        "id": "injury_crisis",
        "question": "You have several key players injured. How will you cope?",
        "options": [
            {"text": "It's a chance for squad players to step up", "morale": +3, "confidence": +1,
             "risk": "Positive spin, boosts fringe players"},
            {"text": "It's going to be very difficult without them", "morale": -2, "confidence": -2,
             "risk": "Honest but lowers morale"},
            {"text": "We'll adjust our tactics to suit the available players", "morale": +1, "confidence": 0,
             "risk": "Tactical, neutral"},
        ],
    },
    {
        "id": "transfer_window",
        "question": "Will you be active in the transfer window?",
        "options": [
            {"text": "Yes, we're looking to strengthen the squad", "morale": +2, "confidence": +2,
             "risk": "Raises expectations for signings"},
            {"text": "We're happy with what we have", "morale": +3, "confidence": 0,
             "risk": "Boosts current squad morale"},
            {"text": "That depends on the board's budget", "morale": 0, "confidence": -2,
             "risk": "Deflects to board, may annoy them"},
        ],
    },
]


def generate_press_conference(career_id: str, season_id: str, round_num: int,
                              home_club: str, away_club: str,
                              rng: random.Random = None) -> dict:
    """Generate a press conference with 3 random questions."""
    if rng is None:
        rng = random.Random()
    # pick 3 random questions
    questions = rng.sample(PRESS_QUESTIONS, min(3, len(PRESS_QUESTIONS)))
    # fill in templates
    opponent = away_club  # simplified
    for q in questions:
        q["question_filled"] = q["question"].format(
            opponent=opponent,
            form_description=rng.choice(["won the last 3", "lost the last 2", "drawn the last 3",
                                          "been inconsistent recently"]),
        )
    return {
        "career_id": career_id, "season_id": season_id, "round": round_num,
        "home_club": home_club, "away_club": away_club,
        "questions": questions,
    }


def save_press_conference(conn: sqlite3.Connection, pc: dict, answers: list[int]) -> dict:
    """Save a completed press conference. answers is a list of option indices."""
    total_morale = 0
    total_confidence = 0
    answer_texts = []
    for i, q in enumerate(pc["questions"]):
        if i < len(answers):
            opt_idx = answers[i]
            opt = q["options"][opt_idx]
            total_morale += opt["morale"]
            total_confidence += opt["confidence"]
            answer_texts.append(opt["text"])

    now = dt.datetime.now().isoformat()
    conn.execute(
        "INSERT INTO press_conferences (career_id, season_id, round, match_home, match_away, "
        "questions_json, answers_json, morale_effect, confidence_effect, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pc["career_id"], pc["season_id"], pc["round"], pc["home_club"], pc["away_club"],
         json.dumps([{"question": q["question_filled"], "options": q["options"]}
                    for q in pc["questions"]]),
         json.dumps(answer_texts), total_morale, total_confidence, now),
    )
    conn.commit()
    return {"morale_effect": total_morale, "confidence_effect": total_confidence}


# ===========================================================================
# 3C.2: Player concerns
# ===========================================================================

def check_player_concerns(conn: sqlite3.Connection, career_id: str, season_id: str,
                          club: str, round_num: int) -> list[dict]:
    """Check for new player concerns based on recent matches."""
    from engine.lineup import load_club_squad, pick_best_xi
    squad = load_club_squad(conn, club)
    xi, _ = pick_best_xi(squad, conn, formation="4-3-3")
    xi_uids = {p["uid"] for p in xi if p.get("uid")}

    # get last 5 matches
    matches = conn.execute(
        "SELECT * FROM matches WHERE season_id = ? AND (home_club = ? OR away_club = ?) "
        "ORDER BY round DESC LIMIT 5",
        (season_id, club, club),
    ).fetchall()

    if len(matches) < 3:
        return []

    # check which players haven't started recently
    new_concerns = []
    now = dt.datetime.now().isoformat()
    for p in squad:
        if not p.get("uid") or p["uid"] in xi_uids:
            continue  # currently starting, no concern
        # check if this player has a concern already
        existing = conn.execute(
            "SELECT 1 FROM player_concerns WHERE career_id = ? AND player_uid = ? "
            "AND resolved = 0 LIMIT 1",
            (career_id, p["uid"]),
        ).fetchone()
        if existing:
            continue

        # check morale
        morale = conn.execute(
            "SELECT morale FROM players WHERE uid = ?", (p["uid"],)
        ).fetchone()
        morale_val = morale["morale"] if morale else 50

        # generate concern
        rng = random.Random(p["uid"] + round_num)
        attrs = p.get("attrs", {})
        amb = attrs.get("Amb") or 50

        if morale_val < 30:
            concern_type = "unhappy"
            concern_text = f"{p['name']} is unhappy with their situation at the club."
        elif amb > 70 and p["age"] < 28:
            concern_type = "ambition"
            concern_text = f"{p['name']} wants to move to a bigger club to fulfill their ambitions."
        else:
            concern_type = "playing_time"
            concern_text = f"{p['name']} is frustrated by lack of first-team opportunities."

        conn.execute(
            "INSERT INTO player_concerns (career_id, season_id, player_uid, player_name, "
            "concern_type, concern_text, rounds_active, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)",
            (career_id, season_id, p["uid"], p["name"],
             concern_type, concern_text, now),
        )
        new_concerns.append({
            "player_uid": p["uid"], "player_name": p["name"],
            "concern_type": concern_type, "concern_text": concern_text,
        })

    conn.commit()
    return new_concerns


def resolve_concern(conn: sqlite3.Connection, concern_id: int, resolution: str) -> None:
    """Resolve a player concern with a given resolution."""
    conn.execute(
        "UPDATE player_concerns SET resolved = 1, resolution = ? WHERE id = ?",
        (resolution, concern_id),
    )
    conn.commit()


def get_active_concerns(conn: sqlite3.Connection, career_id: str) -> list[dict]:
    """Return all unresolved concerns for a career."""
    cur = conn.execute(
        "SELECT * FROM player_concerns WHERE career_id = ? AND resolved = 0 "
        "ORDER BY created_at DESC",
        (career_id,),
    )
    return [dict(r) for r in cur.fetchall()]


# ===========================================================================
# 3C.3: Player happiness (morale + happiness + form)
# ===========================================================================

def get_player_happiness(conn: sqlite3.Connection, uid: int) -> dict:
    """Get a player's happiness indicators: morale, happiness, form."""
    player = conn.execute("SELECT morale FROM players WHERE uid = ?", (uid,)).fetchone()
    morale = player["morale"] if player else 50
    # happiness: derived from morale + recent playing time + ambition satisfaction
    # for now, use morale as base + a small random factor (deterministic by uid)
    rng = random.Random(uid)
    happiness = max(0, min(100, morale + rng.randint(-10, 10)))
    # form: derived from recent match involvements (simplified)
    form = rng.randint(40, 80)  # placeholder — will be computed from match data
    return {"morale": morale, "happiness": happiness, "form": form}


def happiness_label(value: int) -> str:
    """Convert a 0-100 happiness/morale value to a label."""
    if value >= 75:
        return "Happy"
    elif value >= 55:
        return "Content"
    elif value >= 35:
        return "Uneasy"
    elif value >= 20:
        return "Frustrated"
    else:
        return "Angry"


# ===========================================================================
# 3C.4: Fan confidence
# ===========================================================================

def get_fan_confidence(conn: sqlite3.Connection, career_id: str,
                       season_id: str) -> int:
    """Get fan confidence (0-100). Stored in board_expectations as a separate field,
    but for simplicity we compute it from recent results."""
    # for now, derive from recent match results
    matches = conn.execute(
        "SELECT * FROM matches WHERE season_id = ? ORDER BY round DESC LIMIT 5",
        (season_id,),
    ).fetchall()
    if not matches:
        return 70
    # count wins/losses
    wins = losses = 0
    for m in matches:
        # we don't know which club is the user's here, so use a simple heuristic
        # this is a placeholder — in practice we'd pass the user's club
        if m["home_score"] > m["away_score"]:
            wins += 1
        elif m["home_score"] < m["away_score"]:
            losses += 1
    return max(0, min(100, 50 + wins * 10 - losses * 10))


# ===========================================================================
# 3C.5: News feed
# ===========================================================================

def add_news(conn: sqlite3.Connection, career_id: str, season_id: str, round_num: int,
             category: str, headline: str, body: str) -> None:
    """Add a news item to the career's news feed."""
    now = dt.datetime.now().isoformat()
    conn.execute(
        "INSERT INTO news_items (career_id, season_id, round, category, headline, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (career_id, season_id, round_num, category, headline, body, now),
    )
    conn.commit()


def get_news(conn: sqlite3.Connection, career_id: str, limit: int = 20) -> list[dict]:
    """Return recent news items for a career."""
    cur = conn.execute(
        "SELECT * FROM news_items WHERE career_id = ? ORDER BY id DESC LIMIT ?",
        (career_id, limit),
    )
    return [dict(r) for r in cur.fetchall()]


def generate_match_news(conn: sqlite3.Connection, career_id: str, season_id: str,
                        round_num: int, result, user_club: str) -> None:
    """Generate news items based on a match result."""
    home, away = result.home_club, result.away_club
    hs, as_ = result.home_score, result.away_score

    # determine user's involvement
    if user_club == home:
        user_goals, opp_goals = hs, as_
        opponent = away
        is_home = True
    elif user_club == away:
        user_goals, opp_goals = as_, hs
        opponent = home
        is_home = False
    else:
        return  # user's club not in this match

    # generate headline based on result
    if user_goals > opp_goals:
        if user_goals - opp_goals >= 3:
            headline = f"{user_club} thrash {opponent} {user_goals}-{opp_goals}!"
            body = f"{user_club} put in a dominant performance, beating {opponent} by {user_goals - opp_goals} goals."
        else:
            headline = f"{user_club} beat {opponent} {user_goals}-{opp_goals}"
            body = f"A {'home' if is_home else 'away'} win for {user_club} against {opponent}."
        category = "result_win"
    elif user_goals < opp_goals:
        if opp_goals - user_goals >= 3:
            headline = f"{user_club} humiliated by {opponent} {user_goals}-{opp_goals}"
            body = f"A disastrous {'home' if is_home else 'away'} defeat for {user_club}."
        else:
            headline = f"{user_club} lose {user_goals}-{opp_goals} to {opponent}"
            body = f"{user_club} fell to defeat against {opponent}."
        category = "result_loss"
    else:
        headline = f"{user_club} draw {user_goals}-{opp_goals} with {opponent}"
        body = f"A share of the points for {user_club} and {opponent}."
        category = "result_draw"

    add_news(conn, career_id, season_id, round_num, category, headline, body)


# ===========================================================================
# 3C.5: AI transfer activity
# ===========================================================================

def simulate_ai_transfers(conn: sqlite3.Connection, career_id: str, season_id: str,
                          all_clubs: list[str], round_num: int) -> list[dict]:
    """Simulate AI clubs making transfers between themselves.

    Returns a list of completed transfers for news generation.
    """
    rng = random.Random(hash(season_id) + round_num + 7777)
    # each window, 1-3 AI transfers happen
    n_transfers = rng.randint(0, 3)
    completed = []

    for _ in range(n_transfers):
        if len(all_clubs) < 2:
            break
        buying, selling = rng.sample(all_clubs, 2)
        # skip if same club
        if buying == selling:
            continue

        # pick a random player from selling club
        players = conn.execute(
            "SELECT uid, name, transfer_value, primary_family FROM players WHERE club = ? "
            "ORDER BY RANDOM() LIMIT 1",
            (selling,),
        ).fetchall()
        if not players:
            continue
        player = players[0]
        value = player["transfer_value"] or 500_000
        if value <= 0:
            value = 500_000

        # AI accepts if bid >= value
        bid = int(value * rng.uniform(0.9, 1.3))
        # 70% chance of acceptance
        if rng.random() < 0.70:
            now = dt.datetime.now().isoformat()
            conn.execute("UPDATE players SET club = ? WHERE uid = ?",
                        (buying, player["uid"]))
            conn.execute(
                "INSERT INTO transfers (career_id, season_id, player_uid, player_name, "
                "from_club, to_club, fee, transfer_window, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (career_id, season_id, player["uid"], player["name"],
                 selling, buying, bid, "ai", now),
            )
            completed.append({
                "player": player["name"], "from": selling, "to": buying, "fee": bid,
            })
            # add news
            add_news(conn, career_id, season_id, round_num, "transfer",
                     f"{player['name']} moves to {buying}",
                     f"{buying} have signed {player['name']} from {selling} for ${bid:,}.")

    conn.commit()
    return completed
