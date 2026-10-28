"""Nightly stock moves from ESPN game logs: BREAKOUT, TIER-UP, SELLING. Also builds player profiles."""
from datetime import timedelta

import cats
from core import ESPN_WEB, get, norm_name, now_utc, parse_ts, pmap, team_code

# Per-game tier lines (mid starts at the first number, top at the second).
TIERS = {"PTS": (10, 19), "REB": (4, 8), "AST": (3, 6), "3PM": (1.0, 2.2),
         "STL": (0.8, 1.4), "BLK": (0.5, 1.2), "FGM": (4, 7.5), "FTM": (1.5, 4)}
FIELDS = ["min", "fgm", "fga", "ftm", "fta", "3pm", "pts", "reb", "ast", "stl", "blk", "to"]
KEYMAP = {"PTS": "pts", "REB": "reb", "AST": "ast", "3PM": "3pm", "STL": "stl", "BLK": "blk",
          "FGM": "fgm", "FTM": "ftm"}


def parse_gamelog(d):
    """ESPN gamelog JSON -> [(date, totals)] for regular-season games, oldest first."""
    names = d.get("names", [])
    events = d.get("events", {})
    games = []
    for st in d.get("seasonTypes", []):
        if "Regular Season" not in st.get("displayName", ""):
            continue
        for cat in st.get("categories", []):
            for ev in cat.get("events", []):
                v = dict(zip(names, ev.get("stats", [])))
                fgm, fga = cats.made_att(v.get("fieldGoalsMade-fieldGoalsAttempted"))
                ftm, fta = cats.made_att(v.get("freeThrowsMade-freeThrowsAttempted"))
                tpm, _ = cats.made_att(v.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
                date = events.get(ev["eventId"], {}).get("gameDate")
                if not date:
                    continue
                games.append((parse_ts(date), {
                    "min": cats.num(v.get("minutes")), "fgm": fgm, "fga": fga, "ftm": ftm, "fta": fta,
                    "3pm": tpm, "pts": cats.num(v.get("points")), "reb": cats.num(v.get("totalRebounds")),
                    "ast": cats.num(v.get("assists")), "stl": cats.num(v.get("steals")),
                    "blk": cats.num(v.get("blocks")), "to": cats.num(v.get("turnovers"))}))
    return sorted(games, key=lambda g: g[0])


def avg(games):
    n = len(games)
    out = {f: sum(g[1][f] for g in games) / n for f in FIELDS} if n else {f: 0.0 for f in FIELDS}
    out["gp"] = n
    return out


def prod(a):
    """Rough per-game production for 'rising/falling'."""
    return a["pts"] + a["reb"] + 1.5 * a["ast"] + 3 * a["stl"] + 3 * a["blk"] + 2 * a["3pm"] - 2 * a["to"]


def tier(cat, value):
    mid, top = TIERS[cat]
    return 2 if value >= top else 1 if value >= mid else 0


def player_logs(pid, season_year):
    games = parse_gamelog(get(f"{ESPN_WEB}/athletes/{pid}/gamelog"))
    last = []
    cutoff = now_utc() - timedelta(days=7)
    if sum(1 for g in games if g[0] < cutoff) < 3:
        last = parse_gamelog(get(f"{ESPN_WEB}/athletes/{pid}/gamelog", {"season": season_year - 1}))
    return games, last


def analyze(name, team, kind, games, last, th):
    """-> (moves, profile). moves: list of dicts {type, text, helps}."""
    cutoff = now_utc() - timedelta(days=7)
    window = [g for g in games if g[0] >= cutoff and g[1]["min"] > 0]
    base_games = [g for g in games if g[0] < cutoff and g[1]["min"] > 0]
    if len(base_games) < 3:
        base_games = [g for g in last if g[1]["min"] > 0] + base_games
    profile = avg([g for g in games if g[1]["min"] > 0] or base_games)
    if len(window) < 2 or len(base_games) < 3:
        return [], profile
    w, b = avg(window), avg(base_games)
    dmin = w["min"] - b["min"]
    rising = prod(w) >= prod(b) * 1.10
    falling = prod(w) <= prod(b) * 0.80
    label = f"{name} ({team})"
    stat = f"{w['min']:.0f} min vs {b['min']:.0f}, {w['pts']:.1f} pts/{w['reb']:.1f} reb/{w['ast']:.1f} ast last 7d"
    moves = []
    if kind == "roster" and (dmin <= -th["selling_min_delta"] or falling):
        why = f"minutes {dmin:+.0f}" if dmin <= -th["selling_min_delta"] else "production down 20%+"
        moves.append({"type": "SELLING", "text": f"SELLING {label}: {why} — {stat}", "helps": []})
    if kind != "roster" and dmin >= th["breakout_min_delta"] and rising:
        moves.append({"type": "BREAKOUT", "text": f"BREAKOUT {label}: minutes {dmin:+.0f} — {stat}",
                      "helps": cats.helps(w)})
    ups = [c for c in TIERS if tier(c, b[KEYMAP[c]]) < 2 <= tier(c, w[KEYMAP[c]])]
    if ups:
        moves.append({"type": "TIER-UP", "text": f"TIER-UP {label}: top tier in {', '.join(ups)} — {stat}",
                      "helps": ups})
    return moves, profile


def run(cfg, rosters, matchup, avail):
    """-> (summary text or None, markdown report, profiles)."""
    th = cfg["settings"]["thresholds"]
    mine = {norm_name(r["name"]): ("roster", team_code(r["nba_team"]), r["name"]) for r in cfg["roster"]}
    mine.update({norm_name(r["name"]): ("watch", team_code(r["nba_team"]), r["name"]) for r in cfg["watch"]})
    players = []
    for team, plist in rosters.get("teams", {}).items():
        for p in plist:
            kind, _, _ = mine.get(norm_name(p["name"]), ("other", team, p["name"]))
            players.append((p["id"], p["name"], team, kind))
    found = {norm_name(p[1]) for p in players}
    missing = [v[2] for k, v in mine.items() if k not in found]
    season_year = now_utc().year + (1 if now_utc().month >= 8 else 0)

    logs = pmap(lambda p: player_logs(p[0], season_year), players, workers=10)
    moves, profiles, errors = [], {}, 0
    for (pid, name, team, kind), res in zip(players, logs):
        if isinstance(res, Exception):
            errors += 1
            continue
        mv, profile = analyze(name, team, kind, res[0], res[1], th)
        profiles[norm_name(name)] = {k: round(v, 2) for k, v in profile.items()}
        for m in mv:
            m.update(name=name, team=team, kind=kind)
            bonus, tag, hit_close = cats.rank(m["helps"], matchup)
            m["score"] = bonus + {"roster": 6, "watch": 4, "other": 2}[kind] + (3 if m["type"] == "BREAKOUT" else 0)
            m["tag"] = tag
            if kind != "roster":
                a = avail(name)
                m["avail"] = a or "check availability"
                if a and a.startswith("rostered"):
                    m["score"] -= 10
            moves.append(m)
    others = sorted([m for m in moves if m["kind"] == "other" and m["score"] > -5], key=lambda m: -m["score"])[:10]
    keep = [m for m in moves if m["kind"] != "other"] + others
    keep.sort(key=lambda m: -m["score"])

    def fmt(m):
        s = m["text"] + (f" [{m['tag']}]" if m["tag"] else "")
        return s + (f" ({m['avail']})" if m.get("avail") else "")

    date = now_utc().astimezone(cfg["tz"]).strftime("%Y-%m-%d")
    md = [f"# Stock moves — {date}", "",
          f"{len(players)} players on {len(rosters.get('teams', {}))} teams checked, {errors} game-log errors. "
          "Last 7 days vs the rest of the season (last season fills in when there are fewer than "
          "3 earlier games).", ""]
    for t in ("SELLING", "BREAKOUT", "TIER-UP"):
        rows = [m for m in keep if m["type"] == t]
        md += [f"## {t}", ""] + ([f"- {fmt(m)}" for m in rows] or ["- none"]) + [""]
    if missing:
        md += ["## Not found on ESPN rosters", "", f"- {', '.join(missing)} (check nba_team in roster.json)", ""]
    summary = None
    if keep:
        parts = []
        for t in ("SELLING", "BREAKOUT", "TIER-UP"):
            names = [m["name"] + (" (watch)" if m["kind"] == "watch" else "") for m in keep if m["type"] == t]
            if names:
                parts.append(f"{t}: {', '.join(names[:4])}" + (f" +{len(names) - 4}" if len(names) > 4 else ""))
        summary = f"STOCKS {date} — {' | '.join(parts)}. Details: alerts/stocks-{date}.md"
    return summary, "\n".join(md) + "\n", profiles
