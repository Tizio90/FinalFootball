# Build Prompt: Football Management Simulation — Phase 2B (Management Features)

You are continuing a multi-phase Football Manager-style simulation. **Phase 1 and Phase 2A are complete and working.** The app lives at `https://github.com/Tizio90/FinalFootball` and runs locally via `python run.py serve`.

This prompt defines **Phase 2B**: the management layer — manager mode, transfers, injuries/morale carryover, multi-league, save/load careers, and season summaries. As always, depth goes into *simulation logic*, not graphical polish.

---

## 0. Phase 2A — Code-Level Review (read this before touching anything)

Phase 2A shipped: all 10 tech-debt fixes, formations (4-3-3/4-4-2/4-2-3-1/3-5-2), mentalities, set pieces, fouls/cards, injuries, expanded match stats, player-of-the-match, head-to-head standings tiebreakers, Hungarian-algorithm lineup picker, asymmetric home advantage, and a full data restructuring (91k real players, 179 nations, 949 divisions, 0-100 attribute scale). All 7 validation tests pass. A full EPL season simulates in 1.3 seconds.

### What works (don't break it)

- **Normalized schema**: `nations` → `divisions` → `clubs` → `players` with proper FKs and indexes. The `Based` field ("England (Premier Division)") is parsed into (nation, division) during ingestion. 125 leagues are marked `playable=1` (≥4 clubs with ≥14 players each).
- **0-100 attribute scale**: all 61 FM attributes rescaled from 1-20 using `value × 5 + deterministic ±3 noise` (SHA-256 seeded by UID + attr name, so re-ingests are stable). Foot strength 1-5 → 0-100 same way. Morale defaults to 50.
- **Engine tuning**: 48 phases/match, miss threshold 35, on-target 42.5, keeper +3.0, foul margin 15, all gauss sigma ×5 from Phase 1. Avg goals ~2.6/match. Strong-vs-weak win rate ~80%.
- **Performance**: `pick_best_xi` pre-loads player_positions in one query (was 803 queries/club, now 1). 380x faster. Full 342-match EPL season: 1.3s.
- **UI browse hierarchy**: Home → Nations (179) → Nation → Division → Club. Quick-start buttons for top 6 leagues. Division page has season-start form.

### Concrete bugs to fix FIRST (in this order)

These are real defects in the current code. Fix them before adding any 2B features.

1. **Schema drift between `sim/season.py` and `data/ingest.py`** (`sim/season.py:296-314` vs `data/ingest.py:359-368`).
   `sim/season.py:PERSIST_DDL` creates `seasons` WITHOUT `division_id`/`division_name` columns. `data/ingest.py:SEASONS_DDL` HAS them. The app imports `init_persistence` from `sim/season.py` — so on a fresh DB (no prior ingest), the `seasons` table would be missing those columns, and `division_season_new`'s INSERT would crash with `sqlite3.OperationalError: table seasons has no column named division_id`. The only reason it works today is that `data/ingest.py` runs first during ingestion and creates the full table. Fix: replace `PERSIST_DDL` in `sim/season.py` with the version from `data/ingest.py` (or import it). Add a migration test: create a fresh DB, call `init_persistence()` from `sim/season.py`, verify the `seasons` table has `division_id` + `division_name` columns.

2. **`run_season()` does not persist `division_id`** (`sim/season.py:~360`).
   The INSERT inside `run_season` uses only 4 columns: `(id, clubs_json, rounds, created_at)`. But `division_season_new` (in `ui/app.py`) uses 6 columns including `division_id` + `division_name`. So seasons created via "Sim season" quick-start button (which calls `run_season` directly) show no division name on the home page. Fix: add `division_id` and `division_name` parameters to `run_season()`, thread them through to the INSERT. The UI should pass them when starting a division-based season.

3. **`_LINEUP_CACHE` is never invalidated** (`ui/app.py:133`).
   The per-process cache keyed by club name survives forever. If the user re-runs `python run.py ingest` (e.g., after a dataset update) while the server is running, stale XIs will be served. Fix: add a `clear_lineup_cache()` function and call it from the ingest route (or document that the server must restart after re-ingest — simpler).

