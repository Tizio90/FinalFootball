#!/usr/bin/env python3
"""
Football Management Simulation — Phase 1 entry point.

Usage:
  python run.py ingest        # ingest CSV(s) into SQLite (idempotent)
  python run.py season        # quick full season in terminal
  python run.py validate      # 500-match validation suite
  python run.py serve         # Flask UI on http://127.0.0.1:5000
  python run.py match CLUB_A CLUB_B   # one-off match with full feed

Default (no args): serve the UI.
"""
from __future__ import annotations

import os
import sys

# Make project root importable when run as a script
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import argparse


def cmd_ingest(args):
    from data.ingest import ingest, find_csv_parts
    parts = find_csv_parts()
    if not parts:
        print(f"No merged_players*.csv files found in {os.path.join(_HERE, 'data')}")
        sys.exit(1)
    print(f"Ingesting {len(parts)} CSV part(s)...")
    stats = ingest(parts)
    print("Done:", stats)


def cmd_season(args):
    from sim.season import run_season
    res = run_season(n_clubs=args.n_clubs, double=not args.single,
                     seed=args.seed, verbose=True)
    print("\n=== Final Standings ===")
    for i, row in enumerate(res.standings.sorted_rows(), 1):
        print(f"{i:2d}. {row.club:30s}  P={row.played:2d}  W={row.won:2d}  "
              f"D={row.drawn:2d}  L={row.lost:2d}  GF={row.gf:3d}  "
              f"GA={row.ga:3d}  GD={row.gd:+4d}  Pts={row.points:3d}  "
              f"Form={''.join(row.form[-5:])}")


def cmd_validate(args):
    from tests.test_engine import main as validate_main
    validate_main()


def cmd_serve(args):
    from ui.app import app
    # ensure DB is present & persistent tables exist
    from engine.attributes import DB_PATH
    if not os.path.exists(DB_PATH):
        print("DB missing -- running ingestion first...")
        cmd_ingest(args)
    import sqlite3
    from sim.season import init_persistence
    conn = sqlite3.connect(DB_PATH)
    init_persistence(conn)
    conn.close()
    print(f"\n  Football Sim UI starting at http://127.0.0.1:{args.port}/\n")
    app.run(host=args.host, port=args.port, debug=args.debug)


def cmd_match(args):
    import sqlite3
    from engine.attributes import DB_PATH
    from engine.lineup import load_club_squad, pick_best_xi
    from engine.match import play_match

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    home, away = args.home, args.away
    hs = load_club_squad(conn, home); as_ = load_club_squad(conn, away)
    hx, hb = pick_best_xi(hs, conn); ax, ab = pick_best_xi(as_, conn)
    r = play_match(home, away, hx, ax, hb, ab, seed=args.seed)
    print(f"\n=== {home} vs {away} ===\n")
    for ev in r.events:
        marker = " *** GOAL ***" if ev.type == "goal" else ""
        print(f"  {ev.text}{marker}")
    print(f"\nFINAL: {home} {r.home_score} - {r.away_score} {away}")
    print(f"Stats: shots={r.stats.shots}  on_target={r.stats.shots_on_target}  "
          f"corners={r.stats.corners}  poss={r.stats.possession}")
    conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    p_ingest = sub.add_parser("ingest", help="ingest CSV(s) -> SQLite")
    p_ingest.set_defaults(func=cmd_ingest)

    p_season = sub.add_parser("season", help="simulate a full season (terminal)")
    p_season.add_argument("-n", "--n-clubs", type=int, default=20)
    p_season.add_argument("-s", "--seed", type=int, default=1)
    p_season.add_argument("--single", action="store_true",
                          help="single round-robin (default: double)")
    p_season.set_defaults(func=cmd_season)

    p_val = sub.add_parser("validate", help="run engine validation tests")
    p_val.set_defaults(func=cmd_validate)

    p_serve = sub.add_parser("serve", help="start Flask UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=5000)
    p_serve.add_argument("--debug", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_match = sub.add_parser("match", help="simulate one match (full feed)")
    p_match.add_argument("home")
    p_match.add_argument("away")
    p_match.add_argument("-s", "--seed", type=int, default=42)
    p_match.set_defaults(func=cmd_match)

    args = p.parse_args()
    if not getattr(args, "func", None):
        # default: serve UI
        args = argparse.Namespace(host="127.0.0.1", port=5000, debug=False)
        cmd_serve(args)
        return
    args.func(args)


if __name__ == "__main__":
    main()
