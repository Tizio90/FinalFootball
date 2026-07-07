# Build Prompt: Football Management Simulation — Phase 2 (Improvements & Features)

You are continuing a multi-phase Football Manager-style simulation project. **Phase 1 is built and working** — a Python 3 + SQLite + Flask app that ingests a real player dataset, picks best XIs in 4-3-3, runs an attribute-driven match engine, simulates a single-league season, and serves a local web UI with standings, fixtures, and per-match event feeds.

This prompt defines **Phase 2**: improvements to the existing engine realism, plus the next layer of management features. As with Phase 1, depth goes into the *simulation logic*, not graphical polish.

---

## 0. Phase 1 — Code-Level Review (read this before touching anything)

Before extending anything, understand what's already there and where its weak points are. This is a *concrete* review, not generic.

### What works (don't break it)

- **Ingestion is solid** (`data/ingest.py`): idempotent, multi-part-ready, UID-deduped, handles the `Nat.1` collision by renaming to `NatFit`, parses `Position` strings into structured `(role, side)` tuples, normalizes height/weight/foot-strength. Indexed on `club`, `based`, `primary_family`.
- **Validation suite passes** (`tests/test_engine.py`): 500-match strong-vs-weak test shows 76–87% win rate for the stronger side, 500-match goal-distribution test averages ~2.0 goals/match (realistic). Both regression-threshold tests must keep passing after any engine change.
- **Round-robin fixtures** (`sim/season.py`): correct classic algorithm, single or double, BYE handling for odd N.
- **Standings**: P/W/D/L/GF/GA/GD/Pts + last-5 form, persisted to SQLite, sortable.
- **Flask UI**: home / season / match / club pages, all return 200, debug mode ON by default.

### Concrete bugs and tech debt to fix FIRST

These are real defects in the current code, found by reading it carefully. Fix them before adding any new features.

1. **Fatigue math is opaque and brittle** (`engine/match.py:160-178` and `:194-200`).
   The line `base = base * (1.0 - decay * 100)` uses a magic `* 100` to convert accumulated phase count into a percentage. The same logic is duplicated in `_team_strength()`. This is correct only by coincidence — refactor into a single helper `_fatigue_multiplier(player)` returning a 0..1 multiplier, used by both `_eff` and `_team_strength`. Add a unit test that verifies a player with `Sta=1` ends the match at ~60% effectiveness and a player with `Sta=20` ends at ~90%.

2. **Assists are never recorded** (`engine/match.py:443`).
   Every goal calls `_score_goal(..., assist=None)`. The shooter's pass-receiver (the player who made the key pass in step 1 of the phase chain) should be recorded as the assist if they're on the same team as the scorer. Thread the passer through to `_attempt_shot` and on to `_score_goal`.

3. **No yellow/red cards despite the stats dict tracking them** (`engine/match.py:75`).
   `MatchStats.cards` is initialized but never incremented. Add a card-event check: after a failed tackle (in `_simulate_phase` step 2), small probability of a card driven by the tackler's `Agg` + `Dirt` vs. the referee's strictness (a per-match roll). Yellow ~3-5% of fouls, red ~0.2%. Emit a `card` event with the player and minute.

