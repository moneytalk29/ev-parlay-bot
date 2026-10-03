"""Daily +EV scanner: Pinnacle true lines vs FanDuel, DraftKings, BetMGM, Caesars, BetRivers.

Uses The Odds API. Finds the best +EV single bets (parlay is optional, POST_PARLAY=true) (2-5 legs,
mixed sports allowed, one leg per game) and posts it to your Discord webhook at 9am ET.

IMPORTANT: a parlay is placed at ONE sportsbook, so all legs of the posted parlay are
priced at the same book. The scanner builds one candidate parlay per book and posts the best.
"""
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ev_core as core

API = "https://api.the-odds-api.com/v4"
API_KEY = os.getenv("ODDS_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SPORTS = [s.strip() for s in os.getenv(
    "SPORTS", "americanfootball_nfl,basketball_nba,baseball_mlb,mma_mixed_martial_arts"
).split(",") if s.strip()]
MARKETS = os.getenv("MARKETS", "h2h")          # add spreads,totals for more legs (costs more API credits)
BOOKMAKERS = "pinnacle,fanduel,draftkings,betmgm,williamhill_us,betrivers"
BOOK_NAMES = {"fanduel": "FanDuel", "draftkings": "DraftKings", "betmgm": "BetMGM",
              "williamhill_us": "Caesars", "betrivers": "BetRivers"}
MIN_EV = float(os.getenv("MIN_EV", "2"))                 # leg must beat this EV%
MAX_LEG_EV = float(os.getenv("MAX_LEG_EV", "15"))        # above this is usually a stale/bad line
MIN_TRUE_PROB = float(os.getenv("MIN_TRUE_PROB", "0.25"))  # skip longshots (de-vig is least reliable there)
MAX_LEGS = max(2, min(5, int(os.getenv("MAX_LEGS", "5"))))
POST_PARLAY = os.getenv("POST_PARLAY", "true").lower() == "true"    # posts singles AND a parlay; set false for singles only
MAX_SINGLES = int(os.getenv("MAX_SINGLES", "10"))
HOURS_AHEAD = float(os.getenv("HOURS_AHEAD", "12"))     # only games starting soon, so lines are fresh
POST_HOURS = sorted({int(h) for h in os.getenv("POST_HOURS", os.getenv("POST_HOUR", "11,16")).split(",") if h.strip()})  # ET, e.g. "11,16"
RECHECK_MINUTES = int(os.getenv("RECHECK_MINUTES", "60"))    # re-check each posted bet this long before tip-off
RECHECK_BATCH = 20                                            # games within this many extra minutes share one re-check
MAX_RECHECKS = int(os.getenv("MAX_RECHECKS_PER_DAY", "6"))   # credit guard (1 credit per sport per re-check)
TZ = ZoneInfo("America/New_York")
MARKET_LABEL = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}


# ---------------- fetching ----------------
def fetch_sport(sport):
    r = requests.get(f"{API}/sports/{sport}/odds", params={
        "apiKey": API_KEY, "bookmakers": BOOKMAKERS, "markets": MARKETS,
        "oddsFormat": "decimal", "dateFormat": "iso"}, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"{sport}: HTTP {r.status_code} {r.text[:100]}")
    return r.json(), r.headers.get("x-requests-remaining")


def selection_text(market_key, outcome):
    pt = outcome.get("point")
    if pt is None:
        return outcome["name"]
    return f"{outcome['name']} {pt:+g}" if market_key == "spreads" else f"{outcome['name']} {pt:g}"


