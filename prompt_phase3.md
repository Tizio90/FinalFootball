# Build Prompt: Football Management Simulation — Phase 3 (Football Manager-Style Depth)

You are continuing a multi-phase Football Manager-style simulation. **Phases 1, 2A, and 2B are complete and working.** The app lives at `https://github.com/Tizio90/FinalFootball` and runs locally via `python run.py serve`. 87,163 real players, 179 nations, 949 divisions, 125 playable leagues, 0-100 attribute scale. All 8 validation tests pass. The UI was redesigned with a modern broadcast-sports aesthetic.

This prompt defines **Phase 3**: the deep management layer that turns this from a "simulator with a browse UI" into something that feels like **Football Manager** — training, staff, youth development, press interactions, finances, tactical depth, and a living world.

---

## 0. Phase 2 — Honest Review (read this before touching anything)

### What works well (don't break it)

- **Engine core**: 48-phase possession model, opposed-attribute duels, fouls/cards/injuries/set pieces, fatigue, form rolls, MOTM. Avg 2.22 goals/match, 0.76 yellows, 0.02 reds, 0.18 injuries per match — all in realistic bands.
- **Validation**: 8/8 tests pass, including the manager-mode sanity test (defensive 5-4-1 concedes 41.2% fewer goals than attacking 4-3-3).
- **Data**: 87k real players with proper nation→division→club hierarchy, 0-100 attributes with deterministic noise, morale system, injuries with cross-match carryover.
- **Career mode**: create career → pick club/formation/mentality → tactics page (dropdown XI picker) → play round-by-round or sim rest → season review with top scorers/assists.
- **Transfers**: full search of 87k players, bid logic based on value/ambition/reputation, 3 signings per window.
- **Cup**: 32-team single-elimination, random draw, penalties for draws.
- **UI**: modern design system with gradients, shadows, crest circles, color-coded zones, broadcast-style match scoreboard.

### Concrete bugs and tech debt to fix FIRST

1. **Yellow cards are way too rare** — 0.76/match vs real-world ~3.5/match. The `YELLOW_CARD_BASE = 0.04` is too low and the `card_factor * strict_factor * 0.20` multiplier compounds the problem. Fix: raise `YELLOW_CARD_BASE` to ~0.15 and increase the multiplier to 0.35. Re-run validation to confirm cards are now 2-4/match.

2. **Red cards are too rare** — 0.02/match vs real-world ~0.15/match. Same root cause. Raise `RED_CARD_BASE` to ~0.008 and the multiplier to 0.02.

3. **Injuries are too rare** — 0.18/match vs real-world ~0.5/match. `INJURY_BASE_PROB = 0.00008` is too conservative. Raise to ~0.0002.

4. **0-0 frequency slightly high** — 22% of matches (11/50) vs real-world ~8%. The keeper `+3.0` advantage is slightly too strong. Reduce to `+1.5`. Re-run validation to confirm goals stay in the 2.0-3.0 band.

5. **No player attribute progression** — players never improve or decline. A 20-year-old with CA 60 stays at CA 60 forever. This is the single biggest gameplay gap vs Football Manager.

6. **No youth intake** — there's no way to develop young players or promote from an academy. Every club just uses whatever players the CSV gave them.

7. **No staff system** — there's no manager, no coaches, no scouts, no physios. The user IS the manager but has no staff to hire/fire.

8. **No training** — there's no way to influence player development. You can't set training intensity, focus on specific attributes, or train new positions.

9. **No finances** — clubs have no budget, no wage bill, no income from tickets/TV/prize money. The transfer budget is a flat 10% of squad value with no ongoing financial simulation.

10. **No press / media interactions** — no press conferences, no player interactions, no board expectations, no fan confidence. The career feels lifeless between matches.

11. **No player happiness / personality depth** — morale is a single 0-100 number. Real FM has happiness (playing time, ambition, relationship with manager), personality traits (professionalism, ambition, loyalty, temperament), and private concerns.

12. **No tactical depth beyond formation + mentality** — no player roles (e.g., "Ball-Playing Defender", "Poacher", "Inside Forward"), no team instructions (pressing intensity, tempo, width, defensive line), no individual player instructions.

13. **No scouting** — you can search all 87k players instantly. Real FM requires scouting to reveal attributes, with scout quality affecting accuracy.

14. **No save/load across sessions properly** — careers persist in SQLite, but there's no "save game" concept with a timestamp, no multiple save slots, no autosave.

