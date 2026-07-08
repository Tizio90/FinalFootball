"""
Career helpers: injury tracking, morale updates, season summaries,
transfers, and cup competition logic for Phase 2B.

All functions take a sqlite3.Connection and are pure logic — no Flask.
"""
from __future__ import annotations

import json
import random
import sqlite3
import datetime as dt
from typing import Any

from engine.lineup import load_club_squad, pick_best_xi, pick_formation_for_squad
from engine.match import play_match


# ---------------------------------------------------------------------------
# Injury tracking (2B-2)
# ---------------------------------------------------------------------------

INJURY_TYPES = [
    "Hamstring strain", "Ankle sprain", "Knock", "Calf strain",
    "Groin strain", "Dead leg", "Bruised rib", "Minor concussion",
]


def record_injuries_from_match(conn: sqlite3.Connection, result, career_id: str,
                                season_id: str, current_round: int) -> list[dict]:
    """Scan a match result's events for injuries and persist them.

    Returns a list of injury dicts that were recorded.
    Each injury: {player_uid, player_name, club, matches_out, type}
    """
    injuries = []
    now = dt.datetime.now().isoformat()

    for ev in result.events:
        if ev.type != "injury":
            continue
        # determine which club the injured player plays for
        side = ev.side  # 'home' or 'away'
        club = result.home_club if side == "home" else result.away_club
        if not ev.player_uids:
            continue
        uid = ev.player_uids[0]
        name = ev.player_names[0] if ev.player_names else "Unknown"

        # matches_out: 1-3, weighted by Inj Pr (already factored into the
        # engine's injury probability, but the duration is random here)
        matches_out = random.randint(1, 3)

        injury = {
            "player_uid": uid,
            "player_name": name,
            "club": club,
            "matches_out": matches_out,
            "type": random.choice(INJURY_TYPES),
        }
        injuries.append(injury)

        conn.execute(
            "INSERT INTO injuries (career_id, season_id, player_uid, player_name, "
            "club, injury_round, matches_out, matches_remaining, injury_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (career_id, season_id, uid, name, club,
             current_round, matches_out, matches_out, injury["type"], now),
        )

    # also apply morale penalty for injured players
    for inj in injuries:
        _adjust_morale(conn, inj["player_uid"], -5)

    conn.commit()
    return injuries


def get_injured_players(conn: sqlite3.Connection, season_id: str,
                        club: str | None = None) -> list[dict]:
    """Return currently-injured players (matches_remaining > 0).
    Optionally filter by club."""
    if club:
        cur = conn.execute(
            "SELECT * FROM injuries WHERE season_id = ? AND club = ? "
            "AND matches_remaining > 0 ORDER BY injury_round DESC",
            (season_id, club),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM injuries WHERE season_id = ? "
            "AND matches_remaining > 0 ORDER BY injury_round DESC",
            (season_id,),
        )
    return [dict(r) for r in cur.fetchall()]


def decrement_injury_counters(conn: sqlite3.Connection, season_id: str,
                              clubs_played: list[str]) -> None:
    """After a round is played, decrement matches_remaining for all active
    injuries of clubs that played this round. When it reaches 0, the player
    is available again."""
    if not clubs_played:
        return
    placeholders = ",".join("?" * len(clubs_played))
    conn.execute(
        f"UPDATE injuries SET matches_remaining = matches_remaining - 1 "
        f"WHERE season_id = ? AND matches_remaining > 0 "
        f"AND club IN ({placeholders})",
        [season_id] + clubs_played,
    )
    conn.commit()


def is_player_injured(conn: sqlite3.Connection, season_id: str, uid: int) -> bool:
    """Check if a player is currently injured."""
    row = conn.execute(
        "SELECT 1 FROM injuries WHERE season_id = ? AND player_uid = ? "
        "AND matches_remaining > 0 LIMIT 1",
        (season_id, uid),
    ).fetchone()
    return row is not None


def filter_injured_from_squad(squad: list[dict], conn: sqlite3.Connection,
                               season_id: str) -> list[dict]:
    """Remove injured players from a squad list."""
    return [p for p in squad if not is_player_injured(conn, season_id, p["uid"])]


# ---------------------------------------------------------------------------
# Morale system (2B-2)
# ---------------------------------------------------------------------------

