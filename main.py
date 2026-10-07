"""Heartbeat: live NFL + NBA game-script scanner for PrizePicks lines. ALERT ONLY, it never places bets.

What it does
  1. Reads the PrizePicks board (same public feed Captain Hook uses) for NFL and NBA player lines.
  2. Remembers each line from BEFORE kickoff (the "pregame line"), which is the market's expectation for the player.
  3. While a game is live, reads the score, clock and player box score from ESPN's free public data.
  4. Projects the player's final total using the game script:
       NFL: a team that trails throws more and runs less; a team that leads runs more and throws less.
       NBA: in a blowout starters sit (fewer minutes); in a tight finish starters play more.
  5. Posts to your Discord webhook when the projection is far enough from PrizePicks' current line.

Honest limits (read these)
  - The script adjustments below are STARTING GUESSES, not proven numbers. Every alert is logged so they can be
    checked and tuned against real results. Treat the first weeks as testing.
  - ESPN's public data is unofficial and can change or lag a few seconds.
  - It needs the pregame line saved BEFORE kickoff. If the service restarts during a game, lines for that
    game have no saved prior and are skipped (set HB_ALLOW_NO_PRIOR=true to override, at lower trust).
  - It does not know about injuries, a backup QB coming in, or weather.
Everything is a Railway variable. Only WEBHOOK_URL is required.
"""
import os
import re
import time
import unicodedata
from datetime import datetime, timezone

import requests

# ====================== SETTINGS (Railway variables) ======================
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SPORTS = [s.strip().upper() for s in os.getenv("HB_SPORTS", "NFL,NBA").split(",") if s.strip()]
PP_URL = os.getenv("PP_URL", "https://partner-api.prizepicks.com/projections?per_page=1000&single_stat=true&game_mode=pickem")
PP_POLL = float(os.getenv("HB_PP_POLL_SECONDS", "30"))        # how often to re-read PrizePicks
ESPN_POLL = float(os.getenv("HB_ESPN_POLL_SECONDS", "30"))    # how often to re-read live scores/box scores
EDGE_PCT = float(os.getenv("HB_EDGE_PCT", "0.10"))            # gap needed, as a share of the line
MIN_ELAPSED = float(os.getenv("HB_MIN_ELAPSED", "0.15"))      # ignore the first 15% of the game
MIN_REMAINING = float(os.getenv("HB_MIN_REMAINING", "0.08"))  # ignore the last 8% (too late to matter)
SCRIPT_ONLY = os.getenv("HB_SCRIPT_ONLY", "true").lower() == "true"   # only alert when the game script is changing the picture
SCRIPT_MIN = float(os.getenv("HB_SCRIPT_MIN", "0.04"))        # smallest script effect (4%) that counts
ALLOW_NO_PRIOR = os.getenv("HB_ALLOW_NO_PRIOR", "false").lower() == "true"
PACE_WEIGHT_MAX = float(os.getenv("HB_PACE_WEIGHT_MAX", "0.35"))   # how much the player's pace so far can move the baseline
COOLDOWN = float(os.getenv("HB_COOLDOWN_MINUTES", "8")) * 60
MAX_PER_HOUR = int(os.getenv("HB_MAX_ALERTS_PER_HOUR", "20"))
DISCOVERY_POST = os.getenv("HB_DISCOVERY_POST", "true").lower() == "true"   # post one startup summary of what it can see
MAX_SUMMARIES = int(os.getenv("HB_MAX_SUMMARIES", "20"))      # box scores read per cycle
# NFL game-script strength: change in volume per 7 points of margin (a "score"), and the cap
PASS_TRAIL, PASS_TRAIL_CAP = float(os.getenv("HB_PASS_TRAIL", "0.07")), float(os.getenv("HB_PASS_TRAIL_CAP", "0.30"))
PASS_LEAD, PASS_LEAD_CAP = float(os.getenv("HB_PASS_LEAD", "0.05")), float(os.getenv("HB_PASS_LEAD_CAP", "0.20"))
RUSH_LEAD, RUSH_LEAD_CAP = float(os.getenv("HB_RUSH_LEAD", "0.08")), float(os.getenv("HB_RUSH_LEAD_CAP", "0.30"))
RUSH_TRAIL, RUSH_TRAIL_CAP = float(os.getenv("HB_RUSH_TRAIL", "0.09")), float(os.getenv("HB_RUSH_TRAIL_CAP", "0.35"))
# NBA
NBA_EXPECTED_MIN = float(os.getenv("HB_NBA_EXPECTED_MIN", "32"))   # a typical starter's minutes
NBA_BLOWOUT = float(os.getenv("HB_NBA_BLOWOUT", "20"))             # point gap where starters start to sit (3rd/4th quarter)
# What a LIVE PrizePicks line means: "full" = the player's full-game total (default), "rest" = just the rest of the game.
# The first live game will show which one it is (see the "LIVE ROW" log lines). Change this if it turns out to be "rest".
LIVE_LINE_MODE = os.getenv("HB_LIVE_LINE_MODE", "full").lower()

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
           "Origin": "https://app.prizepicks.com", "Referer": "https://app.prizepicks.com/"}
