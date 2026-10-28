"""Shared plumbing: config, state files, HTTP, names, teams, ntfy, alert log."""
import json
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config"
STATE = ROOT / "state"
ALERTS_DIR = ROOT / "alerts"

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_WEB = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
UA = "coachk-live-watch/2 (+https://github.com/kday456/coachk-live-watch)"

# Standard abbreviations -> ESPN's. Anything not listed is already the same.
TEAM_ALIASES = {
    "NYK": "NY", "NOP": "NO", "NOH": "NO", "UTA": "UTAH", "GSW": "GS",
    "SAS": "SA", "WAS": "WSH", "PHO": "PHX", "BRK": "BKN", "CHO": "CHA",
}

TEAM_NICKNAMES = {
    "ATL": "Hawks", "BOS": "Celtics", "BKN": "Nets", "CHA": "Hornets", "CHI": "Bulls",
    "CLE": "Cavaliers|Cavs", "DAL": "Mavericks|Mavs", "DEN": "Nuggets", "DET": "Pistons",
    "GS": "Warriors", "HOU": "Rockets", "IND": "Pacers", "LAC": "Clippers", "LAL": "Lakers",
    "MEM": "Grizzlies", "MIA": "Heat", "MIL": "Bucks", "MIN": "Timberwolves|Wolves",
    "NO": "Pelicans", "NY": "Knicks", "OKC": "Thunder", "ORL": "Magic", "PHI": "76ers|Sixers",
    "PHX": "Suns", "POR": "Trail Blazers|Blazers", "SAC": "Kings", "SA": "Spurs",
    "TOR": "Raptors", "UTAH": "Jazz", "WSH": "Wizards",
}

DEFAULT_SETTINGS = {
    "team_name": "My Team",
    "ntfy_title": "Fantasy live",
    "timezone": "America/Los_Angeles",
    "vulture_after_local_hour": 17,
    "news_max_age_minutes": 90,
    "thresholds": {
        "fouls": 4, "stall_runs": 2, "nmu_min": 20, "nmu_pts": 15,
        "big_pts": 15, "big_3s": 3, "big_stl": 2, "big_blk": 3,
        "breakout_min_delta": 4, "selling_min_delta": 4,
    },
}


# ---------------------------------------------------------------- names & teams

def team_code(abbr):
    abbr = (abbr or "").strip().upper()
    return TEAM_ALIASES.get(abbr, abbr)


def norm_name(name):
    """'Kentavious Caldwell-Pope' / 'Jaren Jackson Jr.' -> comparable key."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[.\-'’]", "", s.lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s)
    return " ".join(s.split())


def norm_text(text):
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[.\-'’]", "", s.lower())
    return " " + " ".join(re.sub(r"[^a-z0-9#]+", " ", s).split()) + " "


def to_int(v):
    try:
        return int(float(str(v).split("-")[0]))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------- time

def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(s):
    """ISO-8601 (with Z) -> aware datetime (naive input is taken as UTC)."""
    return aware(datetime.fromisoformat(s.replace("Z", "+00:00")))


def aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- files

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(exist_ok=True)
    text = json.dumps(data, indent=1, sort_keys=True) + "\n"
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def load_config():
    settings = load_json(CONFIG / "settings.json", {})
    merged = {**DEFAULT_SETTINGS, **settings}
    merged["thresholds"] = {**DEFAULT_SETTINGS["thresholds"], **settings.get("thresholds", {})}
    roster = load_json(CONFIG / "roster.json", None)
    if not roster:
        sys.exit(f"Can't read {CONFIG / 'roster.json'}")
    matchup = load_json(CONFIG / "matchup.json", {})
    sources = load_json(CONFIG / "sources.json", {"sources": []})
    return {
        "settings": merged,
        "tz": ZoneInfo(merged["timezone"]),
        "roster": roster.get("roster", []),
        "watch": roster.get("watch", []),
        "ignore": roster.get("ignore", []),
        "aliases": roster.get("aliases", {}),
        "matchup": matchup,
        "sources": [s for s in sources.get("sources", []) if s.get("enabled", True)],
    }


def cared_teams(cfg):
    return {team_code(r["nba_team"]) for r in cfg["roster"] + cfg["watch"]}


# ---------------------------------------------------------------- HTTP

_session = requests.Session()
_session.headers["User-Agent"] = UA


def get(url, params=None, timeout=20, as_text=False):
    r = _session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.text if as_text else r.json()


def pmap(fn, items, workers=8):
    """Run fn over items in threads; exceptions come back as values."""
    def safe(x):
        try:
            return fn(x)
        except Exception as e:  # noqa: BLE001 — reported by the caller
            return e
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(safe, items))


# ---------------------------------------------------------------- output

def send(cfg, alerts, dry_run=False):
    """Push alerts (highest score first) and append them to today's log.

    Returns the keys that were delivered."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic and not dry_run and alerts:
        print("NTFY_TOPIC not set — alerts will be logged but not pushed.", file=sys.stderr)
    delivered = []
    for a in sorted(alerts, key=lambda a: -a.get("score", 0)):
        print(f"  ALERT [p{a.get('priority', 3)}] {a['msg']}")
        if dry_run:
            continue
        if topic:
            headers = {"Title": cfg["settings"]["ntfy_title"], "Tags": "basketball",
                       "Priority": str(a.get("priority", 3))}
            if a.get("url"):
                headers["Click"] = a["url"]
            try:
                r = requests.post(f"https://ntfy.sh/{topic}", data=a["msg"].encode("utf-8"),
                                  headers=headers, timeout=15)
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"  push failed ({e}); will retry next run", file=sys.stderr)
                continue
        log_line(cfg, a["msg"])
        delivered.append(a["key"])
    return delivered


def log_line(cfg, msg, name=None):
    ALERTS_DIR.mkdir(exist_ok=True)
    local = now_utc().astimezone(cfg["tz"])
    path = ALERTS_DIR / (name or f"{local:%Y-%m-%d}.md")
    if not path.exists():
        path.write_text(f"# {cfg['settings']['team_name']} alerts — {local:%Y-%m-%d}\n\n")
    with path.open("a") as f:
        f.write(f"- {local:%H:%M %Z} — {msg}\n")