15. **Match events are text-only** — no 2D pitch representation, not even a heatmap of where events happened. The feed is a scrolling text log.

16. **No international management** — can't manage national teams.

17. **No hall of fame / records** — no tracking of all-time top scorers, most successful managers, club records, etc.

### Graphics/aesthetics review

The UI was redesigned in the last phase and looks decent, but has gaps vs a real FM-style app:

- **No player photos** — just names in tables. Even a generated silhouette would help.
- **No club crests/logos** — using initials-in-circles. Real logos would transform the feel.
- **No kit colors** — clubs have no visual identity beyond their name.
- **No pitch visualization** — the tactics page is a table, not a pitch with positions.
- **No match animation** — even a simple 2D dot moving on a pitch would be huge.
- **No charts/graphs** — no form lines, no attribute radar charts, no league position over time.
- **Tables are dense** — the squad table is hard to scan. Needs better visual hierarchy.
- **Mobile is broken** — viewport is hardcoded to 1280px. Won't work on phones/tablets.
- **No dark mode** — single light theme.
- **No loading states** — simulating a season just freezes the browser for 1-2 seconds.

### Engine tuning observations

Current averages (50 EPL matches):
- Avg goals/match: **2.22** (real ~1.35 — still slightly high, acceptable)
- Avg yellows: **0.76** (real ~3.5 — WAY too low, must fix)
- Avg reds: **0.02** (real ~0.15 — too low)
- Avg injuries: **0.18** (real ~0.5 — too low)
- 0-0 frequency: **22%** (real ~8% — too high)

The knobs to fix: `YELLOW_CARD_BASE`, `RED_CARD_BASE`, `INJURY_BASE_PROB`, keeper `+3.0` advantage. Document all changes in comments.

---

## 1. Phase 3 Scope — what to build

Split into four milestones. Ship in this order.

### Milestone 3A — Engine Realism Fixes + Player Development

**In scope:**

1. **Fix all 4 tuning bugs** (§0 items 1-4): raise card/injury rates, reduce keeper advantage. Re-run validation after each change. Target: 2.5-3.5 yellows, 0.1-0.2 reds, 0.4-0.6 injuries per match, 1.8-2.5 goals.

2. **Player attribute progression** (§0 item 5) — players now develop over time:
   - **Age curve**: players improve from 16-24, peak at 24-29, decline from 30+ (GKs peak later, 28-34).
   - **Training rate**: each player has a hidden `development_rate` derived from `Prof` (professionalism), `Amb` (ambition), and age. High-prof young players improve fast; old unprofessional players decline fast.
   - **Per-season update**: at the end of each season, every player's attributes are adjusted. Typical change: ±1-3 per attribute per year for young players, -1-2 for old players.
   - **Stored in DB**: add a `player_development` table tracking each player's growth history.
   - Add a `--progress-seasons N` flag to `run.py season` that simulates N seasons and shows how players develop.

3. **Youth intake** (§0 item 6) — each club gets a youth intake once per season:
   - 3-8 new players aged 15-17, generated with random attributes biased toward the club's division quality.
   - Top clubs get better youth intakes (higher average CA).
   - Youth players can be promoted to the first team or kept in the "B team" (just a flag, no separate squad simulation in Phase 3).
   - Add a `/career/<id>/youth` page showing the current season's intake.

4. **Player roles** (§0 item 12) — add 8-10 FM-style roles that modify how a player performs in a slot:
   - **GK**: Sweeper Keeper (more rushing out, better distribution)
   - **DEF**: Ball-Playing Defender (better passing, more risk), No-Nonsense Defender (more tackling, less passing)
   - **MID**: Box-to-Box (more stamina, more tackles), Deep-Lying Playmaker (better passing, less tackling), Ball-Winning Midfielder (more tackling, less passing)
   - **ATT**: Poacher (better finishing, less defensive work), Target Man (better heading/strength, less pace), Inside Forward (better cutting inside, more shots)
   - Each role applies a +10% bonus to certain attributes and -10% penalty to others during match simulation.
   - The tactics page gets a role dropdown next to each XI slot.

5. **Team instructions** (§0 item 12) — add 4 team-level tactical instructions:
   - **Pressing intensity**: Low / Standard / High (affects opponent pass success rate + your fatigue)
   - **Tempo**: Slow / Standard / Fast (affects shot frequency + turnover rate)
   - **Width**: Narrow / Standard / Wide (affects crossing vs through-balls)
   - **Defensive line**: Deep / Standard / High (affects pressure on shooter + through-ball risk)
   - Stored in the `careers` table as `team_instructions_json`.