def scan_event(ev, sport):
    """All (book, outcome) prices for one game, compared to Pinnacle's no-vig line."""
    books = {b["key"]: b for b in ev.get("bookmakers", [])}
    pin = books.get("pinnacle")
    if not pin:
        return []
    legs = []
    title = f"{ev.get('away_team', '?')} @ {ev.get('home_team', '?')}"
    for pm in pin.get("markets", []):
        outs = pm.get("outcomes", [])
        if len(outs) < 2 or any(o["price"] <= 1 for o in outs):
            continue
        true = core.devig([o["price"] for o in outs])
        truemap = {(o["name"], o.get("point")): t for o, t in zip(outs, true)}
        for key, b in books.items():
            if key == "pinnacle":
                continue
            bm = next((m for m in b.get("markets", []) if m["key"] == pm["key"]), None)
            if not bm:
                continue
            for o in bm["outcomes"]:
                k = (o["name"], o.get("point"))
                if k not in truemap or o["price"] <= 1:
                    continue
                p, d = truemap[k], o["price"]
                legs.append({
                    "event_id": ev["id"], "sport": sport, "event": title,
                    "commence": ev["commence_time"], "market_key": pm["key"],
                    "market": MARKET_LABEL.get(pm["key"], pm["key"]),
                    "label": selection_text(pm["key"], o),
                    "book_key": key, "book": BOOK_NAMES.get(key, key),
                    "decimal": d, "american": core.decimal_to_american(d),
                    "true_prob": p, "fair_american": core.decimal_to_american(1 / p),
                    "ev": core.ev_pct(p, d)})
    return legs


def collect_legs():
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=HOURS_AHEAD)
    legs, errors, remaining = [], [], None
    for sport in SPORTS:
        try:
            events, remaining = fetch_sport(sport)
        except Exception as e:
            errors.append(str(e))
            continue
        for ev in events:
            start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            if not (now < start < horizon):
                continue
            legs += scan_event(ev, sport)
    return legs, errors, remaining


# ---------------- picking ----------------
def qualifies(l):
    return MIN_EV <= l["ev"] <= MAX_LEG_EV and l["true_prob"] >= MIN_TRUE_PROB


def build_parlay(cands, max_legs=MAX_LEGS):
    """Best parlay: for each book keep the best leg per game, take the top legs, keep the highest EV."""
    best = None
    for book in {l["book_key"] for l in cands}:
        per_game = {}
        for l in (x for x in cands if x["book_key"] == book):
            g = per_game.get(l["event_id"])
            if g is None or l["ev"] > g["ev"]:
                per_game[l["event_id"]] = l
        ranked = sorted(per_game.values(), key=lambda x: -x["ev"])
        for n in range(2, min(max_legs, len(ranked)) + 1):
            chosen = ranked[:n]
            res = core.parlay([(c["american"], c["true_prob"]) for c in chosen])
            if best is None or res["ev"] > best["ev"]:
                best = {**res, "book": chosen[0]["book"], "picks": chosen}
    return best


def best_singles(cands, n=5):
    """Best price per selection across books (the BEST BOOK for each leg)."""
    seen, out = set(), []
    for l in sorted(cands, key=lambda x: -x["ev"]):
        k = (l["event_id"], l["market_key"], l["label"])
        if k in seen:
            continue
        seen.add(k)
        out.append(l)
        if len(out) == n:
            break
    return out


# ---------------- Discord ----------------
def when(commence):
    dt = datetime.fromisoformat(commence.replace("Z", "+00:00")).astimezone(TZ)
    return dt.strftime("%a %-I:%M %p ET")


