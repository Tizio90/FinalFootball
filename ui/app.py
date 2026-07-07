"""
Flask UI for the Football Management Simulation.

Routes:
  GET  /                  -> home: league selector + recent seasons
  POST /season/new        -> start a new season (n_clubs, seed)
  GET  /season/<id>       -> season overview: standings + fixtures
  GET  /season/<id>/match/<int:match_id>  -> match detail with live event feed
  GET  /club/<name>       -> club squad page
  GET  /api/season/<id>/standings  -> JSON standings
  GET  /api/season/<id>/fixtures   -> JSON fixtures
  GET  /api/match/<int:match_id>   -> JSON match events
  POST /season/<id>/sim-round/<n>  -> simulate round n (or all if n=all)
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

# Make project root importable
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from flask import Flask, render_template, request, redirect, url_for, jsonify, abort

from engine.attributes import DB_PATH
from engine.lineup import (
    load_club_squad, pick_best_xi, pick_formation_for_squad,
    starting_xi_strength,
)
from engine.match import play_match
from sim.season import (
    Standings, round_robin_fixtures, select_top_clubs,
    select_clubs_by_division, get_playable_divisions,
    get_divisions_by_nation, get_nations,
    run_season, init_persistence,
)

import sqlite3
import datetime as dt
from functools import lru_cache

# ---------------------------------------------------------------------------
# Flask setup
# ---------------------------------------------------------------------------

TEMPLATE_DIR = os.path.join(_HERE, "templates")
STATIC_DIR = os.path.join(_HERE, "static")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.config["JSON_AS_ASCII"] = False  # allow non-ASCII names in JSON


# Jinja filters
@app.template_filter("fromjson")
def _fromjson(s: str):
    return json.loads(s) if s else []


@app.template_filter("toLocale")
def _toLocale(n):
    """Format a number with thousands separators: 87163 -> 87,163"""
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


# Make enumerate() available in templates
app.jinja_env.globals.update(enumerate=enumerate)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_season(conn: sqlite3.Connection, season_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM seasons WHERE id = ?", (season_id,)).fetchone()


def _get_season_matches(conn: sqlite3.Connection, season_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM matches WHERE season_id = ? ORDER BY round, id",
        (season_id,),
    ).fetchall()


def _compute_standings(conn: sqlite3.Connection, season_id: str,
                       clubs: list[str]) -> Standings:
    standings = Standings(clubs)
    rows = conn.execute(
        "SELECT home_club, away_club, home_score, away_score FROM matches "
        "WHERE season_id = ? AND played = 1",
        (season_id,),
    ).fetchall()
    for r in rows:
        standings.record_result(r["home_club"], r["away_club"],
                                r["home_score"], r["away_score"])
    return standings


def _build_fixtures(clubs: list[str], season_seed: int | None,
                    rounds: int | None) -> list[list[tuple[str, str]]]:
    # use a fixed seed for fixture generation so it's deterministic per season
    fixtures = round_robin_fixtures(clubs, double=True, seed=season_seed or 1)
    if rounds is not None:
        fixtures = fixtures[:rounds]
    return fixtures


def _club_strengths(conn: sqlite3.Connection, clubs: list[str]) -> dict[str, dict[str, float]]:
    out = {}
    for c in clubs:
        squad = load_club_squad(conn, c)
        xi, _ = pick_best_xi(squad, conn)
        out[c] = starting_xi_strength(xi)
    return out


# §0.8: per-process lineup cache keyed by club name. Avoids re-deriving
# the XI on every match-view request. Cache survives for the process
# lifetime (good enough for a local single-user app).
_LINEUP_CACHE: dict[str, tuple[list, list, str]] = {}


def _get_club_lineup(club: str, conn: sqlite3.Connection) -> tuple[list, list, str]:
    """Return (xi, bench, formation_key) for a club, memoized."""
    if club not in _LINEUP_CACHE:
        squad = load_club_squad(conn, club)
        formation = pick_formation_for_squad(squad)
        xi, bench = pick_best_xi(squad, conn, formation=formation)
        _LINEUP_CACHE[club] = (xi, bench, formation)
    return _LINEUP_CACHE[club]


def clear_lineup_cache(club: str | None = None) -> None:
    """§0.3 fix: invalidate the lineup cache.
    If club is None, clears the entire cache. Otherwise clears just that club.
    Call this after re-ingesting or after a transfer changes a club's squad."""
    if club is None:
        _LINEUP_CACHE.clear()
    else:
        _LINEUP_CACHE.pop(club, None)