def _adjust_morale(conn: sqlite3.Connection, uid: int, delta: int) -> None:
    """Adjust a player's morale by delta, clamped to [0, 100]."""
    conn.execute(
        "UPDATE players SET morale = MAX(0, MIN(100, morale + ?)) WHERE uid = ?",
        (delta, uid),
    )


def update_morale_after_match(conn: sqlite3.Connection, result,
                              season_id: str) -> None:
    """Update morale for all players involved in a match.

    Rules (from prompt_phase2b.md §1 2B-2):
      Win: +3 to all starters, +1 to bench
      Loss: -3 to all starters, -1 to bench
      Draw: no change
      Goal scored: +2 to scorer, +1 to assister
      Yellow card: -1
      Red card: -3
    """
    home_club = result.home_club
    away_club = result.away_club

    # win/loss morale
    if result.home_score > result.away_score:
        # home win
        for p in result.home_xi:
            if p.get("uid"):
                _adjust_morale(conn, p["uid"], +3)
        for p in result.home_bench:
            if p.get("uid"):
                _adjust_morale(conn, p["uid"], +1)
        for p in result.away_xi:
            if p.get("uid"):
                _adjust_morale(conn, p["uid"], -3)
        for p in result.away_bench:
            if p.get("uid"):
                _adjust_morale(conn, p["uid"], -1)
    elif result.home_score < result.away_score:
        # away win
        for p in result.away_xi:
            if p.get("uid"):
                _adjust_morale(conn, p["uid"], +3)
        for p in result.away_bench:
            if p.get("uid"):
                _adjust_morale(conn, p["uid"], +1)
        for p in result.home_xi:
            if p.get("uid"):
                _adjust_morale(conn, p["uid"], -3)
        for p in result.home_bench:
            if p.get("uid"):
                _adjust_morale(conn, p["uid"], -1)
    # draw: no morale change for result

    # goal scorers + assisters
    for ev in result.events:
        if ev.type == "goal" and ev.player_uids:
            scorer_uid = ev.player_uids[0]
            _adjust_morale(conn, scorer_uid, +2)
            if len(ev.player_uids) > 1:
                assister_uid = ev.player_uids[1]
                _adjust_morale(conn, assister_uid, +1)

    # cards
    for ev in result.events:
        if ev.type == "card" and ev.player_uids:
            # check if red or yellow from the text
            is_red = "RED" in ev.text.upper()
            delta = -3 if is_red else -1
            _adjust_morale(conn, ev.player_uids[0], delta)

    conn.commit()


# ---------------------------------------------------------------------------
# Season summaries (2B-2)
# ---------------------------------------------------------------------------

def compute_season_summary(conn: sqlite3.Connection, season_id: str,
                           user_club: str | None = None) -> dict:
    """Compute end-of-season summary: top scorers, assists, best XI, etc.

    Reads all matches in the season and aggregates player stats from events.
    """
    matches = conn.execute(
        "SELECT * FROM matches WHERE season_id = ? AND played = 1 ORDER BY round, id",
        (season_id,),
    ).fetchall()

    # aggregate goals + assists per player
    scorer_stats: dict[int, dict] = {}  # uid -> {name, club, goals, assists}

    for m in matches:
        events = json.loads(m["events_json"])
        for ev in events:
            if ev["type"] == "goal" and ev.get("player_uids"):
                scorer_uid = ev["player_uids"][0]
                scorer_name = ev["player_names"][0] if ev.get("player_names") else "Unknown"
                # determine club from side
                club = m["home_club"] if ev["side"] == "home" else m["away_club"]
                if scorer_uid not in scorer_stats:
                    scorer_stats[scorer_uid] = {
                        "uid": scorer_uid, "name": scorer_name, "club": club,
                        "goals": 0, "assists": 0,
                    }
                scorer_stats[scorer_uid]["goals"] += 1
                if len(ev["player_uids"]) > 1:
                    assister_uid = ev["player_uids"][1]
                    assister_name = ev["player_names"][1] if len(ev.get("player_names", [])) > 1 else "Unknown"
                    if assister_uid not in scorer_stats:
                        scorer_stats[assister_uid] = {
                            "uid": assister_uid, "name": assister_name, "club": club,
                            "goals": 0, "assists": 0,
                        }
                    scorer_stats[assister_uid]["assists"] += 1

    top_scorers = sorted(scorer_stats.values(), key=lambda x: x["goals"], reverse=True)[:10]
    top_assists = sorted(scorer_stats.values(), key=lambda x: x["assists"], reverse=True)[:10]

    # standings
    season = conn.execute("SELECT * FROM seasons WHERE id = ?", (season_id,)).fetchone()
    clubs = json.loads(season["clubs_json"])
    from sim.season import Standings
    standings = Standings(clubs)
    for m in matches:
        standings.record_result(m["home_club"], m["away_club"],
                               m["home_score"], m["away_score"])
    sorted_standings = standings.sorted_rows()

    # user's record
    user_record = None
    if user_club:
        for i, row in enumerate(sorted_standings):
            if row.club == user_club:
                user_record = {
                    "position": i + 1,
                    "played": row.played, "won": row.won, "drawn": row.drawn,
                    "lost": row.lost, "gf": row.gf, "ga": row.ga,
                    "points": row.points,
                }
                break

    return {
        "top_scorers": top_scorers,
        "top_assists": top_assists,
        "standings": sorted_standings,
        "user_record": user_record,
        "total_matches": len(matches),
        "total_goals": sum(s["goals"] for s in scorer_stats.values()),
    }