ESPN = {"NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl",
        "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"}
GAME_SECONDS = {"NFL": 3600.0, "NBA": 2880.0}
PERIOD_SECONDS = {"NFL": 900.0, "NBA": 720.0}

# PrizePicks stat name (lowercase) -> (stat key, group)
STAT_MAP = {
    "NFL": {"pass yards": ("pass_yds", "pass"), "passing yards": ("pass_yds", "pass"),
            "pass attempts": ("pass_att", "pass"), "pass completions": ("pass_cmp", "pass"),
            "rush yards": ("rush_yds", "rush"), "rushing yards": ("rush_yds", "rush"),
            "rush attempts": ("rush_att", "rush"), "rushing attempts": ("rush_att", "rush"), "carries": ("rush_att", "rush")},
    "NBA": {"points": ("pts", "nba"), "rebounds": ("reb", "nba"), "assists": ("ast", "nba"),
            "3-pt made": ("fg3", "nba"), "3-pointers made": ("fg3", "nba"), "three pointers made": ("fg3", "nba"),
            "3pt made": ("fg3", "nba")},
}
STAT_LABEL = {"pass_yds": "Pass Yards", "pass_att": "Pass Attempts", "pass_cmp": "Pass Completions",
              "rush_yds": "Rush Yards", "rush_att": "Rush Attempts",
              "pts": "Points", "reb": "Rebounds", "ast": "Assists", "fg3": "3-PT Made"}
MIN_ABS_EDGE = {"pass_yds": 12, "pass_att": 3, "pass_cmp": 2.5, "rush_yds": 8, "rush_att": 2.5,
                "pts": 3.5, "reb": 1.5, "ast": 1.5, "fg3": 1.0}
YARD_DAMP = {"pass_yds": 0.8, "rush_yds": 0.8, "pass_cmp": 0.9}     # efficiency changes with the script, so shrink these
BAD_WORDS = ("1h", "2h", "1q", "2q", "3q", "4q", "1st", "2nd", "half", "quarter", "combo", "+", "(", "longest",
             "fantasy", "first", "last", "total")

_session = requests.Session()
_session.headers.update(HEADERS)
_state = {"prior": {}, "alerts": {}, "sent": [], "summ": {}, "board": {}, "keys_seen": set(), "flags_seen": {},
          "live_logged": set()}


# ====================== SMALL HELPERS ======================
def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z ]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _now():
    return datetime.now(timezone.utc)


