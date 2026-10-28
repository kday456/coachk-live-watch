"""Insiders: pull free news feeds, keep what matters to my players/teams, dedupe stories, time the sources."""
import re
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime

import cats
from core import (ESPN, aware, cared_teams, get, norm_name, norm_text, now_utc,
                  parse_ts, pmap, team_code)

KEEP_SEEN = timedelta(days=3)
KEEP_STORIES = timedelta(days=21)

# Order matters: first match wins.
BUCKETS = [
    ("OUT", r"ruled out|will not play|wont play|will miss|to miss|is out|are out|out for|out tonight|"
            r"out vs|out against|out indefinitely|out \d+|sidelined|surgery|torn|fracture|"
            r"not expected to play|wont return|will not return|ruled him out|dnp"),
    ("DOUBTFUL", r"doubtful"),
    ("QUESTIONABLE", r"questionable|game ?time decision|gtd|day to day|uncertain"),
    ("MINUTES LIMIT", r"minutes restriction|minute restriction|minutes limit|on a minutes|limited minutes"),
    ("STARTING", r"will start|to start|starting lineup|starts in place|enter the starting|"
                 r"get the start|gets the start|draw the start|draws the start|start tonight|"
                 r"starting in place|replace .* in the lineup|moves? into the starting"),
    ("PROBABLE", r"probable"),
    ("AVAILABLE", r"available|will play|cleared to|upgraded|returns? tonight|back in the lineup|"
                  r"make his season debut|return to action|set to return"),
]
BUCKET_RE = [(b, re.compile(r"\b(" + p + r")\b")) for b, p in BUCKETS]

COMMON_SURNAMES = {
    "allen", "anderson", "brown", "davis", "green", "harris", "jackson", "johnson", "jones",
    "martin", "miller", "moore", "smith", "taylor", "thomas", "thompson", "walker", "white",
    "williams", "wilson", "young", "wells", "ellis", "watson", "bey", "graves", "lee", "james",
    "murray", "porter", "robinson", "mitchell", "bridges", "holiday", "ball", "hunter", "gordon",
}
NEWS_KINDS = {"espn_news", "espn_injuries", "rss_player_news"}   # player-news feeds: any mention counts
OPPORTUNITY = {"OUT", "DOUBTFUL", "STARTING", "MINUTES LIMIT"}      # what matters about other players
BENCH_MIN = 15   # an OUT for someone averaging fewer minutes than this opens nothing


def classify(text):
    return classify_at(text)[0]


def classify_at(text):
    """-> (bucket, position of the keyword in norm_text(text)) or (None, -1)."""
    t = norm_text(text)
    for bucket, rx in BUCKET_RE:
        m = rx.search(t)
        if m:
            return bucket, m.start()
    return None, -1


def nearest(text, pos, idx, extra=None):
    """The one player named closest to position `pos` in norm_text(text), or None."""
    t = norm_text(text)
    best = None
    for pat, entry in list(idx.items()) + list((extra or {}).items()):
        i = t.find(f" {pat} ")
        while i >= 0:
            d = abs(i - pos)
            if best is None or d < best[0]:
                best = (d, entry)
            i = t.find(f" {pat} ", i + 1)
    return best[1] if best else None


# ---------------------------------------------------------------- fetchers -> items

def _rss_items(xml_text):
    root = ET.fromstring(xml_text)
    atom = "{http://www.w3.org/2005/Atom}"
    out = []
    for it in root.iter("item"):
        when = it.findtext("pubDate")
        if not when:
            continue
        out.append({"sid": it.findtext("guid") or it.findtext("link"),
                    "ts": aware(parsedate_to_datetime(when.strip())),
                    "text": f"{it.findtext('title') or ''}. {it.findtext('description') or ''}",
                    "url": (it.findtext("link") or "").strip()})
    for e in root.iter(atom + "entry"):
        when = e.findtext(atom + "updated") or e.findtext(atom + "published")
        link = e.find(atom + "link")
        out.append({"sid": e.findtext(atom + "id"), "ts": parse_ts(when),
                    "text": e.findtext(atom + "title") or "",
                    "url": link.get("href") if link is not None else ""})
    return out