# ---------------------------------------------------------------------------
# Transfers (2B-3)
# ---------------------------------------------------------------------------

def get_transfer_budget(conn: sqlite3.Connection, club: str) -> int:
    """Estimate a club's transfer budget based on its squad's total value."""
    row = conn.execute(
        "SELECT COALESCE(SUM(transfer_value), 0) as total FROM players WHERE club = ?",
        (club,),
    ).fetchone()
    # budget = 10% of squad value, min $500k
    budget = int(row["total"] * 0.10) if row["total"] > 0 else 0
    return max(500_000, budget)


def get_club_reputation(conn: sqlite3.Connection, club: str) -> int:
    """Compute a club's reputation (0-100) based on division tier + squad CA."""
    # division tier: top division = 100, second = 80, etc.
    row = conn.execute(
        "SELECT d.based_raw, d.player_count FROM clubs c "
        "JOIN divisions d ON c.division_id = d.id "
        "WHERE c.name = ?", (club,)
    ).fetchone()
    tier_score = 50  # default
    if row:
        # use player_count as a proxy for division quality
        tier_score = min(100, row["player_count"] // 20)

    # squad CA
    squad = load_club_squad(conn, club)
    if squad:
        xi, _ = pick_best_xi(squad, conn, formation="4-3-3")
        from engine.lineup import starting_xi_strength
        strength = starting_xi_strength(xi)
        ca_score = int(strength["OVERALL"])
    else:
        ca_score = 50

    # weighted: 40% tier, 60% squad CA
    return int(tier_score * 0.4 + ca_score * 0.6)


def evaluate_transfer_bid(conn: sqlite3.Connection, player_uid: int,
                          buying_club: str, bid_amount: int) -> dict:
    """Evaluate whether a transfer bid is accepted.

    Returns {accepted: bool, reason: str, fee: int}.
    """
    # load player info
    player = conn.execute(
        "SELECT p.*, c.player_count as club_squad_size FROM players p "
        "LEFT JOIN clubs c ON p.club = c.name WHERE p.uid = ?",
        (player_uid,),
    ).fetchone()
    if player is None:
        return {"accepted": False, "reason": "Player not found", "fee": 0}

    # can't buy your own player
    if player["club"] == buying_club:
        return {"accepted": False, "reason": "Player already at your club", "fee": 0}

    player_value = player["transfer_value"] or 0
    if player_value <= 0:
        player_value = 500_000  # default minimum

    # if player is "Not for Sale" (-1), require 3x default value
    if player["transfer_value"] == -1:
        required = 1_500_000 * 3
        if bid_amount < required:
            return {"accepted": False,
                    "reason": f"Not for sale — would need ${required:,}",
                    "fee": 0}

    # acceptance: bid must be >= 80% of player value (room for negotiation)
    if bid_amount < player_value * 0.8:
        return {"accepted": False,
                "reason": f"Bid too low — player valued at ${player_value:,}",
                "fee": 0}

    # player ambition: high ambition = more likely to push for move
    attrs = conn.execute(
        'SELECT "Amb" as amb FROM player_attributes WHERE uid = ?',
        (player_uid,),
    ).fetchone()
    ambition = attrs["amb"] if attrs and attrs["amb"] else 50

    # buying club reputation
    buying_rep = get_club_reputation(conn, buying_club)
    selling_rep = get_club_reputation(conn, player["club"] or "")

    # if buying club has higher reputation, player is more willing
    rep_diff = buying_rep - selling_rep
    # base acceptance chance
    base_chance = 0.5
    # bid above value increases chance
    if bid_amount >= player_value:
        base_chance += 0.2
    if bid_amount >= player_value * 1.5:
        base_chance += 0.15
    # reputation difference
    base_chance += rep_diff * 0.005  # +0.5% per reputation point
    # ambition (0-100): high ambition = more likely to move
    base_chance += (ambition - 50) * 0.003

    base_chance = max(0.05, min(0.95, base_chance))

    if random.random() < base_chance:
        return {
            "accepted": True,
            "reason": f"{player['name']} joins {buying_club}!",
            "fee": bid_amount,
            "player_name": player["name"],
            "from_club": player["club"],
        }
    else:
        return {
            "accepted": False,
            "reason": f"{player['name']} or {player['club']} rejected the offer",
            "fee": 0,
        }


def execute_transfer(conn: sqlite3.Connection, player_uid: int, buying_club: str,
                     fee: int, career_id: str, season_id: str) -> None:
    """Execute an accepted transfer: move the player + record it."""
    player = conn.execute("SELECT * FROM players WHERE uid = ?", (player_uid,)).fetchone()
    if player is None:
        return
    now = dt.datetime.now().isoformat()
    # update player's club
    conn.execute("UPDATE players SET club = ? WHERE uid = ?", (buying_club, player_uid))
    # record the transfer
    conn.execute(
        "INSERT INTO transfers (career_id, season_id, player_uid, player_name, "
        "from_club, to_club, fee, transfer_window, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (career_id, season_id, player_uid, player["name"],
         player["club"], buying_club, fee, "mid-season", now),
    )
    conn.commit()


