"""Which 11-cat H2H categories a player helps, and how that ranks against this week's matchup."""

CATS = ["FGM", "FG%", "FTM", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TO"]

# A player "helps" a category when his per-36 rate clears the bar (counting stats)
# or his percentage clears the bar on real volume (FG%/FT%).
PER36_BARS = {"FGM": 8.0, "FTM": 4.5, "3PM": 2.5, "PTS": 20.0, "REB": 9.0,
              "AST": 6.0, "STL": 1.5, "BLK": 1.5}
PCT_BARS = {"FG%": (0.52, 8.0), "FT%": (0.82, 3.0)}   # (pct, attempts per 36)
LOW_TO_PER36 = 1.5


def helps(s):
    """s: totals dict with min, fgm, fga, ftm, fta, 3pm, pts, reb, ast, stl, blk, to.

    Works on a single game line or season totals/averages (rates are what count)."""
    m = s.get("min", 0)
    if m < 6:
        return []
    k = 36.0 / m
    rate = {"FGM": s.get("fgm", 0) * k, "FTM": s.get("ftm", 0) * k, "3PM": s.get("3pm", 0) * k,
            "PTS": s.get("pts", 0) * k, "REB": s.get("reb", 0) * k, "AST": s.get("ast", 0) * k,
            "STL": s.get("stl", 0) * k, "BLK": s.get("blk", 0) * k}
    out = [c for c, bar in PER36_BARS.items() if rate[c] >= bar]
    for c, (pct, vol) in PCT_BARS.items():
        made, att = (s.get("fgm", 0), s.get("fga", 0)) if c == "FG%" else (s.get("ftm", 0), s.get("fta", 0))
        if att * k >= vol and att and made / att >= pct:
            out.append(c)
    if m >= 15 and s.get("to", 0) * k <= LOW_TO_PER36:
        out.append("TO")
    return [c for c in CATS if c in out]


def rank(cats_helped, matchup):
    """-> (score bonus, tag text, hits_close). Close cats are starred."""
    close = set(matchup.get("close", []))
    losing = set(matchup.get("losing", []))
    if not cats_helped:
        return 0, "", False
    parts = [c + ("★" if c in close else "") for c in cats_helped]
    hit_close = bool(close & set(cats_helped))
    bonus = 3 * len(close & set(cats_helped)) + 2 * len(losing & set(cats_helped))
    return bonus, "helps " + ", ".join(parts), hit_close


def line_totals(p):
    """Box-score player dict (live.py) -> totals for helps()."""
    fgm, fga = made_att(p["FG"])
    ftm, fta = made_att(p["FT"])
    return {"min": num(p["min"]), "fgm": fgm, "fga": fga, "ftm": ftm, "fta": fta,
            "3pm": made_att(p["3PT"])[0], "pts": num(p["pts"]), "reb": num(p["REB"]),
            "ast": num(p["AST"]), "stl": num(p["stl"]), "blk": num(p["blk"]), "to": num(p["TO"])}


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def made_att(v):
    """'4-9' -> (4.0, 9.0)."""
    parts = str(v).split("-")
    return num(parts[0]), num(parts[1]) if len(parts) > 1 else 0.0


def decorate(alert, ctx):
    """Add matchup tags, the vulture flag and availability; set final priority/score.

    alert: key, msg, rule, name, team, fa (free-agent candidate), helps, starts_next,
           priority, score.  ctx: matchup, local_hour, vulture_hour, avail(name)->str|None."""
    bonus, tag, hit_close = rank(alert.get("helps", []), ctx["matchup"])
    msg = alert["msg"]
    if tag:
        msg += f" [{tag}]"
    vulture = owned = False
    if alert.get("fa"):
        avail = ctx["avail"](alert["name"])
        owned = avail and avail.startswith("rostered")
        late = ctx["local_hour"] >= ctx["vulture_hour"] or ctx["local_hour"] < 6
        vulture = not owned and (late or alert.get("starts_next"))
        msg += f" ({avail or 'check availability'})"
        if vulture:
            msg = "ADD TONIGHT: " + msg
    priority = alert.get("priority", 3)
    if alert.get("fa") and owned:
        priority = min(priority, 3)
    elif alert["rule"].startswith("nmu") and hit_close:
        priority = 5
    elif vulture or hit_close:
        priority = max(priority, 4)
    alert.update(msg=msg, priority=priority,
                 score=alert.get("score", 0) + bonus + (5 if vulture else 0) + priority)
    return alert