def _to_dt(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _num(x, default=0.0):
    try:
        return float(str(x).replace("+", "").strip())
    except (TypeError, ValueError):
        return default


def _split_pair(x):
    """'20/35' or '3-15' -> (20, 35)"""
    m = re.match(r"^\s*(\d+)\s*[/\-]\s*(\d+)\s*$", str(x or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _clock_seconds(txt):
    txt = str(txt or "0:00").strip()
    try:
        if ":" in txt:
            m, s = txt.split(":")[:2]
            return int(m) * 60 + float(s)
        return float(txt)
    except ValueError:
        return 0.0


def _post(payload):
    if not WEBHOOK_URL:
        print("no WEBHOOK_URL set, would have posted:", str(payload)[:300])
        return
    for _ in range(4):
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        except requests.RequestException as e:
            print("discord post failed:", e)
            time.sleep(2)
            continue
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2.0
            time.sleep(min(wait, 10) + 0.3)
            continue
        if r.status_code >= 300:
            print("discord error", r.status_code, r.text[:120])
        return


# ====================== PRIZEPICKS ======================
def fetch_board():
    r = _session.get(PP_URL, timeout=20)
    r.raise_for_status()
    return r.json()


def parse_board(data):
    """Returns {(sport, norm_name, stat): line dict} for the NFL/NBA lines we can model."""
    players, leagues, games = {}, {}, {}
    for inc in data.get("included", []):
        a = inc.get("attributes", {})
        if inc.get("type") == "new_player":
            players[inc["id"]] = (a.get("name", ""), a.get("team") or a.get("team_name") or "", a.get("position") or "")
        elif inc.get("type") == "league":
            leagues[inc["id"]] = (a.get("name") or "").upper()
        elif inc.get("type") == "game":
            games[inc["id"]] = a
    out, seen_keys = {}, set()
    for item in data.get("data", []):
        try:
            a = item["attributes"]
            rel = item.get("relationships", {})
            sport = leagues.get((rel.get("league", {}).get("data") or {}).get("id"), "")
            if sport not in SPORTS:
                continue
            seen_keys.update(a.keys())
            stat_name = str(a.get("stat_display_name") or a.get("stat_type") or "").lower().strip()
            if any(b in stat_name for b in BAD_WORDS):
                continue
            stat = STAT_MAP.get(sport, {}).get(stat_name)
            if not stat:
                continue
            odds_type = str(a.get("odds_type") or "standard").lower()
            if odds_type != "standard":              # goblin/demon lines pay differently, so skip them
                continue
            name, team, pos = players.get((rel.get("new_player", {}).get("data") or {}).get("id"), ("", "", ""))
            if not name:
                continue
            gid = (rel.get("game", {}).get("data") or {}).get("id")
            start = a.get("start_time") or (games.get(gid) or {}).get("start_time") or ""
            flags = {k: a.get(k) for k in ("status", "is_live", "in_game", "is_promo", "odds_type", "board_time") if k in a}
            for k, v in flags.items():
                _state["flags_seen"].setdefault(k, set()).add(str(v))
            live_row = (str(a.get("is_live")).lower() == "true" or str(a.get("in_game")).lower() == "true"
                        or str(a.get("status") or "").lower() in ("in_game", "live", "in_progress"))
            key = (sport, _norm(name), stat[0])
            if key in out and out[key]["live"] and not live_row:
                continue                             # a live row for this player/stat beats a stale pregame row
            out[key] = {
                "sport": sport, "name": name, "norm": _norm(name), "team": team, "pos": pos,
                "stat": stat[0], "group": stat[1], "line": _num(a.get("line_score")),
                "start": _to_dt(start), "opp": a.get("description") or "", "flags": flags, "live": live_row}
        except Exception:
            continue
    _state["keys_seen"] |= seen_keys
    return out


# ====================== ESPN ======================
def _get_json(url):
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def live_events(sport):
    """List of live games: id, period, clock seconds, elapsed fraction, team scores by ESPN team id."""
    out = []
    data = _get_json(ESPN[sport] + "/scoreboard")
    for ev in data.get("events", []) or []:
        st = ev.get("status", {})
        if (st.get("type") or {}).get("state") != "in":
            continue
        comp = (ev.get("competitions") or [{}])[0]
        teams = {}
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            teams[str(t.get("id"))] = {"score": int(_num(c.get("score"))), "abbr": t.get("abbreviation", ""),
                                        "name": t.get("displayName", ""), "home": c.get("homeAway") == "home"}
        period = int(st.get("period") or 0)
        clock = _clock_seconds(st.get("displayClock"))
        total, per = GAME_SECONDS[sport], PERIOD_SECONDS[sport]
        if period < 1 or period > 4 or len(teams) != 2:
            continue                                   # overtime or odd data: skip
        elapsed = (period - 1) * per + (per - min(clock, per))
        out.append({"id": ev.get("id"), "name": ev.get("name", ""), "period": period, "clock": clock,
                    "f": max(0.0, min(1.0, elapsed / total)), "teams": teams,
                    "clock_txt": st.get("displayClock", "")})
    return out


def box_score(sport, event_id):
    """{norm player name: {...stats, 'team_id'}} from ESPN's box score. Cached for ESPN_POLL seconds."""
    hit = _state["summ"].get((sport, event_id))
    if hit and time.time() - hit[0] < ESPN_POLL * 0.8:
        return hit[1]
    data = _get_json(f"{ESPN[sport]}/summary?event={event_id}")
    res = {}
    for team in (data.get("boxscore") or {}).get("players", []):
        tid = str((team.get("team") or {}).get("id"))
        for cat in team.get("statistics", []):
            labels = cat.get("labels") or []
            cname = (cat.get("name") or "").lower()
            for ath in cat.get("athletes", []):
                nm = _norm((ath.get("athlete") or {}).get("displayName", ""))
                vals = ath.get("stats") or []
                if not nm or not vals or len(vals) != len(labels):
                    continue
                row = dict(zip(labels, vals))
                p = res.setdefault(nm, {"team_id": tid})
                if sport == "NFL":
                    if cname == "passing":
                        cmp_, att = _split_pair(row.get("C/ATT"))
                        p.update(pass_cmp=cmp_, pass_att=att, pass_yds=_num(row.get("YDS")))
                    elif cname == "rushing":
                        p.update(rush_att=_num(row.get("CAR")), rush_yds=_num(row.get("YDS")))
                else:
                    made, _ = _split_pair(row.get("3PT"))
                    p.update(minutes=_num(row.get("MIN")), pts=_num(row.get("PTS")), reb=_num(row.get("REB")),
                             ast=_num(row.get("AST")), fg3=float(made))
    _state["summ"][(sport, event_id)] = (time.time(), res)
    return res


# ====================== MODEL ======================
def _blend_total(prior, current, f):
    """Full-game expectation: the pregame line, nudged toward the player's pace so far."""
    if f >= 0.2 and current is not None:
        w = min(PACE_WEIGHT_MAX, 0.7 * f)
        return (1 - w) * prior + w * (current / f)
    return prior


def nfl_script(group, stat, margin, f):
    """Volume multiplier for the rest of the game, from the team's score margin (+ = leading)."""
    scale = 0.4 + 0.6 * f                         # an early deficit says less than a late one
    units = abs(margin) / 7.0
    if group == "pass":
        m = (1 + min(PASS_TRAIL_CAP, PASS_TRAIL * units * scale)) if margin < 0 else \
            (1 - min(PASS_LEAD_CAP, PASS_LEAD * units * scale)) if margin > 0 else 1.0
    else:
        m = (1 + min(RUSH_LEAD_CAP, RUSH_LEAD * units * scale)) if margin > 0 else \
            (1 - min(RUSH_TRAIL_CAP, RUSH_TRAIL * units * scale)) if margin < 0 else 1.0
    damp = YARD_DAMP.get(stat, 1.0)
    return 1 + (m - 1) * damp


def project_nfl(line, cur, prior, f, margin):
    r = 1 - f
    total = _blend_total(prior, cur.get(line["stat"]), f)
    m = nfl_script(line["group"], line["stat"], margin, f)
    c = cur.get(line["stat"], 0.0)
    proj = c + total * r * m
    notes = [f"Pace baseline {total:.1f} for the full game (pregame line {prior:g})",
             f"Script factor ×{m:.2f} on the rest of the game (team {'leads' if margin > 0 else 'trails' if margin < 0 else 'tied'}"
             + (f" by {abs(margin)}" if margin else "") + ")"]
    if margin >= 21 and f >= 0.7:
        notes.append("⚠️ Big lead late: starters may sit, which supports unders")
    return proj, m, notes


def project_nba(line, cur, prior, f, period, margin_abs):
    c = cur.get(line["stat"], 0.0)
    mins = cur.get("minutes", 0.0)
    base_rate = prior / NBA_EXPECTED_MIN
    rate = base_rate
    if mins >= 8:
        w = min(0.5, mins / 60.0)
        rate = (1 - w) * base_rate + w * (c / mins)
    exp_min = NBA_EXPECTED_MIN
    if f >= 0.25 and mins > 0:
        exp_min = max(20.0, min(40.0, 0.5 * NBA_EXPECTED_MIN + 0.5 * (mins / f)))
    game_left = 48.0 * (1 - f)
    rem_base = max(0.0, min(exp_min - mins, game_left))
    adj, why = 1.0, ""
    if period >= 3 and margin_abs >= NBA_BLOWOUT:
        adj, why = 0.5, f"Blowout ({margin_abs:.0f} pts): starters likely to sit, minutes cut"
        if margin_abs >= 30 or (period == 4 and margin_abs >= 25):
            adj, why = 0.2, f"Deep blowout ({margin_abs:.0f} pts): starters very likely done"
    elif period == 4 and margin_abs <= 6 and f >= 0.75:
        adj, why = 1.15, f"Close finish ({margin_abs:.0f} pts): starters stay on the floor"
    rem = min(rem_base * adj, game_left)
    proj = c + rate * rem
    notes = [f"{mins:.0f} min played, {c:g} so far, rate {rate:.2f}/min",
             f"Expected {rem:.1f} more minutes (base {rem_base:.1f}, script ×{adj:.2f})"]
    if why:
        notes.append(why)
    return proj, adj, notes


def evaluate(line, event, box, prior):
    """Returns an alert dict or None."""
    f = event["f"]
    if f < MIN_ELAPSED or (1 - f) < MIN_REMAINING:
        return None
    cur = box.get(line["norm"])
    if not cur or line["stat"] not in cur:
        return None
    team = event["teams"].get(cur["team_id"])
    opp = next((t for tid, t in event["teams"].items() if tid != cur["team_id"]), None)
    if not team or not opp:
        return None
    margin = team["score"] - opp["score"]
    c = cur[line["stat"]]
    L = line["line"]
    if line["live"] and LIVE_LINE_MODE == "rest":    # the line is only for the rest of the game: add what he has so far
        L = c + line["line"]
    if line["sport"] == "NFL":
        proj, factor, notes = project_nfl(line, cur, prior, f, margin)
    else:
        if cur.get("minutes", 0) <= 0:
            return None
        proj, factor, notes = project_nba(line, cur, prior, f, event["period"], abs(margin))
    if c >= L:                                       # the over has already hit (or the under is dead)
        return None
    gap = proj - L
    need = max(EDGE_PCT * L, MIN_ABS_EDGE.get(line["stat"], 2))
    if abs(gap) < need:
        return None
    if SCRIPT_ONLY and abs(factor - 1) < SCRIPT_MIN:
        return None
    moved = L - prior
    if abs(moved) >= 0.01:
        notes.append(f"PrizePicks line has moved {moved:+g} since pregame (some of the script may already be priced in)")
    if line["live"]:
        notes.append("PrizePicks live line" + (" (rest-of-game, converted to a full-game number)" if LIVE_LINE_MODE == "rest" else ""))
    return {"direction": "OVER" if gap > 0 else "UNDER", "proj": proj, "gap": gap, "cur": c, "line": L,
            "margin": margin, "team": team, "opp": opp, "notes": notes, "factor": factor, "moved": moved}


# ====================== ALERTS ======================
def _can_alert(key, direction):
    now = time.time()
    _state["sent"] = [t for t in _state["sent"] if now - t < 3600]
    if len(_state["sent"]) >= MAX_PER_HOUR:
        return False
    last = _state["alerts"].get(key)
    if last and last[1] == direction and now - last[0] < COOLDOWN:
        return False
    return True


def send_alert(line, event, a):
    key = (line["sport"], line["norm"], line["stat"])
    if not _can_alert(key, a["direction"]):
        return False
    _state["alerts"][key] = (time.time(), a["direction"])
    _state["sent"].append(time.time())
    emoji = "🔥" if a["direction"] == "OVER" else "🧊"
    t, o = a["team"], a["opp"]
    score = f"{t['abbr'] or t['name']} {t['score']} - {o['score']} {o['abbr'] or o['name']}"
    desc = (f"**{line['name']}** — {STAT_LABEL[line['stat']]} **{line['line']:g}**\n"
            f"Now **{a['cur']:g}** • Projected **{a['proj']:.1f}** • Lean **{a['direction']}** "
            f"(gap {a['gap']:+.1f})\n"
            + "\n".join(f"• {n}" for n in a["notes"]))
    embed = {"title": f"{emoji} Heartbeat — LIVE {line['sport']} — LEAN {a['direction']}",
             "description": desc, "color": 3066993 if a["direction"] == "OVER" else 3447003,
             "fields": [{"name": event["name"], "value": f"{score} • Q{event['period']} {event['clock_txt']}", "inline": False}],
             "footer": {"text": "Model v1 • alert only • projection, not a guarantee"}}
    _post({"embeds": [embed]})
    print(f"ALERT {line['sport']} {line['name']} {line['stat']} line {line['line']} proj {a['proj']:.1f} "
          f"{a['direction']} cur {a['cur']} margin {a['margin']} f={event['f']:.2f}")
    return True


def discovery_report(board, live):
    nfl = sum(1 for k in board if k[0] == "NFL")
    nba = sum(1 for k in board if k[0] == "NBA")
    flags = {k: sorted(v)[:6] for k, v in _state["flags_seen"].items()}
    lines = [f"Sports tracked: {', '.join(SPORTS)}",
             f"PrizePicks lines I can model right now: NFL {nfl}, NBA {nba}",
             f"Live games right now: {sum(len(v) for v in live.values())}",
             f"PrizePicks attribute names seen: {', '.join(sorted(_state['keys_seen'])) or 'none yet'}",
             f"Status/odds flags seen: {flags or 'none'}"]
    print("DISCOVERY:", " | ".join(lines))
    if DISCOVERY_POST:
        _post({"embeds": [{"title": "💓 Heartbeat online", "description": "\n".join(f"• {x}" for x in lines),
                           "color": 3066993}]})


# ====================== MAIN LOOP ======================
def update_priors(board, live_names):
    """Save each line's value from before kickoff. Frozen once the game has started."""
    now = _now()
    for key, ln in board.items():
        started = (ln["live"] or (ln["start"] is not None and now >= ln["start"])
                   or ln["norm"] in live_names.get(ln["sport"], set()))
        if not started and ln["line"] > 0:
            _state["prior"][key] = ln["line"]


def cycle(first=False):
    # 1) PrizePicks
    try:
        board = parse_board(fetch_board())
    except Exception as e:
        print("PrizePicks error:", e)
        return
    _state["board"] = board
    # 2) ESPN live games + box scores
    live, boxes, live_names = {}, {}, {}
    budget = MAX_SUMMARIES
    for sport in SPORTS:
        try:
            live[sport] = live_events(sport)
        except Exception as e:
            print(f"ESPN scoreboard error ({sport}):", e)
            live[sport] = []
        live_names[sport] = set()
        for ev in live[sport]:
            if budget <= 0:
                break
            try:
                boxes[(sport, ev["id"])] = box_score(sport, ev["id"])
                budget -= 1
                live_names[sport] |= set(boxes[(sport, ev["id"])].keys())
            except Exception as e:
                print(f"ESPN box score error ({sport} {ev['id']}):", e)
    update_priors(board, live_names)
    if first:
        discovery_report(board, live)
    # 3) project every tracked line that belongs to a live game
    sent = checked = 0
    for key, ln in board.items():
        sport = ln["sport"]
        for ev in live.get(sport, []):
            box = boxes.get((sport, ev["id"]))
            if not box or ln["norm"] not in box:
                continue
            prior = _state["prior"].get(key)
            if prior is None:
                if not ALLOW_NO_PRIOR:
                    continue
                prior = ln["line"]
            checked += 1
            if ln["live"] and key not in _state["live_logged"]:     # log the first live rows so we can see what they mean
                _state["live_logged"].add(key)
                if len(_state["live_logged"]) <= 40:
                    print(f"LIVE ROW: {ln['name']} {ln['stat']} live line {ln['line']:g} | pregame line {prior:g} | "
                          f"he has {box[ln['norm']].get(ln['stat'])} so far | game {ev['f']:.0%} done | flags {ln['flags']}")
            a = evaluate(ln, ev, box, prior)
            if a and send_alert(ln, ev, a):
                sent += 1
            break
    n_live = sum(len(v) for v in live.values())
    if n_live or first:
        print(f"cycle: {n_live} live games, {len(board)} lines tracked, {checked} projected, {sent} alerts")


def main():
    if not WEBHOOK_URL:
        print("WARNING: WEBHOOK_URL is not set. Alerts will only print in the logs.")
    print(f"Heartbeat starting. Sports: {SPORTS}. PrizePicks every {PP_POLL:.0f}s, ESPN every {ESPN_POLL:.0f}s.")
    first = True
    last_pp = 0.0
    while True:
        try:
            cycle(first)
            first = False
        except Exception as e:
            print("cycle error:", e)
        # poll fast only while games are live; otherwise relax
        any_live = any(_state["summ"]) and any(time.time() - v[0] < 120 for v in _state["summ"].values())
        time.sleep(min(PP_POLL, ESPN_POLL) if any_live else max(PP_POLL, 60))


if __name__ == "__main__":
    main()
