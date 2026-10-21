# Fantasy Control Room

One dashboard for four fantasy football teams, plus Parlay Pal (NFL and Power 5 college stat milestones with matchup ratings).

`build.py` pulls fresh data and rewrites `index.html`. A GitHub Action (`.github/workflows/weekly.yml`) runs it every Tuesday and Wednesday morning and commits the result; GitHub Pages serves `index.html`.

- **Sleeper teams** (Melillo Redraft, Melillo Dynasty, DPhi Dynasty): rosters, weekly lineups, points and the transaction log come from the public Sleeper API. No login needed.
- **NFL stats:** nflverse. **NFL schedule and college box scores:** ESPN's public scoreboard data.
- **Eddie Pineiro (ESPN):** edit `data/eddie.json` by hand. Same shape as the other teams: `weeks -> week number -> {s: starters, b: bench}`.
- If one source fails, the last good copy in `data/cache_*.json` is used.

Run it yourself: `pip install pandas requests && python build.py`, then open `index.html`.
Run it now on GitHub: Actions tab, Weekly refresh, Run workflow.

Parlay Pal shows stat milestones (like 5+ catches), not sportsbook lines.
