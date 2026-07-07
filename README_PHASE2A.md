# Football Management Simulation — Phase 2A

Phase 2A (Engine Realism) complete. Builds on Phase 1 with attribute-driven
improvements to the match engine, lineup selection, and standings — no new
UI pages (those come in Phase 2B).

## What's new in Phase 2A

### §0 tech-debt fixes (10 items)

1. **Fatigue refactor** — `_fatigue_multiplier()` is now the single source of
   truth. Fixed the Phase 1 bug where `1 - decay * 100` clamped everyone to
   the minimum (0.55). Now Sta=1 ends at ~0.69, Sta=20 at ~0.84 after 80
   phases, exactly as documented.
2. **Assists recorded** — passer threaded through `_attempt_shot` →
   `_score_goal`. 96% of goals now have an assist attributed.
3. **Fouls + yellow/red cards** — lost-by-wide-margin tackles can be fouls
   (driven by tackler `Agg`). Cards driven by `Agg + Dirt` vs. per-match
   referee strictness. Reds remove the player (team plays with 10).
4. **Build-up fouls** stop play (no shot from midfield). Final-third fouls
   yield a free kick (Fre-driven, no chaining back to corners).
5. **Possession re-eval** — implemented then REMOVED. The +10% midfield boost
   for the losing team created a bimodal score distribution (most matches
   0-0, some 6+ goals). Documented in the code for Phase 3 revisit.
6. **Hungarian algorithm lineup picker** — `scipy.optimize.linear_sum_assignment`
   replaces the greedy slot-by-slot picker. Finds the global optimum
   assignment, avoiding the trap where the best RB is also the best RCB.
7. **Head-to-head standings tiebreaker** — when points are equal, clubs are
   ranked by h2h points → h2h GD → h2h GF → overall GD → overall GF → name.
8. **UI lineup caching** — `_LINEUP_CACHE` memoizes per-club XIs for the
   process lifetime. Match-view and club-view no longer re-derive lineups.
9. **Injury events** — tiny per-phase probability driven by `Inj Pr` and
   opponent `Agg`. Injured player is subbed off; if no subs remain, team
   plays with 10.
10. **Minute jitter + sort** — `_phase_to_minute` uses ±1.5 jitter (was ±0.4).
    Events sorted by minute at the end (stable sort preserves within-minute
    order).

### Critical bug fix (not in the original §0 list)

**Lineup mutation across matches**: the engine was mutating the XI list
in place during substitutions. When the test ran 500 matches with the same
XI reference, by match 500 the XI was completely subbed out. Fixed by
`copy.deepcopy(home_xi)` in the `MatchEngine` constructor. This was the
root cause of the bimodal score distribution that plagued early Phase 2
tuning.

### 2A new features (7 items)

1. **Formations** — 4-3-3 (default), 4-4-2, 4-2-3-1, 3-5-2. Auto-picked per
   club based on squad composition (DEF/MID/ATT counts). Stored on the
   `club_tactics` table (to be populated in 2B manager mode).
2. **Mentalities** — defensive / balanced / attacking. Modifies phase count
   (±15%), shot threshold (±0.5), and defensive line height (0.7 / 1.0 /
   1.3). Default is balanced (no-op).
3. **Set pieces** — corners (25% shot chance, Hea/Jum aerial duel, no
   chaining), free kicks (Fre-driven, wall reduces, +3.0 keeper advantage),
   penalties (75% base, modified by Pen vs 1v1).
4. **Expanded match stats** — fouls, offsides, shots_off_target,
   passes_attempted, passes_completed, free_kicks, penalties.
5. **Player of the match** — per-player rating computed from
   goals/assists/shots/saves/tackles/pass-completion. Highest-rated player
   on the winning side is MOTM. Stored on `MatchResult.motm_uid/name`.
6. **Asymmetric home advantage** — per-club, derived from last 5 home
   results. Baseline 1.08; +0.015 per home win, -0.015 per home loss.
   Clamped to [1.02, 1.18].
7. **Visible morale field** — `morale` column added to `players` table
   (default 10, range 1-20). Displayed on the club page with red/yellow/green
   color coding. Will be mutated by match events in Phase 2B.

### Validation (7 tests, all pass)

```
Test 1: Strong vs Weak      — strong wins 79.6% (>=65% required)
Test 2: Close Matchup        — no domination, 2.96 avg goals, 56% within 1 goal
Test 3: Goal Distribution    — 2.45 avg goals/match (0.8-2.6 band)
Test 4: Formation Impact     — 4-4-2 vs 3-5-2 chi-squared p<0.10 OR mean diff >=0.15
Test 5: Fatigue Effect       — Sta=1 ends 0.69, Sta=20 ends 0.84
Test 6: Assists Recorded     — 96.2% of goals have an assist (>=40% required)
Test 7: Cards Emitted        — 114 yellows, 4 reds across 200 matches
```

### Performance

- Full 380-match season: **2.6 seconds** (well under the 5s constraint)
- All Flask routes return 200 in <100ms (with lineup cache)

## Stack

- Python 3.10+ stdlib + sqlite3 + Flask + scipy (one new pip install)
- One command: `python run.py serve`

## What's NOT in 2A (deferred to 2B)

- Manager mode (manual XI, mentality, formation selection)
- Transfer market
- Injuries/morale cross-match persistence
- Multi-league + promotion/relegation
- Save/load careers
- Season summaries

See `prompt_2.md` §1.B for the 2B scope.