**Validation gate for 3A:** all 8 existing tests still pass. New test: "Development test" — simulate 5 seasons, verify that 18-year-olds with high `Prof` improve by ≥5 CA points, and 33-year-olds decline by ≥3 CA points.

### Milestone 3B — Staff, Training, Finances

**In scope:**

1. **Staff system** (§0 item 7) — each club can hire up to 5 staff:
   - **Assistant Manager** (improves training efficiency + lineup suggestions)
   - **Coach** (improves attribute development for specific families — DEF coach, MID coach, ATT coach, GK coach)
   - **Scout** (improves attribute visibility accuracy — see 3B item 5)
   - **Physio** (reduces injury frequency + duration)
   - **Sports Scientist** (reduces fatigue decline)
   - Staff are generated NPCs with a `rating` (1-100) and a `specialty`. Hired via a `/career/<id>/staff` page. Cost: weekly wages.
   - Add a `staff` table: `id, career_id, name, role, rating, specialty, wage, hired_at`.

2. **Training system** (§0 item 8) — the user sets training focus:
   - **General** (balanced development)
   - **Attacking** (boosts ATT/MID attributes, slight DEF decline)
   - **Defending** (boosts DEF/GK attributes, slight ATT decline)
   - **Fitness** (boosts physical attributes, slight technical decline)
   - **Technical** (boosts technical attributes, slight physical decline)
   - Training intensity: Light / Standard / Intensive (affects development rate + injury risk + fatigue).
   - Stored per-career as `training_focus` + `training_intensity`.
   - Training interacts with staff: a Coach with high rating + matching specialty boosts the development rate for that family.

