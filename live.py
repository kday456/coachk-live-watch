"""Live box-score rules (v1): lineup, fouls, minutes stall, next man up, watch-list big lines."""
import sys
from datetime import datetime, timedelta

import cats
from core import ESPN, get, norm_name, now_utc, team_code, to_int

FINAL_GRACE = timedelta(minutes=30)
STAT_KEYS = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "PF"]


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
                p["threes"] = to_int(p["3PT"])
                p["stl"] = to_int(p["STL"])
                p["blk"] = to_int(p["BLK"])
                roster[norm_name(p["name"])] = p
    return game


def fetch_game(event_id):
    return parse_game(get(f"{ESPN}/summary", {"event": event_id}), event_id)


def previous_game_id(team, before_event):
    """Most recent completed game for `team` other than `before_event`."""
    data = get(f"{ESPN}/teams/{team.lower()}/schedule")
    done = [e for e in data.get("events", [])
            if e["competitions"][0]["status"]["type"]["completed"] and str(e["id"]) != str(before_event)]
    done.sort(key=lambda e: e["date"])
    return str(done[-1]["id"]) if done else None


# ---------------------------------------------------------------- starts

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
    """Return alert dicts for everything that currently matches a rule."""
    th = cfg["settings"]["thresholds"]
    out = []
    gid = game["id"]
    pre_q4 = 1 <= game["period"] <= 3
    live = game["state"] == "in"
    roster = {norm_name(r["name"]): team_code(r["nba_team"]) for r in cfg["roster"]}
    watch = {norm_name(r["name"]): team_code(r["nba_team"]) for r in cfg["watch"]}
    ignore = {norm_name(n) for n in cfg["ignore"]}
    gstate = state["games"].setdefault(gid, {"final_seen_at": None, "progress": None, "minutes": {}})

    progress = [game["period"], game["clock"]]
    clock_moved = game["status"] == "STATUS_IN_PROGRESS" and gstate["progress"] not in (None, progress)
    gstate["progress"] = progress

    def alert(key, rule, msg, p=None, name=None, team=None, priority=3, score=0, fa=False):
        out.append({"key": key, "rule": rule, "msg": msg, "name": name or p["name"],
                    "team": team or p["team"], "fa": fa, "priority": priority, "score": score,
                    "helps": cats.helps(cats.line_totals(p)) if p and fa else []})

    # --- roster players
    for key, team in roster.items():
        if team not in game["teams"]:
            continue
        p = game["players"].get(team, {}).get(key)
        name = p["name"] if p else next(r["name"] for r in cfg["roster"] if norm_name(r["name"]) == key)
        ls = state["last_start"].get(key, {})

        if live and ls.get("game") == gid and ls.get("prev_started") and not ls.get("started"):
            why = f" ({p['reason'].title()})" if p and p["dnp"] else ""
            alert(f"{key}|{gid}|no_start", "no_start",
                  f"CHECK LINEUP: {name} did NOT start{why} — started last game. {where(game)}",
                  name=name, team=team, priority=4, score=8)
        elif live and p is None:
            print(f"  note: {name} not in {team} box score for {gid}", file=sys.stderr)

        if not p:
            continue

        if pre_q4 and live and p["pf"] >= th["fouls"]:
            alert(f"{key}|{gid}|fouls", "fouls",
                  f"FOUL TROUBLE: {name} has {p['pf']} fouls in Q{game['period']}. {where(game)}", p, score=4)

        m = gstate["minutes"].setdefault(key, {"min": p["min"], "stall": 0})
        if p["on_court"] or p["min"] > m["min"]:
            m["stall"] = 0
        elif clock_moved and pre_q4 and p["min"] > 0:
            m["stall"] += 1
        m["min"] = p["min"]
        if m["stall"] >= th["stall_runs"]:
            alert(f"{key}|{gid}|stall", "stall",
                  f"POSSIBLE INJURY: {name} stuck at {p['MIN']} min for {th['stall_runs']} checks "
                  f"in Q{game['period']}. {where(game)}", p, priority=4, score=7)

    # --- next man up on roster teams
    for team in set(roster.values()) & set(game["teams"]):
        ts = state["team_starters"].get(team, {})
        baseline = ts.get("prev_starters") if ts.get("game") == gid else ts.get("starters")
        for key, p in game["players"].get(team, {}).items():
            if key in roster or key in ignore:
                continue
            if live and p["starter"] and baseline and key not in baseline:
                alert(f"{key}|{gid}|nmu_start", "nmu_start",
                      f"NEXT MAN UP: {p['name']} ({team}) started — not a starter last game. {where(game)}",
                      p, score=6, fa=True)
            if pre_q4 and live and (p["min"] >= th["nmu_min"] or p["pts"] >= th["nmu_pts"]):
                alert(f"{key}|{gid}|nmu_usage", "nmu_usage",
                      f"NEXT MAN UP: {p['name']} ({team}) {p['MIN']} min / {p['PTS']} pts "
                      f"in Q{game['period']}. {where(game)}", p, score=6, fa=True)

    # --- watch list
    for key, team in watch.items():
        p = game["players"].get(team, {}).get(key)
        if not p:
            continue
        hits = []
        if p["pts"] >= th["big_pts"]: hits.append(f"{p['pts']} pts")
        if p["threes"] >= th["big_3s"]: hits.append(f"{p['threes']} threes")
        if p["stl"] >= th["big_stl"]: hits.append(f"{p['stl']} stl")
        if p["blk"] >= th["big_blk"]: hits.append(f"{p['blk']} blk")
        if hits:
            alert(f"{key}|{gid}|big_line", "big_line",
                  f"FA {p['name']} ({team}): {', '.join(hits)} — {line(p)}. {where(game)}",
                  p, score=5, fa=True)
    return out


# ---------------------------------------------------------------- run

def relevant_events(cfg, state, only_event=None):
    """Event ids worth fetching: in progress, or final within FINAL_GRACE, involving our teams."""
    if only_event:
        return [str(only_event)]
    teams = {team_code(r["nba_team"]) for r in cfg["roster"] + cfg["watch"]}
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


def run(cfg, state, only_event=None):
    """-> list of alert dicts (not yet deduped/decorated)."""
    ids = relevant_events(cfg, state, only_event)
    if not ids:
        print("live: no relevant game in progress.")
        return []
    roster_teams = {team_code(r["nba_team"]) for r in cfg["roster"]}
    alerts = []
    for gid in ids:
        game = fetch_game(gid)
        print(f"live: {gid} {where(game)} ({game['status']})")
        for team in roster_teams & set(game["teams"]):
            seed_team(state, team, gid)
            roll_starts(state, game, team, [norm_name(r["name"]) for r in cfg["roster"]
                                            if team_code(r["nba_team"]) == team])
        alerts += evaluate(game, cfg, state)
    keep = {k.split("|")[1] for k in state["sent"]} | set(ids)
    state["games"] = {g: v for g, v in state["games"].items() if g in keep}
    return alerts
