"""
CSV -> SQLite ingestion for the Football Management Simulation (Phase 2+).

Features:
- Idempotent: drops & recreates tables on each run (safe to re-run).
- Multi-part ready: accepts a list of CSV paths, merges & de-duplicates by UID.
- Normalized schema: nations, divisions, clubs, players, player_attributes,
  player_positions — proper foreign keys and indexes.
- Parses the `Based` field (e.g. "England (Premier Division)") into a
  (nation, division) pair, building the full Nation -> Division -> Club
  hierarchy that the UI browses.
- Rescales all 1-20 FM attributes to 0-100 using ×5 base + deterministic
  ±3 noise (seeded by UID + attribute name, so re-ingests are stable).
- Foot strength (1-5) is rescaled to 0-100 the same way (×20 base + noise).
- Marks each (nation, division) as "playable" if it has >=4 clubs with
  >=14 players each (enough for a meaningful league with subs).

Run directly:  python data/ingest.py
Or import:     from data.ingest import ingest
"""
from __future__ import annotations

import csv
import hashlib
import os
import random
import re
import sqlite3
from datetime import date
from typing import Iterable

# ---------------------------------------------------------------------------
# Path conventions
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(HERE, "football.db")
DEFAULT_CSV_DIR = HERE

# In-game "today" used to compute age from DOB.
IN_GAME_TODAY = date(2024, 8, 15)

# Playable league thresholds
MIN_CLUBS_PER_LEAGUE = 4
MIN_PLAYERS_PER_CLUB = 14

# ---------------------------------------------------------------------------
# Categorical mappings
# ---------------------------------------------------------------------------
FOOT_STRENGTH = {
    "Weak": 1,
    "Reasonable": 2,
    "Fairly Strong": 3,
    "Strong": 4,
    "Very Strong": 5,
}

# Position role groups -- FM short codes -> canonical role family.
ROLE_FAMILY = {
    "GK": "GK",
    "D":  "DEF",
    "WB": "DEF",
    "DM": "MID",
    "M":  "MID",
    "AM": "MID",
    "ST": "ATT",
    "W":  "ATT",
}

# ---------------------------------------------------------------------------
# Attribute scaling (1-20 -> 0-100)
# ---------------------------------------------------------------------------
ATTR_SCALE_BASE = 5        # 1->5, 20->100
ATTR_SCALE_NOISE = 3       # ±3 deterministic noise


def _deterministic_noise(uid: int, attr_name: str) -> int:
    """Return a deterministic ±ATTR_SCALE_NOISE integer, seeded by (uid, attr).

    Uses SHA-256 of (uid, attr_name) so the same player always gets the same
    noise on the same attribute -- re-ingests are stable.
    """
    h = hashlib.sha256(f"{uid}|{attr_name}".encode("utf-8")).digest()
    # use first 4 bytes as a uint32, then map to [-NOISE, +NOISE]
    val = int.from_bytes(h[:4], "big")
    return (val % (2 * ATTR_SCALE_NOISE + 1)) - ATTR_SCALE_NOISE


def scale_attr(value: int | None, uid: int, attr_name: str) -> int | None:
    """Scale a 1-20 attribute to 0-100 with deterministic noise.

    1 -> ~5, 10 -> ~50, 20 -> ~100, each ±3 noise, clamped to [0, 100].
    None stays None.
    """
    if value is None:
        return None
    base = value * ATTR_SCALE_BASE
    noise = _deterministic_noise(uid, attr_name)
    return max(0, min(100, base + noise))


def scale_foot(value: int | None, uid: int, side: str) -> int | None:
    """Scale 1-5 foot strength to 0-100 with deterministic noise."""
    if value is None:
        return None
    base = value * 20  # 1->20, 5->100
    noise = _deterministic_noise(uid, f"foot_{side}")
    return max(0, min(100, base + noise))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_DOB_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")


def parse_dob(raw: str) -> date | None:
    if not raw:
        return None
    m = _DOB_RE.match(raw.strip())
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def age_from_dob(dob: date | None, today: date = IN_GAME_TODAY) -> int | None:
    if dob is None:
        return None
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years


_HEIGHT_RE = re.compile(r"(?:(\d+)['\u2019])\s*(?:(\d+))?\s*\"?")


def parse_height_cm(raw: str) -> float | None:
    if not raw:
        return None
    s = raw.strip().replace("\u201d", '"').replace("\u2019", "'")
    m = _HEIGHT_RE.match(s)
    if not m:
        return None
    ft = int(m.group(1) or 0)
    inch = int(m.group(2) or 0)
    total_in = ft * 12 + inch
    return round(total_in * 2.54, 1)