def get_signings_count(conn: sqlite3.Connection, career_id: str,
                       season_id: str) -> int:
    """Count how many players the user's club has signed this season."""
    career = conn.execute("SELECT club FROM careers WHERE id = ?", (career_id,)).fetchone()
    if not career:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) as n FROM transfers WHERE season_id = ? AND to_club = ?",
        (season_id, career["club"]),
    ).fetchone()
    return row["n"]


# ---------------------------------------------------------------------------
# Cup competition (2B-3)
# ---------------------------------------------------------------------------

CUP_ROUND_NAMES = {
    32: "Round of 32",
    16: "Round of 16",
    8: "Quarter-Finals",
    4: "Semi-Finals",
    2: "Final",
}


def generate_cup_fixtures(conn: sqlite3.Connection, career_id: str,
                          season_id: str, clubs: list[str],
                          cup_name: str = "Continental Cup",
                          seed: int = 42) -> None:
    """Generate a random-draw single-elimination cup with 32 clubs.

    If fewer than 32 clubs are available, use the next power of 2.
    """
    rng = random.Random(seed)
    # take up to 32 clubs, must be a power of 2
    n = min(32, len(clubs))
    # find largest power of 2 <= n
    while n & (n - 1) != 0:
        n -= 1
    participants = clubs[:n]
    rng.shuffle(participants)

    now = dt.datetime.now().isoformat()
    # generate round of N fixtures
    for i in range(0, n, 2):
        round_name = CUP_ROUND_NAMES.get(n, f"Round of {n}")
        conn.execute(
            "INSERT INTO cup_fixtures (career_id, season_id, cup_name, round, "
            "round_name, home_club, away_club, played, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (career_id, season_id, cup_name, n, round_name,
             participants[i], participants[i + 1], now),
        )
    conn.commit()


