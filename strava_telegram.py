#!/usr/bin/env python3
"""Strava -> Telegram: raporty biegowe i trener AI.

Tryby:
  --mode coach   trener AI: analizuje całą historię i raportuje ostatni bieg
                 (--hours 24: tylko jeśli bieg był w ostatnich 24 h,
                  --hours 0: ostatni bieg bez względu na datę)
  --mode daily   surowe dane biegów z ostatnich 24 h
  --mode weekly  podsumowanie ostatnich 7 dni na tle poprzednich 7 dni

Zmienne środowiskowe:
  STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  ANTHROPIC_API_KEY  (tylko dla trybu coach)

Profil zawodnika czytany jest z pliku profil.md obok skryptu.
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
from datetime import datetime, timedelta

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "profil.md")
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Brak zmiennej środowiskowej: {name}")
    return value


def http_json(url, data=None, headers=None, json_body=None):
    headers = {"User-Agent": "strava-coach/1.0", **(headers or {})}
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
    else:
        body = None
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
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


def strava_headers(token):
    return {"Authorization": f"Bearer {token}"}


def fetch_runs(token, after_ts, max_pages=20):
    runs, page = [], 1
    while page <= max_pages:
        qs = urllib.parse.urlencode(
            {"after": int(after_ts), "per_page": 100, "page": page})
        batch = http_json(f"{STRAVA_API}/athlete/activities?{qs}",
                          headers=strava_headers(token))
        if not batch:
            break
        runs += [a for a in batch
                 if a.get("sport_type", a.get("type")) in RUN_TYPES]
        if len(batch) < 100:
            break
        page += 1
    return sorted(runs, key=lambda r: r["start_date"])


def fetch_activity(token, activity_id):
    return http_json(f"{STRAVA_API}/activities/{activity_id}",
                     headers=strava_headers(token))


def start_ts(run):
    return datetime.fromisoformat(
        run["start_date"].replace("Z", "+00:00")).timestamp()


def local_dt(run):
    return datetime.fromisoformat(
        run.get("start_date_local", run["start_date"])[:19])


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


# ---------- trener AI ----------

COACH_SYSTEM = """Jesteś osobistym trenerem biegowym. Piszesz po polsku do jednej osoby, wiadomość trafi na Telegram.

Zasady:
- Opieraj się wyłącznie na przekazanych danych. Nie zmyślaj liczb. Jeśli czegoś brakuje (np. tętna), powiedz to krótko.
- Styl i cele bierz z profilu zawodnika. Bądź szczery: jeśli bieg był za szybki jak na spokojny, obciążenie rośnie zbyt gwałtownie albo tydzień był za lekki, powiedz to wprost, ale bez pouczania.
- Zawodnik jest początkujący, więc priorytety to regularność, unikanie kontuzji i spokojna progresja. Większość biegów powinna być spokojna (da się swobodnie rozmawiać). Przy zwiększaniu tygodniowego kilometrażu zwykle nie więcej niż około 10% tygodniowo. Pilnuj dni odpoczynku.
- Nie diagnozujesz medycznie. Przy bólu, kontuzji lub niepokojących objawach zalecaj przerwę i konsultację z lekarzem lub fizjoterapeutą.
- Tętno interpretuj ostrożnie: nie znasz dokładnego tętna maksymalnego, więc nie podawaj zbyt precyzyjnych stref.