_WEIGHT_RE = re.compile(r"([\d.]+)\s*kg", re.IGNORECASE)


def parse_weight_kg(raw: str) -> float | None:
    if not raw:
        return None
    m = _WEIGHT_RE.search(raw)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_transfer_value(raw: str) -> int:
    if not raw:
        return 0
    s = raw.strip()
    if "Not for Sale" in s:
        return -1
    m = re.search(r"\$?\s*([\d.]+)\s*([KMB]?)", s, re.IGNORECASE)
    if not m:
        return 0
    base = float(m.group(1))
    unit = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[unit]
    return int(base * mult)


_POSITION_RE = re.compile(r"^\s*([A-Z/]+)\s*(?:\(\s*([LCR]+)\s*\))?\s*$")


def parse_position(raw: str) -> list[tuple[str, str]]:
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _POSITION_RE.match(chunk)
        if not m:
            continue
        roles = m.group(1).split("/")
        sides_str = m.group(2) or "C"
        sides = [c for c in sides_str if c in "LCR"] or ["C"]
        for r in roles:
            r = r.strip().upper()
            if not r:
                continue
            for s in sides:
                out.append((r, s))
    seen = set()
    deduped = []
    for pair in out:
        if pair not in seen:
            seen.add(pair)
            deduped.append(pair)
    return deduped


def parse_int(raw: str) -> int | None:
    if raw is None or raw == "" or raw == "-":
        return None
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


# Parse "Based" field: "England (Premier Division)" -> ("England", "Premier Division")
_BASED_RE = re.compile(r"^(.+?)(?:\s*\((.+)\))?$")


def parse_based(raw: str) -> tuple[str | None, str | None]:
    """Return (nation, division) from a Based string like 'England (Premier Division)'."""
    if not raw:
        return (None, None)
    m = _BASED_RE.match(raw.strip())
    if not m:
        return (raw.strip(), None)
    nation = m.group(1).strip() or None
    division = m.group(2).strip() if m.group(2) else None
    return (nation, division)


# ---------------------------------------------------------------------------
# Schema (Phase 2+ — normalized with nations/divisions/clubs)
# ---------------------------------------------------------------------------

NATIONS_DDL = """
CREATE TABLE nations (
    code TEXT PRIMARY KEY,          -- 3-letter code where known, else slug
    name TEXT NOT NULL UNIQUE,
    player_count INTEGER DEFAULT 0
);
"""

DIVISIONS_DDL = """
CREATE TABLE divisions (
    id INTEGER PRIMARY KEY,
    nation_code TEXT NOT NULL REFERENCES nations(code),
    name TEXT NOT NULL,
    based_raw TEXT NOT NULL,
    player_count INTEGER DEFAULT 0,
    club_count INTEGER DEFAULT 0,
    playable INTEGER DEFAULT 0,
    UNIQUE(nation_code, name)
)
"""

DIVISIONS_INDEXES = [
    "CREATE INDEX idx_divisions_nation ON divisions(nation_code);",
    "CREATE INDEX idx_divisions_playable ON divisions(playable);",
]

CLUBS_DDL = """
CREATE TABLE clubs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    nation_code TEXT REFERENCES nations(code),
    division_id INTEGER REFERENCES divisions(id),
    player_count INTEGER DEFAULT 0,
    based_raw TEXT,
    UNIQUE(name, based_raw)
)
"""

CLUBS_INDEXES = [
    "CREATE INDEX idx_clubs_division ON clubs(division_id);",
    "CREATE INDEX idx_clubs_nation ON clubs(nation_code);",
]

PLAYERS_DDL = """
CREATE TABLE players (
    uid              INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    dob              TEXT,
    age              INTEGER,
    inf              TEXT,
    club             TEXT,
    club_id          INTEGER REFERENCES clubs(id),
    division_id      INTEGER REFERENCES divisions(id),
    nation_code      TEXT REFERENCES nations(code),
    nationality      TEXT,
    based            TEXT,
    height_cm        REAL,
    weight_kg        REAL,
    position_raw     TEXT,
    primary_role     TEXT,
    primary_family   TEXT,
    transfer_value   INTEGER,
    preferred_foot   TEXT,
    left_foot        INTEGER,
    right_foot       INTEGER,
    rc_injury        TEXT,
    media_handling   TEXT,
    morale           INTEGER DEFAULT 50
)
"""

PLAYERS_INDEXES = [
    "CREATE INDEX idx_players_club ON players(club);",
    "CREATE INDEX idx_players_club_id ON players(club_id);",
    "CREATE INDEX idx_players_division ON players(division_id);",
    "CREATE INDEX idx_players_nation ON players(nation_code);",
    "CREATE INDEX idx_players_primary_family ON players(primary_family);",
]

