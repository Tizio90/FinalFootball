"""Smoke test: simulate one match and print the event feed."""
import os
import random
import sqlite3
import sys

# Make project root importable
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from engine import play_match, load_club_squad, pick_best_xi
from engine.attributes import DB_PATH

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # pick two clubs with deep squads
    cur = conn.execute(
        "SELECT club, COUNT(*) AS n FROM players GROUP BY club ORDER BY n DESC LIMIT 2"
    )
    clubs = [r["club"] for r in cur.fetchall()]
    home, away = clubs[0], clubs[1]
    print(f"=== {home} vs {away} ===")

    home_squad = load_club_squad(conn, home)
    away_squad = load_club_squad(conn, away)
    home_xi, home_bench = pick_best_xi(home_squad, conn)
    away_xi, away_bench = pick_best_xi(away_squad, conn)

    result = play_match(home, away, home_xi, away_xi,
                        home_bench, away_bench, seed=42)
    print(f"\nFINAL: {home} {result.home_score} - {result.away_score} {away}\n")
    print("Events:")
    for ev in result.events:
        marker = ""
        if ev.type == "goal":
            marker = " *** GOAL ***"
        print(f"  {ev.text}{marker}")
    print(f"\nStats: shots={result.stats.shots}  on_target={result.stats.shots_on_target}  "
          f"corners={result.stats.corners}  poss={result.stats.possession}")

    conn.close()

if __name__ == "__main__":
    main()