4. **No schema migration mechanism**. The DB is dropped + recreated on every `python run.py ingest`. This destroys any saved seasons, careers, transfer history. For Phase 2B (which adds careers, transfers, cross-season state), this is unacceptable. Fix: add `PRAGMA user_version` tracking and a `migrate()` function that applies incremental ALTER TABLE statements. Never drop tables that contain user data (seasons, matches, careers, transfers).

5. **`run_season()` caches XIs per club but never re-picks after transfers/injuries** (`sim/season.py:~345`). In Phase 2B, if a club signs a player mid-season, the XI cache for that club is stale. Fix: invalidate the per-club XI cache when its squad changes (transfer in/out, injury).

6. **Test suite is slow on the full 91k DB** — 7 tests take ~50 seconds because each test loads 14 clubs with 70+ players. Fix: add a `--fast` flag to `tests/test_engine.py` that uses a smaller club sample (e.g., 6 clubs instead of 14) and reduces match counts to 50. The full suite should run in under 15 seconds.

7. **`select_top_clubs()` is dead code** (`sim/season.py:240`) — the UI now uses `select_clubs_by_division()` exclusively. Either remove it or document it as a legacy fallback.

8. **No `/health` or error handling for missing DB** — if a user clones the repo and runs `python run.py serve` without first running `python run.py ingest`, every route crashes with `no such table: clubs`. Fix: in `ui/app.py`, check if `data/football.db` exists and has the `clubs` table on startup; if not, redirect to a "Please run `python run.py ingest` first" page.

### Engine tuning observations (don't change without re-running validation)

Current averages on the 0-100 scale:
- Avg goals/match: **~2.6** (real-world ~1.3 — slightly high, acceptable for now)
- Strong-vs-weak win rate: **~80%** (good)
- 0-0 frequency: **~12%** (real-world ~8% — slightly high)
- Yellow cards: ~1/match (real-world ~3/match — too few; bump YELLOW_CARD_BASE)
- Red cards: ~0.05/match (real-world ~0.1/match — slightly low)

If goal count drops below 2.0 or rises above 3.5, or 0-0 frequency exceeds 20%, re-tune. The knobs: `PHASES_PER_MATCH` (48), `on_target_threshold` (42.5), `keeper +3.0`, `pressure * 0.30`, `FOUL_MARGIN_THRESHOLD` (15). Document trade-offs in comments above each.

---

## 1. Phase 2B Scope — what to build

Split into three milestones: **2B-1 (manager mode)**, **2B-2 (persistence + state)**, **2B-3 (transfers + multi-league)**. Ship in this order.

### Milestone 2B-1 — Manager Mode (highest user value)

**In scope:**

1. **Career creation** — user picks a club at season start. Store in a new `careers` table:
   ```
   careers (id TEXT PK, club TEXT, division_id INT, season_id TEXT,
            current_round INT, formation TEXT, mentality TEXT,
            manual_xi_json TEXT,  -- user's chosen XI (list of UIDs, or null for auto)
            manual_bench_json TEXT,
            created_at TEXT, updated_at TEXT)
   ```
   The home page gets a "New Career" button → pick a division → pick a club → choose formation + mentality → start.

2. **Tactics page** (`/career/<id>/tactics`) — user can:
   - Choose formation (4-3-3, 4-4-2, 4-2-3-1, 3-5-2)
   - Choose mentality (defensive, balanced, attacking)
   - View the auto-picked XI and override it: click a slot → dropdown of eligible players (sorted by suitability for that slot) → save. The chosen XI is stored as `manual_xi_json` (list of 11 UIDs in slot order, or null for auto).
   - Bench: user picks 7 subs from the remaining squad.

3. **Pre-match decisions only** (per the Phase 2 Q&A answer). The user sets tactics before the match, then the whole match simulates. No interactive mid-match subs in 2B.

