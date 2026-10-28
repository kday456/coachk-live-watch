#!/usr/bin/env python3
"""Coach K live watcher.

Polls ESPN's public NBA endpoints, applies the alert rules in the README, and
pushes new alerts to ntfy. Run every 5 minutes by .github/workflows/watch.yml.

    python watch.py                 # normal run (needs NTFY_TOPIC to push)
    python watch.py --dry-run       # print alerts, push nothing
    python watch.py --event 401909877 --dry-run   # test against one game
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
ROSTER_FILE = ROOT / "config" / "roster.json"
STATE_FILE = ROOT / "state" / "state.json"
ALERTS_DIR = ROOT / "alerts"

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
NTFY_TITLE = "Coach K live"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")  # names the alerts/ file for a game night

# Alert thresholds — tweak here.
FOULS = 4            # fouls before the 4th
STALL_RUNS = 2       # consecutive runs with no new minutes (Q1-Q3, off the floor)
NMU_MIN, NMU_PTS = 20, 15
BIG_PTS, BIG_3S, BIG_STL, BIG_BLK = 15, 3, 2, 3

FINAL_GRACE = timedelta(minutes=30)
KEEP_SENT = timedelta(days=4)

# Standard abbreviations -> ESPN's. Anything not listed is already the same.
TEAM_ALIASES = {
    "NYK": "NY", "NOP": "NO", "NOH": "NO", "UTA": "UTAH", "GSW": "GS",
    "SAS": "SA", "WAS": "WSH", "PHO": "PHX", "BRK": "BKN", "CHO": "CHA",
}

STAT_KEYS = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "PF"]


# ---------------------------------------------------------------- helpers

def team_code(abbr):
    abbr = abbr.strip().upper()
    return TEAM_ALIASES.get(abbr, abbr)


def norm_name(name):
    """'Kentavious Caldwell-Pope' / 'Jaren Jackson Jr.' -> comparable key."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[.\-']", "", s.lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s)
    return " ".join(s.split())


def to_int(v):
    try:
        return int(str(v).split("-")[0])
    except (ValueError, TypeError):
        return 0


def made(v):
    """'3-7' -> 3."""
    return to_int(v)


def get(url, **params):
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def now_utc():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- ESPN parsing

def parse_game(summary, event_id):
    comp = summary["header"]["competitions"][0]
    status = comp["status"]
    game = {
        "id": str(event_id),
        "status": status["type"]["name"],          # STATUS_IN_PROGRESS, STATUS_HALFTIME, ...
        "state": status["type"]["state"],          # pre / in / post
        "period": int(status.get("period") or 0),
        "clock": status.get("displayClock", ""),
        "detail": status["type"].get("shortDetail", ""),
        "teams": [team_code(c["team"]["abbreviation"]) for c in comp["competitors"]],
        "players": {},                             # team -> {norm_name: player}
    }
    for block in summary.get("boxscore", {}).get("players", []):
        team = team_code(block["team"]["abbreviation"])
        roster = game["players"].setdefault(team, {})
        for stats in block.get("statistics", []):
            labels = stats.get("labels") or stats.get("names") or []
            for a in stats.get("athletes", []):
                vals = dict(zip(labels, a.get("stats") or []))
                p = {
                    "name": a["athlete"]["displayName"],
                    "team": team,
                    "starter": bool(a.get("starter")),
                    "on_court": bool(a.get("active")),
                    "dnp": bool(a.get("didNotPlay")),
                    "reason": a.get("reason", ""),
                }
                for k in STAT_KEYS:
                    p[k] = vals.get(k, "0")
                p["min"] = to_int(p["MIN"])
                p["pts"] = to_int(p["PTS"])
                p["pf"] = to_int(p["PF"])
                p["threes"] = made(p["3PT"])
                p["stl"] = to_int(p["STL"])
                p["blk"] = to_int(p["BLK"])
                roster[norm_name(p["name"])] = p
    return game


def fetch_game(event_id):
    return parse_game(get(f"{ESPN}/summary", event=event_id), event_id)


def previous_game_id(team, before_event):
    """Most recent completed game for `team` other than `before_event`."""
    data = get(f"{ESPN}/teams/{team.lower()}/schedule")
    done = [e for e in data.get("events", [])
            if e["competitions"][0]["status"]["type"]["completed"] and str(e["id"]) != str(before_event)]
    done.sort(key=lambda e: e["date"])
    return str(done[-1]["id"]) if done else None


# ---------------------------------------------------------------- state

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def empty_state():
    return {
        "sent": {},        # "player|game|rule" -> iso time sent
        "last_start": {},  # norm_name -> {"game", "started", "prev_started"}
        "team_starters": {},  # team -> {"game", "starters", "prev_starters"}
        "games": {},       # game_id -> {"final_seen_at", "progress", "minutes": {name: {"min", "stall"}}}
    }


