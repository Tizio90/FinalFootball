"""
Season & league management.

- Round-robin fixture generation (double round-robin by default).
- Standings table: P / W / D / L / GF / GA / GD / Pts, with head-to-head
  tiebreakers (§0.7).
- League selection: by default, the top-20 clubs by squad size from the DB.
- Each match: pull squad, pick best XI (auto-formation by squad composition),
  run the engine, collect result.
- Asymmetric home advantage (2A.7): per-club home advantage derived from
  recent home form.
- Persist results to SQLite (matches table) so the UI can browse them.
"""
from __future__ import annotations

import os
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# Make engine importable when sim.season is run as a module
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in __import__("sys").path:
    __import__("sys").path.insert(0, _ROOT)

from engine.attributes import DB_PATH
from engine.lineup import (
    load_club_squad, pick_best_xi, pick_formation_for_squad,
    starting_xi_strength,
)
from engine.match import MatchResult, play_match


# ---------------------------------------------------------------------------
# Standings
# ---------------------------------------------------------------------------

@dataclass
class StandingRow:
    club: str
    played: int = 0
    won: int = 0
    drawn: int = 0
    lost: int = 0
    gf: int = 0
    ga: int = 0
    gd: int = 0
    points: int = 0
    form: list[str] = field(default_factory=list)  # last 5, oldest first

    def to_dict(self) -> dict:
        return {
            "club": self.club, "played": self.played,
            "won": self.won, "drawn": self.drawn, "lost": self.lost,
            "gf": self.gf, "ga": self.ga, "gd": self.gd,
            "points": self.points,
            "form": "".join(self.form[-5:]),
        }