def fetch_source(src):
    kind = src["type"]
    if kind == "espn_news":
        d = get(src["url"], {"limit": 50})
        return [{"sid": str(h["id"]), "ts": parse_ts(h["published"]),
                 "text": f"{h.get('headline', '')}. {h.get('description', '')}",
                 "url": h.get("links", {}).get("web", {}).get("href", ""),
                 "hint": [c["description"] for c in h.get("categories", [])
                          if c.get("type") == "athlete" and c.get("description")]}
                for h in d.get("headlines", []) if h.get("published")]
    if kind == "espn_injuries":
        d = get(src["url"])
        out = []
        for team in d.get("injuries", []):
            for inj in team.get("injuries", []):
                who = inj.get("athlete", {}).get("displayName", "")
                out.append({"sid": f"{inj['id']}:{inj.get('date')}", "ts": parse_ts(inj["date"]),
                            "text": f"{who} — {inj.get('status', '')}: {inj.get('shortComment', '')}",
                            "url": "https://www.espn.com/nba/injuries", "hint": [who]})
        return out
    if kind in ("rss", "rss_player_news"):
        return _rss_items(get(src["url"], as_text=True))
    if kind == "bluesky":
        d = get("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
                {"actor": src["handle"], "limit": 30, "filter": "posts_no_replies"})
        out = []
        for f in d.get("feed", []):
            post = f["post"]
            if post["author"]["handle"] != src["handle"]:
                continue  # skip reposts
            rkey = post["uri"].rsplit("/", 1)[-1]
            out.append({"sid": post["uri"], "ts": parse_ts(post["record"]["createdAt"]),
                        "text": post["record"].get("text", ""),
                        "url": f"https://bsky.app/profile/{src['handle']}/post/{rkey}"})
        return out
    raise ValueError(f"unknown source type {kind}")


# ---------------------------------------------------------------- matching

def player_index(cfg, rosters):
    """norm text pattern -> (player key, display name, team, kind). Roster/watch get alias patterns."""
    idx = {}
    for kind, rows in (("other", None), ("watch", cfg["watch"]), ("roster", cfg["roster"])):
        if rows is None:
            for team, players in rosters.get("teams", {}).items():
                for p in players:
                    idx[norm_text(p["name"]).strip()] = (norm_name(p["name"]), p["name"], team, "other")
            continue
        for r in rows:
            key, team = norm_name(r["name"]), team_code(r["nba_team"])
            entry = (key, r["name"], team, kind)
            idx[norm_text(r["name"]).strip()] = entry
            last = key.split()[-1]
            if len(last) >= 5 and last not in COMMON_SURNAMES:
                idx.setdefault(last, entry)
            for alias in cfg["aliases"].get(r["name"], []):
                idx[norm_text(alias).strip()] = entry
    return idx


def team_surnames(idx):
    """team -> {surname: entry} for surnames unique on that team (beat reporters say 'Siakam')."""
    by_team = {}
    for entry in set(idx.values()):
        last = entry[0].split()[-1]
        if len(last) >= 4:
            by_team.setdefault(entry[2], {}).setdefault(last, []).append(entry)
    return {t: {k: v[0] for k, v in d.items() if len(v) == 1} for t, d in by_team.items()}


def find_players(text, hint, idx, extra=None):
    t = norm_text(" ".join([text] + (hint or [])))
    found = {}
    for pat, entry in list(idx.items()) + list((extra or {}).items()):
        if f" {pat} " in t:
            found.setdefault(entry[0], entry)
    rank = {"roster": 0, "watch": 1, "other": 2}
    return sorted(found.values(), key=lambda e: rank[e[3]])


# ---------------------------------------------------------------- run

