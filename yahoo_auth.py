#!/usr/bin/env python3
"""One-time Yahoo setup. Run on your own computer: python3 yahoo_auth.py

Asks for your Yahoo app's Client ID and Client Secret, opens Yahoo's consent page,
and prints the refresh token to paste into the GitHub secret YAHOO_REFRESH_TOKEN.
Nothing is saved to disk.
"""
import getpass
import urllib.parse
import webbrowser

import requests

REDIRECT = "https://localhost:8000"

cid = input("Yahoo Client ID: ").strip()
secret = getpass.getpass("Yahoo Client Secret (hidden): ").strip()
url = "https://api.login.yahoo.com/oauth2/request_auth?" + urllib.parse.urlencode(
    {"client_id": cid, "redirect_uri": REDIRECT, "response_type": "code"})
print("\nA browser tab will open. Click Agree. The next page won't load — that's expected.")
print("Copy the WHOLE address from the address bar and paste it here.\n")
webbrowser.open(url)
pasted = input("Address (or just the code): ").strip()
code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [pasted])[0]
r = requests.post("https://api.login.yahoo.com/oauth2/get_token", auth=(cid, secret),
                  data={"grant_type": "authorization_code", "redirect_uri": REDIRECT, "code": code},
                  timeout=20)
if not r.ok:
    raise SystemExit(f"Yahoo said {r.status_code}: {r.text[:300]}")
print("\nYAHOO_REFRESH_TOKEN (paste into GitHub → Settings → Secrets → Actions):\n")
print(r.json()["refresh_token"])