class Standings:
    """Standings table with head-to-head tiebreaker (§0.7).

    Tiebreaker order (when points are equal):
      1. Head-to-head points (only between the tied clubs)
      2. Head-to-head GD
      3. Head-to-head GF
      4. Overall GD
      5. Overall GF
      6. Club name (alphabetical)
    """

    def __init__(self, clubs: list[str]):
        self.rows: dict[str, StandingRow] = {c: StandingRow(c) for c in clubs}
        # head-to-head store: {(club_a, club_b): [home_goals_a, away_goals_b, ...]}
        # We store each match's goals from a's perspective.
        self.h2h: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)

    def record_result(self, home: str, away: str,
                      home_goals: int, away_goals: int) -> None:
        h = self.rows[home]
        a = self.rows[away]
        h.played += 1
        a.played += 1
        h.gf += home_goals
        h.ga += away_goals
        a.gf += away_goals
        a.ga += home_goals
        if home_goals > away_goals:
            h.won += 1; h.points += 3
            a.lost += 1
            h.form.append("W"); a.form.append("L")
        elif home_goals < away_goals:
            a.won += 1; a.points += 3
            h.lost += 1
            h.form.append("L"); a.form.append("W")
        else:
            h.drawn += 1; a.drawn += 1
            h.points += 1; a.points += 1
            h.form.append("D"); a.form.append("D")
        h.gd = h.gf - h.ga
        a.gd = a.gf - a.ga
        # store from home's perspective: (home_goals, away_goals)
        self.h2h[(home, away)].append((home_goals, away_goals))
        # also store from away's perspective for symmetric lookup
        self.h2h[(away, home)].append((away_goals, home_goals))

    def _h2h_summary(self, club_a: str, club_b: str) -> tuple[int, int, int]:
        """Return (points_for_a, gd_for_a, gf_for_a) across all matches
        between club_a and club_b in this season."""
        matches = self.h2h.get((club_a, club_b), [])
        pts = gd = gf = 0
        for ga_goals, gb_goals in matches:  # from a's perspective
            gf += ga_goals
            gd += (ga_goals - gb_goals)
            if ga_goals > gb_goals:
                pts += 3
            elif ga_goals == gb_goals:
                pts += 1
        return pts, gd, gf

    def sorted_rows(self) -> list[StandingRow]:
        """Sort with head-to-head tiebreakers."""
        all_rows = list(self.rows.values())

        # group by points
        points_groups: dict[int, list[StandingRow]] = defaultdict(list)
        for r in all_rows:
            points_groups[r.points].append(r)

        sorted_rows: list[StandingRow] = []
        # iterate from highest points to lowest
        for pts in sorted(points_groups.keys(), reverse=True):
            group = points_groups[pts]
            if len(group) == 1:
                sorted_rows.extend(group)
                continue
            # within a tied group, sort by h2h (pairwise within the group),
            # then overall GD, GF, name
            sorted_rows.extend(self._sort_tied_group(group))

        return sorted_rows

    def _sort_tied_group(self, group: list[StandingRow]) -> list[StandingRow]:
        """Sort a tied (same-points) group using h2h among them."""
        # compute h2h points for each club against the OTHER clubs in the group
        club_names = [r.club for r in group]
        h2h_data: dict[str, tuple[int, int, int]] = {}
        for r in group:
            h2h_pts = h2h_gd = h2h_gf = 0
            for other in club_names:
                if other == r.club:
                    continue
                pts, gd, gf = self._h2h_summary(r.club, other)
                h2h_pts += pts
                h2h_gd += gd
                h2h_gf += gf
            h2h_data[r.club] = (h2h_pts, h2h_gd, h2h_gf)
        return sorted(
            group,
            key=lambda r: (
                -h2h_data[r.club][0],  # h2h points
                -h2h_data[r.club][1],  # h2h GD
                -h2h_data[r.club][2],  # h2h GF
                -r.gd,                 # overall GD
                -r.gf,                 # overall GF
                r.club,                # name asc
            ),
        )

    def to_list(self) -> list[dict]:
        return [r.to_dict() for r in self.sorted_rows()]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def round_robin_fixtures(clubs: list[str], double: bool = True,
                         seed: int | None = None) -> list[list[tuple[str, str]]]:
    """Return a list of rounds; each round is a list of (home, away) fixtures.

    Uses the classic round-robin algorithm (fix one team, rotate the rest).
    For odd N, a dummy BYE is added and any team paired with BYE skips that
    round.
    """
    n = len(clubs)
    if n < 2:
        return []
    teams = list(clubs)
    if n % 2 == 1:
        teams.append(None)  # BYE
        n += 1

    rng = random.Random(seed)
    rng.shuffle(teams)

    rounds = []
    half = n // 2
    fixed = teams[0]
    rest = teams[1:]

    for r in range(n - 1):
        round_matches = []
        # first match involves the fixed team
        pairs = [(fixed, rest[-1])] if r % 2 == 0 else [(rest[-1], fixed)]
        for i in range(half - 1):
            home = rest[i]
            away = rest[-(i + 2)]
            # alternate home/away by round parity for fairness
            if r % 2 == 1:
                home, away = away, home
            pairs.append((home, away))
        # filter BYEs
        for h, a in pairs:
            if h is not None and a is not None:
                round_matches.append((h, a))
        rounds.append(round_matches)
        # rotate rest
        rest = [rest[-1]] + rest[:-1]

    if double:
        # second half: same rounds but with home/away swapped
        second_half = []
        for r_matches in rounds:
            swapped = [(a, h) for (h, a) in r_matches]
            second_half.append(swapped)
        rounds = rounds + second_half

    return rounds


# ---------------------------------------------------------------------------
# League selection
# ---------------------------------------------------------------------------

def select_top_clubs(conn: sqlite3.Connection, n: int = 20) -> list[str]:
    """Return the top-N clubs by player count (legacy fallback)."""
    cur = conn.execute(
        "SELECT name FROM clubs WHERE player_count >= 14 "
        "ORDER BY player_count DESC, name ASC LIMIT ?",
        (n,),
    )
    return [r["name"] for r in cur.fetchall()]


def select_clubs_by_division(conn: sqlite3.Connection, division_id: int,
                             min_squad_size: int = 14) -> list[str]:
    """Return all club names in a division that have enough players for a full XI + subs."""
    cur = conn.execute(
        "SELECT name FROM clubs WHERE division_id = ? AND player_count >= ? "
        "ORDER BY name ASC",
        (division_id, min_squad_size),
    )
    return [r["name"] for r in cur.fetchall()]