def get_pending_cup_fixtures(conn: sqlite3.Connection, season_id: str) -> list[dict]:
    """Return all unplayed cup fixtures for this season."""
    cur = conn.execute(
        "SELECT * FROM cup_fixtures WHERE season_id = ? AND played = 0 "
        "ORDER BY round DESC, id",
        (season_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def play_cup_round(conn: sqlite3.Connection, season_id: str,
                   career_id: str, user_club: str | None = None,
                   user_formation: str = "4-3-3",
                   user_mentality: str = "balanced") -> list[dict]:
    """Play the current pending cup round. Returns results list."""
    pending = get_pending_cup_fixtures(conn, season_id)
    if not pending:
        return []

    # group by round (should all be the same round)
    current_round = pending[0]["round"]
    round_fixtures = [f for f in pending if f["round"] == current_round]

    rng = random.Random(hash(season_id) + current_round + 999)
    results = []
    winners = []

    for fixture in round_fixtures:
        home, away = fixture["home_club"], fixture["away_club"]
        # build XIs
        home_squad = load_club_squad(conn, home)
        away_squad = load_club_squad(conn, away)
        # if either squad is too small, give them a bye (auto-win)
        if len(home_squad) < 7:
            winner = away
            conn.execute(
                "UPDATE cup_fixtures SET home_score = 0, away_score = 3, played = 1 "
                "WHERE id = ?", (fixture["id"],),
            )
            winners.append(winner)
            results.append({
                "home": home, "away": away,
                "home_score": 0, "away_score": 3,
                "winner": winner, "round_name": fixture["round_name"],
            })
            continue
        if len(away_squad) < 7:
            winner = home
            conn.execute(
                "UPDATE cup_fixtures SET home_score = 3, away_score = 0, played = 1 "
                "WHERE id = ?", (fixture["id"],),
            )
            winners.append(winner)
            results.append({
                "home": home, "away": away,
                "home_score": 3, "away_score": 0,
                "winner": winner, "round_name": fixture["round_name"],
            })
            continue

        home_formation = pick_formation_for_squad(home_squad)
        away_formation = pick_formation_for_squad(away_squad)
        hx, hb = pick_best_xi(home_squad, conn, formation=home_formation)
        ax, ab = pick_best_xi(away_squad, conn, formation=away_formation)

        # user's club uses their tactics
        home_ment = user_mentality if home == user_club else "balanced"
        away_ment = user_mentality if away == user_club else "balanced"
        if home == user_club:
            hx, hb = pick_best_xi(home_squad, conn, formation=user_formation)
        if away == user_club:
            ax, ab = pick_best_xi(away_squad, conn, formation=user_formation)

        m_seed = rng.randrange(0, 2**31)
        result = play_match(home, away, hx, ax, hb, ab, seed=m_seed,
                           home_mentality=home_ment, away_mentality=away_ment)

        # cup matches can't end in a draw — go to penalties if tied
        if result.home_score == result.away_score:
            # simple penalty shootout: 70% chance home wins
            if rng.random() < 0.50:
                result.home_score += 1
            else:
                result.away_score += 1

        winner = home if result.home_score > result.away_score else away
        winners.append(winner)

        conn.execute(
            "UPDATE cup_fixtures SET home_score = ?, away_score = ?, played = 1 "
            "WHERE id = ?",
            (result.home_score, result.away_score, fixture["id"]),
        )

        results.append({
            "home": home, "away": away,
            "home_score": result.home_score, "away_score": result.away_score,
            "winner": winner, "round_name": fixture["round_name"],
        })

    # generate next round if we have enough winners
    if len(winners) >= 2:
        next_round = current_round // 2
        now = dt.datetime.now().isoformat()
        rng2 = random.Random(hash(season_id) + next_round + 888)
        rng2.shuffle(winners)
        for i in range(0, len(winners), 2):
            if i + 1 < len(winners):
                round_name = CUP_ROUND_NAMES.get(next_round, f"Round of {next_round}")
                conn.execute(
                    "INSERT INTO cup_fixtures (career_id, season_id, cup_name, round, "
                    "round_name, home_club, away_club, played, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (career_id, season_id, "Continental Cup", next_round, round_name,
                     winners[i], winners[i + 1], now),
                )

    conn.commit()
    return results


def get_cup_results(conn: sqlite3.Connection, season_id: str) -> list[dict]:
    """Return all played cup fixtures."""
    cur = conn.execute(
        "SELECT * FROM cup_fixtures WHERE season_id = ? AND played = 1 "
        "ORDER BY round DESC, id",
        (season_id,),
    )
    return [dict(r) for r in cur.fetchall()]