4. **Career dashboard** (`/career/<id>`) — shows:
   - Current season standings (highlighted user club)
   - Next fixture (user's next match)
   - "Play next match" button → simulates the user's match with their chosen tactics, then auto-sims the rest of the round
   - "Sim to end of season" button
   - Recent results (last 5 matches with scores + link to match detail)

5. **Match integration** — when simulating a match involving the user's club, the engine must use the user's chosen formation, mentality, and manual XI (if set). Other matches use auto-picked XIs as before. The `play_match()` call already accepts `home_mentality`/`away_mentality` — extend it to accept a full `home_xi`/`away_xi` override.

**Validation gate for 2B-1:** a sanity test where the user picks the weakest club in a division and plays attacking 4-3-3 every game → should lose ~65-75% of matches against stronger sides. Picking defensive 5-4-1 (add this formation) should reduce goals conceded by ~25% but also reduce goals scored by ~20%.

### Milestone 2B-2 — Persistence + Cross-Match State

**In scope:**

1. **Schema migrations** (§0 item 4) — implement `PRAGMA user_version` + `migrate()`. Never drop user data tables. The `careers`, `transfers`, `injuries` tables are created via migration, not by re-ingesting.

2. **Injury carryover between matches** — injuries from match N persist into match N+1. A player flagged as injured during a match sits out the next 1-3 matches (random, weighted by `Inj Pr`). Store in a new `injuries` table:
   ```
   injuries (id INT PK, career_id TEXT, player_uid INT, club TEXT,
             injury_date TEXT, matches_out INT, matches_remaining INT,
             injury_type TEXT)
   ```
   When picking the XI, injured players are excluded. When the player returns, morale drops by 5-10 (frustration).

3. **Morale system** (visible, per the Phase 2 Q&A answer) — each player has a 0-100 morale value (already in the DB, default 50). Morale changes:
   - Win: +3 to all starters, +1 to bench
   - Loss: -3 to all starters, -1 to bench
   - Goal scored: +2 to scorer, +1 to assister
   - Yellow card: -1
   - Red card: -3
   - Benched for 3+ straight matches: -2 per match
   - Injury: -5 on return
   Morale affects match performance: low morale (<30) degrades `Cmp` and `Dec` by up to 20%; high morale (>80) boosts them by up to 10%. Display morale on the squad page with color coding (already done in club.html — extend to career tactics page).

4. **Save / load across sessions** — careers persist in SQLite. The home page lists existing careers with a "Continue" button. Multiple parallel careers are allowed.

5. **Season summaries** — at season end, show a "Season Review" page (`/career/<id>/season/<season_id>/review`):
   - Final standings (user's club highlighted)
   - Top 10 scorers (across all clubs in the division)
   - Top 10 assist leaders
   - Best XI of the season (one player per position, by average rating)
   - Manager of the season (AI club that most exceeded expected points)
   - User's season record: W/D/L, GF/GA, final position, points vs expected

**Validation gate for 2B-2:** start a career, play 5 matches, verify injuries carry over (injured player doesn't appear in the next XI). Verify morale changes are visible on the squad page. Reload the app, verify the career is still there.

### Milestone 2B-3 — Transfers + Multi-League

**In scope:**

1. **Transfer market (user-only)** (per the Phase 2 Q&A answer — AI clubs don't bid against each other):
   - Each club has a "transfer-listed" set: players with low `Inf` status OR low minutes played in the current season.
   - Transfer window: pre-season (1 week) + mid-season (after round 19).
   - User bids on any player in the DB. AI club accepts/rejects based on:
     - Bid amount vs player's `Transfer Value` (already in the dataset, currently unused)
     - Player's `Amb` (ambition) — high ambition = more likely to push for a move
     - Buying club's reputation (derived from last season's finish + division tier)
     - Selling club's willingness (if the player is transfer-listed, acceptance is ~80%; otherwise ~20%)
   - Limit: 3 signings per window per club. Budget: derived from club's `Transfer Value` sum / 10.
   - AI clubs can also bid for the user's players — the user gets a notification and can accept/reject.

2. **Multi-league + promotion/relegation** (per the Phase 2 Q&A — 1 league of 20 for now, but schema-ready):
   - The `divisions` table already has `nation_code`. Add a `tier` column (1 = top division, 2 = second division, etc.).
   - For Phase 2B, just stub the multi-league code: support running two divisions in parallel (e.g., England Premier Division + England Sky Bet Championship), with promotion/relegation between them at season end (top 3 up, bottom 3 down).
   - Cup competition: single-elimination, 32 teams from both divisions, random draw. Play during the season (one round per month).

3. **Reputation system** — each club has a `reputation` value (0-100, derived from: division tier × 20 + last season finish × -2 + historical trophies). Affects transfer acceptance and player morale (players at high-reputation clubs are happier).

**Validation gate for 2B-3:** start a career in EPL, sign 1-2 players in the mid-season window, verify they appear in the next match's squad. Run a 2-division season (EPL + Championship), verify promotion/relegation at season end.

---

## 2. Hard Constraints (carry over from Phase 1+2A)

- **No new heavy dependencies.** `scipy` is already in use. Anything else requires explicit justification.
- **No game engines, no Docker, no Node tooling, no external DB server.** Python 3 stdlib + SQLite + Flask + scipy.
- **One command to run.** `python run.py serve` must still bring up the whole UI.
- **Performance:** a full 380-match season must simulate in under 5 seconds. A career match (with user tactics) must simulate in under 200ms.
- **Backwards compatibility:** re-running `python run.py ingest` on the existing `merged_players.csv` must still work. Don't change the CSV format or the attribute scaling.
- **No data loss on re-ingest:** §0 item 4 — implement migrations. Seasons, careers, transfers must survive a re-ingest.

---

## 3. Suggested Order of Work

1. **Fix §0 items 1-8 first** (schema drift, run_season division_id, cache invalidation, migrations, test speed, dead code, missing-DB handling). Run the full validation suite after each fix.
2. **Milestone 2B-1 (manager mode)** — highest user value. Ship tactics page + career dashboard + pre-match decisions. Add the manager-mode sanity test.
3. **Milestone 2B-2 (persistence + state)** — injuries, morale, season summaries. Cross-match persistence is what makes it feel like a real career.
4. **Milestone 2B-3 (transfers + multi-league)** — most complex, ship last. Start with user-only transfers, then multi-league stub, then cup.

---

## 4. File-by-File Improvement Notes

| File | What to add / change |
|---|---|
| `data/ingest.py` | Add `PRAGMA user_version` + `migrate()`. Add `tier` column to `divisions`. Never drop `seasons`/`matches`/`careers`/`transfers`/`injuries` tables. Add `reputation` column to `clubs`. |
| `engine/attributes.py` | Add morale modifier to `_eff()` — low morale degrades Cmp/Dec, high morale boosts them. Add `injury_status` check (injured players return 0 effectiveness). |
| `engine/lineup.py` | Add `pick_manual_xi()` (already stubbed) — accept user-chosen UIDs + formation, return the XI. Exclude injured players. Apply morale to suitability scoring. |
| `engine/match.py` | No major changes — the engine already accepts `home_xi`/`away_xi`/`home_mentality`/`away_mentality`. Just ensure morale modifier is applied in `_eff()`. Add 5-4-1 formation (defensive, for the sanity test). |
| `sim/season.py` | Fix §0 items 1, 2. Add `careers`/`injuries`/`transfers` tables. Add injury carryover logic. Add morale updates after each match. Add multi-league + promotion/relegation + cup fixtures. Add `run_career_match()` that uses the user's tactics. |
| `ui/app.py` | Add routes: `/career/new`, `/career/<id>`, `/career/<id>/tactics`, `/career/<id>/play-next`, `/career/<id>/sim-rest`, `/career/<id>/transfers`, `/career/<id>/season/<sid>/review`. Fix §0 items 3, 8. |
| `ui/templates/` | Add: `career.html` (dashboard), `tactics.html` (XI picker), `transfers.html`, `season_review.html`. Update `home.html` to list careers + "New Career" button. |
| `tests/test_engine.py` | Fix §0 item 6 (add `--fast` flag). Add: manager-mode sanity test, injury-carryover test, morale-effect test, transfer-acceptance test, migration test. |

---

## 5. What's still OUT of scope for Phase 2B (defer to Phase 3+)

- **2D pitch graphics** — still text/tables only. A heat-map of event locations is fine in Phase 3.
- **Training and attribute progression** — players don't improve over seasons yet. Defer to Phase 3.
- **Staff / scouts / academy** — no AI director of football, no youth intake.
- **Press conferences / player interactions** — no narrative layer.
- **Interactive mid-match substitutions** — deferred per the Phase 2 Q&A (pre-match only).
- **Full AI transfer market** — deferred per the Phase 2 Q&A (user-only market).
- **Online multiplayer** — single-user local only.
- **Mobile-responsive UI** — desktop browser only.

---

## 6. Success Criteria for Phase 2B

A user can:

1. Start a career as Chelsea (or any club in any playable league), choose 4-3-3 attacking, manually set the XI, and play a match — and the result is clearly influenced by their choices (validated by the manager-mode sanity test).
2. Play through a full season, see injuries carry over between matches, watch morale rise and fall, and adapt their lineup accordingly.
3. Sign 1-2 players in the mid-season transfer window and see them in the next match's squad.
4. See a season-review page at the end with top scorers, best XI, and their final position.
5. Reload the app the next day and continue the same career.
6. Run a 2-division season (EPL + Championship) with promotion/relegation and a cup competition.

All Phase 2A validation tests still pass. The full season simulates in under 5 seconds. The DB schema is forward-compatible (migrations, not re-ingest). The `football.db` file is gitignored — users run `python run.py ingest` once after cloning.

---

## 7. Before You Start Building — Ask Me

1. For the manual XI picker UI: **dropdown-per-slot** (simple, fast to build) or **drag-and-drop pitch view** (nicer, more work)?
2. For transfers: should the user see a **searchable list of all 87k players** (with filters by position/nation/club/value), or just a **shortlist of transfer-listed players** (~5-10% of the DB)?
3. For multi-league: when running EPL + Championship in parallel, should the user's career be **locked to one division** (can only manage a club in one division per career), or can they **switch divisions** mid-career (if relegated/promoted)?
4. For the cup competition: **random draw** (simple) or **seeded draw** (top teams kept apart until later rounds, more realistic)?
5. Should morale be displayed as a **raw 0-100 number** (already done) or also as a **mood word** ("Happy", "Content", "Frustrated", "Angry") for flavor?

---

## 8. Quick Reference — Current File Sizes & Layout

```
FinalFootball/                   (repo root — github.com/Tizio90/FinalFootball)
├── .gitignore                   ignores __pycache__/, data/football.db, venvs
├── README_PHASE2A.md            2A changelog
├── prompt_01.md                 Phase 1 prompt (original spec)
├── prompt_2.md                  Phase 2A prompt (this is the current one)
├── run.py                       8 KB  entry point (ingest/season/validate/serve/match)
├── data/
│   ├── ingest.py               24 KB  CSV → SQLite, normalized schema, 0-100 scaling
│   ├── merged_players.csv      32 MB  91,672 rows (87,163 unique after dedup) — gitignored? NO, it's source data
│   └── football.db             36 MB  generated by ingest.py — GITIGNORED
├── engine/
│   ├── attributes.py            8 KB  CA weights, suitability, form roll (0-100 scale)
│   ├── lineup.py               16 KB  Hungarian-algorithm picker, 4 formations, manual override stub
│   └── match.py                44 KB  the engine — biggest file (48 phases, fouls, cards, set pieces, MOTM)
├── sim/
│   └── season.py               20 KB  fixtures, standings (h2h tiebreakers), run_season — FIX §0 items 1,2
├── ui/
│   ├── app.py                  20 KB  Flask routes — add career/transfer/review routes; FIX §0 items 3,8
│   └── templates/                     base, home, nations, nation, division, club, season, match (8 files)
├── tests/
│   └── test_engine.py          20 KB  7 passing tests — FIX §0 item 6 (add --fast)
└── scripts/
    └── smoke_match.py           4 KB
```

**DB schema (9 tables):** `nations`, `divisions`, `clubs`, `players`, `player_attributes`, `player_positions`, `seasons`, `matches`, `sqlite_sequence`. Phase 2B adds: `careers`, `injuries`, `transfers`.

**Key numbers:** 87,163 players · 7,061 clubs · 949 divisions · 179 nations · 125 playable leagues · 0-100 attribute scale · 48 phases/match · ~2.6 goals/match · 1.3s/season · 7/7 tests pass.

Start at `run.py` to understand the entry points, then `sim/season.py` for the §0 schema bugs, then `ui/app.py` for the new routes. The §0 review above is your map — don't wander.