3. **Finances** (§0 item 9) — each club now has a financial model:
   - **Income**: matchday revenue (per home match, based on division tier), TV money (seasonal, based on division), prize money (based on final position), transfer sales.
   - **Expenses**: player wages (sum of all players' wage — derived from CA + age), staff wages, transfer purchases, youth academy upkeep.
   - **Balance**: tracked per-season. If balance goes negative for 2+ seasons, the board may fire you (career ends).
   - **Transfer budget**: derived from balance + projected income, not a flat 10%.
   - Add a `/career/<id>/finances` page showing income/expenses/balance.
   - Each player gets a `wage` column (derived at ingestion from CA + age: `wage = CA * 1000 * age_factor`).

4. **Board expectations** (§0 item 10) — at season start, the board sets expectations:
   - "Finish in the top 4" (for top clubs)
   - "Mid-table finish" (for mid clubs)
   - "Avoid relegation" (for weak clubs)
   - Meeting expectations: +board confidence, +transfer budget next season.
   - Failing: -board confidence. 2 consecutive failures = sacked.
   - Board confidence shown on career dashboard.

5. **Scouting** (§0 item 13) — players' attributes are now hidden until scouted:
   - Unscouted players show only name/age/position/club (no CA, no attributes).
   - Hire scouts (staff system) to reveal attributes. Scout quality affects accuracy (a 50-rated scout shows attributes ±15; a 90-rated scout shows ±3).
   - Scouting takes time: 1-3 days per player, depending on scout quality and player location.
   - Your own squad's attributes are always visible.
   - The transfer market search now only shows scouted players' CA/attributes.

**Validation gate for 3B:** finances test — simulate a season, verify income > expenses for a top club, verify wage bill is realistic (~50-70% of income). Staff test — hire a coach, verify training development rate increases.

### Milestone 3C — Media, Interactions, Living World

**In scope:**

1. **Press conferences** (§0 item 10) — before/after matches, a press conference popup with 3 questions:
   - Each question has 3-4 response options (e.g., "Praise the team", "Criticize the referee", "Stay neutral").
   - Responses affect: player morale, fan confidence, board confidence, media perception.
   - Stored as `press_conferences` table with the questions, chosen answers, and effects.
   - Can be skipped (auto-neutral) but with a small morale penalty.

2. **Player interactions** (§0 item 11) — players can now have concerns:
   - "I want more playing time" (if benched 3+ matches in a row)
   - "I want to leave for a bigger club" (if high ambition + club underperforming)
   - "I'm unhappy with the manager" (if morale <30 for 5+ matches)
   - The user gets a notification and can respond: promise playing time, fine them, sell them, etc.
   - Responses affect morale + happiness + transfer value.
   - Add a `player_concerns` table.

3. **Player happiness** (§0 item 11) — replaces the single morale number with a richer model:
   - **Morale** (0-100) — match-driven (win/loss/goals)
   - **Happiness** (0-100) — relationship-driven (playing time, ambition, manager)
   - **Form** (0-100) — recent performance (last 5 matches)
   - Displayed as three separate indicators on the squad page.
   - Low happiness → player requests transfer. Low form → performance penalty.

4. **Fan confidence + board confidence** — two new 0-100 meters:
   - **Fan confidence**: rises with wins + attractive football (goals scored), falls with losses + boring football.
   - **Board confidence**: rises with meeting expectations + financial health, falls with failing expectations.
   - Both shown on career dashboard. If either hits 0, you're sacked.

5. **Transfer rumors + AI transfer activity** — AI clubs now make transfers between themselves:
   - Each transfer window, AI clubs bid for players based on their needs (weak defense → buy a defender).
   - Transfer rumors appear on a `/career/<id>/news` page: "Chelsea interested in Haaland", "Real Madrid bid rejected for Mbappé".
   - Your players may receive interest from AI clubs — you can accept/reject.

6. **News feed** — a career dashboard news section showing:
   - Recent results (with media-style headlines)
   - Transfer rumors involving your club
   - Injury updates
   - Board/fan confidence changes
   - Milestone achievements (player scored 20th goal, club reached 1000 points all-time)

**Validation gate for 3C:** interaction test — bench a player for 5 matches, verify they raise a "playing time" concern. Press conference test — answer a question, verify morale changes.

### Milestone 3D — Visual Polish + Match Experience

**In scope:**

1. **2D pitch visualization for matches** (§0 item 15) — a simple top-down pitch view:
   - 22 dots (11 per team) positioned by formation.
   - The ball moves between positions during phases.
   - Goal celebrations: dot flashes + text overlay.
   - Not full animation — just position updates per phase (every ~2 seconds).
   - Implemented in vanilla JS + SVG or canvas, no external libraries.
   - Toggle between "pitch view" and "text feed" on the match page.

2. **Tactics pitch view** (§0 item 12) — the tactics page shows a pitch with the XI positioned:
   - Drag-and-drop to swap players between slots.
   - Hover to see player attributes.
   - Formation selector changes the pitch layout.
   - Replaces the current dropdown-per-slot table.

3. **Attribute radar charts** (§0 item 15) — each player page shows a radar chart of their attributes:
   - 6 axes: Technical, Mental, Physical, Defending, Attacking, Goalkeeping.
   - Implemented in vanilla SVG.
   - Compare two players side-by-side.

4. **Form line chart** — career dashboard shows a line chart of league position over the season:
   - X-axis: round 1-38. Y-axis: position 1-20.
   - Highlights your club's trajectory.

5. **League position history** — the season review shows a "league positions over time" chart for all clubs.

6. **Dark mode** (§0 item 15) — add a dark theme toggle in the header:
   - CSS variables for both themes.
   - Stored in localStorage.
   - Auto-detects system preference on first visit.

7. **Responsive design** (§0 item 15) — make the app work on tablets (768px+) and phones (375px+):
   - Tables become cards on mobile.
   - Sidebars stack below main content.
   - Touch-friendly buttons (min 44px tap target).

8. **Loading states** (§0 item 15) — when simulating, show a loading overlay:
   - "Simulating round 5..."
   - Progress bar for sim-rest-of-season.
   - Prevents the "frozen browser" feeling.

9. **Club kit colors** (§0 item 15) — add primary/secondary kit colors per club:
   - Stored in the `clubs` table as `kit_primary` + `kit_secondary` (hex colors).
   - Generated from club name hash at ingestion (deterministic).
   - Shown as a colored stripe on club pages + crests.

10. **Generated player avatars** (§0 item 15) — since we don't have real photos:
    - SVG silhouettes with kit color + player initials.
    - Different body types by position (GK = gloves, DEF = stocky, ATT = lean).

**Validation gate for 3D:** visual review — every page must look good in both light + dark mode, on desktop + tablet + mobile. The 2D pitch must render correctly for all 5 formations.

---

## 2. Hard Constraints (carry over from Phase 1+2)

- **No new heavy dependencies.** `scipy` is already in use. Vanilla JS + SVG only for visualizations — no D3.js, no Chart.js, no Three.js. If you must add one library, justify it explicitly.
- **No game engines, no Docker, no Node tooling, no external DB server.** Python 3 stdlib + SQLite + Flask + scipy.
- **One command to run.** `python run.py serve` must still bring up the whole UI.
- **Performance:** a full 380-match season must simulate in under 5 seconds. A career match must simulate in under 200ms. The 2D pitch must render at 30+ FPS.
- **Backwards compatibility:** re-running `python run.py ingest` on the existing CSV must still work. Don't change the CSV format.
- **No data loss on re-ingest:** migrations, not re-ingest, for all new tables.
- **All 8 existing validation tests must still pass** after every change. Add new tests for new features.

---

## 3. Suggested Order of Work

1. **Fix §0 tuning bugs** (items 1-4) first — 30 minutes, huge realism impact.
2. **Milestone 3A** (player development, youth intake, roles, team instructions) — this is the gameplay depth that makes it feel like FM.
3. **Milestone 3B** (staff, training, finances, scouting) — the management simulation layer.
4. **Milestone 3C** (media, interactions, living world) — the immersion layer.
5. **Milestone 3D** (visual polish, 2D pitch, charts) — the "wow" layer.

After each milestone, run the full validation suite + a manual playtest.

---

## 4. File-by-File Improvement Notes

| File | What to add / change |
|---|---|
| `data/ingest.py` | Add `wage` column to players (derived from CA + age). Add `kit_primary`/`kit_secondary` to clubs (generated from name hash). Add `development_rate` to players (derived from Prof/Amb/age). |
| `engine/attributes.py` | Add role-specific CA modifiers. Add `apply_role()` function that adjusts effective attributes based on assigned role. Add `player_development.py` with age-curve + progression logic. |
| `engine/match.py` | Fix tuning bugs (cards/injuries/keeper). Apply role modifiers in `_eff()`. Apply team instructions (pressing/tempo/width/def line). Track position data per phase for 2D pitch. |
| `engine/lineup.py` | Add role assignment to slots. Update `pick_best_xi` to consider role suitability. |
| `sim/season.py` | Add end-of-season player progression. Add youth intake generation. Add finances (income/expenses/balance). Add board expectations tracking. |
| `sim/career.py` | Add staff hiring/firing. Add training focus/intensity. Add scouting system. Add press conferences. Add player concerns. Add fan/board confidence. Add AI transfer activity. |
| `ui/app.py` | Add routes: `/career/<id>/staff`, `/career/<id>/training`, `/career/<id>/finances`, `/career/<id>/youth`, `/career/<id>/scouting`, `/career/<id>/news`, `/career/<id>/interactions`. Add 2D pitch data endpoint. Add dark mode cookie. |
| `ui/templates/` | Add: `staff.html`, `training.html`, `finances.html`, `youth.html`, `scouting.html`, `news.html`, `interactions.html`, `pitch.html` (2D match view). Update `tactics.html` with pitch view + roles. Update `base.html` with dark mode + responsive CSS. Add SVG radar chart template. |
| `tests/test_engine.py` | Add: development test, youth intake test, finances test, staff test, scouting test, interaction test, press conference test. |

---

## 5. What's still OUT of scope for Phase 3 (defer to Phase 4+)

- **3D match engine** — the 2D pitch is fine for Phase 3. 3D is a Phase 4+ goal.
- **Online multiplayer / network play** — still single-user local.
- **Real licensed data** — player photos, real club logos, real kits. Would require licensing.
- **Editor / custom database** — no in-app editor for attributes or creating custom leagues.
- **Replay system** — can't rewatch old matches. The event log is the only artifact.
- **Steam Workshop-style custom content** — no modding support.
- **International management** — managing national teams. Defer to Phase 4.
- **Hall of fame** — all-time records across careers. Defer to Phase 4.

---

## 6. Success Criteria for Phase 3

A user can:

1. Start a career, hire an assistant manager + a GK coach + a scout, set training to "Attacking / Intensive", and see players develop faster over the season.
2. Play 5 seasons and watch a 17-year-old prospect become a 22-year-old star, with attributes visibly improving on the radar chart.
3. Get a youth intake each season with 3-8 newgens, promote the best to the first team.
4. Manage finances: see income vs expenses, avoid bankruptcy, get a bigger transfer budget by finishing high.
5. Face press conferences with real choices that affect morale + fan/board confidence.
6. Deal with player concerns ("I want to leave") by promising playing time or selling them.
7. See AI clubs make transfers between themselves, with rumors on the news page.
8. Watch a match on a 2D pitch with dots moving, not just a text feed.
9. Use drag-and-drop on a pitch tactics board, not dropdowns.
10. Toggle dark mode, use the app on a tablet, see radar charts for players.
11. Set player roles (Poacher, Ball-Playing Defender, etc.) that visibly affect match performance.
12. Set team instructions (pressing intensity, tempo, width, defensive line) that affect match stats.

All Phase 2 validation tests still pass. New tests cover all new features. The full season simulates in under 5 seconds. The 2D pitch renders at 30+ FPS.

---

## 7. Before You Start Building — Ask Me

1. **Player progression rate**: should development be fast (visible improvement in 1 season) or realistic (slow, 3-5 seasons to see a youth player become a star)? Fast is more fun for a game; realistic is more like FM.
2. **2D pitch detail**: simple dots on a green rectangle (easy, fast), or detailed pitch with markings + player numbers + ball trail (more work, more immersive)?
3. **Scouting**: should unscouted players show NOTHING (just name/position, realistic but frustrating), or show rough estimates (CA ±20, less realistic but more playable)?
4. **Finances depth**: simple (income vs expenses, one number) or detailed (transfer budget separate from wage budget, loan system, FFP rules)?
5. **Press conferences**: every match (can get repetitive) or only big matches (derbies, finals, top-of-table clashes)?
6. **AI transfer realism**: should AI clubs only bid for realistic targets (based on need + budget), or should there be occasional blockbuster moves (Mbappé to Real Madrid) for excitement?
7. **Dark mode default**: should the app default to dark mode (modern preference) or light mode (traditional)?
8. **Youth intake**: real-style newgens (random names from a name pool by nationality) or placeholder names ("Youth Player 1, 2, 3")? Real names are more immersive but need a name database.

---

## 8. Quick Reference — Current File Sizes & Layout

```
FinalFootball/                   (repo root — github.com/Tizio90/FinalFootball)
├── .gitignore
├── README.md, README_PHASE2A.md
├── prompt_01.md                 Phase 1 prompt
├── prompt_phase2.md             Phase 2A prompt
├── prompt_phase2b.md            Phase 2B prompt (this one's predecessor)
├── prompt_phase3.md             THIS FILE
├── run.py                       8 KB
├── data/
│   ├── ingest.py               24 KB  (CSV → SQLite, 0-100 scaling, normalized schema)
│   └── merged_players.csv      32 MB  (91k players — gitignored? NO, source data)
├── engine/
│   ├── attributes.py            8 KB  (CA weights, suitability, form roll)
│   ├── lineup.py               16 KB  (Hungarian picker, 5 formations, manual override)
│   └── match.py                44 KB  (the engine — 48 phases, fouls, cards, set pieces, MOTM)
├── sim/
│   ├── season.py               24 KB  (fixtures, standings, h2h, migrations, run_season)
│   └── career.py               24 KB  (injuries, morale, transfers, cup, season summary)
├── ui/
│   ├── app.py                  48 KB  (Flask routes — careers, transfers, cup, tactics)
│   └── templates/                     (15 files, ~100 KB total — redesigned in Phase 2B)
├── tests/
│   └── test_engine.py          24 KB  (8 passing tests)
└── scripts/
    └── smoke_match.py           4 KB
```

**DB schema (13 tables):** `nations`, `divisions`, `clubs`, `players`, `player_attributes`, `player_positions`, `seasons`, `matches`, `careers`, `injuries`, `transfers`, `cup_fixtures`, `sqlite_sequence`. Phase 3 adds: `staff`, `player_development`, `youth_intake`, `press_conferences`, `player_concerns`, `scouting_reports`, `news_items`, `finances`.

**Key numbers:** 87,163 players · 7,061 clubs · 949 divisions · 179 nations · 125 playable leagues · 0-100 attribute scale · 48 phases/match · 2.22 goals/match · 0.76 yellows (too low) · 8/8 tests pass · 1.3s/season.

Start at `run.py` for entry points, `engine/match.py` for the §0 tuning fixes, then `sim/career.py` for the management features. The §0 review above is your map — fix the tuning bugs first, then build 3A → 3B → 3C → 3D.
