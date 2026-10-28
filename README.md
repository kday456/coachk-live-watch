# Fantasy live watch

A free, phone-first alert bot for H2H category fantasy basketball. It runs on GitHub
Actions every 5 minutes, reads ESPN's public box scores plus free insider feeds, and
pushes to your phone through [ntfy](https://ntfy.sh). No paid APIs, no X/Twitter.

Everything personal lives in `config/`. Fork it, edit four small files, add one secret.

## What it sends you

**During games** (live box scores):

| Alert | When |
|---|---|
| CHECK LINEUP | Your player didn't start (or isn't in the box score) after starting his last game |
| FOUL TROUBLE | Your player has 4+ fouls before the 4th |
| POSSIBLE INJURY | Your player is off the floor and his minutes haven't moved for 2 checks while the clock runs (Q1–Q3) |
| NEXT MAN UP | A teammate of one of your players starts when he didn't last game, or has 20+ min or 15+ pts before the 4th |
| FA … | A watch-list free agent has 15+ pts, 3+ threes, 2+ steals or 3+ blocks |

**Any time from 6am to 1am Pacific** (insider news):
OUT / DOUBTFUL / QUESTIONABLE / PROBABLE / AVAILABLE / STARTING / MINUTES LIMIT news about
your players and watch list, plus "NEXT MAN UP?" when a rotation player on one of your
teams is ruled out. Each story is sent once no matter how many sources carry it, with
the source and the time it was originally posted. Tap the notification to open the post.

**Nightly at 2:40am ET** (stock moves, one push + `alerts/stocks-YYYY-MM-DD.md`):
- **SELLING:** your player's minutes are down 4+ over the last 7 days, or his production is down 20%+.
- **BREAKOUT:** someone on your teams has minutes up 4+ and rising production.
- **TIER-UP:** a player has moved into the top tier of a category (PTS, REB, AST, 3PM, STL, BLK, FGM, FTM).

**Tags on every alert:**
- `[helps 3PM★, BLK]` lists the categories a player helps. A ★ marks one you've listed as close this week.
  Alerts that help close categories are sent first. A next-man-up who helps a close category goes out
  at ntfy's top priority.
- `ADD TONIGHT:` goes on a pickup alert (next man up, breakout, watch list, "will start" news) after
  5pm Pacific or overnight, or whenever the news says he starts next game, so you move before the
  3am waiver vultures. With Yahoo connected it adds `FA, 12% rostered`, `waivers`, or
  `rostered by …`. Without Yahoo it says `check availability`.

**Weekly speed leaderboard** (a line in that day's `alerts/` file): for stories that 2+ sources
reported, which source had it first and by how many minutes. Use it to learn who's fastest.

## Setup (about 10 minutes)

1. **Get the code.** Fork this repo, or use it as a template. Keep it **public**: public repos
   get free, unlimited Actions minutes. Your phone topic stays secret either way.
2. **Pick a topic name.** Make up something long and random, like `fbb-7k2q9xw3mzr1p8vd`.
   Anyone who knows it can read your alerts.
3. **Phone:** install **ntfy** (iPhone or Android), tap **+**, enter the topic, Subscribe, and
   allow notifications.
4. **GitHub secret:** **Settings → Secrets and variables → Actions → New repository secret**.
   Name: `NTFY_TOPIC`. Value: your topic.
5. **Edit `config/`** (on GitHub: open the file → pencil → Commit):
   - `roster.json`: your players and a watch list of free agents, `{"name": ..., "nba_team": ...}`.
   - `settings.json`: team name, notification title, your timezone, thresholds.
   - `matchup.json`: this week's close and losing categories (see below).
   - `sources.json`: the news sources. The defaults work. Add beat reporters for your teams.
6. **Test:** **Actions → Fantasy live watch → Run workflow** (mode: watch). It should go green.

## Weekly: config/matchup.json

```json
{
  "week": 2,
  "opponent": "Team Name",
  "close": ["STL", "FT%"],
  "losing": ["3PM", "BLK"]
}
```

Categories: `FGM FG% FTM FT% 3PM PTS REB AST STL BLK TO`. Empty lists are fine: alerts
still show "helps …", just unranked.

## Optional: connect Yahoo (roster + matchup + FA status, automatically)

When this is on, your roster and this week's close/losing categories come from Yahoo
(the JSON files become the fallback), and pickup alerts show FA/waivers and % rostered.

1. Go to <https://developer.yahoo.com/apps/create/>. App name: anything. Redirect URI:
   `https://localhost:8000`. API permissions: **Fantasy Sports → Read**. Create the app.
2. On your computer, in this folder: `pip install requests`, then `python3 yahoo_auth.py`.
   Paste the Client ID and Client Secret, click Agree in the browser, then paste the
   address of the page that fails to load. It prints your refresh token.
3. Add three repository secrets: `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, `YAHOO_REFRESH_TOKEN`.
4. In `config/matchup.json` set `"yahoo": {"enabled": true, "league_id": "<your league number>"}`.
   The league number is in your Yahoo league URL.

If anything goes wrong, it falls back to the JSON files and writes the reason to
`state/sources_health.json` under `yahoo`.

## Tuning

- **Thresholds:** in `config/settings.json`: fouls, stall checks, next-man-up minutes/points,
  big-line bars, breakout/selling minute deltas, the vulture hour, and how old a news item
  can be and still alert (`news_max_age_minutes`).
- **Mute a source:** set `"enabled": false` on it in `sources.json`. Add a Bluesky account
  with `{"name": ..., "type": "bluesky", "handle": "someone.bsky.social", "team": "IND"}`.
  The `team` field is for beat reporters: their posts can match players by last name and
  report team-only news.
- **Too many NEXT MAN UP alerts** for an established starter: add his name to `ignore` in `roster.json`.
- **Nicknames:** `aliases` in `roster.json`, e.g. `"Karl-Anthony Towns": ["KAT"]`.
- **Source health:** `state/sources_health.json` shows whether each source worked on the
  last run and how many items it returned.

## Run it by hand

- **On GitHub:** Actions → Fantasy live watch → Run workflow → pick `watch` or `nightly`.
- **On your computer** (`pip install requests` first):
  ```bash
  python3 watch.py --dry-run                              # what would alert right now
  python3 watch.py --only news --backfill 240 --dry-run   # news from the last 4 hours
  python3 watch.py --event 401909877 --dry-run            # one ESPN game id, any status
  python3 watch.py --nightly --dry-run                    # tonight's stock moves
  ```
  `--dry-run` sends nothing and saves nothing.

## Pause

Actions → Fantasy live watch → **⋯** → **Disable workflow** (Enable to resume). To mute only
your phone, pause the topic in the ntfy app.

## Good to know

- Each news source's first poll is silent, so you don't get a backlog. Items older than 90
  minutes never alert.
- GitHub's cron runs late at busy times. "Every 5 minutes" is often 5–15.
- POSSIBLE INJURY can't tell an injury from a long rest on the bench.
- ESPN and Bluesky endpoints are free and unofficial. Reddit sometimes blocks cloud servers.
  If a source fails, the run continues without it and `sources_health.json` shows why.
- Data files: `state/` (memory between runs, committed by the bot), `alerts/` (everything sent).