def seed_team(state, team, current_game_id):
    """First time we see a team: look up its previous game so 'started last game' works on night one."""
    if team in state["team_starters"]:
        return
    prev_id = previous_game_id(team, current_game_id)
    starters = []
    if prev_id:
        prev = fetch_game(prev_id)
        for key, p in prev["players"].get(team, {}).items():
            if p["starter"]:
                starters.append(key)
            state["last_start"].setdefault(key, {"game": prev_id, "started": p["starter"], "prev_started": None})
    state["team_starters"][team] = {"game": prev_id, "starters": starters, "prev_starters": None}


def roll_starts(state, game, team, roster_keys):
    """Record this game's starters, keeping the previous game's alongside.

    A roster player missing from a populated box score (ruled out pre-game)
    counts as not starting."""
    box = game["players"].get(team, {})
    starters = [k for k, p in box.items() if p["starter"]]
    if not starters:
        return  # box score not populated yet
    ts = state["team_starters"][team]
    if ts["game"] != game["id"]:
        ts.update(prev_starters=ts["starters"], starters=starters, game=game["id"])
    for key in set(box) | set(roster_keys):
        started = key in box and box[key]["starter"]
        ls = state["last_start"].get(key)
        if ls is None:
            state["last_start"][key] = {"game": game["id"], "started": started, "prev_started": None}
        elif ls["game"] != game["id"]:
            ls.update(prev_started=ls["started"], started=started, game=game["id"])


# ---------------------------------------------------------------- rules

def line(p):
    return (f"{p['MIN']} min, {p['PTS']} pts, {p['REB']} reb, {p['AST']} ast, "
            f"{p['3PT']} 3PT, {p['STL']} stl, {p['BLK']} blk, {p['PF']} PF")


def where(game):
    a, b = game["teams"]
    return f"{a}-{b} {game['detail']}"


def evaluate(game, cfg, state):
    """Return a list of (key, message) for everything that currently matches a rule."""
    out = []
    gid = game["id"]
    pre_q4 = 1 <= game["period"] <= 3
    live = game["state"] == "in"
    roster = {norm_name(r["name"]): team_code(r["nba_team"]) for r in cfg["roster"]}
    watch = {norm_name(r["name"]): team_code(r["nba_team"]) for r in cfg["watch"]}
    ignore = {norm_name(n) for n in cfg.get("ignore", [])}
    gstate = state["games"].setdefault(gid, {"final_seen_at": None, "progress": None, "minutes": {}})

    progress = [game["period"], game["clock"]]
    clock_moved = game["status"] == "STATUS_IN_PROGRESS" and gstate["progress"] not in (None, progress)
    gstate["progress"] = progress

    def find(key, team):
        return game["players"].get(team, {}).get(key)

    # --- roster players
    for key, team in roster.items():
        if team not in game["teams"]:
            continue
        p = find(key, team)
        name = p["name"] if p else next(r["name"] for r in cfg["roster"] if norm_name(r["name"]) == key)
        ls = state["last_start"].get(key, {})

        if live and ls.get("game") == gid and ls.get("prev_started") and not ls.get("started"):
            why = f" ({p['reason'].title()})" if p and p["dnp"] else ""
            out.append((f"{key}|{gid}|no_start",
                        f"CHECK LINEUP: {name} did NOT start{why} — started last game. {where(game)}"))
        elif live and p is None:
            print(f"  note: {name} not in {team} box score for {gid}", file=sys.stderr)

        if not p:
            continue

        if pre_q4 and live and p["pf"] >= FOULS:
            out.append((f"{key}|{gid}|fouls",
                        f"FOUL TROUBLE: {name} has {p['pf']} fouls in Q{game['period']}. {where(game)}"))

        m = gstate["minutes"].setdefault(key, {"min": p["min"], "stall": 0})
        if p["on_court"] or p["min"] > m["min"]:
            m["stall"] = 0
        elif clock_moved and pre_q4 and p["min"] > 0:
            m["stall"] += 1
        m["min"] = p["min"]
        if m["stall"] >= STALL_RUNS:
            out.append((f"{key}|{gid}|stall",
                        f"POSSIBLE INJURY: {name} stuck at {p['MIN']} min for {STALL_RUNS} checks in Q{game['period']}. {where(game)}"))

    # --- next man up on roster teams
    roster_teams = set(roster.values()) & set(game["teams"])
    for team in roster_teams:
        ts = state["team_starters"].get(team, {})
        baseline = ts.get("prev_starters") if ts.get("game") == gid else ts.get("starters")
        for key, p in game["players"].get(team, {}).items():
            if key in roster or key in ignore:
                continue
            if live and p["starter"] and baseline and key not in baseline:
                out.append((f"{key}|{gid}|nmu_start",
                            f"NEXT MAN UP: {p['name']} ({team}) started — not a starter last game. {where(game)}"))
            if pre_q4 and live and (p["min"] >= NMU_MIN or p["pts"] >= NMU_PTS):
                out.append((f"{key}|{gid}|nmu_usage",
                            f"NEXT MAN UP: {p['name']} ({team}) {p['MIN']} min / {p['PTS']} pts in Q{game['period']}. {where(game)}"))

    # --- watch list
    for key, team in watch.items():
        p = find(key, team)
        if not p:
            continue
        hits = []
        if p["pts"] >= BIG_PTS: hits.append(f"{p['pts']} pts")
        if p["threes"] >= BIG_3S: hits.append(f"{p['threes']} threes")
        if p["stl"] >= BIG_STL: hits.append(f"{p['stl']} stl")
        if p["blk"] >= BIG_BLK: hits.append(f"{p['blk']} blk")
        if hits:
            out.append((f"{key}|{gid}|big_line",
                        f"ADD? FA {p['name']} ({team}): {', '.join(hits)} — {line(p)}. {where(game)}"))
    return out