4. **No fouls at all** — tackles are won/lost cleanly in the event log. Add a foul outcome: when the tackler's `Agg` is high and the duel is lost by a wide margin, it's a foul (free kick to the attacker's team). Free kicks have a small chance of becoming a shot (using `Fre` for the taker).

5. **Possession is computed ONCE at match start** (`engine/match.py:230`) and never re-evaluated.
   A side that goes 3-0 down should not maintain 55% possession. Re-evaluate the possession split every ~10 phases based on current scoreline (losing side attacks more, winning side sits deeper).

6. **Lineup picker is greedy slot-by-slot** (`engine/lineup.py:88-127`), which can produce a sub-optimal XI (e.g., the best RB is also the best RCB, and picking them at RB forces a worse RCB). Replace with a global assignment: build a `(player × slot)` suitability matrix, then solve with the Hungarian algorithm (`scipy.optimize.linear_sum_assignment` — `scipy` is a one-line `pip install`) to maximize total suitability across all 11 slots.

7. **Standings tiebreaker is weak** (`sim/season.py:85-90`): points → GD → GF → name. Real leagues use head-to-head record first. Add: head-to-head points → head-to-head GD → head-to-head GF → overall GD → overall GF → name. Store per-pair results in a dict for the season.

8. **UI re-derives lineups on every match view** (`ui/app.py:259-268`), which calls `load_club_squad` + `pick_best_xi` for both clubs. Cache the per-club XI in a `club_lineups` table at season creation, or memoize per request. Becomes critical when the full 90k dataset arrives.

9. **No injury events** — the original prompt listed this as an optional stretch goal. `Inj Pr` attribute exists but is never read. Add: each phase, each starter has a tiny probability of a minor injury proportional to `Inj Pr` (and increased by opponent `Agg`); injured player is subbed off (or the team plays with 10 if no subs remain). Persist injuries across the season in Phase 3.

10. **`_phase_to_minute` produces clustered events** — the jitter is ±0.4 minutes, so two phases that happen to land at index 40 and 41 produce events at minute 32 and 33. Increase jitter to ±1.5 and ensure strictly increasing minutes within a phase sequence (sort events by minute at the end before emitting).

### Engine tuning observations (don't change without re-running validation)

Current averages after the last tuning pass:
- Avg goals/match: **2.04** (real-world ~1.3 — slightly high, acceptable)
- Strong-vs-weak win rate: **76–87%** (good)
- Close-matchup upset rate: **~20%** (good)
- 0-0 frequency: **16%** (real-world ~8% — too many 0-0s)

If goal count drops below 1.5 or rises above 2.5, or 0-0 frequency exceeds 20%, the engine needs re-tuning. The knobs are: `PHASES_PER_MATCH`, `on_target_threshold`, `keeper_quality + 1.0` advantage, and `pressure * 0.30` reduction. Document the trade-offs in a comment block above each.

---

## 1. Phase 2 Scope — what to build

Split Phase 2 into two milestones: **2A (realism)** and **2B (management)**. Ship 2A first, validate, then 2B.

### Milestone 2A — Engine Realism (no new UI, all logic)

**In scope:**

1. **Fix all 10 tech-debt items above**, in order. After each fix, re-run `python tests/test_engine.py` and confirm all 3 tests still pass.

2. **Formation flexibility** — currently hardcoded to 4-3-3 in `engine/lineup.py:FORMATION_433`. Add at least 3 more formations: 4-4-2, 4-2-3-1, 3-5-2. Each club should pick its formation based on squad composition (e.g., if a club has 4 natural CBs and few wingers, prefer 3-5-2). Store the chosen formation per club in a new `club_tactics` table.

3. **Mentality / team instructions** — add three mentalities: defensive, balanced, attacking. Mentality modifies:
   - Number of attacking phases per side (attacking mentality → +15% phases for that side, defensive → -15%)
   - Shot threshold (attacking → lower, defensive → higher, i.e., attacking takes more risks)
   - Defensive line height (affects pressure calculation — high line = more pressure on attacker but more through-ball risk)

4. **Set pieces** — corners and free kicks are counted but never simulated as events. Implement:
   - Corner: 25% chance of a shot, with `Hea`/`Jum` for attackers vs. `Hea`/`Jum`/`Pos` for defenders + GK
   - Free kick (direct): shot quality uses `Fre` (free kicks) + `Tec` + `Cmp`; wall reduces quality
   - Penalty: 75% conversion rate baseline, modified by shooter's `Pen` and GK's `1v1`

5. **Match statistics expansion** — track and display: fouls, offsides, shots off target, passes completed / attempted (with completion %), pass accuracy per team. Add a `MatchStats` field for each.

6. **Player-of-the-match** — after each match, compute a rating per player based on their duel involvements (passes completed, tackles won, shots on target, saves) and pick the top player. Display on the match page.

7. **Asymmetric home advantage** — current `home_advantage = 1.10` is fixed. Make it a per-club attribute derived from past home results in the season (a club on a 5-game home winning streak gets +1.15, a club with 5 home losses gets +1.05).

**Validation gate for 2A:** all 3 existing tests still pass, plus a new test: "Formation impact test" — same squad, simulated 200 times in 4-4-2 vs. 200 times in 3-5-2, the formation choice should produce a statistically different goal distribution (p < 0.05 on a chi-squared test).

### Milestone 2B — Management Features (new UI surfaces)

**In scope:**

1. **Manager mode** — the user picks a club at season start and makes decisions:
   - Choose formation (from the available set)
   - Choose mentality (defensive / balanced / attacking)
   - Choose starting XI manually (override the auto-picker) — drag-and-drop or select dropdowns
   - Make substitutions (pick which bench player replaces which starter, choose the minute)
   - The auto-sim continues for all OTHER matches in the round

2. **Transfer market (lightweight)** — at season start and mid-season window:
   - Each club has a list of "transfer-listed" players (those with low `Inf` status or low minutes played)
   - Each club can bid for players using a simplified `Transfer Value` (already in the dataset, currently unused)
   - Bids resolve based on player ambition (`Amb`) and the buying club's reputation (a per-club attribute derived from last-season finish)
   - Limit: 3 signings per window per club

3. **Injuries and morale (cross-match persistence)** — injuries from match N carry into match N+1 (a player flagged as injured sits out the next 1-3 matches). Morale: each player has a 1-20 morale value that goes up with wins/goals and down with losses/benching. Low morale degrades `Cmp` and `Dec` in matches.

4. **Multiple leagues + promotion/relegation** — once the full 90k dataset arrives, support a 2-division structure (e.g., 20 clubs in Division 1, 20 in Division 2). Top 3 from D2 promote, bottom 3 from D1 relegate. Cross-division fixtures in a cup competition (single-elimination, 32 teams).

5. **Save / load across sessions** — currently the SQLite DB persists everything but the user can't have multiple parallel careers. Add a `careers` table: each career has an ID, a chosen club, a current season, a current round, and a JSON blob of user tactical preferences. The UI lets the user switch careers.

6. **Season summaries** — at season end: league champions, top scorers (with goal counts), most assists, best XI of the season (one player per position), manager of the season (the AI manager whose club most exceeded expected points). Display on a new "Season Review" page.

**Validation gate for 2B:** manager mode must let the user win/lose based on their tactical choices — a "sanity test" where the user picks the weakest club and plays attacking 4-3-3 every game should lose ~70% of matches against stronger sides; picking defensive 5-4-1 should reduce goals conceded by ~30% but also reduce goals scored.

---

## 2. Hard Constraints (carry over from Phase 1, still apply)

- **No new heavy dependencies.** `scipy` (for the Hungarian algorithm) is acceptable. Anything else requires explicit justification.
- **No game engines, no Docker, no Node tooling, no external DB server.** Still Python 3 stdlib + SQLite + Flask.
- **One command to run.** `python run.py serve` must still bring up the whole UI. Don't fragment into multiple processes.
- **Performance:** the engine must still simulate a full 380-match season in under 5 seconds on a laptop. If 2A changes push it over, profile and optimize before moving to 2B.
- **Backwards compatibility:** the existing `merged_players_part1.csv` must still ingest cleanly. Don't change the DB schema in a way that requires re-ingesting — write a migration instead.

---

## 3. Suggested Order of Work

1. Read every file under `engine/`, `sim/`, and `ui/app.py`. Don't skim — the bugs listed in §0 are subtle.
2. Write a regression test for each §0 bug before fixing it (test-driven).
3. Fix §0 items 1-10 in order, re-running `python tests/test_engine.py` after each.
4. Implement Milestone 2A items 1-7. After each, add a new validation test.
5. Run the full validation suite, commit, then start Milestone 2B.
6. For 2B, ship manager mode + season summaries first (highest user value), transfers and multi-league last (highest complexity).

---

## 4. File-by-File Improvement Notes

| File | What to add / change |
|---|---|
| `data/ingest.py` | Add a `club_tactics` table (formation, mentality per club). Add a `careers` table. Add schema migration support (versioned schema with `PRAGMA user_version`). |
| `engine/attributes.py` | Add role-specific CA (e.g., ball-playing defender uses `Pas`/`Vis` more than a traditional CB). Add a `morale` field per player (defaults to 10, modified by match events). |
| `engine/lineup.py` | Replace greedy with Hungarian algorithm. Add formation-aware slot definitions. Add a "manual override" entry point that takes user-chosen XI + bench. |
| `engine/match.py` | Implement all of §0 items 1-5, 9, 10. Add set-piece simulation. Add foul/card logic. Expand `MatchStats`. Add player-of-the-match computation. |
| `sim/season.py` | Fix standings tiebreaker (§0 item 7). Add injury/morale carryover between matches. Add transfer window logic. Add multi-league + promotion/relegation. Add cup fixtures. |
| `ui/app.py` | Cache lineups. Add manager-mode routes (`/career/<id>/tactics`, `/career/<id>/match/<id>/live` for interactive matches). Add transfer-market UI. Add season-review page. |
| `ui/templates/` | Add `manager.html` (tactics picker), `transfers.html`, `season_review.html`, `live_match.html` (interactive sub dialogue). |
| `tests/test_engine.py` | Keep the 3 existing tests. Add: fatigue test, assist test, card test, formation-impact test, set-piece test, manager-mode sanity test. |

---

## 5. What's still OUT of scope for Phase 2 (defer to Phase 3+)

- **2D pitch graphics** — still text/tables only. A heat-map of event locations is fine in Phase 3.
- **Training and attribute progression** — players don't improve over seasons yet. Defer to Phase 3.
- **Staff / scouts / academy** — no AI director of football, no youth intake.
- **Press conferences / player interactions** — no narrative layer.
- **Online multiplayer** — single-user local only.
- **Mobile-responsive UI** — desktop browser only.
- **Replay / highlight reels** — the event log is the only match artifact.

---

## 6. Success Criteria for Phase 2

A user can:

1. Start a season, pick a club, choose a formation and mentality, manually set the XI, and play a match with live substitutions — and the result is clearly influenced by their choices (validated by the manager-mode sanity test).
2. Sign 1-3 players in a mid-season transfer window and see them in the next match's squad.
3. See injuries carry over between matches and adapt their lineup accordingly.
4. Run a 2-division season with promotion/relegation and a cup competition, and see a season-review page at the end.
5. Reload the app the next day and continue the same career.

All Phase 1 validation tests still pass. The full season simulates in under 5 seconds. The DB schema is forward-compatible (migrations, not re-ingest).

---

## 7. Before You Start Building — Ask Me

1. Should `scipy` be added for the Hungarian algorithm, or should I implement a custom assignment solver to keep dependencies at zero?
2. For manager mode, do you want **interactive** matches (you watch minute-by-minute and make sub decisions mid-match) or **pre-match** decisions only (you set tactics, then the whole match simulates and you see the result)?
3. For the transfer market, should AI clubs also bid against each other (full market simulation), or only respond to the user's bids?
4. When the full 90k dataset arrives, what's the desired league structure — 2 divisions of 20, 4 divisions of 20, or a single super-league with grouped stages?
5. Should morale be visible to the user (a number per player) or hidden (only its effects show up in match performance)?

---

## 8. Quick Reference — Current File Sizes & Layout

```
football_sim/                    ~17 MB total (mostly football.db)
├── README.md                    8 KB
├── run.py                       8 KB  (entry point: ingest/season/validate/serve/match)
├── data/
│   ├── ingest.py               15 KB  (CSV → SQLite, idempotent, multi-part ready)
│   ├── merged_players_part1.csv 352 KB (1,000 players; full dataset will be 90k+)
│   └── football.db              16 MB (players, player_attributes, player_positions,
│                                       matches, seasons — will need club_tactics,
│                                       careers, injuries, transfers in Phase 2)
├── engine/
│   ├── attributes.py            8 KB  (CA weights, suitability, form roll)
│   ├── lineup.py                7 KB  (greedy best-XI picker — replace with Hungarian)
│   └── match.py                24 KB  (the engine — biggest file, most §0 work)
├── sim/
│   └── season.py               20 KB  (fixtures, standings, run_season)
├── ui/
│   ├── app.py                  12 KB  (Flask routes)
│   └── templates/                     (base, home, season, match, club — 5 files, ~19 KB)
├── tests/
│   └── test_engine.py          12 KB  (3 passing tests — keep them passing)
└── scripts/
    └── smoke_match.py           4 KB
```

Start at `run.py` to understand the entry points, then `engine/match.py` for the core logic. The §0 review above is your map — don't wander.