def _check_db_ready() -> str | None:
    """§0.8 fix: return an error message if the DB is missing or not ingested."""
    if not os.path.exists(DB_PATH):
        return ("Database not found. Run <code>python run.py ingest</code> first "
                "to build it from the CSV.")
    conn = get_db()
    try:
        conn.execute("SELECT 1 FROM clubs LIMIT 1").fetchone()
        return None
    except sqlite3.OperationalError:
        return ("Database exists but the <code>clubs</code> table is missing. "
                "Run <code>python run.py ingest</code> to rebuild it.")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    # §0.8 fix: check DB is ready before any query
    db_error = _check_db_ready()
    if db_error:
        return render_template("error.html", error_title="Database not ready",
                              error_message=db_error), 503

    conn = get_db()
    init_persistence(conn)
    seasons = conn.execute(
        "SELECT * FROM seasons ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    careers = conn.execute(
        "SELECT * FROM careers ORDER BY updated_at DESC LIMIT 10"
    ).fetchall()
    stats = {
        "players": conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"],
        "clubs": conn.execute("SELECT COUNT(*) AS n FROM clubs").fetchone()["n"],
        "nations": conn.execute("SELECT COUNT(*) AS n FROM nations").fetchone()["n"],
        "divisions": conn.execute("SELECT COUNT(*) AS n FROM divisions").fetchone()["n"],
        "playable": conn.execute("SELECT COUNT(*) AS n FROM divisions WHERE playable = 1").fetchone()["n"],
    }
    # Quick-start: top 6 playable leagues by player count
    quick_leagues = conn.execute(
        "SELECT d.id, d.name, d.based_raw, d.player_count, d.club_count, "
        "n.name as nation_name "
        "FROM divisions d JOIN nations n ON d.nation_code = n.code "
        "WHERE d.playable = 1 "
        "ORDER BY d.player_count DESC LIMIT 6"
    ).fetchall()
    conn.close()
    return render_template("home.html", seasons=seasons, stats=stats,
                          quick_leagues=quick_leagues, careers=careers)


@app.route("/nations")
def nations():
    conn = get_db()
    all_nations = get_nations(conn)
    conn.close()
    return render_template("nations.html", nations=all_nations)


@app.route("/nation/<nation_code>")
def nation(nation_code: str):
    conn = get_db()
    nation_row = conn.execute(
        "SELECT * FROM nations WHERE code = ?", (nation_code,)
    ).fetchone()
    if nation_row is None:
        conn.close()
        abort(404)
    divisions = get_divisions_by_nation(conn, nation_code)
    conn.close()
    return render_template("nation.html", nation=nation_row, divisions=divisions)


@app.route("/division/<int:division_id>")
def division(division_id: int):
    conn = get_db()
    div = conn.execute(
        "SELECT d.*, n.name as nation_name, n.code as nation_code "
        "FROM divisions d JOIN nations n ON d.nation_code = n.code "
        "WHERE d.id = ?", (division_id,)
    ).fetchone()
    if div is None:
        conn.close()
        abort(404)
    clubs = conn.execute(
        "SELECT * FROM clubs WHERE division_id = ? "
        "ORDER BY player_count DESC, name ASC",
        (division_id,)
    ).fetchall()
    conn.close()
    return render_template("division.html", division=div, clubs=clubs)


