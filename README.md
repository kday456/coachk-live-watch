# coachk-live-watch

Live fantasy-basketball watcher for **Coach K** (Yahoo league 106275).
Every 5 minutes during NBA game hours a GitHub Action reads ESPN's free
public box scores and pushes an alert to your iPhone through
[ntfy](https://ntfy.sh) when something happens to your players.

## What it alerts on

| Alert | Rule |
|---|---|
| **CHECK LINEUP** | A roster player did not start (or isn't in the box score) after starting his previous game |
| **FOUL TROUBLE** | A roster player has 4+ fouls before the 4th quarter |
| **POSSIBLE INJURY** | A roster player is off the floor and his minutes haven't moved for 2 checks in a row while the clock runs in Q1–Q3 |
| **NEXT MAN UP** | A non-roster player on one of your players' teams starts when he didn't start last game, or has 20+ min or 15+ pts before the 4th |
| **ADD? FA** | A watch-list free agent has 15+ pts, 3+ threes, 2+ steals or 3+ blocks |

Each alert is sent once per player, game, and rule. Every alert is also
written to `alerts/YYYY-MM-DD.md` (Pacific date). The state between runs lives in
`state/state.json`, which the workflow commits back to the repo.

"Started his previous game" is looked up from ESPN the first time a team
appears, so it works from the first night.

## Setup (one time)

1. **Install ntfy** on your iPhone from the App Store (free, by Philipp Heckel).
2. **Subscribe to your topic:** open ntfy → **+** → paste the topic name
   given to you at setup (starts with `coachk-`) → Subscribe. Leave the server
   as `ntfy.sh`. Allow notifications when iOS asks. A test message is waiting there.
3. **Add the secret on GitHub:** this repo → **Settings → Secrets and variables
   → Actions → New repository secret**. Name: `NTFY_TOPIC`. Secret: the same topic name.
4. **Check that it runs:** **Actions** tab → *Coach K live watch* → **Run workflow**.
   It should finish green.

The topic name is the only thing protecting your alerts. Anyone who knows it can
read them, so don't post it anywhere.

## Editing your roster

Edit `config/roster.json` on GitHub (open the file → pencil icon → *Commit changes*).

- `roster`: your team. Add or remove `{"name": "...", "nba_team": "..."}` lines.
- `watch`: free agents you're tracking for big lines.
- `ignore`: names that should never trigger NEXT MAN UP (established
  starters you don't care about), e.g. `["Myles Turner", "Andrew Nembhard"]`.

Names are matched without case, accents, punctuation, or "Jr."
Team codes are the usual three letters (`NYK`, `NOP`, `UTA`, `GSW`, `SAS`,
`WAS` all work). Mind the commas: every line except the last in a list ends with one.

Thresholds (fouls, 20 min / 15 pts, etc.) are at the top of `watch.py`.

## Running it manually

- **On GitHub:** Actions → *Coach K live watch* → **Run workflow**. Manual runs
  skip the game-hours check.
- **On your Mac:**
  ```bash
  pip install requests
  python3 watch.py --dry-run                    # show what would alert right now
  python3 watch.py --event 401909877 --dry-run  # check one ESPN game id
  ```
  `--dry-run` sends nothing and doesn't touch state. The game id is the number in an
  ESPN game URL (`espn.com/nba/game/_/gameId/<id>`).

## Pausing

Actions → *Coach K live watch* → **⋯** (top right) → **Disable workflow**.
Turn it back on with **Enable workflow**. To mute only your phone, pause the topic
in the ntfy app.

## Notes

- GitHub's cron isn't punctual. "Every 5 minutes" often runs 5–15 minutes apart
  at busy times.
- Outside 16:00–07:59 UTC the job exits in a few seconds.
- POSSIBLE INJURY can't tell an injury from a long rest on the bench. Treat it as
  a nudge to check the game.
- Data comes from ESPN's unofficial public endpoints (no key, no cost). If ESPN
  changes them, the run fails and shows red in the Actions tab.
