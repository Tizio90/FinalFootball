"""
Hall of Fame: cross-career persistent records (Phase 4C).

Records persist independent of any single career — deleting a career
doesn't erase history. Uses denormalized snapshots so career deletion
doesn't cascade.
"""
from __future__ import annotations

import sqlite3
import datetime as dt


HOF_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS hall_of_fame_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        record_type TEXT NOT NULL,
        entity_name TEXT NOT NULL,
        entity_uid INTEGER,
        value REAL NOT NULL,
        value_label TEXT,
        season_id TEXT,
        career_id TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS manager_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        manager_name TEXT NOT NULL,
        career_id TEXT,
        seasons_managed INTEGER DEFAULT 0,
        matches_won INTEGER DEFAULT 0,
        matches_drawn INTEGER DEFAULT 0,
        matches_lost INTEGER DEFAULT 0,
        trophies_won INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
]


def run_hof_migrations(conn: sqlite3.Connection) -> None:
    """Apply Hall of Fame migrations."""
    cur = conn.cursor()
    for sql in HOF_MIGRATIONS:
        try:
            cur.execute(sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def record_achievement(conn: sqlite3.Connection, category: str, record_type: str,
                       entity_name: str, value: float, value_label: str = None,
                       season_id: str = None, career_id: str = None,
                       entity_uid: int = None) -> None:
    """Record a Hall of Fame achievement."""
    now = dt.datetime.now().isoformat()
    conn.execute(
        "INSERT INTO hall_of_fame_records (category, record_type, entity_name, "
        "entity_uid, value, value_label, season_id, career_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (category, record_type, entity_name, entity_uid, value, value_label,
         season_id, career_id, now),
    )
    conn.commit()


def get_top_scorers(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Get all-time top scorers from hall_of_fame_records."""
    cur = conn.execute(
        "SELECT entity_name, entity_uid, SUM(value) as total_goals, "
        "COUNT(*) as seasons "
        "FROM hall_of_fame_records "
        "WHERE category = 'scoring' AND record_type = 'season_goals' "
        "GROUP BY entity_uid ORDER BY total_goals DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def get_top_managers(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Get all-time top managers by trophies."""
    cur = conn.execute(
        "SELECT manager_name, seasons_managed, matches_won, matches_drawn, "
        "matches_lost, trophies_won "
        "FROM manager_records ORDER BY trophies_won DESC, matches_won DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def get_club_records(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Get club records (biggest wins, most points, etc.)."""
    cur = conn.execute(
        "SELECT * FROM hall_of_fame_records "
        "WHERE category = 'club' "
        "ORDER BY value DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def update_manager_record(conn: sqlite3.Connection, manager_name: str,
                          career_id: str, won: int = 0, drawn: int = 0,
                          lost: int = 0, trophy: bool = False) -> None:
    """Update or create a manager record."""
    row = conn.execute(
        "SELECT id FROM manager_records WHERE manager_name = ? AND career_id = ?",
        (manager_name, career_id),
    ).fetchone()
    now = dt.datetime.now().isoformat()
    if row:
        conn.execute(
            "UPDATE manager_records SET matches_won = matches_won + ?, "
            "matches_drawn = matches_drawn + ?, matches_lost = matches_lost + ?, "
            "trophies_won = trophies_won + ? WHERE id = ?",
            (won, drawn, lost, 1 if trophy else 0, row[0]),
        )
    else:
        conn.execute(
            "INSERT INTO manager_records (manager_name, career_id, seasons_managed, "
            "matches_won, matches_drawn, matches_lost, trophies_won, created_at) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
            (manager_name, career_id, won, drawn, lost, 1 if trophy else 0, now),
        )
    conn.commit()


def record_season_achievements(conn: sqlite3.Connection, career_id: str,
                               season_id: str, summary: dict,
                               club: str, manager_name: str = "Manager") -> None:
    """Record end-of-season achievements to the Hall of Fame."""
    # top scorers
    for scorer in summary.get("top_scorers", [])[:5]:
        record_achievement(conn, "scoring", "season_goals",
                          scorer["name"], scorer["goals"],
                          f"{scorer['goals']} goals",
                          season_id, career_id, scorer.get("uid"))
    # top assisters
    for assister in summary.get("top_assists", [])[:5]:
        record_achievement(conn, "assists", "season_assists",
                          assister["name"], assister["assists"],
                          f"{assister['assists']} assists",
                          season_id, career_id, assister.get("uid"))
    # club record: most points in a season
    if summary.get("user_record"):
        ur = summary["user_record"]
        record_achievement(conn, "club", "season_points",
                          club, ur["points"],
                          f"{ur['points']} points ({ur['won']}W {ur['drawn']}D {ur['lost']}L)",
                          season_id, career_id)
        # biggest win
        if ur["gf"] - ur["ga"] > 0:
            record_achievement(conn, "club", "goal_difference",
                              club, ur["gf"] - ur["ga"],
                              f"GD +{ur['gf'] - ur['ga']}",
                              season_id, career_id)
    # manager record
    if summary.get("user_record"):
        ur = summary["user_record"]
        update_manager_record(conn, manager_name, career_id,
                             won=ur["won"], drawn=ur["drawn"], lost=ur["lost"])