@app.route("/division/<int:division_id>/season/new", methods=["POST"])
def division_season_new(division_id: int):
    """Start a new season using all clubs in this division that have enough players."""
    conn = get_db()
    init_persistence(conn)
    div = conn.execute("SELECT * FROM divisions WHERE id = ?", (division_id,)).fetchone()
    if div is None:
        conn.close()
        abort(404)
    clubs = select_clubs_by_division(conn, division_id, min_squad_size=14)
    conn.close()
    if len(clubs) < 2:
        return "Not enough playable clubs in this division", 400
    seed = int(request.form.get("seed", 1))
    double = request.form.get("double", "y") == "y"
    mode = request.form.get("mode", "all")
    season_id = f"season_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if mode == "all":
        # §0.2 fix: pass division_id + division_name to run_season
        run_season(clubs=clubs, double=double, seed=seed,
                   season_id=season_id, verbose=False,
                   division_id=division_id, division_name=div["based_raw"])
    else:
        # create empty season for round-by-round play
        conn = get_db()
        init_persistence(conn)
        fixtures = round_robin_fixtures(clubs, double=double, seed=seed)
        conn.execute(
            "INSERT INTO seasons (id, clubs_json, rounds, created_at, division_id, division_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (season_id, json.dumps(clubs), len(fixtures),
             dt.datetime.now().isoformat(), division_id, div["based_raw"]),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("season_view", season_id=season_id))


# ---------------------------------------------------------------------------
# Career routes (Phase 2B-1: Manager Mode)
# ---------------------------------------------------------------------------

@app.route("/career/new")
def career_new():
    """Step 1: pick a division."""
    db_error = _check_db_ready()
    if db_error:
        return render_template("error.html", error_title="Database not ready",
                              error_message=db_error), 503
    conn = get_db()
    init_persistence(conn)
    divisions = conn.execute(
        "SELECT d.id, d.name, d.based_raw, d.player_count, d.club_count, "
        "n.name as nation_name "
        "FROM divisions d JOIN nations n ON d.nation_code = n.code "
        "WHERE d.playable = 1 "
        "ORDER BY d.player_count DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return render_template("career_new.html", divisions=divisions, step="division")


@app.route("/career/new/<int:division_id>")
def career_new_club(division_id: int):
    """Step 2: pick a club in the chosen division."""
    conn = get_db()
    div = conn.execute(
        "SELECT d.*, n.name as nation_name "
        "FROM divisions d JOIN nations n ON d.nation_code = n.code "
        "WHERE d.id = ?", (division_id,)
    ).fetchone()
    if div is None:
        conn.close()
        abort(404)
    clubs = conn.execute(
        "SELECT * FROM clubs WHERE division_id = ? AND player_count >= 14 "
        "ORDER BY name ASC",
        (division_id,)
    ).fetchall()
    conn.close()
    return render_template("career_new.html", division=div, clubs=clubs, step="club")


@app.route("/career/create", methods=["POST"])
def career_create():
    """Step 3: create the career + season + redirect to dashboard."""
    division_id = int(request.form.get("division_id"))
    club = request.form.get("club")
    formation = request.form.get("formation", "4-3-3")
    mentality = request.form.get("mentality", "balanced")
    seed = int(request.form.get("seed", 1))
    double = request.form.get("double", "y") == "y"

    conn = get_db()
    init_persistence(conn)
    div = conn.execute("SELECT * FROM divisions WHERE id = ?", (division_id,)).fetchone()
    if div is None:
        conn.close()
        abort(404)
    clubs = select_clubs_by_division(conn, division_id, min_squad_size=14)
    if club not in clubs:
        conn.close()
        return "Selected club is not in this division or has too few players", 400

    import datetime as dt
    season_id = f"season_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    career_id = f"career_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    fixtures = round_robin_fixtures(clubs, double=double, seed=seed)
    conn.execute(
        "INSERT INTO seasons (id, clubs_json, rounds, created_at, division_id, division_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (season_id, json.dumps(clubs), len(fixtures),
         dt.datetime.now().isoformat(), division_id, div["based_raw"]),
    )
    conn.execute(
        "INSERT INTO careers (id, club, division_id, season_id, current_round, "
        "formation, mentality, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (career_id, club, division_id, season_id, formation, mentality,
         dt.datetime.now().isoformat(), dt.datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("career_view", career_id=career_id))