def build_embeds(parlay_res, singles, remaining):
    embeds = []
    if singles:
        lines = []
        for l in singles:
            lines.append(
                f"**{l['label']}** ({l['market']}) — {l['event']}\n"
                f"{l['book']} **{core.fmt_american(l['american'])}** vs true "
                f"{core.fmt_american(l['fair_american'])} • EV **{l['ev']:+.1f}%** → PLAY "
                f"• {when(l['commence'])}")
        embeds.append({"title": f"🎯 +EV singles — {len(singles)} bet{'s' if len(singles) != 1 else ''} today",
                       "description": "\n\n".join(lines), "color": 3066993})
    if parlay_res:
        fields = []
        for i, l in enumerate(parlay_res["picks"], start=1):
            fields.append({
                "name": f"{i}. {l['label']} ({l['market']})",
                "value": (f"{l['event']} • {when(l['commence'])}\n"
                          f"**{l['book']} {core.fmt_american(l['american'])}** vs true "
                          f"{core.fmt_american(l['fair_american'])} ({l['true_prob']*100:.1f}%) • "
                          f"leg EV **{l['ev']:+.1f}%**"),
                "inline": False})
        embeds.append({
            "title": f"Parlay option — {len(parlay_res['picks'])} legs at {parlay_res['book']}",
            "description": (f"**Total EV {parlay_res['ev']:+.1f}%** → {parlay_res['verdict']}\n"
                            f"Parlay odds **{core.fmt_american(parlay_res['american'])}** • "
                            f"win chance {parlay_res['win_prob']*100:.1f}%"),
            "color": 3066993 if parlay_res["ev"] > core.EV_THRESHOLD else 15158332,
            "fields": fields})
    if embeds:
        foot = (f"Re-checked ~{RECHECK_MINUTES} min before tip • check injury news before betting • "
                "EV is an estimate, not a guarantee")
        if remaining is not None:
            foot += f" • API credits left: {remaining}"
        embeds[-1]["footer"] = {"text": foot}
    return embeds


def post(payload):
    if not WEBHOOK_URL:
        print("WEBHOOK_URL not set; would post:", payload)
        return
    r = requests.post(WEBHOOK_URL, json={"username": "EV Scanner", **payload}, timeout=15)
    print("Discord status", r.status_code)


_posted = {"day": None, "seen": {}, "parlay": None}


def _key(l):
    return (l["event_id"], l["market_key"], l["label"], l["book_key"])


def _reset_if_new_day():
    today = datetime.now(TZ).date()
    if _posted["day"] != today:
        _posted.update(day=today, seen={}, parlay=None)


def new_only(singles):
    """Skip bets already posted today unless the edge grew by 1+ points."""
    return [l for l in singles
            if _key(l) not in _posted["seen"] or l["ev"] >= _posted["seen"][_key(l)] + 1.0]


def run_daily():
    if not API_KEY:
        post({"content": "❌ EV Scanner: ODDS_API_KEY is not set."})
        return
    _reset_if_new_day()
    legs, errors, remaining = collect_legs()
    cands = [l for l in legs if qualifies(l)]
    print(f"{len(legs)} priced legs, {len(cands)} qualify, errors: {errors}")
    singles = new_only(best_singles(cands, 999))[:MAX_SINGLES]
    parlay_res = build_parlay(cands) if POST_PARLAY else None
    if parlay_res:
        sig = tuple(sorted((p["label"], p["book_key"]) for p in parlay_res["picks"]))
        if sig == _posted["parlay"]:
            parlay_res = None                      # same parlay as the earlier post
        else:
            _posted["parlay"] = sig
    embeds = build_embeds(parlay_res, singles, remaining)
    if embeds:
        post({"embeds": embeds})
        for l in singles:
            _posted["seen"][_key(l)] = l["ev"]
        watch(singles + (parlay_res["picks"] if parlay_res else []))
    elif _posted["seen"]:
        post({"content": "No new +EV bets since the last post."})
    else:
        msg = (f"No +EV singles today ({len(legs)} lines checked, need EV between "
               f"{MIN_EV:g}% and {MAX_LEG_EV:g}%).")
        if errors:
            msg += "\nProblems: " + "; ".join(errors)[:600]
        post({"content": msg})


def safe_run():
    try:
        run_daily()
    except Exception as e:
        print("daily run failed:", e)
        try:
            post({"content": f"❌ EV Scanner failed: {e}"})
        except Exception:
            pass


# ---------------- pre-game re-check ----------------
_watch = []
_rc = {"day": None, "n": 0}