ATTRS_DDL_TEMPLATE = """
CREATE TABLE player_attributes (
    uid INTEGER PRIMARY KEY REFERENCES players(uid) ON DELETE CASCADE,
    {cols}
);
"""

POSITIONS_DDL = """
CREATE TABLE player_positions (
    uid   INTEGER NOT NULL REFERENCES players(uid) ON DELETE CASCADE,
    role  TEXT NOT NULL,
    side  TEXT NOT NULL,
    family TEXT NOT NULL,
    PRIMARY KEY (uid, role, side)
)
"""

POSITIONS_INDEX = "CREATE INDEX idx_player_positions_family ON player_positions(family);"

PERSIST_DDL = """
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    home_club TEXT NOT NULL,
    away_club TEXT NOT NULL,
    home_score INTEGER NOT NULL,
    away_score INTEGER NOT NULL,
    events_json TEXT NOT NULL,
    played INTEGER NOT NULL DEFAULT 1
)
"""

SEASONS_DDL = """
CREATE TABLE IF NOT EXISTS seasons (
    id TEXT PRIMARY KEY,
    clubs_json TEXT NOT NULL,
    rounds INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    division_id INTEGER,
    division_name TEXT
)
"""

# Attribute columns (short names from the CSV header). "Nat.1" renamed to "NatFit".
ATTR_COLUMNS = [
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
    # Natural fitness (renamed from "Nat.1")
    "NatFit",
]

ATTR_HEADER_ALIAS = {
    "Nat.1": "NatFit",
    "1v1":   "1v1",
    "Imp M": "Imp M",
    "Inj Pr": "Inj Pr",
    "L Th":  "L Th",
}


def _attr_col_def(name: str) -> str:
    return f'"{name}" INTEGER'


def init_persistence(conn: sqlite3.Connection) -> None:
    """Create matches/seasons tables if missing (called on app startup)."""
    cur = conn.cursor()
    for stmt in (PERSIST_DDL, SEASONS_DDL):
        cur.execute(stmt)
    conn.commit()


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def _open_csv(path: str) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [dict(zip(header, r)) for r in reader if r]
    return header, rows