# ---------------------------------------------------------------- output

def push(topic, msg):
    r = requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"),
                      headers={"Title": NTFY_TITLE, "Tags": "basketball"}, timeout=15)
    r.raise_for_status()


def log_alert(msg, when):
    ALERTS_DIR.mkdir(exist_ok=True)
    local = when.astimezone(LOCAL_TZ)
    path = ALERTS_DIR / f"{local:%Y-%m-%d}.md"
    if not path.exists():
        path.write_text(f"# Coach K alerts — {local:%Y-%m-%d}\n\n")
    with path.open("a") as f:
        f.write(f"- {local:%H:%M} PT — {msg}\n")


# ---------------------------------------------------------------- main

def relevant_events(cfg, state, only_event=None):
    """Event ids worth fetching: in progress, or final within FINAL_GRACE, involving our teams."""
    teams = {team_code(r["nba_team"]) for r in cfg["roster"] + cfg["watch"]}
    if only_event:
        return [str(only_event)]
    board = get(f"{ESPN}/scoreboard")
    ids = []
    for e in board.get("events", []):
        gid = str(e["id"])
        st = e["status"]["type"]
        abbrs = {team_code(c["team"]["abbreviation"]) for c in e["competitions"][0]["competitors"]}
        if not abbrs & teams:
            continue
        if st["state"] == "in":
            ids.append(gid)
        elif st["completed"] and gid in state["games"]:
            g = state["games"][gid]
            g["final_seen_at"] = g["final_seen_at"] or now_utc().isoformat()
            if now_utc() - datetime.fromisoformat(g["final_seen_at"]) <= FINAL_GRACE:
                ids.append(gid)
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print alerts; don't push, log or save state")
    ap.add_argument("--event", help="check one ESPN event id regardless of status (testing)")
    args = ap.parse_args()

    cfg = load_json(ROSTER_FILE, None)
    if not cfg:
        sys.exit(f"Can't read {ROSTER_FILE}")
    state = load_json(STATE_FILE, empty_state())
    for k, v in empty_state().items():
        state.setdefault(k, v)
    before = json.dumps(state, sort_keys=True)

    ids = relevant_events(cfg, state, args.event)
    if not ids:
        print("No relevant NBA game in progress — nothing to do.")
        if not args.dry_run and json.dumps(state, sort_keys=True) != before:
            STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
        return

    roster_teams = {team_code(r["nba_team"]) for r in cfg["roster"]}
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic and not args.dry_run:
        print("NTFY_TOPIC not set — alerts will be logged but not pushed.", file=sys.stderr)

    new_alerts = 0
    for gid in ids:
        game = fetch_game(gid)
        print(f"{gid}: {where(game)} ({game['status']})")
        for team in roster_teams & set(game["teams"]):
            seed_team(state, team, gid)
            roll_starts(state, game, team, [norm_name(r["name"]) for r in cfg["roster"]
                                            if team_code(r["nba_team"]) == team])
        for key, msg in evaluate(game, cfg, state):
            if key in state["sent"]:
                continue
            new_alerts += 1
            print(f"  ALERT {msg}")
            if args.dry_run:
                continue
            if topic:
                try:
                    push(topic, msg)
                except requests.RequestException as e:
                    print(f"  push failed ({e}); will retry next run", file=sys.stderr)
                    continue
            log_alert(msg, now_utc())
            state["sent"][key] = now_utc().isoformat()

    cutoff = now_utc() - KEEP_SENT
    state["sent"] = {k: t for k, t in state["sent"].items() if datetime.fromisoformat(t) >= cutoff}
    keep = {k.split("|")[1] for k in state["sent"]} | set(ids)
    state["games"] = {g: v for g, v in state["games"].items() if g in keep}

    print(f"{new_alerts} new alert(s).")
    if not args.dry_run:
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