def refresh_rosters(cfg, rosters):
    """ESPN team rosters for the teams I care about, refreshed once a day."""
    today = now_utc().date().isoformat()
    if rosters.get("date") == today:
        return rosters
    teams = sorted(cared_teams(cfg))
    res = pmap(lambda t: get(f"{ESPN}/teams/{t.lower()}/roster"), teams)
    out = {"date": today, "teams": {}}
    for team, r in zip(teams, res):
        if isinstance(r, Exception):
            out["teams"][team] = rosters.get("teams", {}).get(team, [])
            continue
        out["teams"][team] = [{"id": a["id"], "name": a["displayName"]} for a in r.get("athletes", [])]
    return out


def run(cfg, state, speed, health, rosters, profiles, backfill=None):
    """backfill=N (testing): alert on items up to N minutes old even on a first poll."""
    max_age = timedelta(minutes=backfill or cfg["settings"]["news_max_age_minutes"])
    idx = player_index(cfg, rosters)
    surnames = team_surnames(idx)
    teams = cared_teams(cfg)
    tz = cfg["tz"]
    now = now_utc()
    seen = state.setdefault("news_seen", {})
    polled = state.setdefault("news_polled", [])
    srcs = cfg["sources"]
    results = pmap(fetch_source, srcs)
    alerts = []
    for src, items in zip(srcs, results):
        name = src["name"]
        h = health.setdefault(name, {})
        h["checked"] = now.isoformat(timespec="seconds")
        if isinstance(items, Exception):
            h.update(ok=False, error=str(items)[:200])
            print(f"news: {name} FAILED {str(items)[:120]}")
            continue
        h.update(ok=True, error=None, items=len(items), last_ok=h["checked"])
        first_poll = name not in polled and not backfill
        for it in items:
            sid = f"{name}|{it['sid']}"
            if sid in seen:
                continue
            seen[sid] = now.isoformat(timespec="seconds")
            for story in match(it, src, idx, teams, surnames, profiles, tz):
                skey = story["key"]
                rec = speed["stories"].setdefault(skey, {"seen": {}})
                rec["seen"].setdefault(name, it["ts"].astimezone(timezone.utc).isoformat(timespec="seconds"))
                rec["first_src"], rec["first_ts"] = min(rec["seen"].items(), key=lambda kv: parse_ts(kv[1]))
                if first_poll or now - it["ts"] > max_age or f"news|{skey}" in state["sent"]:
                    continue
                alerts.append(to_alert(story, it, name, profiles, tz))
        if first_poll:
            polled.append(name)
    cutoff = now - KEEP_SEEN
    state["news_seen"] = {k: v for k, v in seen.items() if parse_ts(v) >= cutoff}
    scut = (now - KEEP_STORIES).isoformat()
    speed["stories"] = {k: v for k, v in speed["stories"].items() if v.get("first_ts", "9") >= scut}
    return dedupe_run(alerts)