def get_playable_divisions(conn: sqlite3.Connection) -> list[dict]:
    """Return all divisions marked as playable, with nation info."""
    cur = conn.execute(
        "SELECT d.id, d.name, d.based_raw, d.player_count, d.club_count, "
        "n.name as nation_name, n.code as nation_code "
        "FROM divisions d JOIN nations n ON d.nation_code = n.code "
        "WHERE d.playable = 1 "
        "ORDER BY n.name, d.player_count DESC",
    )
    return [dict(r) for r in cur.fetchall()]


def get_divisions_by_nation(conn: sqlite3.Connection, nation_code: str) -> list[dict]:
    """Return all divisions in a nation, with playability flag."""
    cur = conn.execute(
        "SELECT id, name, based_raw, player_count, club_count, playable "
        "FROM divisions WHERE nation_code = ? "
        "ORDER BY player_count DESC",
        (nation_code,),
    )
    return [dict(r) for r in cur.fetchall()]


def get_nations(conn: sqlite3.Connection) -> list[dict]:
    """Return all nations, sorted by player count."""
    cur = conn.execute(
        "SELECT code, name, player_count FROM nations ORDER BY player_count DESC",
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Persistence — DDL and schema migrations
# ---------------------------------------------------------------------------

# §0.1 fix: this DDL must match data/ingest.py:SEASONS_DDL exactly.
# Both define the seasons table with division_id + division_name columns.
# The old PERSIST_DDL here was missing those columns, causing a crash on
# fresh DBs where init_persistence() ran before ingest().
PERSIST_DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        season_id TEXT NOT NULL,
        round INTEGER NOT NULL,
        home_club TEXT NOT NULL,
        away_club TEXT NOT NULL,
        home_score INTEGER NOT NULL,
        away_score INTEGER NOT NULL,
        events_json TEXT NOT NULL,
        played INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS seasons (
        id TEXT PRIMARY KEY,
        clubs_json TEXT NOT NULL,
        rounds INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        division_id INTEGER,
        division_name TEXT
    )""",
]

# §0.4: schema migrations for Phase 2B+ tables. Each migration is idempotent
# (uses CREATE TABLE IF NOT EXISTS or ALTER TABLE ... ADD COLUMN with a check).
# Version history:
#   1 = Phase 2A schema (seasons + matches with division columns)
#   2 = Phase 2B-1: careers table
#   3 = Phase 2B-2: injuries table + player morale tracking
#   4 = Phase 2B-3: transfers table + club reputation
MIGRATIONS = [
    # version 1 -> 2: add careers table for manager mode
    """CREATE TABLE IF NOT EXISTS careers (
        id TEXT PRIMARY KEY,
        club TEXT NOT NULL,
        division_id INTEGER,
        season_id TEXT,
        current_round INTEGER DEFAULT 0,
        formation TEXT DEFAULT '4-3-3',
        mentality TEXT DEFAULT 'balanced',
        manual_xi_json TEXT,
        manual_bench_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    # version 2 -> 3: add injuries table for cross-match injury carryover
    """CREATE TABLE IF NOT EXISTS injuries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        season_id TEXT,
        player_uid INTEGER NOT NULL,
        player_name TEXT,
        club TEXT NOT NULL,
        injury_round INTEGER NOT NULL,
        matches_out INTEGER NOT NULL,
        matches_remaining INTEGER NOT NULL,
        injury_type TEXT,
        created_at TEXT NOT NULL
    )""",
    # version 3 -> 4: add transfers table + cup fixtures
    """CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        season_id TEXT,
        player_uid INTEGER NOT NULL,
        player_name TEXT,
        from_club TEXT NOT NULL,
        to_club TEXT NOT NULL,
        fee INTEGER DEFAULT 0,
        transfer_window TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cup_fixtures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        career_id TEXT,
        season_id TEXT,
        cup_name TEXT NOT NULL,
        round INTEGER NOT NULL,
        round_name TEXT,
        home_club TEXT NOT NULL,
        away_club TEXT NOT NULL,
        home_score INTEGER,
        away_score INTEGER,
        played INTEGER DEFAULT 0,
        next_round_match_id INTEGER,
        created_at TEXT NOT NULL
    )""",
]


def init_persistence(conn: sqlite3.Connection) -> None:
    """Create matches/seasons tables if missing, then run migrations.

    §0.4: Uses PRAGMA user_version to track schema version and apply
    incremental migrations. Never drops user data tables.

    Also force-creates the careers table if it's missing for any reason
    (e.g. an old DB that predates migrations). This is a safety net.
    """
    cur = conn.cursor()
    for stmt in PERSIST_DDL_STATEMENTS:
        cur.execute(stmt)
    conn.commit()
    _run_migrations(conn)
    # Safety net: ensure all Phase 2B tables exist even if migrations failed
    # silently on an old DB. CREATE TABLE IF NOT EXISTS is a no-op if they
    # already exist.
    for safety_sql in [
        """CREATE TABLE IF NOT EXISTS careers (
            id TEXT PRIMARY KEY, club TEXT NOT NULL, division_id INTEGER,
            season_id TEXT, current_round INTEGER DEFAULT 0,
            formation TEXT DEFAULT '4-3-3', mentality TEXT DEFAULT 'balanced',
            manual_xi_json TEXT, manual_bench_json TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS injuries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, career_id TEXT, season_id TEXT,
            player_uid INTEGER NOT NULL, player_name TEXT, club TEXT NOT NULL,
            injury_round INTEGER NOT NULL, matches_out INTEGER NOT NULL,
            matches_remaining INTEGER NOT NULL, injury_type TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, career_id TEXT, season_id TEXT,
            player_uid INTEGER NOT NULL, player_name TEXT,
            from_club TEXT NOT NULL, to_club TEXT NOT NULL, fee INTEGER DEFAULT 0,
            transfer_window TEXT, created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS cup_fixtures (
            id INTEGER PRIMARY KEY AUTOINCREMENT, career_id TEXT, season_id TEXT,
            cup_name TEXT NOT NULL, round INTEGER NOT NULL, round_name TEXT,
            home_club TEXT NOT NULL, away_club TEXT NOT NULL,
            home_score INTEGER, away_score INTEGER, played INTEGER DEFAULT 0,
            next_round_match_id INTEGER, created_at TEXT NOT NULL
        )""",
    ]:
        cur.execute(safety_sql)
    # Fix: add created_at column to cup_fixtures if it's missing (old DBs)
    try:
        cur.execute("SELECT created_at FROM cup_fixtures LIMIT 0")
    except sqlite3.OperationalError:
        try:
            cur.execute("ALTER TABLE cup_fixtures ADD COLUMN created_at TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists or table doesn't exist
    conn.commit()
    # Phase 3: run Phase 3 migrations (player development, staff, finances, etc.)
    try:
        from sim.phase3 import run_phase3_migrations
        run_phase3_migrations(conn)
    except ImportError:
        pass  # phase3.py not available yet


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending migrations based on PRAGMA user_version.

    On older DBs (user_version = 0, pre-Phase-2B), we jump straight to
    the latest version after ensuring all tables exist. This handles the
    case where a user pulled the new code but their DB was created before
    migrations existed.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA user_version")
    current_version = cur.fetchone()[0] or 0

    for i, migration_sql in enumerate(MIGRATIONS):
        target_version = i + 2  # migrations start at version 2
        if current_version < target_version:
            try:
                cur.execute(migration_sql)
                conn.commit()
                cur.execute(f"PRAGMA user_version = {target_version}")
                conn.commit()
            except sqlite3.OperationalError as e:
                # table/column already exists — safe to ignore, but still
                # bump the version so we don't retry every request
                if "already exists" in str(e):
                    cur.execute(f"PRAGMA user_version = {target_version}")
                    conn.commit()
                else:
                    raise


# ---------------------------------------------------------------------------
# Season runner
# ---------------------------------------------------------------------------

@dataclass
class SeasonResult:
    season_id: str
    clubs: list[str]
    rounds: list[list[tuple[str, str]]]
    standings: Standings
    matches: list[MatchResult] = field(default_factory=list)


def run_season(
    clubs: list[str] | None = None,
    n_clubs: int = 20,
    double: bool = True,
    seed: int | None = None,
    db_path: str = DB_PATH,
    persist: bool = True,
    season_id: str | None = None,
    verbose: bool = True,
    division_id: int | None = None,
    division_name: str | None = None,
) -> SeasonResult:
    """Run a full season: fixtures -> simulate every match -> standings.

    Phase 2 additions:
      - Auto-formation per club based on squad composition
      - Asymmetric home advantage derived from each club's recent home form
    Phase 2B fix (§0.2):
      - Persists division_id + division_name so the home page can show
        which league a season belongs to.
    """
    import json
    import datetime as dt

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if persist:
        init_persistence(conn)

    if clubs is None:
        clubs = select_top_clubs(conn, n=n_clubs)
    if len(clubs) < 2:
        raise ValueError(f"Need >=2 clubs, got {len(clubs)}")

    season_id = season_id or f"season_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    fixtures = round_robin_fixtures(clubs, double=double, seed=seed)
    standings = Standings(clubs)

    if persist:
        # §0.2 fix: include division_id + division_name in the INSERT
        conn.execute(
            "INSERT INTO seasons (id, clubs_json, rounds, created_at, division_id, division_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (season_id, json.dumps(clubs), len(fixtures),
             dt.datetime.now().isoformat(), division_id, division_name),
        )
        conn.commit()

    # Pre-build squad + auto-formation + xi for each club. The XI is reused
    # every match (Phase 2 has no tactical changes or fatigue carryover).
    cache: dict[str, tuple[list, list, list, str]] = {}
    for c in clubs:
        squad = load_club_squad(conn, c)
        formation_key = pick_formation_for_squad(squad)
        xi, bench = pick_best_xi(squad, conn, formation=formation_key)
        cache[c] = (squad, xi, bench, formation_key)
        if verbose:
            print(f"  {c}: formation={formation_key}")

    # Asymmetric home advantage (2A.7): track recent home results per club
    home_form: dict[str, list[str]] = {c: [] for c in clubs}  # 'W'/'D'/'L'

    def _home_advantage(club: str) -> float:
        """Home advantage derived from last 5 home results.
        Baseline 1.08; +0.01 per recent home win, -0.01 per home loss.
        Clamped to [1.02, 1.18]."""
        recent = home_form[club][-5:]
        if not recent:
            return 1.08
        wins = recent.count("W")
        losses = recent.count("L")
        adv = 1.08 + (wins - losses) * 0.015
        return max(1.02, min(1.18, adv))

    rng = random.Random(seed)
    matches: list[MatchResult] = []
    for r_idx, round_matches in enumerate(fixtures, start=1):
        if verbose:
            print(f"--- Round {r_idx} ---")
        for home, away in round_matches:
            if home not in cache or away not in cache:
                continue
            _, home_xi, home_bench, _ = cache[home]
            _, away_xi, away_bench, _ = cache[away]
            m_seed = rng.randrange(0, 2**31)
            ha = _home_advantage(home)
            result = play_match(home, away, home_xi, away_xi,
                                home_bench, away_bench, seed=m_seed,
                                home_advantage=ha)
            matches.append(result)
            standings.record_result(home, away, result.home_score, result.away_score)
            # update home form
            if result.home_score > result.away_score:
                home_form[home].append("W")
            elif result.home_score < result.away_score:
                home_form[home].append("L")
            else:
                home_form[home].append("D")
            if verbose:
                print(f"  {home} {result.home_score} - {result.away_score} {away}")
            if persist:
                conn.execute(
                    "INSERT INTO matches (season_id, round, home_club, away_club, "
                    "home_score, away_score, events_json, played) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (season_id, r_idx, home, away,
                     result.home_score, result.away_score,
                     json.dumps([{
                         "minute": e.minute, "type": e.type, "side": e.side,
                         "text": e.text,
                         "player_uids": e.player_uids,
                         "player_names": e.player_names,
                     } for e in result.events])),
                )
        conn.commit()

    conn.close()
    return SeasonResult(
        season_id=season_id, clubs=clubs, rounds=fixtures,
        standings=standings, matches=matches,
    )


if __name__ == "__main__":
    res = run_season(n_clubs=20, double=True, seed=1, verbose=True)
    print("\n=== Final Standings ===")
    for i, row in enumerate(res.standings.sorted_rows(), 1):
        print(f"{i:2d}. {row.club:30s}  P={row.played:2d}  W={row.won:2d}  "
              f"D={row.drawn:2d}  L={row.lost:2d}  GF={row.gf:3d}  "
              f"GA={row.ga:3d}  GD={row.gd:+4d}  Pts={row.points:3d}  "
              f"Form={row.form[-5:]}")