@app.route("/career/<career_id>")
def career_view(career_id: str):
    """Career dashboard: standings, next fixture, play buttons."""
    conn = get_db()
    init_persistence(conn)
    career = conn.execute("SELECT * FROM careers WHERE id = ?", (career_id,)).fetchone()
    if career is None:
        conn.close()
        abort(404)
    season = conn.execute("SELECT * FROM seasons WHERE id = ?",
                         (career["season_id"],)).fetchone()
    if season is None:
        conn.close()
        abort(404)
    clubs = json.loads(season["clubs_json"])
    from sim.season import Standings
    standings = Standings(clubs)
    matches = conn.execute(
        "SELECT * FROM matches WHERE season_id = ? ORDER BY round, id",
        (season["id"],),
    ).fetchall()
    for m in matches:
        if m["played"]:
            standings.record_result(m["home_club"], m["away_club"],
                                   m["home_score"], m["away_score"])
    rounds_played = max((m["round"] for m in matches), default=0)
    total_rounds = season["rounds"]
    fixtures = round_robin_fixtures(clubs, double=True, seed=1)
    next_round = rounds_played + 1 if rounds_played < total_rounds else None
    next_fixtures = fixtures[next_round - 1] if next_round else []
    user_next = None
    for home, away in next_fixtures:
        if home == career["club"] or away == career["club"]:
            user_next = (home, away)
            break
    user_results = [m for m in matches
                    if m["home_club"] == career["club"] or m["away_club"] == career["club"]]
    user_results = user_results[-5:]
    conn.close()
    return render_template("career.html", career=career, season=season,
                          standings=standings.sorted_rows(),
                          rounds_played=rounds_played, total_rounds=total_rounds,
                          user_next=user_next, next_round=next_round,
                          user_results=user_results)