def ingest(csv_paths: Iterable[str], db_path: str = DB_PATH,
           verbose: bool = True) -> dict:
    """Ingest one or more CSVs into a clean normalized SQLite DB. Idempotent.

    Returns a dict with stats: {players, clubs, divisions, nations, deduped, playable_leagues}.
    """
    csv_paths = list(csv_paths)
    if not csv_paths:
        raise ValueError("ingest() requires at least one CSV path")

    # ---- Read & merge all parts, dedupe by UID ----
    by_uid: dict[str, dict[str, str]] = {}
    total_rows_seen = 0
    for p in csv_paths:
        if not os.path.exists(p):
            if verbose:
                print(f"  [skip] missing: {p}")
            continue
        header, rows = _open_csv(p)
        total_rows_seen += len(rows)
        for r in rows:
            uid = r.get("UID", "").strip()
            if not uid:
                continue
            if uid in by_uid:
                continue
            by_uid[uid] = r
        if verbose:
            print(f"  [read] {os.path.basename(p)}: {len(rows)} rows")

    deduped = total_rows_seen - len(by_uid)
    if verbose:
        print(f"  [merge] {total_rows_seen} total rows -> {len(by_uid)} unique UIDs "
              f"({deduped} duplicates dropped)")

    # ---- Build nations / divisions / clubs maps ----
    nations_map: dict[str, str] = {}   # nation_name -> code
    divisions_map: dict[tuple[str, str], int] = {}  # (nation_name, div_name) -> id
    clubs_map: dict[tuple[str, str], int] = {}  # (club_name, based_raw) -> id

    # First pass: extract all unique nations and divisions
    nation_counter: dict[str, int] = {}
    div_counter: dict[tuple[str, str], int] = {}

    for uid, r in by_uid.items():
        nation_name, div_name = parse_based(r.get("Based", ""))
        if nation_name:
            nation_counter[nation_name] = nation_counter.get(nation_name, 0) + 1
            key = (nation_name, div_name or "(None)")
            div_counter[key] = div_counter.get(key, 0) + 1

    # Build nation codes: try to use a 3-letter code, else slug
    # We'll use a simple slug for now
    def _nation_code(name: str, idx: int) -> str:
        # try to find a 3-letter abbreviation
        slug = re.sub(r'[^A-Za-z]', '', name).upper()[:3]
        if not slug:
            return f"N{idx:03d}"
        return slug

    for i, (name, count) in enumerate(sorted(nation_counter.items(), key=lambda x: -x[1])):
        code = _nation_code(name, i)
        # ensure uniqueness
        while code in nations_map.values():
            code = code + str(i)
        nations_map[name] = code

    # ---- Build SQLite ----
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    cur = conn.cursor()

    cur.execute(NATIONS_DDL)
    cur.execute(DIVISIONS_DDL)
    for idx in DIVISIONS_INDEXES:
        cur.execute(idx)
    cur.execute(CLUBS_DDL)
    for idx in CLUBS_INDEXES:
        cur.execute(idx)
    cur.execute(PLAYERS_DDL)
    for idx in PLAYERS_INDEXES:
        cur.execute(idx)
    attr_cols_sql = ",\n    ".join(_attr_col_def(c) for c in ATTR_COLUMNS)
    cur.execute(ATTRS_DDL_TEMPLATE.format(cols=attr_cols_sql))
    cur.execute(POSITIONS_DDL)
    cur.execute(POSITIONS_INDEX)
    init_persistence(conn)

    # Insert nations
    for name, count in sorted(nation_counter.items(), key=lambda x: -x[1]):
        cur.execute(
            "INSERT INTO nations (code, name, player_count) VALUES (?, ?, ?)",
            (nations_map[name], name, count),
        )

    # Insert divisions
    div_id_counter = 0
    for (nation_name, div_name), count in sorted(div_counter.items(), key=lambda x: -x[1]):
        div_id_counter += 1
        divisions_map[(nation_name, div_name)] = div_id_counter
        based_raw = nation_name if div_name == "(None)" else f"{nation_name} ({div_name})"
        cur.execute(
            "INSERT INTO divisions (id, nation_code, name, based_raw, player_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (div_id_counter, nations_map[nation_name],
             div_name if div_name != "(None)" else "(None)",
             based_raw, count),
        )

    # Insert clubs (first pass: just names + based_raw)
    club_counter: dict[tuple[str, str], int] = {}
    for uid, r in by_uid.items():
        club_name = (r.get("Club") or "").strip()
        if not club_name:
            continue
        based_raw = (r.get("Based") or "").strip()
        key = (club_name, based_raw)
        club_counter[key] = club_counter.get(key, 0) + 1

    club_id_counter = 0
    for (club_name, based_raw), count in sorted(club_counter.items(), key=lambda x: -x[1]):
        club_id_counter += 1
        clubs_map[(club_name, based_raw)] = club_id_counter
        nation_name, div_name = parse_based(based_raw)
        nation_code = nations_map.get(nation_name) if nation_name else None
        div_key = (nation_name or "", div_name or "(None)")
        div_id = divisions_map.get(div_key)
        cur.execute(
            "INSERT INTO clubs (id, name, nation_code, division_id, player_count, based_raw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (club_id_counter, club_name, nation_code, div_id, count, based_raw),
        )

    # ---- Insert players + attributes + positions ----
    attr_placeholder = ", ".join("?" for _ in ATTR_COLUMNS)
    attr_sql = (
        f'INSERT INTO player_attributes (uid, {", ".join(chr(34)+c+chr(34) for c in ATTR_COLUMNS)}) '
        f'VALUES (?, {attr_placeholder})'
    )

    player_sql = """
        INSERT INTO players (
            uid, name, dob, age, inf, club, club_id, division_id,
            nation_code, nationality, based,
            height_cm, weight_kg, position_raw,
            primary_role, primary_family,
            transfer_value, preferred_foot, left_foot, right_foot,
            rc_injury, media_handling, morale
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    pos_sql = "INSERT OR IGNORE INTO player_positions (uid, role, side, family) VALUES (?, ?, ?, ?)"

    written = 0
    for uid_str, r in by_uid.items():
        try:
            uid_i = int(uid_str)
        except ValueError:
            continue

        dob = parse_dob(r.get("DOB", ""))
        age = age_from_dob(dob)

        club_name = (r.get("Club") or "").strip() or None
        based_raw = (r.get("Based") or "").strip() or None
        club_id = clubs_map.get((club_name, based_raw)) if club_name else None

        nation_name, div_name = parse_based(based_raw or "")
        nation_code = nations_map.get(nation_name) if nation_name else None
        div_key = (nation_name or "", div_name or "(None)")
        division_id = divisions_map.get(div_key)

        nationality = (r.get("Nat") or "").strip() or None

        pos_list = parse_position(r.get("Position", ""))
        primary_role = pos_list[0][0] if pos_list else None
        primary_family = ROLE_FAMILY.get(primary_role) if primary_role else None

        # attributes -- scale 1-20 to 0-100 with deterministic noise
        attr_vals: list[int | None] = []
        for c in ATTR_COLUMNS:
            csv_header = c
            for alias, canon in ATTR_HEADER_ALIAS.items():
                if canon == c:
                    csv_header = alias
                    break
            raw_val = parse_int(r.get(csv_header))
            attr_vals.append(scale_attr(raw_val, uid_i, c) if raw_val is not None else None)

        # foot strength -- scale 1-5 to 0-100
        left_foot_raw = FOOT_STRENGTH.get((r.get("Left Foot") or "").strip())
        right_foot_raw = FOOT_STRENGTH.get((r.get("Right Foot") or "").strip())
        left_foot = scale_foot(left_foot_raw, uid_i, "left") if left_foot_raw else None
        right_foot = scale_foot(right_foot_raw, uid_i, "right") if right_foot_raw else None

        cur.execute(player_sql, (
            uid_i,
            (r.get("Name") or "").strip(),
            dob.isoformat() if dob else None,
            age,
            (r.get("Inf") or "").strip() or None,
            club_name,
            club_id,
            division_id,
            nation_code,
            nationality,
            based_raw,
            parse_height_cm(r.get("Height", "")),
            parse_weight_kg(r.get("Weight", "")),
            (r.get("Position") or "").strip() or None,
            primary_role,
            primary_family,
            parse_transfer_value(r.get("Transfer Value", "")),
            (r.get("Preferred Foot") or "").strip() or None,
            left_foot,
            right_foot,
            (r.get("Rc Injury") or "").strip() or None,
            (r.get("Media Handling") or "").strip() or None,
            50,  # default morale on 0-100 scale
        ))

        cur.execute(attr_sql, [uid_i] + attr_vals)

        for role, side in pos_list:
            family = ROLE_FAMILY.get(role, "MID")
            cur.execute(pos_sql, (uid_i, role, side, family))

        written += 1

    # ---- Mark playable leagues ----
    # Commit all inserts first so the playable query sees consistent data
    conn.commit()

    # A league is playable if it has >= MIN_CLUBS_PER_LEAGUE clubs, each with
    # >= MIN_PLAYERS_PER_CLUB players.
    cur2 = conn.cursor()
    cur2.execute("""
        SELECT d.id
        FROM divisions d
        JOIN clubs c ON c.division_id = d.id AND c.player_count >= ?
        GROUP BY d.id
        HAVING COUNT(*) >= ?
    """, (MIN_PLAYERS_PER_CLUB, MIN_CLUBS_PER_LEAGUE))
    playable_div_ids = [row[0] for row in cur2.fetchall()]
    for did in playable_div_ids:
        cur.execute("UPDATE divisions SET playable = 1, club_count = "
                    "(SELECT COUNT(*) FROM clubs WHERE division_id = ? AND player_count >= ?) "
                    "WHERE id = ?",
                    (did, MIN_PLAYERS_PER_CLUB, did))
    # also update club_count for all divisions
    cur.execute("""
        UPDATE divisions SET club_count = (
            SELECT COUNT(*) FROM clubs WHERE division_id = divisions.id
        )
    """)

    conn.commit()
    conn.close()

    stats = {
        "players": written,
        "clubs": len(clubs_map),
        "divisions": len(divisions_map),
        "nations": len(nations_map),
        "deduped": deduped,
        "playable_leagues": len(playable_div_ids),
        "db_path": db_path,
    }
    if verbose:
        print(f"  [write] {written} players, {len(clubs_map)} clubs, "
              f"{len(divisions_map)} divisions, {len(nations_map)} nations")
        print(f"  [playable] {len(playable_div_ids)} leagues have "
              f">={MIN_CLUBS_PER_LEAGUE} clubs with >={MIN_PLAYERS_PER_CLUB} players each")
        print(f"  -> {db_path}")
    return stats


def find_csv_parts(directory: str = DEFAULT_CSV_DIR) -> list[str]:
    """Return sorted list of CSV paths matching merged_players*.csv."""
    if not os.path.isdir(directory):
        return []
    out = []
    for fn in sorted(os.listdir(directory)):
        if fn.lower().endswith(".csv") and "merged_players" in fn.lower():
            out.append(os.path.join(directory, fn))
    return out


if __name__ == "__main__":
    parts = find_csv_parts()
    if not parts:
        raise SystemExit(f"No merged_players*.csv files found in {DEFAULT_CSV_DIR}")
    print(f"Ingesting {len(parts)} CSV part(s)...")
    stats = ingest(parts)
    print("Done:", stats)