def match(it, src, idx, teams, surnames, profiles, tz):
    """-> list of stories (usually 0 or 1) in this item.

    Player-news feeds (ESPN, RotoWire) are about the player they're tagged with, any news counts.
    Social posts are read sentence by sentence and need a news keyword next to the name.
    Other players on my teams only count for opportunity news (OUT/DOUBTFUL/STARTING/MINUTES
    LIMIT) and only if they play real minutes. A team-only story (no player named) only comes
    from that team's beat reporter."""
    beat = team_code(src["team"]) if src.get("team") else None
    extra = surnames.get(beat) if beat else None
    day = it["ts"].astimezone(tz).date().isoformat()
    pairs = []                                   # (player entry, bucket)
    if src["type"] in NEWS_KINDS:
        bucket = classify(it["text"])
        tagged = find_players(" ".join(it.get("hint") or []), None, idx) if it.get("hint") else []
        if src["type"] == "espn_news":          # a headline is about who it names
            headline = it["text"].split(". ")[0]
            tagged = [p for p in tagged if find_players(headline, None, {k: v for k, v in idx.items()
                                                                        if v == p})] or tagged[:1]
        for p in tagged or find_players(it["text"], None, idx)[:1]:
            pairs.append((p, bucket))
    else:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", it["text"]):
            bucket, pos = classify_at(sentence)
            who = nearest(sentence, pos, idx, extra) if bucket else None
            if who:
                pairs.append((who, bucket))
        if not pairs and beat in teams and classify(it["text"]) and not find_players(it["text"], None, idx, extra):
            bucket = classify(it["text"])
            words = "-".join(norm_text(it["text"]).split()[:6])
            return [{"key": f"{beat}|{bucket}|{day}|{words}", "name": None, "team": beat,
                     "kind": "team", "bucket": bucket, "action": f"{beat} {bucket}"}]
    stories, keys = [], set()
    for (key, name, team, kind), bucket in pairs:
        if kind == "other" and (bucket not in OPPORTUNITY or
                                (bucket == "OUT" and profiles.get(key, {}).get("min", 99) < BENCH_MIN)):
            continue
        skey = f"{key}|{bucket or 'NEWS'}|{day}"
        if skey in keys:
            continue
        keys.add(skey)
        action = f"{bucket or 'NEWS'}: {'FA ' if kind == 'watch' else ''}{name} ({team})"
        if kind == "other" and bucket == "OUT":
            action = f"NEXT MAN UP? {name} ({team}) OUT"
        stories.append({"key": skey, "pkey": key, "name": name, "team": team,
                        "kind": kind, "bucket": bucket, "action": action})
    return stories[:3]


def to_alert(story, it, source, profiles, tz):
    posted = it["ts"].astimezone(tz).strftime("%-I:%M %p")
    snippet = " ".join(it["text"].split())[:160]
    add = story["kind"] in ("other", "watch") and story["bucket"] in ("STARTING", "MINUTES LIMIT", None)
    return {
        "key": f"news|{story['key']}", "rule": "news", "name": story["name"], "team": story["team"],
        "msg": f"{story['action']}: {snippet} — {source}, posted {posted}", "url": it["url"],
        "fa": add, "starts_next": story["bucket"] == "STARTING",
        "helps": cats.helps(profiles.get(story.get("pkey"), {})) if add else [],
        "priority": 4 if story["kind"] == "roster" and story["bucket"] in ("OUT", "DOUBTFUL") else 3,
        "score": {"roster": 6, "watch": 4, "other": 3, "team": 2}[story["kind"]],
    }


def dedupe_run(alerts):
    """Same story from several sources in one run -> keep the earliest-listed (sources are ordered)."""
    out, keys = [], set()
    for a in alerts:
        if a["key"] not in keys:
            keys.add(a["key"])
            out.append(a)
    return out


def leaderboard(speed, days=7):
    """Stories that 2+ sources reported in the last `days`.

    -> [(source, firsts, shared stories, avg minutes ahead of the next source when first,
         avg minutes behind the first source otherwise)], fastest first."""
    since = now_utc() - timedelta(days=days)
    rows = {}
    for rec in speed["stories"].values():
        if len(rec["seen"]) < 2 or parse_ts(rec["first_ts"]) < since:
            continue
        times = sorted((parse_ts(t), src) for src, t in rec["seen"].items())
        first_t, first_src = times[0]
        for t, src in times:
            r = rows.setdefault(src, {"first": 0, "n": 0, "ahead": [], "behind": []})
            r["n"] += 1
            if src == first_src:
                r["first"] += 1
                r["ahead"].append((times[1][0] - first_t).total_seconds() / 60)
            else:
                r["behind"].append((t - first_t).total_seconds() / 60)
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    table = [(s, r["first"], r["n"], mean(r["ahead"]), mean(r["behind"])) for s, r in rows.items()]
    return sorted(table, key=lambda x: (-x[1] / x[2], x[4]))
