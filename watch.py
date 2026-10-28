#!/usr/bin/env python3
"""Fantasy live watcher. Run every 5 minutes (and once nightly) by .github/workflows/watch.yml.

    python watch.py                      # live box scores + insider news
    python watch.py --nightly            # stock moves + weekly speed leaderboard
    python watch.py --dry-run            # print alerts; push, log and save nothing
    python watch.py --event 401909877 --dry-run   # one ESPN game, any status
    python watch.py --only news --backfill 240 --dry-run   # news from the last 4 hours
"""
import argparse
from datetime import timedelta

import cats
import live
import news
import stocks
import yahoo
from core import STATE, ALERTS_DIR, load_config, load_json, log_line, now_utc, parse_ts, save_json, send

KEEP_SENT = timedelta(days=4)


def empty_state():
    return {
        "sent": {},           # alert key -> iso time sent
        "last_start": {},     # player -> {"game", "started", "prev_started"}
        "team_starters": {},  # team -> {"game", "starters", "prev_starters"}
        "games": {},          # game id -> {"final_seen_at", "progress", "minutes"}
        "news_seen": {},      # "source|item id" -> iso time first seen
        "news_polled": [],    # sources polled at least once (first poll is silent)
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nightly", action="store_true", help="stock moves + weekly speed leaderboard")
    ap.add_argument("--only", choices=["live", "news"], help="run just one half of the 5-minute job")
    ap.add_argument("--event", help="check one ESPN event id regardless of status (testing)")
    ap.add_argument("--backfill", type=int, help="news: alert on items up to N minutes old (testing)")
    ap.add_argument("--dry-run", action="store_true", help="print alerts; don't push, log or save")
    args = ap.parse_args()

    cfg = load_config()
    state = load_json(STATE / "state.json", {})
    for k, v in empty_state().items():
        state.setdefault(k, v)
    speed = load_json(STATE / "speed.json", {"stories": {}, "last_weekly": None})
    health = load_json(STATE / "sources_health.json", {})
    rosters = load_json(STATE / "rosters.json", {})
    profiles = load_json(STATE / "profiles.json", {})

    # Yahoo (optional) overrides the roster and this week's categories.
    y = yahoo.connect(cfg, health)
    cfg["roster"] = yahoo.safe(y, health, y.roster, cfg["roster"]) or cfg["roster"]
    matchup = {**cfg["matchup"], **yahoo.safe(y, health, y.matchup, {})}
    avail_cache = {}

    def avail(name):
        if name not in avail_cache:
            avail_cache[name] = yahoo.safe(y, health, lambda: y.availability(name), None)
        return avail_cache[name]

    local = now_utc().astimezone(cfg["tz"])
    ctx = {"matchup": matchup, "local_hour": local.hour, "avail": avail,
           "vulture_hour": cfg["settings"]["vulture_after_local_hour"]}
    if matchup.get("close") or matchup.get("losing"):
        print(f"matchup vs {matchup.get('opponent') or '?'}: close {matchup.get('close')}, "
              f"losing {matchup.get('losing')}")

    rosters = news.refresh_rosters(cfg, rosters)
    alerts = []
    if args.nightly:
        summary, report, profiles = stocks.run(cfg, rosters, matchup, avail)
        print(report)
        if summary:
            alerts.append({"key": f"stocks|{local:%Y-%m-%d}", "rule": "stocks", "msg": summary,
                           "priority": 3, "score": 0, "url": ""})
        if not args.dry_run:
            ALERTS_DIR.mkdir(exist_ok=True)
            (ALERTS_DIR / f"stocks-{local:%Y-%m-%d}.md").write_text(report)
        last = speed.get("last_weekly")
        if not last or now_utc() - parse_ts(last) >= timedelta(days=6, hours=12):
            board = news.leaderboard(speed)
            if board:
                line = "SPEED last 7 days (stories 2+ sources had) — " + "; ".join(
                    f"{s} first {f}/{n}" + (f", ~{ahead:.0f}m ahead" if f else "")
                    + (f", ~{behind:.0f}m behind when not" if n > f else "")
                    for s, f, n, ahead, behind in board[:10])
                print(line)
                if not args.dry_run:
                    log_line(cfg, line)
                    speed["last_weekly"] = now_utc().isoformat(timespec="seconds")
    else:
        if args.only != "news":
            alerts += live.run(cfg, state, args.event)
        if args.only != "live" and not args.event:
            alerts += news.run(cfg, state, speed, health, rosters, profiles, args.backfill)

    fresh = [cats.decorate(a, ctx) for a in alerts if a["key"] not in state["sent"]]
    for key in send(cfg, fresh, args.dry_run):
        state["sent"][key] = now_utc().isoformat(timespec="seconds")
    print(f"{len(fresh)} new alert(s).")

    cutoff = now_utc() - KEEP_SENT
    state["sent"] = {k: t for k, t in state["sent"].items() if parse_ts(t) >= cutoff}
    if not args.dry_run:
        save_json(STATE / "state.json", state)
        save_json(STATE / "speed.json", speed)
        save_json(STATE / "sources_health.json", health)
        save_json(STATE / "rosters.json", rosters)
        save_json(STATE / "profiles.json", profiles)


if __name__ == "__main__":
    main()
