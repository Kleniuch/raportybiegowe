#!/usr/bin/env python3
"""Strava -> Telegram: raporty biegowe wysyłane na telefon.

Tryby:
  --mode daily   biegi z ostatnich 24 h (nic nie wysyła, jeśli nie było biegu)
  --mode weekly  podsumowanie ostatnich 7 dni na tle poprzednich 7 dni

Zmienne środowiskowe:
  STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Tylko biblioteka standardowa Pythona, nic nie trzeba instalować.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Brak zmiennej środowiskowej: {name}")
    return value


def http_json(url, data=None, headers=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        sys.exit(f"Błąd HTTP {e.code}: {detail}")


# ---------- Strava ----------

def exchange_code(code):
    """Jednorazowa zamiana kodu z autoryzacji na refresh token."""
    d = http_json(STRAVA_TOKEN_URL, data={
        "client_id": env("STRAVA_CLIENT_ID"),
        "client_secret": env("STRAVA_CLIENT_SECRET"),
        "code": code,
        "grant_type": "authorization_code",
    })
    print("Twój STRAVA_REFRESH_TOKEN:", d["refresh_token"])


def get_access_token():
    d = http_json(STRAVA_TOKEN_URL, data={
        "client_id": env("STRAVA_CLIENT_ID"),
        "client_secret": env("STRAVA_CLIENT_SECRET"),
        "refresh_token": env("STRAVA_REFRESH_TOKEN"),
        "grant_type": "refresh_token",
    })
    return d["access_token"]


def fetch_runs(token, after_ts):
    runs, page = [], 1
    while True:
        qs = urllib.parse.urlencode(
            {"after": int(after_ts), "per_page": 100, "page": page})
        batch = http_json(f"{STRAVA_ACTIVITIES_URL}?{qs}",
                          headers={"Authorization": f"Bearer {token}"})
        if not batch:
            break
        runs += [a for a in batch
                 if a.get("sport_type", a.get("type")) in RUN_TYPES]
        if len(batch) < 100:
            break
        page += 1
    return sorted(runs, key=lambda r: r["start_date"])


def start_ts(run):
    return datetime.fromisoformat(
        run["start_date"].replace("Z", "+00:00")).timestamp()


# ---------- formatowanie ----------

def fmt_time(sec):
    sec = round(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_pace(sec_per_km):
    if not sec_per_km:
        return "–"
    m, s = divmod(round(sec_per_km), 60)
    return f"{m}:{s:02d}/km"


def pace(run):
    km = run["distance"] / 1000
    return run["moving_time"] / km if km > 0 else 0


def bieg_word(n):
    if n == 1:
        return "bieg"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "biegi"
    return "biegów"


def describe_run(r):
    km = r["distance"] / 1000
    lines = [
        f"🏃 {r.get('name', 'Bieg')}",
        f"{km:.2f} km • {fmt_time(r['moving_time'])} • {fmt_pace(pace(r))}",
    ]
    if r.get("average_heartrate"):
        hr = f"Tętno: śr. {r['average_heartrate']:.0f}"
        if r.get("max_heartrate"):
            hr += f", maks. {r['max_heartrate']:.0f}"
        lines.append(hr)
    if r.get("total_elevation_gain"):
        lines.append(f"Przewyższenie: {r['total_elevation_gain']:.0f} m")
    return "\n".join(lines)


def totals(runs):
    km = sum(r["distance"] for r in runs) / 1000
    secs = sum(r["moving_time"] for r in runs)
    hr_runs = [r for r in runs if r.get("average_heartrate")]
    hr_time = sum(r["moving_time"] for r in hr_runs)
    avg_hr = (sum(r["average_heartrate"] * r["moving_time"] for r in hr_runs)
              / hr_time) if hr_time else None
    return {
        "n": len(runs),
        "km": km,
        "secs": secs,
        "pace": secs / km if km > 0 else 0,
        "avg_hr": avg_hr,
        "longest": max(runs, key=lambda r: r["distance"]) if runs else None,
    }


def build_daily_message(runs):
    return "\n\n".join(describe_run(r) for r in runs)


def build_weekly_message(this_week, prev_week):
    t = totals(this_week)
    if not t["n"]:
        return "📊 Ostatnie 7 dni: brak biegów."
    lines = [
        "📊 Ostatnie 7 dni",
        f"{t['n']} {bieg_word(t['n'])} • {t['km']:.1f} km • "
        f"{fmt_time(t['secs'])}",
        f"Średnie tempo: {fmt_pace(t['pace'])}",
    ]
    if t["avg_hr"]:
        lines.append(f"Średnie tętno: {t['avg_hr']:.0f}")
    lines.append(f"Najdłuższy bieg: {t['longest']['distance'] / 1000:.1f} km")
    p = totals(prev_week)
    if p["km"] > 0:
        change = (t["km"] - p["km"]) / p["km"] * 100
        lines.append(f"Poprzednie 7 dni: {p['km']:.1f} km ({change:+.0f}%)")
    else:
        lines.append("Poprzednie 7 dni: brak biegów")
    return "\n".join(lines)


# ---------- Telegram ----------

def send_telegram(text):
    token = env("TELEGRAM_BOT_TOKEN")
    http_json(f"https://api.telegram.org/bot{token}/sendMessage",
              data={"chat_id": env("TELEGRAM_CHAT_ID"), "text": text})


def deliver(text, dry_run):
    if dry_run:
        print(text)
    else:
        send_telegram(text)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    ap.add_argument("--hours", type=int, default=24,
                    help="okno czasu dla trybu daily (domyślnie 24)")
    ap.add_argument("--dry-run", action="store_true",
                    help="wypisz wiadomość zamiast wysyłać na Telegram")
    ap.add_argument("--exchange-code",
                    help="jednorazowo: zamień kod z autoryzacji Stravy na refresh token")
    args = ap.parse_args()

    if args.exchange_code:
        exchange_code(args.exchange_code)
        return

    token = get_access_token()
    now = time.time()

    if args.mode == "daily":
        runs = fetch_runs(token, now - args.hours * 3600)
        if runs:
            deliver(build_daily_message(runs), args.dry_run)
        else:
            print("Brak nowych biegów.")
    else:
        runs = fetch_runs(token, now - 14 * 86400)
        week_ago = now - 7 * 86400
        this_week = [r for r in runs if start_ts(r) >= week_ago]
        prev_week = [r for r in runs if start_ts(r) < week_ago]
        deliver(build_weekly_message(this_week, prev_week), args.dry_run)


if __name__ == "__main__":
    main()
