# Football Management Simulation — Phase 1

A text-based football (soccer) management simulation in the spirit of Football
Manager, built around real player attribute data. Phase 1 delivers an
attribute-driven match engine, a single league season, and a clean local web
UI for browsing fixtures, results, and per-match event feeds.

## Stack

- **Python 3.10+** standard library
- **SQLite** (via `sqlite3`) for player, club, match, and season storage
- **Flask** (single `pip install`) for the local web UI
- No game engines, no Node tooling, no Docker, no external database server

## Quick start

```bash
# one-time install of Flask
pip install flask

# from this directory:
python run.py ingest          # CSV -> SQLite (idempotent)
python run.py serve           # browse at http://127.0.0.1:5000/
```

Other entry points:

```bash
python run.py season          # full season in terminal
python run.py validate        # 500-match validation suite
python run.py match "KSA" "Midwest United"   # one-off match with event feed
```

## Project layout

```
football_sim/
├── data/
│   ├── ingest.py                CSV -> SQLite (idempotent, multi-part, UID-dedup)
│   ├── merged_players_part1.csv source data
│   └── football.db              generated SQLite database
├── engine/
│   ├── attributes.py            CA / suitability / form-roll logic
│   ├── lineup.py                best-XI picker for 4-3-3
│   └── match.py                 the match engine (possessions, duels, events)
├── sim/
│   └── season.py                round-robin fixtures + standings + persistence
├── ui/
│   ├── app.py                   Flask routes
│   └── templates/               base / home / season / match / club
├── tests/
│   └── test_engine.py           500-match validation suite
├── scripts/
│   └── smoke_match.py           one-match smoke test
└── run.py                       entry point (ingest / season / validate / serve / match)
```

## Match engine overview

Each match is simulated as ~80 discrete "phases" (attacking possessions)
distributed across 90 minutes proportional to midfield share. Each phase
flows through a chain of opposed-attribute duels:

1. **Build-up** — passer `Pas/Tec/Vis/Cmp/Dec` vs. presser `Ant/Pos/Tck/Agg/Wor`
2. **Final third** — attacker `Dri/Agi/Fla/OtB/Bal/Tec` vs. marker `Tck/Ant/Pos/Str/Mar`
3. **Shot** — shooter `Fin/Tec/Cmp/OtB/Dec/Lon` minus pressure `Tck/Pos/Mar`, with on-target threshold
4. **Save** — keeper `Ref/1v1/Pos/Han/Cmd` vs. shot quality

Fatigue: each tick, physical attributes (`Acc/Agi/Pac/Sta/Str/Jum`) decay
proportional to `(1 - stamina recovery)`. Substitutions at minutes 60/70/75
replace the most-fatigued starter at each family with the best bench
replacement (max 3 subs per side).

Variance: per-player per-match form roll driven by `Cons/Temp/Pres/Imp M`,
scaled so stable players swing ±5%, volatile players swing ±20%.

The scoreline is **never computed separately** — it is the count of `goal`
events emitted by the engine. Stats (shots, on-target, corners, possession)
are accumulated from the same event stream.

## Validation

`python run.py validate` runs three batch tests:

1. **Strong vs Weak** — 500 matches between the strongest and weakest clubs
   by overall CA. Pass = strong side wins ≥65%.
2. **Close matchup** — 500 matches between two closely-rated mid-table clubs.
   Pass = neither side wins >75%, avg goals ≤3.2, ≥45% within 1 goal.
3. **Goal distribution** — 500 random matches. Pass = avg goals in 0.8–2.6
   band (real-world average is ~1.3).

Current results (seed-dependent):

```
Strong (Minn. Utd Academy, CA 10.42) vs Weak (Juventus SC, CA 9.30)
  Strong wins: 78.0%   Draws: 19.4%   Upsets: 2.6%

Close: OCSC Academy (10.09) vs Louisiana TDP Elite (10.06)
  A wins: 19.2%   Draws: 22.0%   B wins: 58.8%   avg goals 2.57

Distribution:  avg goals/match 2.08
```

## Data ingestion

`data/ingest.py` is idempotent and multi-part-ready:

- Drops & recreates the schema on each run (safe to re-run).
- Accepts a list of CSV paths; merges by `UID`, keeping the first occurrence.
- Parses `DOB`, `Height` (5'9" → cm), `Weight` (65 kg → numeric),
  `Position` (`D/WB/AM (R)` → list of `(role, side)` tuples),
  `Preferred Foot`, `Left/Right Foot` (categorical → 1..5).
- Renames `Nat.1` → `NatFit` to avoid collision with `Nat` (nationality).
- Indexes `club`, `based`, `primary_family`, `player_positions.family`.

When the full 90k+ dataset arrives as multiple `merged_players_partN.csv`
files in `data/`, re-running `python run.py ingest` will pick them all up
automatically.

## Web UI features

- **Home**: list of seasons, "start new season" form (clubs, seed, single/
  double round-robin, simulate-all vs. round-by-round).
- **Season**: standings table (P/W/D/L/GF/GA/GD/Pts + last-5 form), fixtures
  grouped by round (collapsible), round-by-round simulation controls.
- **Match**: final score header, scrolling minute-by-minute event feed
  (goals highlighted, color-coded by event type), both lineups + benches.
- **Club**: starting XI in 4-3-3, bench, full squad with per-family CA.

## Phase 1 scope — what's done, what's not

**Done:**
- CSV → SQLite ingestion with all attribute normalization
- Position-family CA model + position-suitability scoring
- Greedy best-XI picker in 4-3-3 per club
- Possession/phase match engine with opposed-attribute duels, fatigue, subs
- Round-robin fixtures (single or double)
- Standings table with full tiebreakers
- Persistent match history (SQLite) + Flask UI to browse it
- 500-match validation suite

**Out of scope for Phase 1** (roadmap items):
- Transfers, contracts, training, attribute progression
- Scouting, staff, finances, press interactions
- Multiple divisions / promotion-relegation
- Save/load across sessions (basic SQLite persistence only)
- 2D pitch view (text/tables only by design)