Format: zwykły tekst, bez markdown (żadnych gwiazdek, # ani list z myślnikami w nadmiarze), krótkie akapity, oszczędnie emoji, łącznie do około 1500 znaków. Układ:
1) Ocena ostatniego biegu w 1-2 zdaniach.
2) Co wyszło dobrze, a co gorzej, z liczbami (tempo, tętno, równomierność kilometrów, porównanie z poprzednimi biegami).
3) Trend z historii (obciążenie tygodniowe, regularność).
4) 2-3 konkretne wskazówki na najbliższy bieg i najbliższy tydzień."""


def load_profile():
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def run_line(r):
    s = (f"{local_dt(r):%Y-%m-%d}: {r['distance'] / 1000:.2f} km, "
         f"{fmt_time(r['moving_time'])}, {fmt_pace(pace(r))}")
    if r.get("average_heartrate"):
        s += f", tętno śr. {r['average_heartrate']:.0f}"
        if r.get("max_heartrate"):
            s += f" maks. {r['max_heartrate']:.0f}"
    if r.get("total_elevation_gain"):
        s += f", +{r['total_elevation_gain']:.0f} m"
    return s


def week_history(runs, max_weeks=52):
    if not runs:
        return []
    by_week = {}
    for r in runs:
        d = local_dt(r).date()
        by_week.setdefault(d - timedelta(days=d.weekday()), []).append(r)
    today = datetime.now().date()
    last = today - timedelta(days=today.weekday())
    lines, mon = [], min(by_week)
    while mon <= last:
        wr = by_week.get(mon, [])
        if wr:
            t = totals(wr)
            line = (f"tydzień od {mon}: {t['n']} {bieg_word(t['n'])}, "
                    f"{t['km']:.1f} km, najdłuższy "
                    f"{t['longest']['distance'] / 1000:.1f} km, "
                    f"śr. tempo {fmt_pace(t['pace'])}")
            if t["avg_hr"]:
                line += f", śr. tętno {t['avg_hr']:.0f}"
        else:
            line = f"tydzień od {mon}: 0 biegów"
        lines.append(line)
        mon += timedelta(days=7)
    if lines:
        lines[-1] += " (bieżący tydzień, jeszcze trwa)"
    return lines[-max_weeks:]


def splits_text(detail):
    out = []
    for i, s in enumerate(detail.get("splits_metric") or [], 1):
        dist = s.get("distance", 0)
        if dist < 100:
            continue
        p = s.get("elapsed_time", 0) / (dist / 1000)
        line = f"km {i}: {fmt_pace(p)}"
        if dist < 950:
            line += f" (odcinek {dist / 1000:.2f} km)"
        if s.get("average_heartrate"):
            line += f", tętno {s['average_heartrate']:.0f}"
        if s.get("elevation_difference") is not None:
            line += f", przewyższenie {s['elevation_difference']:+.0f} m"
        out.append(line)
    return "\n".join(out)


def build_coach_prompt(profile, runs, latest, detail):
    sp = splits_text(detail or {})
    latest_block = (f"{describe_run(latest)}\n"
                    f"Data: {local_dt(latest):%Y-%m-%d %H:%M}")
    if sp:
        latest_block += "\nPodział na kilometry:\n" + sp
    parts = [
        "PROFIL ZAWODNIKA:\n" + (profile.strip() or "(brak profilu)"),
        f"DZIŚ: {datetime.now():%Y-%m-%d}",
        f"LICZBA BIEGÓW W HISTORII: {len(runs)} "
        f"(pierwszy: {local_dt(runs[0]):%Y-%m-%d})",
        "HISTORIA TYGODNIOWA:\n" + "\n".join(week_history(runs)),
        "OSTATNIE BIEGI (od najstarszego):\n"
        + "\n".join(run_line(r) for r in runs[-10:]),
        "BIEG DO RAPORTU:\n" + latest_block,
        "Napisz raport z tego biegu zgodnie z instrukcją.",
    ]
    return "\n\n".join(parts)


def call_claude(system, user):
    d = http_json(ANTHROPIC_URL, json_body={
        "model": os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
        "max_tokens": 1500,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }, headers={"x-api-key": env("ANTHROPIC_API_KEY"),
                "anthropic-version": "2023-06-01"})
    return "".join(b.get("text", "") for b in d["content"]
                   if b.get("type") == "text").strip()


def run_coach(token, hours, dry_run):
    runs = fetch_runs(token, 0)
    if not runs:
        print("Brak biegów na Stravie.")
        return
    latest = runs[-1]
    if hours and start_ts(latest) < time.time() - hours * 3600:
        print("Brak nowych biegów.")
        return
    detail = fetch_activity(token, latest["id"])
    prompt = build_coach_prompt(load_profile(), runs, latest, detail)
    report = call_claude(COACH_SYSTEM, prompt)
    deliver(report[:4000], dry_run)


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
    ap.add_argument("--mode", choices=["coach", "daily", "weekly"],
                    default="coach")
    ap.add_argument("--hours", type=int, default=24,
                    help="okno czasu w godzinach (coach: 0 = ostatni bieg "
                         "bez względu na datę)")
    ap.add_argument("--dry-run", action="store_true",
                    help="wypisz wiadomość zamiast wysyłać na Telegram")
    ap.add_argument("--exchange-code",
                    help="jednorazowo: zamień kod z autoryzacji Stravy "
                         "na refresh token")
    args = ap.parse_args()

    if args.exchange_code:
        exchange_code(args.exchange_code)
        return

    token = get_access_token()
    now = time.time()

    if args.mode == "coach":
        run_coach(token, args.hours, args.dry_run)
    elif args.mode == "daily":
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
