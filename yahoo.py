"""Optional Yahoo Fantasy API: my roster, this week's close/losing categories, FA/waiver status.

Turned on by config/matchup.json -> "yahoo": {"enabled": true, "league_id": "..."} plus the
GitHub secrets YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, YAHOO_REFRESH_TOKEN (see README).
Any failure falls back to the JSON files; the reason lands in state/sources_health.json.
"""
import os

import requests

from core import team_code

API = "https://fantasysports.yahooapis.com/fantasy/v2"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
# Yahoo display names -> ours
CAT_NAMES = {"3PTM": "3PM", "ST": "STL", "FGM": "FGM", "FG%": "FG%", "FTM": "FTM", "FT%": "FT%",
             "PTS": "PTS", "REB": "REB", "AST": "AST", "BLK": "BLK", "TO": "TO"}


def flat(x):
    """Yahoo's JSON nests lists of one-key dicts and {"0":..,"count":n} maps. Make it normal."""
    if isinstance(x, dict):
        digits = [k for k in x if k.isdigit()]
        if digits and "count" in x:
            return [flat(x[k]) for k in sorted(digits, key=int)]
        return {k: flat(v) for k, v in x.items()}
    if isinstance(x, list):
        items = [flat(i) for i in x if i not in ([], {}, None)]
        if items and all(isinstance(i, dict) for i in items):
            merged = {}
            for i in items:
                if set(i) & set(merged):
                    return items
                merged.update(i)
            return merged
        if len(items) == 1 and isinstance(items[0], list):
            return items[0]
        return items
    return x


class Yahoo:
    def __init__(self, cfg):
        y = cfg["matchup"].get("yahoo", {})
        self.enabled = bool(y.get("enabled")) and all(
            os.environ.get(k) for k in ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "YAHOO_REFRESH_TOKEN"))
        self.league_id = str(y.get("league_id", ""))
        self.error = None
        self._token = None
        self._cache = {}

    def _auth(self):
        r = requests.post(TOKEN_URL, auth=(os.environ["YAHOO_CLIENT_ID"], os.environ["YAHOO_CLIENT_SECRET"]),
                          data={"grant_type": "refresh_token", "redirect_uri": "https://localhost:8000",
                                "refresh_token": os.environ["YAHOO_REFRESH_TOKEN"]}, timeout=20)
        r.raise_for_status()
        self._token = r.json()["access_token"]

    def get(self, path):
        if path in self._cache:
            return self._cache[path]
        if not self._token:
            self._auth()
        r = requests.get(f"{API}/{path}", params={"format": "json"},
                         headers={"Authorization": f"Bearer {self._token}"}, timeout=20)
        r.raise_for_status()
        self._cache[path] = flat(r.json()["fantasy_content"])
        return self._cache[path]

    # ------------------------------------------------------------ league objects

    def league_key(self):
        game = self.get("game/nba")["game"]
        return f"{game['game_key']}.l.{self.league_id}"

    def my_team(self):
        teams = self.get(f"league/{self.league_key()}/teams")["league"]["teams"]
        for t in teams:
            t = t["team"]
            if str(t.get("is_owned_by_current_login")) == "1":
                return t
        raise RuntimeError("no team owned by this Yahoo login in that league")

    def roster(self):
        tk = self.my_team()["team_key"]
        players = self.get(f"team/{tk}/roster/players")["team"]["roster"]["players"]
        out = []
        for p in players:
            p = p["player"]
            out.append({"name": p["name"]["full"], "nba_team": team_code(p.get("editorial_team_abbr", ""))})
        return out

    def matchup(self):
        """-> {"opponent", "close": [...], "losing": [...]} from live category totals."""
        lk = self.league_key()
        stat_names = {}
        for s in self.get(f"league/{lk}/settings")["league"]["settings"]["stat_categories"]["stats"]:
            s = s["stat"]
            stat_names[str(s["stat_id"])] = CAT_NAMES.get(s["display_name"], s["display_name"])
        week = self.get(f"league/{lk}")["league"]["current_week"]
        tk = self.my_team()["team_key"]
        m = self.get(f"team/{tk}/matchups;weeks={week}")["team"]["matchups"][0]["matchup"]
        teams = [t["team"] for t in m["teams"]]
        me = next(t for t in teams if t["team_key"] == tk)
        opp = next(t for t in teams if t["team_key"] != tk)

        def totals(t):
            return {stat_names.get(str(s["stat"]["stat_id"])): s["stat"]["value"]
                    for s in t["team_stats"]["stats"]}
        mine, theirs = totals(me), totals(opp)
        close, losing = [], []
        for cat in CAT_NAMES.values():
            if cat not in mine or cat not in theirs:
                continue
            try:
                a, b = float(mine[cat]), float(theirs[cat])
            except (TypeError, ValueError):
                continue
            if cat.endswith("%"):
                near = abs(a - b) <= 0.015
            else:
                near = abs(a - b) <= max(3, 0.10 * max(a, b))
            ahead = a < b if cat == "TO" else a > b
            if near:
                close.append(cat)
            elif not ahead:
                losing.append(cat)
        return {"opponent": opp["name"], "close": close, "losing": losing, "week": week}

    def availability(self, name):
        """-> 'FA, 12% rostered' / 'waivers, 30% rostered' / 'rostered by <team>' / None."""
        lk = self.league_key()
        q = requests.utils.quote(name)
        res = self.get(f"league/{lk}/players;search={q};out=ownership,percent_owned")
        players = res["league"].get("players") or []
        if not players:
            return None
        p = players[0]["player"]
        own = p.get("ownership", {})
        pct = p.get("percent_owned", {}).get("value")
        pct_txt = f", {pct}% rostered" if pct not in (None, "") else ""
        kind = own.get("ownership_type")
        if kind == "team":
            return f"rostered by {own.get('owner_team_name', 'another team')}"
        if kind == "waivers":
            return f"waivers{pct_txt}"
        return f"FA{pct_txt}"


def connect(cfg, health):
    """-> Yahoo client (enabled or not). Records status in health['yahoo']."""
    y = Yahoo(cfg)
    if not cfg["matchup"].get("yahoo", {}).get("enabled"):
        health["yahoo"] = {"ok": None, "error": "off (matchup.json yahoo.enabled is false)"}
    elif not y.enabled:
        health["yahoo"] = {"ok": False, "error": "enabled in matchup.json but YAHOO_* secrets missing"}
    return y


def safe(y, health, fn, default):
    """Call a Yahoo method; on any error disable Yahoo for this run and return default."""
    if not y.enabled:
        return default
    try:
        out = fn()
        health["yahoo"] = {"ok": True, "error": None}
        return out
    except Exception as e:  # noqa: BLE001
        y.enabled = False
        health["yahoo"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        print(f"yahoo: falling back to config files ({e})")
        return default