def _parse_dt(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def watch(legs):
    """Remember posted bets so they can be re-checked shortly before tip-off."""
    have = {_key(w["leg"]) for w in _watch}
    for l in legs:
        if _key(l) in have:
            continue
        have.add(_key(l))
        start = _parse_dt(l["commence"])
        _watch.append({"leg": l, "start": start,
                       "check_at": start - timedelta(minutes=RECHECK_MINUTES), "done": False})


def status_line(w, now_leg):
    l = w["leg"]
    head = f"**{l['label']}** ({l['event']}) — {l['book']}"
    if now_leg is None:
        return (f"{head}\n⚠️ No price found now. The line was pulled or the game started. "
                f"That can mean news, so check before betting.")
    posted = f"{core.fmt_american(l['american'])} (true {core.fmt_american(l['fair_american'])}, EV {l['ev']:+.1f}%)"
    now = f"{core.fmt_american(now_leg['american'])} (true {core.fmt_american(now_leg['fair_american'])}, EV {now_leg['ev']:+.1f}%)"
    if now_leg["ev"] >= MIN_EV:
        icon, verdict = "✅", "still +EV"
    elif now_leg["ev"] > 0:
        icon, verdict = "🟡", "edge shrank, below your minimum"
    else:
        icon, verdict = "🔴", "no longer +EV, skip"
    return f"{head}\n{icon} {verdict}\nPosted: {posted}\nNow: {now}"


def recheck_due():
    now = datetime.now(timezone.utc)
    _watch[:] = [w for w in _watch if now - w["start"] < timedelta(hours=6)]   # tidy old items
    pending = [w for w in _watch if not w["done"] and w["start"] > now]
    if not any(w["check_at"] <= now for w in pending):
        return
    today = datetime.now(TZ).date()
    if _rc["day"] != today:
        _rc.update(day=today, n=0)
    batch = [w for w in pending if w["check_at"] <= now + timedelta(minutes=RECHECK_BATCH)]
    sports = sorted({w["leg"]["sport"] for w in batch})
    for w in batch:
        w["done"] = True
    if _rc["n"] + len(sports) > MAX_RECHECKS:
        post({"content": "Pre-game re-check skipped (daily credit cap reached). Check these bets' prices manually."})
        return
    current = {}
    for sp in sports:
        events, _ = fetch_sport(sp)
        _rc["n"] += 1
        for ev in events:
            for l in scan_event(ev, sp):
                current[_key(l)] = l
    lines = [status_line(w, current.get(_key(w["leg"]))) for w in batch]
    post({"embeds": [{"title": "🔄 Pre-game re-check", "description": "\n\n".join(lines),
                      "color": 3447003,
                      "footer": {"text": "Lines can still move. Confirm the price in the sportsbook app before betting."}}]})


def safe_recheck():
    try:
        recheck_due()
    except Exception as e:
        print("re-check failed:", e)


# ---------------- scheduling ----------------
def seconds_until(hour, minute=0):
    now = datetime.now(TZ)
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return (t - now).total_seconds()


def scheduler_loop():
    now = datetime.now(TZ)
    fired = {(now.date(), h) for h in POST_HOURS
             if now >= now.replace(hour=h, minute=0, second=0, microsecond=0)}   # already past today
    if os.getenv("RUN_NOW", "false").lower() == "true":
        safe_run()                              # test post right at startup
    wait = min(seconds_until(h) for h in POST_HOURS)
    print(f"Next post in {wait/3600:.1f} hours (daily at {', '.join(f'{h}:00' for h in POST_HOURS)} ET)")
    while True:
        time.sleep(30)
        now = datetime.now(TZ)
        for h in POST_HOURS:
            target = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if now >= target and (now.date(), h) not in fired:
                fired.add((now.date(), h))
                if (now - target).total_seconds() < 1800:
                    safe_run()
        fired = {k for k in fired if k[0] == now.date()}
        safe_recheck()


if __name__ == "__main__":
    safe_run()