@app.route("/career/<career_id>/tactics", methods=["GET", "POST"])
def career_tactics(career_id: str):
    """View or update the user's formation, mentality, and manual XI."""
    conn = get_db()
    init_persistence(conn)
    career = conn.execute("SELECT * FROM careers WHERE id = ?", (career_id,)).fetchone()
    if career is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        formation = request.form.get("formation", career["formation"])
        mentality = request.form.get("mentality", career["mentality"])
        manual_xi = []
        for i in range(11):
            uid = request.form.get(f"slot_{i}")
            if uid and uid != "auto":
                manual_xi.append(int(uid))
            else:
                manual_xi = []
                break
        manual_bench = []
        for i in range(7):
            uid = request.form.get(f"bench_{i}")
            if uid and uid != "auto":
                manual_bench.append(int(uid))
            else:
                manual_bench = []
                break
        import datetime as dt
        conn.execute(
            "UPDATE careers SET formation=?, mentality=?, manual_xi_json=?, "
            "manual_bench_json=?, updated_at=? WHERE id=?",
            (formation, mentality,
             json.dumps(manual_xi) if manual_xi else None,
             json.dumps(manual_bench) if manual_bench else None,
             dt.datetime.now().isoformat(), career_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("career_view", career_id=career_id))

    from engine.lineup import ALL_FORMATIONS, load_club_squad, pick_best_xi, pick_manual_xi
    squad = load_club_squad(conn, career["club"])
    formation = career["formation"]
    manual_xi_json = json.loads(career["manual_xi_json"]) if career["manual_xi_json"] else None
    if manual_xi_json and len(manual_xi_json) == 11:
        xi, bench = pick_manual_xi(squad, conn, formation, manual_xi_json,
                                   json.loads(career["manual_bench_json"]) if career["manual_bench_json"] else None)
    else:
        xi, bench = pick_best_xi(squad, conn, formation=formation)
    conn.close()
    return render_template("tactics.html", career=career, squad=squad,
                          xi=xi, bench=bench, formations=ALL_FORMATIONS,
                          current_formation=formation,
                          current_mentality=career["mentality"])


@app.route("/career/<career_id>/play-next", methods=["POST"])
def career_play_next(career_id: str):
    """Simulate the next round: user's match with their tactics, rest auto."""
    conn = get_db()
    init_persistence(conn)
    career = conn.execute("SELECT * FROM careers WHERE id = ?", (career_id,)).fetchone()
    if career is None:
        conn.close()
        abort(404)
    season = conn.execute("SELECT * FROM seasons WHERE id = ?",
                         (career["season_id"],)).fetchone()
    clubs = json.loads(season["clubs_json"])
    fixtures = round_robin_fixtures(clubs, double=True, seed=1)
    rounds_played = conn.execute(
        "SELECT MAX(round) FROM matches WHERE season_id = ?",
        (season["id"],)
    ).fetchone()[0] or 0
    next_round = rounds_played + 1
    if next_round > season["rounds"]:
        conn.close()
        return redirect(url_for("career_view", career_id=career_id))

    round_fixtures = fixtures[next_round - 1]
    import random
    rng = random.Random(hash(season["id"]) + next_round)

    from engine.lineup import (load_club_squad, pick_best_xi, pick_manual_xi,
                                pick_formation_for_squad)
    cache = {}
    for c in clubs:
        squad = load_club_squad(conn, c)
        formation = pick_formation_for_squad(squad)
        xi, bench = pick_best_xi(squad, conn, formation=formation)
        cache[c] = (xi, bench, formation)

    user_club = career["club"]
    user_formation = career["formation"]
    user_mentality = career["mentality"]
    manual_xi_json = json.loads(career["manual_xi_json"]) if career["manual_xi_json"] else None
    manual_bench_json = json.loads(career["manual_bench_json"]) if career["manual_bench_json"] else None
    if manual_xi_json and len(manual_xi_json) == 11:
        user_squad = load_club_squad(conn, user_club)
        user_xi, user_bench = pick_manual_xi(user_squad, conn, user_formation,
                                             manual_xi_json, manual_bench_json)
    else:
        user_squad = load_club_squad(conn, user_club)
        user_xi, user_bench = pick_best_xi(user_squad, conn, formation=user_formation)
    cache[user_club] = (user_xi, user_bench, user_formation)

    for home, away in round_fixtures:
        if home not in cache or away not in cache:
            continue
        hx, hb, _ = cache[home]
        ax, ab, _ = cache[away]
        home_ment = user_mentality if home == user_club else "balanced"
        away_ment = user_mentality if away == user_club else "balanced"
        m_seed = rng.randrange(0, 2**31)
        result = play_match(home, away, hx, ax, hb, ab, seed=m_seed,
                           home_mentality=home_ment, away_mentality=away_ment)
        conn.execute(
            "INSERT INTO matches (season_id, round, home_club, away_club, "
            "home_score, away_score, events_json, played) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (season["id"], next_round, home, away,
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
    return redirect(url_for("career_view", career_id=career_id))


@app.route("/career/<career_id>/sim-rest", methods=["POST"])
def career_sim_rest(career_id: str):
    """Simulate the rest of the season (all remaining rounds)."""
    conn = get_db()
    init_persistence(conn)
    career = conn.execute("SELECT * FROM careers WHERE id = ?", (career_id,)).fetchone()
    if career is None:
        conn.close()
        abort(404)
    season = conn.execute("SELECT * FROM seasons WHERE id = ?",
                         (career["season_id"],)).fetchone()
    clubs = json.loads(season["clubs_json"])
    fixtures = round_robin_fixtures(clubs, double=True, seed=1)
    rounds_played = conn.execute(
        "SELECT MAX(round) FROM matches WHERE season_id = ?",
        (season["id"],)
    ).fetchone()[0] or 0

    import random
    rng = random.Random(hash(season["id"]) + rounds_played)

    from engine.lineup import (load_club_squad, pick_best_xi, pick_manual_xi,
                                pick_formation_for_squad)
    cache = {}
    for c in clubs:
        squad = load_club_squad(conn, c)
        formation = pick_formation_for_squad(squad)
        xi, bench = pick_best_xi(squad, conn, formation=formation)
        cache[c] = (xi, bench, formation)

    user_club = career["club"]
    user_formation = career["formation"]
    user_mentality = career["mentality"]
    manual_xi_json = json.loads(career["manual_xi_json"]) if career["manual_xi_json"] else None
    manual_bench_json = json.loads(career["manual_bench_json"]) if career["manual_bench_json"] else None
    if manual_xi_json and len(manual_xi_json) == 11:
        user_squad = load_club_squad(conn, user_club)
        user_xi, user_bench = pick_manual_xi(user_squad, conn, user_formation,
                                             manual_xi_json, manual_bench_json)
    else:
        user_squad = load_club_squad(conn, user_club)
        user_xi, user_bench = pick_best_xi(user_squad, conn, formation=user_formation)
    cache[user_club] = (user_xi, user_bench, user_formation)

    for r_idx in range(rounds_played + 1, season["rounds"] + 1):
        round_fixtures = fixtures[r_idx - 1]
        for home, away in round_fixtures:
            if home not in cache or away not in cache:
                continue
            hx, hb, _ = cache[home]
            ax, ab, _ = cache[away]
            home_ment = user_mentality if home == user_club else "balanced"
            away_ment = user_mentality if away == user_club else "balanced"
            m_seed = rng.randrange(0, 2**31)
            result = play_match(home, away, hx, ax, hb, ab, seed=m_seed,
                               home_mentality=home_ment, away_mentality=away_ment)
            conn.execute(
                "INSERT INTO matches (season_id, round, home_club, away_club, "
                "home_score, away_score, events_json, played) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (season["id"], r_idx, home, away,
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
    return redirect(url_for("career_view", career_id=career_id))


@app.route("/season/new", methods=["POST"])
def season_new():
    n_clubs = int(request.form.get("n_clubs", 20))
    seed = int(request.form.get("seed", 1))
    double = request.form.get("double", "y") == "y"
    mode = request.form.get("mode", "all")  # 'all' or 'manual'

    season_id = f"season_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    conn = get_db()
    init_persistence(conn)
    clubs = select_top_clubs(conn, n=n_clubs)
    if len(clubs) < 2:
        conn.close()
        return "Not enough clubs", 400
    fixtures = round_robin_fixtures(clubs, double=double, seed=seed)
    conn.execute(
        "INSERT INTO seasons (id, clubs_json, rounds, created_at) VALUES (?, ?, ?, ?)",
        (season_id, json.dumps(clubs), len(fixtures),
         dt.datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    if mode == "all":
        # simulate the entire season in one go (in-process)
        # delete this stub season and re-run run_season which will recreate it
        conn = get_db()
        conn.execute("DELETE FROM seasons WHERE id = ?", (season_id,))
        conn.execute("DELETE FROM matches WHERE season_id = ?", (season_id,))
        conn.commit()
        conn.close()
        run_season(clubs=clubs, double=double, seed=seed,
                   season_id=season_id, verbose=False)
    return redirect(url_for("season_view", season_id=season_id))


@app.route("/season/<season_id>")
def season_view(season_id: str):
    conn = get_db()
    season = _get_season(conn, season_id)
    if season is None:
        conn.close()
        abort(404)
    clubs = json.loads(season["clubs_json"])
    standings = _compute_standings(conn, season_id, clubs)
    fixtures = _build_fixtures(clubs, season_seed=1, rounds=season["rounds"])

    # annotate fixtures with results if played
    matches_by_round: dict[int, dict[tuple[str, str], dict]] = {}
    for m in _get_season_matches(conn, season_id):
        matches_by_round.setdefault(m["round"], {})[(m["home_club"], m["away_club"])] = {
            "home_score": m["home_score"],
            "away_score": m["away_score"],
            "match_id": m["id"],
            "played": m["played"],
        }

    # build a "rounds played" counter
    rounds_played = max(matches_by_round.keys(), default=0)
    total_rounds = season["rounds"]

    # squad sizes per club (cheap)
    squads = {}
    for c in clubs:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM players WHERE club = ?", (c,)
        ).fetchone()["n"]
        squads[c] = n

    conn.close()
    return render_template(
        "season.html", season=season, season_id=season_id,
        clubs=clubs, standings=standings.sorted_rows(),
        fixtures=fixtures, matches_by_round=matches_by_round,
        rounds_played=rounds_played, total_rounds=total_rounds,
        squads=squads,
    )


@app.route("/season/<season_id>/sim-round/<round_n>", methods=["POST"])
def season_sim_round(season_id: str, round_n: str):
    """Simulate a single round (round_n = '1', '2', ...) or 'next' or 'all'."""
    conn = get_db()
    season = _get_season(conn, season_id)
    if season is None:
        conn.close()
        abort(404)
    clubs = json.loads(season["clubs_json"])
    fixtures = round_robin_fixtures(clubs, double=True, seed=1)
    total_rounds = len(fixtures)

    # figure out which rounds to simulate
    existing = {r["round"] for r in _get_season_matches(conn, season_id)}
    if round_n == "all":
        target_rounds = [r for r in range(1, total_rounds + 1) if r not in existing]
    elif round_n == "next":
        next_r = None
        for r in range(1, total_rounds + 1):
            if r not in existing:
                next_r = r
                break
        target_rounds = [next_r] if next_r else []
    else:
        r = int(round_n)
        target_rounds = [r] if r not in existing else []

    # cache squads
    cache: dict[str, tuple[list, list]] = {}
    for c in clubs:
        squad = load_club_squad(conn, c)
        xi, bench = pick_best_xi(squad, conn)
        cache[c] = (xi, bench)

    import random
    rng = random.Random(hash(season_id) & 0xFFFFFFFF)

    for r in target_rounds:
        if r > total_rounds:
            continue
        for home, away in fixtures[r - 1]:
            if home not in cache or away not in cache:
                continue
            hx, hb = cache[home]; ax, ab = cache[away]
            m_seed = rng.randrange(0, 2**31)
            result = play_match(home, away, hx, ax, hb, ab, seed=m_seed)
            conn.execute(
                "INSERT INTO matches (season_id, round, home_club, away_club, "
                "home_score, away_score, events_json, played) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (season_id, r, home, away,
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
    return redirect(url_for("season_view", season_id=season_id))


@app.route("/match/<int:match_id>")
def match_view(match_id: int):
    conn = get_db()
    m = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if m is None:
        conn.close()
        abort(404)
    events = json.loads(m["events_json"])
    # §0.8: use cached lineups instead of re-deriving on every request
    home_xi, home_bench, home_form = [], [], ""
    away_xi, away_bench, away_form = [], [], ""
    try:
        home_xi, home_bench, home_form = _get_club_lineup(m["home_club"], conn)
        away_xi, away_bench, away_form = _get_club_lineup(m["away_club"], conn)
    except Exception:
        pass

    # Parse MOTM from events (look for the highest-rated player if we had ratings)
    # For now, just pass events; the template will show all event types
    conn.close()
    return render_template(
        "match.html", match=m, events=events,
        home_xi=home_xi, away_xi=away_xi,
        home_bench=home_bench, away_bench=away_bench,
        home_formation=home_form, away_formation=away_form,
    )


@app.route("/club/<path:club_name>")
def club_view(club_name: str):
    conn = get_db()
    # §0.8: use cached lineup
    xi, bench, formation = _get_club_lineup(club_name, conn)
    strength = starting_xi_strength(xi)
    # also load squad for the full-squad table (with morale)
    squad = load_club_squad(conn, club_name)
    # fetch morale for each player
    morale_map: dict[int, int] = {}
    for p in squad:
        row = conn.execute("SELECT morale FROM players WHERE uid = ?", (p["uid"],)).fetchone()
        morale_map[p["uid"]] = row["morale"] if row else 10
    conn.close()
    return render_template(
        "club.html", club=club_name, xi=xi, bench=bench,
        strength=strength, squad=squad, formation=formation,
        morale_map=morale_map,
    )


# ---------------------------------------------------------------------------
# API (used by live event feed JS)
# ---------------------------------------------------------------------------

@app.route("/api/match/<int:match_id>")
def api_match(match_id: int):
    conn = get_db()
    m = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if m is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    events = json.loads(m["events_json"])
    conn.close()
    return jsonify({
        "match_id": m["id"],
        "season_id": m["season_id"],
        "round": m["round"],
        "home_club": m["home_club"],
        "away_club": m["away_club"],
        "home_score": m["home_score"],
        "away_score": m["away_score"],
        "events": events,
    })


@app.route("/api/season/<season_id>/standings")
def api_standings(season_id: str):
    conn = get_db()
    season = _get_season(conn, season_id)
    if season is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    clubs = json.loads(season["clubs_json"])
    standings = _compute_standings(conn, season_id, clubs)
    conn.close()
    return jsonify(standings.to_list())


if __name__ == "__main__":
    # ensure DB exists & matches table is initialized
    conn = get_db()
    init_persistence(conn)
    conn.close()
    app.run(host="127.0.0.1", port=5000, debug=True)
