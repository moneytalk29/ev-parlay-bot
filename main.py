"""Heartbeat v1.9.1: live NFL + NBA + NHL + TENNIS scanner for PrizePicks lines, plus a pregame line check. ALERT ONLY, it never places bets.

What it does
  1. Reads the PrizePicks board (PrizePicks' public feed) for NFL, NBA and tennis player lines.
  2. Remembers each line from BEFORE kickoff (the "pregame line"), which is the market's expectation for the player.
  3. While a game is live, reads the score, clock and player box score from ESPN's free public data.
  4. Projects the player's final total using the game script:
       NFL: a team that trails throws more and runs less; a team that leads runs more and throws less.
       NBA: in a blowout starters sit (fewer minutes); in a tight finish starters play more.
            v1.4 adds 1st-half / 2nd-half props (1H and 2H points, rebounds, assists, PRA, fantasy score) and full-game
            PRA / fantasy score. 2H props are judged at HALFTIME using the score, first-half minutes and foul trouble.
       NHL (v1.9): "score effects": a team that trails shoots more and the leading team blocks more, so the trailing
            team's skaters get more Shots On Goal and the leading team's goalie faces more shots (more Saves).
            Props: Shots On Goal, Goalie Saves, Blocked Shots, Hits (hits only alert when HB_SCRIPT_ONLY=false).
       TENNIS (v1.7): ESPN gives the live games score of every set (no aces or double faults), so the bot plays out the
            rest of the match a couple of thousand times from the current score and counts how often each line goes
            over or under. Props: Total Games, Total Games Won, Total Sets, Total Tie Breaks, 1st Set Total Games and
            Fantasy Score. Aces, Double Faults and Break Points Won are skipped (no free live data).
            Only juicy picks post: the over or under must hit in at least HB_TENNIS_MIN_PROB (70%) of the simulations.
  5. PREGAME (v1.8, free): before a match or game starts, checks PrizePicks' lines against each other and flags a line
     that doesn't fit the player's other lines:
       TENNIS: Total Games Won (vs the other player's Games Won and Total Games) and Fantasy Score (vs games, sets and
               the Aces line). NBA: PRA, Pts+Rebs, Pts+Asts, Rebs+Asts vs the single Points/Rebounds/Assists lines.
     Each line posts once (again only if the line changes), at most HB_PRE_MAX_LEGS_PER_HOUR legs an hour.
  6. Posts to your Discord webhook when the projection is far enough from PrizePicks' current line.
     Picks go out as PAIRS (two legs from DIFFERENT games, never the same game, same game-shape preferred) so you can
     lock both fast. A lone pick waits HB_PAIR_WAIT_SECONDS for a partner, then posts alone, so nothing is lost.

Honest limits (read these)
  - The script adjustments below are STARTING GUESSES, not proven numbers. Every alert is logged so they can be
    checked and tuned against real results. Treat the first weeks as testing.
  - ESPN's public data is unofficial and can change or lag a few seconds.
  - It needs the pregame line saved BEFORE kickoff. If the service restarts during a game, lines for that
    game have no saved prior and are skipped (set HB_ALLOW_NO_PRIOR=true to override, at lower trust).
  - It does not know about injuries, a backup QB coming in, or weather.
  - Tennis: it does not know who is serving, and it assumes best-of-3 sets (men's Grand Slams are best-of-5:
    set HB_TENNIS_BEST_OF=5 for those weeks). If a player retires, PrizePicks keeps the stats so far (risk for overs).
Everything is a Railway variable. Only WEBHOOK_URL is required.
"""
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                                   # no time zone data: show UTC instead
    _ET = timezone.utc

import requests

# ====================== SETTINGS (Railway variables) ======================
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SPORTS = [s.strip().upper() for s in os.getenv("HB_SPORTS", "NFL,NBA,NHL").split(",") if s.strip()]
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
# Pairs: live lines move fast, so picks go out two at a time (PrizePicks needs 2+ legs).
PAIR_MODE = os.getenv("HB_PAIR_MODE", "true").lower() == "true"
PAIR_WAIT = float(os.getenv("HB_PAIR_WAIT_SECONDS", "75"))     # how long a lone pick waits for a partner before posting alone
PAIR_REQUIRE_SAME_SHAPE = os.getenv("HB_PAIR_REQUIRE_SAME_SHAPE", "false").lower() == "true"  # true = only pair lopsided with lopsided, tight with tight
PAIR_SAME_SHAPE_BONUS = float(os.getenv("HB_PAIR_SAME_SHAPE_BONUS", "0.3"))   # preference for same-shape partners
LOPSIDED = {"NFL": float(os.getenv("HB_NFL_LOPSIDED", "10")), "NBA": float(os.getenv("HB_NBA_LOPSIDED", "15")),
            "NHL": float(os.getenv("HB_NHL_LOPSIDED", "2"))}  # margin that makes a game "lopsided"
# NFL game-script strength: change in volume per 7 points of margin (a "score"), and the cap
PASS_TRAIL, PASS_TRAIL_CAP = float(os.getenv("HB_PASS_TRAIL", "0.07")), float(os.getenv("HB_PASS_TRAIL_CAP", "0.30"))
PASS_LEAD, PASS_LEAD_CAP = float(os.getenv("HB_PASS_LEAD", "0.05")), float(os.getenv("HB_PASS_LEAD_CAP", "0.20"))
RUSH_LEAD, RUSH_LEAD_CAP = float(os.getenv("HB_RUSH_LEAD", "0.08")), float(os.getenv("HB_RUSH_LEAD_CAP", "0.30"))
RUSH_TRAIL, RUSH_TRAIL_CAP = float(os.getenv("HB_RUSH_TRAIL", "0.09")), float(os.getenv("HB_RUSH_TRAIL_CAP", "0.35"))
# NHL (v1.9) score effects: change in the rest-of-game rate per goal of margin, and the cap
NHL_TRAIL, NHL_TRAIL_CAP = float(os.getenv("HB_NHL_TRAIL", "0.08")), float(os.getenv("HB_NHL_TRAIL_CAP", "0.25"))
NHL_LEAD, NHL_LEAD_CAP = float(os.getenv("HB_NHL_LEAD", "0.06")), float(os.getenv("HB_NHL_LEAD_CAP", "0.20"))
# NBA
NBA_EXPECTED_MIN = float(os.getenv("HB_NBA_EXPECTED_MIN", "32"))   # a typical starter's minutes
NBA_BLOWOUT = float(os.getenv("HB_NBA_BLOWOUT", "20"))             # point gap where starters start to sit (3rd/4th quarter)
# NBA halves (v1.4)
NBA_EXP_H = {1: float(os.getenv("HB_NBA_EXPECTED_MIN_1H", "17")), 2: float(os.getenv("HB_NBA_EXPECTED_MIN_2H", "15"))}  # a starter's usual minutes per half
HALF_SHARE = {1: 0.52, 2: 0.48}                                     # used only if there is no saved half line: share of the full-game line
HALF_MIN_ELAPSED = float(os.getenv("HB_HALF_MIN_ELAPSED", "0.3"))   # 1H props: wait until 30% of the half is played
HALF_MIN_REMAINING = float(os.getenv("HB_HALF_MIN_REMAINING", "0.12"))  # ignore the last 12% of a half
HALF_EDGE_SCALE = float(os.getenv("HB_HALF_EDGE_SCALE", "0.6"))     # half props need a smaller absolute gap than full-game ones
FANT = tuple(float(x) for x in os.getenv("HB_FANT_WEIGHTS", "1,1.2,1.5,3,3,-1").split(","))  # pts, reb, ast, stl, blk, turnover
# What a LIVE PrizePicks line means: "full" = the player's full-game total (default), "rest" = just the rest of the game.
# The first live game will show which one it is (see the "LIVE ROW" log lines). Change this if it turns out to be "rest".
LIVE_LINE_MODE = os.getenv("HB_LIVE_LINE_MODE", "full").lower()

# Tennis alerts (v1.7): ON by default. HB_TENNIS=false turns them off.
TENNIS_ON = os.getenv("HB_TENNIS", "true").lower() == "true"
T_MIN_PROB = float(os.getenv("HB_TENNIS_MIN_PROB", "0.70"))        # "juicy": the pick must hit in 70%+ of simulated finishes
T_FANT_EXTRA = float(os.getenv("HB_TENNIS_FANT_EXTRA", "0.05"))    # Fantasy Score needs 5% more (aces/double faults aren't in the live data)
T_MIN_GAMES = int(os.getenv("HB_TENNIS_MIN_GAMES", "4"))           # wait until at least 4 games are played
T_SIMS = int(os.getenv("HB_TENNIS_SIMS", "2000"))                  # simulated finishes per match
T_BEST_OF = int(os.getenv("HB_TENNIS_BEST_OF", "3"))               # sets in a match (5 only for men's Grand Slams)
T_HOLD = {"ATP": float(os.getenv("HB_TENNIS_HOLD_ATP", "0.80")),   # how often a server holds (used when there is no Total Games line)
          "WTA": float(os.getenv("HB_TENNIS_HOLD_WTA", "0.66"))}
T_SERVE_ADJ = float(os.getenv("HB_TENNIS_SERVE_ADJ", "0"))         # expected fantasy points from aces minus double faults
T_PRIOR_GAMES = float(os.getenv("HB_TENNIS_PRIOR_GAMES", "14"))    # how many games it takes before the live score outweighs the pregame lines
# Pregame line check (v1.8): ON by default. HB_PREGAME=false turns it off.
PREGAME_ON = os.getenv("HB_PREGAME", "true").lower() == "true"
PRE_MIN_PROB = float(os.getenv("HB_PRE_MIN_PROB", "0.66"))        # tennis: pick must hit in 66%+ of matches built from the other lines
PRE_NBA_GAP = float(os.getenv("HB_PRE_NBA_GAP", "2.5"))            # NBA: combo line must be 2.5+ away from the sum of the single lines
PRE_WINDOW_H = float(os.getenv("HB_PRE_WINDOW_HOURS", "24"))       # only matches/games starting within 24 hours
PRE_MAX_LEGS = int(os.getenv("HB_PRE_MAX_LEGS_PER_HOUR", "8"))     # cap on pregame picks per hour, so the live alerts keep room
# Tennis discovery (v1.6): logs only. Not needed any more now that tennis alerts exist; leave it off to keep the logs short.
TENNIS_DISCOVERY = os.getenv("HB_TENNIS_DISCOVERY", "false").lower() == "true"
TENNIS_POST = os.getenv("HB_TENNIS_POST", "false").lower() == "true"     # false = tennis discovery writes to the logs only, nothing goes to Discord
TENNIS_POLL = float(os.getenv("HB_TENNIS_POLL_SECONDS", "60"))
TENNIS_ESPN = {"ATP": "https://site.api.espn.com/apis/site/v2/sports/tennis/atp",
               "WTA": "https://site.api.espn.com/apis/site/v2/sports/tennis/wta"}

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
           "Origin": "https://app.prizepicks.com", "Referer": "https://app.prizepicks.com/"}
ESPN = {"NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl",
        "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
        "NHL": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"}
GAME_SECONDS = {"NFL": 3600.0, "NBA": 2880.0, "NHL": 3600.0}
PERIOD_SECONDS = {"NFL": 900.0, "NBA": 720.0, "NHL": 1200.0}
LAST_PERIOD = {"NFL": 4, "NBA": 4, "NHL": 3}     # overtime is skipped

# PrizePicks stat name (lowercase) -> (stat key, group)
STAT_MAP = {
    "NFL": {"pass yards": ("pass_yds", "pass"), "passing yards": ("pass_yds", "pass"),
            "pass attempts": ("pass_att", "pass"), "pass completions": ("pass_cmp", "pass"),
            "rush yards": ("rush_yds", "rush"), "rushing yards": ("rush_yds", "rush"),
            "rush attempts": ("rush_att", "rush"), "rushing attempts": ("rush_att", "rush"), "carries": ("rush_att", "rush"),
            "receiving yards": ("rec_yds", "rec"), "rec yards": ("rec_yds", "rec"),
            "receptions": ("rec_cnt", "rec")},
    "NBA": {"points": ("pts", "nba"), "rebounds": ("reb", "nba"), "assists": ("ast", "nba"),
            "3-pt made": ("fg3", "nba"), "3-pointers made": ("fg3", "nba"), "three pointers made": ("fg3", "nba"),
            "3pt made": ("fg3", "nba"), "3ptm": ("fg3", "nba"), "3pm": ("fg3", "nba"),
            "pts+rebs+asts": ("pra", "nba"), "points+rebounds+assists": ("pra", "nba"), "pra": ("pra", "nba"),
            "fantasy score": ("fant", "nba"), "fantasy points": ("fant", "nba"),
            "pts+rebs": ("pr", "nba"), "points+rebounds": ("pr", "nba"), "pts+asts": ("pa", "nba"),
            "points+assists": ("pa", "nba"), "rebs+asts": ("ra", "nba"), "rebounds+assists": ("ra", "nba")},
    "NHL": {"shots on goal": ("sog", "shots"), "shots": ("sog", "shots"), "sog": ("sog", "shots"),
            "goalie saves": ("saves", "goalie"), "saves": ("saves", "goalie"),
            "blocked shots": ("nhl_blk", "def"), "hits": ("hits", "hits")},
}
# PrizePicks tennis stat name (lowercase) -> stat key. Aces, double faults and break points are left out on purpose.
TENNIS_STATS = {"total games": "t_games", "total games won": "t_gw", "total sets": "t_sets",
                "total tie breaks": "t_tb", "total tiebreaks": "t_tb", "1st set total games": "t_set1",
                "fantasy score": "t_fant", "aces": "t_aces", "double faults": "t_df"}
T_NO_LIVE = ("t_aces", "t_df")       # read only to estimate Fantasy Score; no live data, so never picked
T_MATCH_LEVEL = ("t_games", "t_sets", "t_tb", "t_set1")    # same number for both players: alert once per match
STAT_LABEL = {"pass_yds": "Pass Yards", "pass_att": "Pass Attempts", "pass_cmp": "Pass Completions",
              "rush_yds": "Rush Yards", "rush_att": "Rush Attempts",
              "rec_yds": "Receiving Yards", "rec_cnt": "Receptions",
              "pts": "Points", "reb": "Rebounds", "ast": "Assists", "fg3": "3-PT Made",
              "pra": "Pts+Rebs+Asts", "fant": "Fantasy Score", "pr": "Pts+Rebs", "pa": "Pts+Asts", "ra": "Rebs+Asts",
              "t_games": "Total Games", "t_gw": "Total Games Won", "t_sets": "Total Sets", "t_tb": "Total Tie Breaks",
              "t_set1": "1st Set Total Games", "t_fant": "Fantasy Score",
              "t_aces": "Aces", "t_df": "Double Faults",
              "sog": "Shots On Goal", "saves": "Goalie Saves", "nhl_blk": "Blocked Shots", "hits": "Hits"}
MIN_ABS_EDGE = {"pass_yds": 12, "pass_att": 3, "pass_cmp": 2.5, "rush_yds": 8, "rush_att": 2.5, "rec_yds": 12, "rec_cnt": 1.5,
                "pts": 3.5, "reb": 1.5, "ast": 1.5, "fg3": 1.0, "pra": 4.0, "fant": 5.0, "pr": 3.0, "pa": 3.0, "ra": 2.0,
                "sog": 1.0, "saves": 3.0, "nhl_blk": 1.0, "hits": 1.0}
YARD_DAMP = {"pass_yds": 0.8, "rush_yds": 0.8, "rec_yds": 0.8, "pass_cmp": 0.9, "rec_cnt": 0.9}   # efficiency changes with the script, so shrink these
# v1.3: VOLUME props (attempts, carries, catches) follow the game script more reliably than YARD props (a trailing QB can
# throw 42 times and still miss his yards). Yard props need a bigger gap to alert, and volume props rank first when pairing.
YARD_STATS = ("pass_yds", "rec_yds")      # rush yards are NOT here: a leading team runs on purpose, so they follow the script closely
YARD_EDGE_MULT = float(os.getenv("HB_YARD_EDGE_MULT", "1.3"))
PRIORITY = {"pass_att": 1.2, "rush_att": 1.15, "rec_cnt": 1.05, "pass_cmp": 1.0,
            "rush_yds": 1.1, "rec_yds": 0.9, "pass_yds": 0.8, "fant": 0.95, "sog": 1.1, "saves": 1.05}
BAD_WORDS = ("1h", "2h", "1q", "2q", "3q", "4q", "1st", "2nd", "half", "quarter", "combo", "+", "(", "longest",
             "fantasy", "first", "last", "total")

_session = requests.Session()
_session.headers.update(HEADERS)
_rng = random.Random()
_state = {"prior": {}, "alerts": {}, "sent": [], "summ": {}, "board": {}, "keys_seen": set(), "flags_seen": {},
          "live_logged": set(), "pending": {}, "snap": {}, "names": {}, "names_printed": 0,
          "tboard": {}, "tcal": {}, "tlive": 0.0, "t_logged": set(), "pre_sent": {}, "pre_times": [],
          "tennis": {"last": 0.0, "pp_sig": None, "matches": set(), "posts": 0}}


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


_H1 = re.compile(r"\b(1h|1st half|first half|h1)\b")
_H2 = re.compile(r"\b(2h|2nd half|second half|h2)\b")
_QTR = re.compile(r"\b(1q|2q|3q|4q|q1|q2|q3|q4|quarter)\b")


def classify_nba(raw):
    """PrizePicks NBA stat name -> (stat key, half) where half is None (full game), 1 or 2. None if we don't model it."""
    s = str(raw).lower()
    if _QTR.search(s):
        return None
    half = 1 if _H1.search(s) else 2 if _H2.search(s) else None
    s = _H2.sub(" ", _H1.sub(" ", s))
    s = s.replace("combo", " ")
    s = re.sub(r"[()]", " ", s)
    s = re.sub(r"\s+-\s+", " ", s)
    s = re.sub(r"\s*\+\s*", "+", s)
    s = re.sub(r"\s+", " ", s).strip()
    stat = STAT_MAP["NBA"].get(s)
    return (stat[0], half) if stat else None


def derive(p):
    """Adds the combined NBA stats (PRA and fantasy score) from the basic ones."""
    pts, reb, ast = p.get("pts", 0.0), p.get("reb", 0.0), p.get("ast", 0.0)
    p["pra"] = pts + reb + ast
    p["pr"], p["pa"], p["ra"] = pts + reb, pts + ast, reb + ast
    w = FANT
    p["fant"] = pts * w[0] + reb * w[1] + ast * w[2] + p.get("stl", 0.0) * w[3] + p.get("blk", 0.0) * w[4] + p.get("tov", 0.0) * w[5]
    return p


RAW_NBA = ("minutes", "pts", "reb", "ast", "fg3", "stl", "blk", "tov")


def half_stats(cur, snap_p):
    """What a player did since the snapshot (2nd half = full box minus the halftime box)."""
    out = {"team_id": cur.get("team_id"), "pf": cur.get("pf", 0.0)}
    for k in RAW_NBA:
        out[k] = max(0.0, cur.get(k, 0.0) - (snap_p or {}).get(k, 0.0))
    return derive(out)


def stat_label(line):
    return ({1: "1H ", 2: "2H "}.get(line.get("half"), "")) + STAT_LABEL[line["stat"]]


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
            _state["names"].setdefault(sport, set()).add(stat_name)
            half = None
            if sport == "NBA":
                cl = classify_nba(stat_name)
                if not cl:
                    continue
                stat, half = (cl[0], "nba"), cl[1]
            else:
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
            key = (sport, _norm(name), stat[0], half)
            if key in out and out[key]["live"] and not live_row:
                continue                             # a live row for this player/stat beats a stale pregame row
            out[key] = {
                "sport": sport, "name": name, "norm": _norm(name), "team": team, "pos": pos,
                "stat": stat[0], "group": stat[1], "half": half, "line": _num(a.get("line_score")),
                "start": _to_dt(start), "opp": a.get("description") or "", "flags": flags, "live": live_row, "gid": gid}
        except Exception:
            continue
    _state["keys_seen"] |= seen_keys
    return out


def _is_tennis_league(lg):
    return "TENNIS" in lg or lg in ("ATP", "WTA")


def parse_tennis(data):
    """Tennis lines we can model: {("TENNIS", norm_name, stat, None): line dict}. Standard lines only."""
    players, leagues, games = {}, {}, {}
    for inc in data.get("included", []):
        a = inc.get("attributes", {})
        if inc.get("type") == "new_player":
            players[inc["id"]] = a.get("name", "")
        elif inc.get("type") == "league":
            leagues[inc["id"]] = (a.get("name") or "").upper()
        elif inc.get("type") == "game":
            games[inc["id"]] = a
    out = {}
    for item in data.get("data", []):
        try:
            a = item["attributes"]
            rel = item.get("relationships", {})
            lg = leagues.get((rel.get("league", {}).get("data") or {}).get("id"), "")
            if not _is_tennis_league(lg):
                continue
            stat_name = re.sub(r"\s+", " ", str(a.get("stat_display_name") or a.get("stat_type") or "").lower()).strip()
            _state["names"].setdefault("TENNIS", set()).add(stat_name)
            stat = TENNIS_STATS.get(stat_name)
            if not stat:
                continue
            if str(a.get("odds_type") or "standard").lower() != "standard":
                continue
            name = players.get((rel.get("new_player", {}).get("data") or {}).get("id"), "")
            if not name:
                continue
            gid = (rel.get("game", {}).get("data") or {}).get("id")
            start = a.get("start_time") or (games.get(gid) or {}).get("start_time") or ""
            flags = {k: a.get(k) for k in ("status", "is_live", "in_game", "odds_type") if k in a}
            live_row = (str(a.get("is_live")).lower() == "true" or str(a.get("in_game")).lower() == "true"
                        or str(a.get("status") or "").lower() in ("in_game", "live", "in_progress"))
            key = ("TENNIS", _norm(name), stat, None)
            if key in out and out[key]["live"] and not live_row:
                continue
            out[key] = {"sport": "TENNIS", "name": name, "norm": _norm(name), "team": "", "pos": "",
                        "stat": stat, "group": "tennis", "half": None, "line": _num(a.get("line_score")),
                        "start": _to_dt(start), "opp": a.get("description") or "", "flags": flags, "live": live_row, "gid": gid}
        except Exception:
            continue
    return out


# ====================== ESPN ======================
def _get_json(url):
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def _espn_dates():
    """Today's date in New York (and yesterday until 6 AM, for late games that run past midnight), as YYYYMMDD."""
    now = datetime.now(_ET) if _ET is not timezone.utc else _now()
    days = [now]
    if now.hour < 6:
        days.append(now - timedelta(days=1))
    return [d.strftime("%Y%m%d") for d in days]


def _scoreboard_events(sport):
    """ESPN's scoreboard for the exact date(s). v1.9.1: the plain scoreboard can show an old day (seen for NHL), so ask by date."""
    evs, seen = [], set()
    for d in _espn_dates():
        try:
            data = _get_json(f"{ESPN[sport]}/scoreboard?dates={d}&limit=100")
        except Exception as e:
            print(f"ESPN scoreboard error ({sport} {d}):", e)
            continue
        for ev in data.get("events", []) or []:
            if ev.get("id") not in seen:
                seen.add(ev.get("id"))
                evs.append(ev)
    return evs


def live_events(sport):
    """List of live games: id, period, clock seconds, elapsed fraction, team scores by ESPN team id."""
    out = []
    for ev in _scoreboard_events(sport):
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
        if period < 1 or period > LAST_PERIOD[sport] or len(teams) != 2:
            continue                                   # overtime or odd data: skip
        elapsed = (period - 1) * per + (per - min(clock, per))
        tname = str((st.get("type") or {}).get("name") or "")
        halftime = sport == "NBA" and (tname == "STATUS_HALFTIME" or (period == 2 and clock <= 1.0))
        out.append({"id": ev.get("id"), "name": ev.get("name", ""), "period": period, "clock": clock, "halftime": halftime,
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
            if sport == "NHL":
                labels = cat.get("keys") or labels           # NHL labels are ambiguous ("SOG" = shootout goals), keys are not
            cname = (cat.get("name") or "").lower()
            for ath in cat.get("athletes", []):
                nm = _norm((ath.get("athlete") or {}).get("displayName", ""))
                vals = ath.get("stats") or []
                if not nm or not vals or len(vals) != len(labels):
                    continue
                row = dict(zip(labels, vals))
                p = res.setdefault(nm, {"team_id": tid})
                if sport == "NHL":
                    if cname == "goalies":
                        p.update(saves=_num(row.get("saves")), goals_against=_num(row.get("goalsAgainst")),
                                 g_toi=_clock_seconds(row.get("timeOnIce")) / 60.0, goalie=True)
                    else:
                        p.update(sog=_num(row.get("shotsTotal")), nhl_blk=_num(row.get("blockedShots")),
                                 hits=_num(row.get("hits")), goals=_num(row.get("goals")), assists=_num(row.get("assists")),
                                 toi=_clock_seconds(row.get("timeOnIce")) / 60.0)
                elif sport == "NFL":
                    if cname == "passing":
                        cmp_, att = _split_pair(row.get("C/ATT"))
                        p.update(pass_cmp=cmp_, pass_att=att, pass_yds=_num(row.get("YDS")))
                    elif cname == "rushing":
                        p.update(rush_att=_num(row.get("CAR")), rush_yds=_num(row.get("YDS")))
                    elif cname == "receiving":
                        p.update(rec_cnt=_num(row.get("REC")), rec_yds=_num(row.get("YDS")))
                else:
                    made, _ = _split_pair(row.get("3PT"))
                    p.update(minutes=_num(row.get("MIN")), pts=_num(row.get("PTS")), reb=_num(row.get("REB")),
                             ast=_num(row.get("AST")), fg3=float(made), stl=_num(row.get("STL")), blk=_num(row.get("BLK")),
                             tov=_num(row.get("TO")), pf=_num(row.get("PF")))
                    derive(p)
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
    if group in ("pass", "rec"):                  # receivers ride the same team passing volume as the QB
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


def nhl_script(group, margin, f):
    """Rest-of-game multiplier from the team's goal margin (+ = leading). Score effects grow as the game goes on."""
    scale = 0.5 + 0.5 * f
    u = min(abs(margin), 3)
    up_trail = 1 + min(NHL_TRAIL_CAP, NHL_TRAIL * u * scale)
    down_lead = 1 - min(NHL_LEAD_CAP, NHL_LEAD * u * scale)
    if margin == 0 or group == "hits":
        return 1.0
    if group == "shots":                              # trailing team presses and shoots more
        return up_trail if margin < 0 else down_lead
    if group == "goalie":                             # his team leads -> the other team presses -> more shots at him
        return up_trail if margin > 0 else down_lead
    if group == "def":                                # leading team sits back and blocks more shots
        return (1 + min(NHL_LEAD_CAP, NHL_LEAD * u * scale)) if margin > 0 else (1 - min(NHL_LEAD_CAP, NHL_LEAD * u * scale))
    return 1.0


def project_nhl(line, cur, prior, f, margin):
    """Returns (projection, script factor, notes) or None when the goalie isn't the one in net."""
    if line["group"] == "goalie":
        if not cur.get("goalie"):
            return None
        if cur.get("g_toi", 0.0) < f * 60.0 - 3.0:
            return None                               # pulled, or came in late: he isn't the starter in net
    c = cur.get(line["stat"], 0.0)
    total = _blend_total(prior, c, f)
    m = nhl_script(line["group"], margin, f)
    proj = c + total * (1 - f) * m
    lead_txt = "leads" if margin > 0 else "trails" if margin < 0 else "tied"
    notes = [f"Pace baseline {total:.1f} for the full game (pregame line {prior:g})",
             f"Score effect ×{m:.2f} on the rest of the game (team {lead_txt}" + (f" by {abs(margin)}" if margin else "") + ")"]
    if line["group"] == "shots" and margin < 0 and f >= 0.85:
        notes.append("Trailing late: the goalie may be pulled for an extra attacker (more shots)")
    if line["group"] == "goalie" and margin < 0 and f >= 0.85:
        notes.append("⚠️ His team trails late: he may be pulled for an extra attacker (fewer saves)")
    if line["group"] == "goalie" and abs(margin) >= 4:
        notes.append("⚠️ Lopsided game: a goalie can get pulled")
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


def _foul_adj(half, pf, period):
    """Minutes cut for foul trouble (guess): a player with many fouls sits."""
    if half == 1:
        return (0.6 if pf >= 4 else 0.8 if pf >= 3 else 1.0)
    if half == 2:
        return (0.6 if pf >= 5 else 0.8 if pf >= 4 else 1.0)
    return 1.0


def project_nba_half(line, event, cur_full, prior, margin_abs):
    """1st-half or 2nd-half NBA prop. Returns (stat so far in THIS half, projection, script factor, notes) or None."""
    half, stat = line["half"], line["stat"]
    f, period = event["f"], event["period"]
    sp = None
    if half == 1:
        if period >= 3 or event.get("halftime"):
            return None                                   # the first half is over
        prog = f / 0.5
        if prog < HALF_MIN_ELAPSED or prog > 1 - HALF_MIN_REMAINING:
            return None
        cur = cur_full
    else:
        snap = _state["snap"].get(("NBA", event["id"]))
        if snap is None:
            return None                                   # no halftime snapshot (bot started late): can't split the halves
        sp = snap.get(line["norm"])
        if not sp or sp.get("minutes", 0) < 5:
            return None                                   # didn't play the first half (or no data)
        prog = max(0.0, (f - 0.5) / 0.5)
        if prog > 1 - HALF_MIN_REMAINING:
            return None
        cur = half_stats(cur_full, sp)
    mins, c = cur.get("minutes", 0.0), cur.get(stat, 0.0)
    exp = NBA_EXP_H[half]
    base_rate = prior / exp
    rate = base_rate
    if half == 2 and sp and sp.get("minutes", 0) >= 10:           # weak carry-over from the first half (hot/cold halves happen)
        rate = 0.85 * base_rate + 0.15 * (sp.get(stat, 0.0) / sp["minutes"])
    if mins >= 6:
        w = min(0.5, mins / 40.0)
        rate = (1 - w) * rate + w * (c / mins)
    exp_min = exp
    if prog >= 0.35 and mins > 0:
        exp_min = max(8.0, min(22.0, 0.5 * exp + 0.5 * (mins / prog)))
    half_left = 24.0 * (1 - prog)
    rem_base = max(0.0, min(exp_min - mins, half_left))
    adj, why = 1.0, ""
    if half == 2:
        if f < 0.6:                                                # start of the 2nd half: judge by the halftime score
            if margin_abs >= 25:
                adj, why = 0.75, f"{margin_abs:.0f}-point game at the half: starters likely get cut minutes"
            elif margin_abs >= 18:
                adj, why = 0.88, f"{margin_abs:.0f}-point game at the half: starters may lose some minutes"
            elif margin_abs <= 4:
                adj, why = 1.06, f"Close game at the half ({margin_abs:.0f}): starters should play heavy minutes"
        elif period >= 3 and margin_abs >= NBA_BLOWOUT:
            adj, why = 0.5, f"Blowout ({margin_abs:.0f} pts): starters likely to sit"
            if margin_abs >= 30 or (period == 4 and margin_abs >= 25):
                adj, why = 0.2, f"Deep blowout ({margin_abs:.0f} pts): starters very likely done"
        elif period == 4 and margin_abs <= 6 and f >= 0.75:
            adj, why = 1.15, f"Close finish ({margin_abs:.0f} pts): starters stay on the floor"
    pf = cur_full.get("pf", 0.0)
    fa = _foul_adj(half, pf, period)
    notes = []
    if half == 2 and sp:
        notes.append(f"1st half: {sp.get('minutes', 0):.0f} min, {sp.get(stat, 0):g} {STAT_LABEL[stat]}; 2nd half so far: {mins:.0f} min, {c:g}")
    else:
        notes.append(f"{mins:.0f} min played this half, {c:g} so far, rate {rate:.2f}/min")
    if fa < 1:
        why = (why + " • " if why else "") + f"Foul trouble ({pf:.0f} fouls): minutes cut ×{fa:.2f}"
    adj *= fa
    rem = min(rem_base * adj, half_left)
    proj = c + rate * rem
    notes.append(f"Expected {rem:.1f} more minutes this half (base {rem_base:.1f}, script ×{adj:.2f})")
    if why:
        notes.append(why)
    return c, proj, adj, notes


def evaluate(line, event, box, prior):
    """Returns an alert dict or None."""
    f = event["f"]
    half = line.get("half")
    cur = box.get(line["norm"])
    if not cur:
        return None
    team = event["teams"].get(cur["team_id"])
    opp = next((t for tid, t in event["teams"].items() if tid != cur["team_id"]), None)
    if not team or not opp:
        return None
    margin = team["score"] - opp["score"]
    if line["sport"] == "NBA" and half:
        res = project_nba_half(line, event, cur, prior, abs(margin))
        if not res:
            return None
        c, proj, factor, notes = res
        if line.get("prior_est"):
            notes.append("No saved half line: baseline estimated from the full-game line")
    else:
        if f < MIN_ELAPSED or (1 - f) < MIN_REMAINING:
            return None
        if line["stat"] not in cur:
            return None
        c = cur[line["stat"]]
        res = None
    L = line["line"]
    if line["live"] and LIVE_LINE_MODE == "rest":    # the line is only for the rest of the game: add what he has so far
        L = c + line["line"]
    if res is None:
        if line["sport"] == "NFL":
            proj, factor, notes = project_nfl(line, cur, prior, f, margin)
        elif line["sport"] == "NHL":
            res_nhl = project_nhl(line, cur, prior, f, margin)
            if not res_nhl:
                return None
            proj, factor, notes = res_nhl
        else:
            if cur.get("minutes", 0) <= 0:
                return None
            proj, factor, notes = project_nba(line, cur, prior, f, event["period"], abs(margin))
    if c >= L:                                       # the over has already hit (or the under is dead)
        return None
    gap = proj - L
    min_abs = MIN_ABS_EDGE.get(line["stat"], 2) * (HALF_EDGE_SCALE if half else 1.0)
    need = max(EDGE_PCT * L, min_abs)
    if line["stat"] in YARD_STATS:
        need *= YARD_EDGE_MULT                       # yards are riskier than volume, so ask for a bigger gap
    if abs(gap) < need:
        return None
    if half == 1:
        need *= float(os.getenv("HB_HALF1_EDGE_MULT", "1.25"))     # 1st-half props have no game-script edge, only pace, so ask for a bigger gap
        if abs(gap) < need:
            return None
    elif SCRIPT_ONLY and abs(factor - 1) < SCRIPT_MIN:
        return None
    moved = L - prior
    if abs(moved) >= 0.01:
        notes.append(f"PrizePicks line has moved {moved:+g} since pregame (some of the script may already be priced in)")
    if line["live"]:
        notes.append("PrizePicks live line" + (" (rest-of-game, converted to a full-game number)" if LIVE_LINE_MODE == "rest" else ""))
    shape = "lopsided" if abs(margin) >= LOPSIDED[line["sport"]] else "tight"
    return {"direction": "OVER" if gap > 0 else "UNDER", "proj": proj, "gap": gap, "cur": c, "line": L,
            "margin": margin, "team": team, "opp": opp, "notes": notes, "factor": factor, "moved": moved,
            "strength": abs(gap) / need * PRIORITY.get(line["stat"], 1.0), "shape": f"{line['sport']}-{shape}", "game": event["id"],
            "sport": line["sport"]}


# ====================== TENNIS (v1.7) ======================
def _set_over(x, y):
    """A set is finished at 6+ games with a 2-game lead, or 7-6 after a tiebreak."""
    hi, lo = max(x, y), min(x, y)
    return (hi >= 6 and hi - lo >= 2) or (hi == 7 and lo == 6)


def _cl(x, lo=0.3, hi=0.97):
    return max(lo, min(hi, x))


def _match_state(m):
    """From ESPN's set-by-set games: finished sets, the set in progress, sets won, games, tiebreaks."""
    a_l, b_l = (m["sets"] + [[], []])[:2]
    done, cur = [], (0, 0)
    for i in range(max(len(a_l), len(b_l))):
        x = a_l[i] if i < len(a_l) else 0
        y = b_l[i] if i < len(b_l) else 0
        if _set_over(x, y):
            done.append((x, y))
        else:
            cur = (x, y)
            break
    return {"done": done, "cur": cur,
            "sw": (sum(1 for x, y in done if x > y), sum(1 for x, y in done if y > x)),
            "g_done": (sum(x for x, _ in done), sum(y for _, y in done)),
            "tbs": sum(1 for x, y in done if max(x, y) == 7 and min(x, y) == 6),
            "ns": len(done), "set1": (done[0][0] + done[0][1]) if done else None}


def _sim(st, hA, hB, tbA, best_of, n):
    """Plays out the rest of the match n times. Each result: (games A, games B, sets played, tiebreaks, set-1 games, sets A, sets B).
    The server is unknown, so it is picked at random and then alternates game by game."""
    need = best_of // 2 + 1
    out = []
    rnd = _rng.random
    for _ in range(n):
        sa, sb = st["sw"]
        ga, gb = st["g_done"]
        tbs, ns, s1 = st["tbs"], st["ns"], st["set1"]
        a, b = st["cur"]
        srv_a = rnd() < 0.5
        while sa < need and sb < need:
            while not _set_over(a, b):
                if a == 6 and b == 6:                     # tiebreak: counts as one game
                    if rnd() < tbA:
                        a += 1
                    else:
                        b += 1
                    tbs += 1
                    break
                if rnd() < (hA if srv_a else 1 - hB):
                    a += 1
                else:
                    b += 1
                srv_a = not srv_a
            ga += a
            gb += b
            ns += 1
            if s1 is None:
                s1 = a + b
            if a > b:
                sa += 1
            else:
                sb += 1
            a = b = 0
        out.append((ga, gb, ns, tbs, s1, sa, sb))
    return out


def _calibrate_hold(share, tg_line):
    """Picks the serve-hold rate that makes a fresh match land on PrizePicks' pregame Total Games line."""
    d = share - 0.5
    st0 = {"sw": (0, 0), "g_done": (0, 0), "tbs": 0, "ns": 0, "set1": None, "cur": (0, 0)}
    best = None
    for base in (0.58, 0.62, 0.66, 0.70, 0.74, 0.78, 0.82, 0.86):
        sims = _sim(st0, _cl(base + d), _cl(base - d), _cl(0.5 + d, 0.1, 0.9), T_BEST_OF, 400)
        tot = sorted(s[0] + s[1] for s in sims)
        err = abs(tot[len(tot) // 2] - tg_line)
        if best is None or err < best[0]:
            best = (err, base)
    return best[1]


def _tval(norm, stat):
    """Pregame value of a tennis line if saved, else the current board line, else None."""
    if not norm:
        return None
    key = ("TENNIS", norm, stat, None)
    v = _state["prior"].get(key)
    if v:
        return v
    ln = _state["tboard"].get(key)
    return ln["line"] if ln and ln["line"] > 0 else None


def _resolve(espn_name, by_norm, last_idx):
    """Matches an ESPN name to a PrizePicks name (exact, else same last name and first initial)."""
    n = _norm(espn_name)
    if n in by_norm:
        return n
    parts = n.split()
    if not parts:
        return None
    cands = [c for c in last_idx.get(parts[-1], []) if c[:1] == n[:1]]
    return cands[0] if len(cands) == 1 else None


def tennis_matches_live():
    out = []
    for tour, base in TENNIS_ESPN.items():
        try:
            ms = _tennis_matches(base)
        except Exception as e:
            print(f"tennis ESPN error ({tour}):", e)
            continue
        for m in ms:
            if m["state"] == "in" and len(m["names"]) == 2 and all(k == "athlete" for k in m["kinds"]):
                m["tour"] = tour
                out.append(m)
    return out


def _t_val(stat, side, sim, adj=0.0):
    """One simulated finish -> the final number for this stat, from one player's side."""
    ga, gb, ns, tbs, s1, sa, sb = sim
    x, y = (ga, gb) if side == 0 else (gb, ga)
    xs, ys = (sa, sb) if side == 0 else (sb, sa)
    if stat == "t_fant":
        return 10 + x - y + 3 * (xs - ys) + adj
    return {"t_games": ga + gb, "t_gw": x, "t_sets": ns, "t_tb": tbs, "t_set1": s1}[stat]


def _serve_adj(norm):
    """Fantasy points from aces (+0.5) and double faults (-0.5), from PrizePicks' Aces/Double Faults lines. None if no Aces line."""
    ac = _state["tboard"].get(("TENNIS", norm, "t_aces", None))
    if not ac or ac["line"] <= 0:
        return None
    df = _state["tboard"].get(("TENNIS", norm, "t_df", None))
    return 0.5 * ac["line"] - 0.5 * (df["line"] if df and df["line"] > 0 else 3.0)


def _t_eval(ln, side, st, sims, share, played, m):
    stat, L = ln["stat"], ln["line"]
    if L <= 0:
        return None
    cur = st["cur"]
    gA, gB = st["g_done"][0] + cur[0], st["g_done"][1] + cur[1]
    gX, gY = (gA, gB) if side == 0 else (gB, gA)
    sX, sY = st["sw"] if side == 0 else st["sw"][::-1]
    if stat == "t_set1" and st["ns"] > 0:
        return None                                        # 1st set is already settled
    c = {"t_games": played, "t_gw": gX, "t_sets": st["ns"] + 1,
         "t_tb": st["tbs"] + (1 if cur == (6, 6) else 0), "t_set1": cur[0] + cur[1],
         "t_fant": 10 + gX - gY + 3 * (sX - sY)}[stat]
    if stat != "t_fant" and c >= L:
        return None                                        # the over has already hit (or the under is dead)
    adj = _serve_adj(ln["norm"]) if stat == "t_fant" else 0.0
    vals = [_t_val(stat, side, s, T_SERVE_ADJ if adj is None else adj) for s in sims]
    n = len(vals)
    p_over = sum(1 for v in vals if v > L) / n
    p_under = sum(1 for v in vals if v < L) / n
    direction, prob = ("OVER", p_over) if p_over >= p_under else ("UNDER", p_under)
    need = T_MIN_PROB + (T_FANT_EXTRA if stat == "t_fant" else 0.0)
    if prob < need:
        return None
    proj = sum(vals) / n
    my_share = share if side == 0 else 1 - share
    notes = [f"Hits {direction} in {prob:.0%} of {n} simulated finishes",
             f"Has won {gX} of {played} games; strength estimate {my_share:.0%} of games from here"]
    prior = _state["prior"].get(("TENNIS", ln["norm"], stat, None))
    moved = (L - prior) if prior else 0.0
    if abs(moved) >= 0.01:
        notes.append(f"PrizePicks line has moved {moved:+g} since before the match")
    if stat == "t_fant":
        notes.append("Aces and double faults (±0.5 each) aren't in ESPN's live data, so a bigger edge is required")
    if direction == "OVER":
        notes.append("If a player retires, the stats so far stand: a risk for overs")
    if ln["live"]:
        notes.append("PrizePicks live line")
    score = " ".join(f"{x}-{y}" for x, y in st["done"] + ([cur] if cur != (0, 0) or not st["done"] else []))
    name_a, name_b = m["names"]
    shape = "lopsided" if abs(gA - gB) >= 4 else "tight"
    return {"direction": direction, "proj": proj, "gap": proj - L, "cur": c, "line": L, "margin": gX - gY,
            "team": {}, "opp": {}, "notes": notes, "factor": 1.0, "moved": moved, "prob": prob,
            "strength": prob / need, "shape": f"TENNIS-{shape}", "game": "T" + m["id"],
            "score_txt": f"{name_a} {score} {name_b} • set {st['ns'] + 1}"}


def tennis_cycle(raw):
    """Finds juicy tennis picks in live singles matches. Returns [(key, line, event, alert)], at most one per match."""
    tb = parse_tennis(raw)
    _state["tboard"] = tb
    matches = tennis_matches_live()
    live_norms = {_norm(n) for m in matches for n in m["names"]}
    update_priors(tb, {"TENNIS": live_norms})
    if matches:
        _state["tlive"] = time.time()
    by_norm, last_idx = {}, {}
    for key, ln in tb.items():
        by_norm.setdefault(ln["norm"], []).append((key, ln))
    for n in by_norm:
        if n.split():
            last_idx.setdefault(n.split()[-1], []).append(n)
    fresh = []
    for m in matches:
        pa, pb = _resolve(m["names"][0], by_norm, last_idx), _resolve(m["names"][1], by_norm, last_idx)
        if not pa and not pb:
            continue
        st = _match_state(m)
        cur = st["cur"]
        gA, gB = st["g_done"][0] + cur[0], st["g_done"][1] + cur[1]
        played = gA + gB
        if played < T_MIN_GAMES:
            continue
        gwA, gwB = _tval(pa, "t_gw"), _tval(pb, "t_gw")
        tg = _tval(pa, "t_games") or _tval(pb, "t_games")
        if gwA and gwB:
            share0 = gwA / (gwA + gwB)
        elif gwA and tg:
            share0 = gwA / tg
        elif gwB and tg:
            share0 = 1 - gwB / tg
        else:
            share0 = 0.5
        share0 = _cl(share0, 0.3, 0.7)
        w = played / (played + T_PRIOR_GAMES)
        share = _cl((1 - w) * share0 + w * (gA / played), 0.2, 0.8)
        mid = m["id"]
        if tg:
            if mid not in _state["tcal"]:
                _state["tcal"][mid] = _calibrate_hold(share0, tg)
            base = _state["tcal"][mid]
        else:
            base = T_HOLD.get(m["tour"], 0.75)
        d = share - 0.5
        sims = _sim(st, _cl(base + d), _cl(base - d), _cl(0.5 + d, 0.1, 0.9), T_BEST_OF, T_SIMS)
        event = {"id": "T" + mid, "name": f"{m['names'][0]} vs {m['names'][1]} ({m['tournament']})",
                 "f": min(1.0, played / (tg or 22.0)), "period": st["ns"] + 1, "clock_txt": ""}
        best, done_stats = None, set()
        for side, pn in ((0, pa), (1, pb)):
            if not pn:
                continue
            for key, ln in by_norm.get(pn, []):
                if ln["stat"] in T_NO_LIVE or (ln["stat"] in T_MATCH_LEVEL and ln["stat"] in done_stats):
                    continue
                if ln["live"] and key not in _state["t_logged"] and len(_state["t_logged"]) < 20:
                    _state["t_logged"].add(key)
                    print(f"TENNIS LIVE ROW: {ln['name']} {ln['stat']} live line {ln['line']:g} | flags {ln['flags']} | "
                          f"games so far {played}")
                a = _t_eval(ln, side, st, sims, share, played, m)
                if not a:
                    continue
                if ln["stat"] in T_MATCH_LEVEL:
                    done_stats.add(ln["stat"])
                if best is None or a["prob"] > best[3]["prob"]:
                    best = (key, ln, event, a)
        if best:
            fresh.append(best)
    return fresh


# ====================== PREGAME LINE CHECK (v1.8) ======================
def _start_txt(dt):
    try:
        return dt.astimezone(_ET).strftime("%a %-I:%M %p") + (" ET" if _ET is not timezone.utc else " UTC")
    except Exception:
        return ""


def _upcoming(lines):
    """Start time if none of these lines is live and the match starts within the window, else None."""
    if any(ln["live"] for ln in lines):
        return None
    starts = [ln["start"] for ln in lines if ln["start"]]
    if not starts:
        return None
    st = min(starts)
    secs = (st - _now()).total_seconds()
    return st if 0 < secs <= PRE_WINDOW_H * 3600 else None


def tennis_pregame(tb):
    """Tennis: rebuilds each upcoming match from PrizePicks' own Games Won and Total Games lines, then checks
    Games Won and Fantasy Score against it. Returns at most one pick per match."""
    groups = {}
    for key, ln in tb.items():
        if ln.get("gid"):
            groups.setdefault(ln["gid"], {}).setdefault(ln["norm"], {})[ln["stat"]] = (key, ln)
    out = []
    for gid, pl in groups.items():
        if len(pl) != 2:
            continue
        start = _upcoming([ln for d in pl.values() for _, ln in d.values()])
        if not start:
            continue
        (pa, A), (pb, B) = sorted(pl.items())

        def L(d, stat):
            return d[stat][1]["line"] if stat in d and d[stat][1]["line"] > 0 else None
        gwA, gwB, tg = L(A, "t_gw"), L(B, "t_gw"), (L(A, "t_games") or L(B, "t_games"))
        if not (gwA and gwB and tg):
            continue
        share = _cl(gwA / (gwA + gwB), 0.25, 0.75)
        ck = "P" + str(gid)
        if ck not in _state["tcal"]:
            _state["tcal"][ck] = (tg, gwA, gwB, _calibrate_hold(share, tg))
        if _state["tcal"][ck][:3] != (tg, gwA, gwB):               # lines changed: recalibrate
            _state["tcal"][ck] = (tg, gwA, gwB, _calibrate_hold(share, tg))
        base = _state["tcal"][ck][3]
        d = share - 0.5
        st0 = {"sw": (0, 0), "g_done": (0, 0), "tbs": 0, "ns": 0, "set1": None, "cur": (0, 0)}
        sims = _sim(st0, _cl(base + d), _cl(base - d), _cl(0.5 + d, 0.1, 0.9), T_BEST_OF, T_SIMS)
        name_a, name_b = A[next(iter(A))][1]["name"], B[next(iter(B))][1]["name"]
        best = None
        for side, D in ((0, A), (1, B)):
            for stat in ("t_gw", "t_fant"):
                if stat not in D:
                    continue
                key, ln = D[stat]
                adj = 0.0
                if stat == "t_fant":
                    adj = _serve_adj(ln["norm"])
                    if adj is None:
                        continue                                   # no Aces line: can't price the serve part fairly
                Lv = ln["line"]
                vals = [_t_val(stat, side, s, adj) for s in sims]
                n = len(vals)
                p_over = sum(1 for v in vals if v > Lv) / n
                p_under = sum(1 for v in vals if v < Lv) / n
                direction, prob = ("OVER", p_over) if p_over >= p_under else ("UNDER", p_under)
                need = PRE_MIN_PROB + (T_FANT_EXTRA if stat == "t_fant" else 0.0)
                if prob < need:
                    continue
                proj = sum(vals) / n
                notes = [f"Hits {direction} in {prob:.0%} of {n} simulated matches built from PrizePicks' other lines"]
                if stat == "t_gw":
                    notes.append(f"Games Won lines: {name_a} {gwA:g} + {name_b} {gwB:g} = {gwA + gwB:g}, "
                                 f"but Total Games is {tg:g}")
                else:
                    notes.append(f"Fantasy = 10 + games won − games lost + 3 × (sets won − sets lost) + aces/double faults "
                                 f"({adj:+.1f} from the Aces line)")
                notes.append("Pregame check: no live data yet, so injuries and late news aren't known")
                a = {"direction": direction, "proj": proj, "gap": proj - Lv, "cur": 0, "line": Lv, "margin": 0,
                     "team": {}, "opp": {}, "notes": notes, "factor": 1.0, "moved": 0.0, "prob": prob,
                     "strength": prob / need, "shape": "PRE-pregame", "game": "TP" + str(gid), "pre": True,
                     "score_txt": f"Starts {_start_txt(start)}"}
                ev = {"id": "TP" + str(gid), "name": f"{name_a} vs {name_b}", "f": 0.0, "period": 0, "clock_txt": ""}
                if best is None or prob > best[3]["prob"]:
                    best = (key, ln, ev, a)
        if best:
            out.append(best)
    return out


NBA_COMBOS = (("pra", ("pts", "reb", "ast")), ("pr", ("pts", "reb")), ("pa", ("pts", "ast")), ("ra", ("reb", "ast")))


def nba_pregame(board):
    """NBA: a combo line (PRA, Pts+Rebs, Pts+Asts, Rebs+Asts) that is far from the sum of the player's single lines."""
    players = {}
    for key, ln in board.items():
        if ln["sport"] == "NBA" and ln.get("half") is None and ln["line"] > 0:
            players.setdefault(ln["norm"], {})[ln["stat"]] = (key, ln)
    out = []
    for norm, D in players.items():
        start = _upcoming([ln for _, ln in D.values()])
        if not start:
            continue
        best = None
        for combo, parts in NBA_COMBOS:
            if combo not in D or not all(p in D for p in parts):
                continue
            key, ln = D[combo]
            total = sum(D[p][1]["line"] for p in parts)
            gap = total - ln["line"]
            need = max(PRE_NBA_GAP, 0.08 * ln["line"])
            if abs(gap) < need:
                continue
            direction = "OVER" if gap > 0 else "UNDER"
            parts_txt = " + ".join(f"{STAT_LABEL[p]} {D[p][1]['line']:g}" for p in parts)
            notes = [f"PrizePicks' own lines: {parts_txt} = {total:g}, but {STAT_LABEL[combo]} is {ln['line']:g}",
                     "Each line is a middle guess, so the sum is a fair check but not exact",
                     "Pregame check: injuries and minutes news aren't known"]
            a = {"direction": direction, "proj": total, "gap": gap, "cur": 0, "line": ln["line"], "margin": 0,
                 "team": {}, "opp": {}, "notes": notes, "factor": 1.0, "moved": 0.0, "prob": 0.0,
                 "strength": abs(gap) / need, "shape": "PRE-pregame", "game": "NP" + str(ln.get("gid") or norm),
                 "pre": True, "score_txt": f"{ln['opp'] or 'Game'} • starts {_start_txt(start)}"}
            ev = {"id": a["game"], "name": f"{ln['name']} ({ln['team'] or 'NBA'})", "f": 0.0, "period": 0, "clock_txt": ""}
            if best is None or a["strength"] > best[3]["strength"]:
                best = (key, ln, ev, a)
        if best:
            out.append(best)
    return out


def _pre_room():
    now = time.time()
    _state["pre_times"] = [t for t in _state["pre_times"] if now - t < 3600]
    return len(_state["pre_times"]) < PRE_MAX_LEGS


# ====================== ALERTS ======================
def _room():
    now = time.time()
    _state["sent"] = [t for t in _state["sent"] if now - t < 3600]
    return len(_state["sent"]) < MAX_PER_HOUR


def _can_alert(key, direction):
    if not _room():
        return False
    last = _state["alerts"].get(key)
    if last and last[1] == direction and time.time() - last[0] < COOLDOWN:
        return False
    return True


def _emoji(a):
    return "🔥" if a["direction"] == "OVER" else "🧊"


def _score_text(event, a):
    if a.get("score_txt"):
        return a["score_txt"]
    t, o = a["team"], a["opp"]
    return f"{t['abbr'] or t['name']} {t['score']} - {o['score']} {o['abbr'] or o['name']} • {'P' if a.get('sport') == 'NHL' else 'Q'}{event['period']} {event['clock_txt']}"


def _leg_text(line, a):
    if a.get("pre"):
        return (f"**{line['name']}** — {stat_label(line)} **{line['line']:g}** → **{a['direction']}**\n"
                f"Fair value from the other lines **{a['proj']:.1f}** (gap {a['gap']:+.1f})\n"
                + "\n".join(f"• {n}" for n in a["notes"]))
    return (f"**{line['name']}** — {stat_label(line)} **{line['line']:g}** → **{a['direction']}**\n"
            f"Now **{a['cur']:g}** • Projected **{a['proj']:.1f}** (gap {a['gap']:+.1f})\n"
            + "\n".join(f"• {n}" for n in a["notes"]))


def _mark_sent(key, a):
    _state["alerts"][key] = (time.time(), a["direction"])
    _state["pending"].pop(key, None)
    if a.get("pre"):
        _state["pre_sent"][key] = a["line"]                   # posted once at this line; again only if the line changes
        _state["pre_times"].append(time.time())


def send_single(cand, label="no pair found"):
    key, line, event, a = cand
    _mark_sent(key, a)
    _state["sent"].append(time.time())
    footer = "Model v1.9.1 • alert only • projection, not a guarantee" + (f" • {label}" if label else "")
    tag = " PREGAME" if a.get("pre") else (" LIVE" if line["live"] else "")
    embed = {"title": f"{_emoji(a)} Heartbeat — {line['sport']}{tag} — LEAN {a['direction']}",
             "description": _leg_text(line, a), "color": 3066993 if a["direction"] == "OVER" else 3447003,
             "fields": [{"name": event["name"], "value": _score_text(event, a), "inline": False}],
             "footer": {"text": footer}}
    _post({"embeds": [embed]})
    print(f"ALERT(single) {line['sport']} {line['name']} {line.get('half') or 'full'} {line['stat']} line {line['line']} proj {a['proj']:.1f} "
          f"{a['direction']} cur {a['cur']} margin {a['margin']} f={event['f']:.2f}")


def send_pair(c1, c2):
    fields, legs = [], []
    for key, line, event, a in (c1, c2):
        _mark_sent(key, a)
        fields.append({"name": f"{_emoji(a)} {line['name']} — {stat_label(line)} {a['direction']} {line['line']:g}",
                       "value": f"{_leg_text(line, a)}\n_{event['name']} • {_score_text(event, a)}_"[:1000], "inline": False})
        legs.append(f"{line['sport']} {line['name']} {line['stat']} {a['direction']} {line['line']:g}")
    _state["sent"].append(time.time())
    same_shape = c1[3]["shape"] == c2[3]["shape"]
    pre = bool(c1[3].get("pre"))
    tag = "PREGAME " if pre else ("LIVE " if (c1[1]["live"] or c2[1]["live"]) else "")
    embed = {"title": "💓 Heartbeat — " + tag + "PAIR — lock these two",
             "description": "Two different games" + (f", both {c1[3]['shape'].split('-')[1]} games" if same_shape and not pre else "") +
                            ". Lines move fast, so check both are still available before you lock.",
             "color": 15844367, "fields": fields,
             "footer": {"text": "Model v1.9.1 • alert only • projections, not guarantees"}}
    _post({"embeds": [embed]})
    print(f"ALERT(pair) {' + '.join(legs)}")


def process_candidates(fresh):
    """fresh = [(key, line, event, alert)] that qualify on THIS cycle's data. Pairs them across different games."""
    now = time.time()
    live_keys = {c[0] for c in fresh}
    for k in list(_state["pending"]):                 # a pick that stopped qualifying is dropped
        if k not in live_keys:
            del _state["pending"][k]
    ready = []
    for c in fresh:
        if c[3].get("pre"):
            if _state["pre_sent"].get(c[0]) == c[3]["line"] or not _pre_room():
                continue                               # already posted at this line, or the pregame hourly cap is reached
        if not _can_alert(c[0], c[3]["direction"]):
            continue
        _state["pending"].setdefault(c[0], now)
        ready.append(c)
    posted = 0
    if not PAIR_MODE:
        for c in ready:
            if not _room():
                break
            send_single(c, label="")
            posted += 1
        return posted
    ready.sort(key=lambda c: -c[3]["strength"])
    used = set()
    for i, c1 in enumerate(ready):
        if c1[0] in used:
            continue
        best = None
        for c2 in ready[i + 1:]:
            if c2[0] in used or c2[3]["game"] == c1[3]["game"]:      # NEVER two legs from the same game
                continue
            if bool(c2[3].get("pre")) != bool(c1[3].get("pre")):     # pregame picks pair with pregame, live with live
                continue
            same = c1[3]["shape"] == c2[3]["shape"]
            if PAIR_REQUIRE_SAME_SHAPE and not same:
                continue
            score = c2[3]["strength"] + (PAIR_SAME_SHAPE_BONUS if same else 0.0)
            if best is None or score > best[0]:
                best = (score, c2)
        if best:
            if not _room():                            # hourly cap reached: stop posting this cycle
                break
            send_pair(c1, best[1])
            used |= {c1[0], best[1][0]}
            posted += 1
    for c in ready:                                    # nobody to pair with: post alone once it has waited long enough
        if c[0] not in used and now - _state["pending"].get(c[0], now) >= PAIR_WAIT:
            if not _room():
                break
            send_single(c)
            posted += 1
    return posted


def update_snapshot(ev, box):
    """Saves every NBA player's box score at halftime. The 2nd half = current box minus this snapshot."""
    k = ("NBA", ev["id"])
    if ev.get("halftime"):
        _state["snap"][k] = {n: dict(v) for n, v in box.items()}          # keeps refreshing until the 2nd half starts
    elif ev["period"] == 3 and ev["clock"] >= PERIOD_SECONDS["NBA"] - 120 and k not in _state["snap"]:
        _state["snap"][k] = {n: dict(v) for n, v in box.items()}          # bot woke up in the first 2 min of the 3rd: close enough


def get_prior(key, ln):
    """(prior, estimated?). Half lines with no saved line fall back to a share of the saved full-game line."""
    p = _state["prior"].get(key)
    if p is not None:
        return p, False
    if ln.get("half") in (1, 2):
        fp = _state["prior"].get((ln["sport"], ln["norm"], ln["stat"], None))
        if fp:
            return fp * HALF_SHARE[ln["half"]], True
    return None, False


def discovery_report(board, live):
    nfl = sum(1 for k in board if k[0] == "NFL")
    nba = sum(1 for k in board if k[0] == "NBA")
    nhl = sum(1 for k in board if k[0] == "NHL")
    flags = {k: sorted(v)[:6] for k, v in _state["flags_seen"].items()}
    lines = [f"Sports tracked: {', '.join(SPORTS)}" + (", TENNIS" if TENNIS_ON else ""),
             f"PrizePicks lines I can model right now: NFL {nfl}, NBA {nba}, NHL {nhl}"
             + (f", tennis {len(_state['tboard'])}" if TENNIS_ON else ""),
             f"Live games right now: {sum(len(v) for v in live.values())}",
             f"PrizePicks attribute names seen: {', '.join(sorted(_state['keys_seen'])) or 'none yet'}",
             f"Status/odds flags seen: {flags or 'none'}"]
    lines.append("Tennis alerts: " + (f"ON (only picks that hit in {T_MIN_PROB:.0%}+ of simulations)" if TENNIS_ON else "off"))
    lines.append("Pregame line check: " + (f"ON (max {PRE_MAX_LEGS} picks an hour)" if PREGAME_ON else "off"))
    lines.append("Tennis discovery: " + ("ON (logging only)" if TENNIS_DISCOVERY else "off"))
    nba_half = sum(1 for k in board if k[0] == "NBA" and k[3])
    lines.append(f"NBA half-game lines I can model: {nba_half}")
    nm = sorted(_state["names"].get("NBA", set()))
    if nm:
        lines.append("NBA stat names on the board: " + ", ".join(nm)[:600])
    print("DISCOVERY:", " | ".join(lines))
    if DISCOVERY_POST:
        _post({"embeds": [{"title": "💓 Heartbeat online", "description": "\n".join(f"• {x}" for x in lines)[:4000],
                           "color": 3066993}]})


# ====================== TENNIS DISCOVERY (v1.6) ======================
def _tennis_pp_summary(raw):
    """PrizePicks tennis lines: how many, which stat names, a few examples."""
    players, leagues = {}, {}
    for inc in raw.get("included", []):
        a = inc.get("attributes", {})
        if inc.get("type") == "new_player":
            players[inc["id"]] = a.get("name", "")
        elif inc.get("type") == "league":
            leagues[inc["id"]] = (a.get("name") or "").upper()
    names, who, rows = {}, set(), []
    for item in raw.get("data", []):
        try:
            a = item["attributes"]
            rel = item.get("relationships", {})
            lg = leagues.get((rel.get("league", {}).get("data") or {}).get("id"), "")
            if not _is_tennis_league(lg):
                continue
            stat = str(a.get("stat_display_name") or a.get("stat_type") or "").strip()
            nm = players.get((rel.get("new_player", {}).get("data") or {}).get("id"), "")
            names[stat] = names.get(stat, 0) + 1
            who.add(nm)
            if len(rows) < 6 and stat not in [r[1] for r in rows]:
                rows.append((nm, stat, a.get("line_score"), a.get("odds_type")))
        except Exception:
            continue
    return names, who, rows


def _tennis_matches(base):
    """Every match on ESPN's scoreboard for one tour, as simple dicts."""
    data = _get_json(base + "/scoreboard")
    out = []
    for ev in data.get("events", []) or []:
        comps = list(ev.get("competitions") or [])
        for g in ev.get("groupings") or []:
            comps += g.get("competitions") or []
        for c in comps:
            st = ((c.get("status") or {}).get("type") or {})
            names, sets, kinds = [], [], []
            for p in c.get("competitors") or []:
                names.append((p.get("athlete") or {}).get("displayName") or p.get("displayName") or "?")
                sets.append([int(_num(x.get("value"))) for x in (p.get("linescores") or [])])
                kinds.append(p.get("type") or "")
            out.append({"id": str(c.get("id") or ev.get("id") or ""), "tournament": ev.get("name") or "", "state": st.get("state") or "",
                        "detail": st.get("detail") or st.get("shortDetail") or "", "names": names, "sets": sets,
                        "kinds": kinds, "raw": c})
    return out


def tennis_discovery(raw):
    """Logs (and posts a short note to Discord) what tennis data exists. Sends NO picks. Never raises."""
    t = _state["tennis"]
    if time.time() - t["last"] < TENNIS_POLL:
        return
    t["last"] = time.time()
    # 1) PrizePicks tennis board
    try:
        names, who, rows = _tennis_pp_summary(raw)
        sig = tuple(sorted(names))
        if sig != t["pp_sig"]:
            t["pp_sig"] = sig
            line = "; ".join(f"{k} x{v}" for k, v in sorted(names.items()))
            ex = " | ".join(f"{r[0]} {r[1]} {r[2]} ({r[3]})" for r in rows)
            print(f"TENNIS BOARD: {len(who)} players. Stats: {line or 'none on the board right now'}")
            if ex:
                print("TENNIS EXAMPLES:", ex)
            if TENNIS_POST and DISCOVERY_POST and t["posts"] < 4:
                t["posts"] += 1
                _post({"embeds": [{"title": "🎾 Heartbeat — tennis discovery (no picks)", "color": 3066993,
                                   "description": f"• PrizePicks tennis: {len(who)} players\n• Stat names: {line or 'none on the board right now'}"[:1500]
                                                  + (f"\n• Examples: {ex}"[:600] if ex else "")}]})
    except Exception as e:
        print("tennis board error:", e)
    # 2) ESPN live tennis
    for tour, base in TENNIS_ESPN.items():
        try:
            ms = _tennis_matches(base)
        except Exception as e:
            print(f"tennis ESPN error ({tour}):", e)
            continue
        live = [m for m in ms if m["state"] == "in"]
        print(f"TENNIS ESPN {tour}: {len(ms)} matches on the scoreboard, {len(live)} live")
        for m in live[:3]:
            if m["id"] in t["matches"] or len(t["matches"]) >= 6:
                continue
            t["matches"].add(m["id"])
            score = " vs ".join(f"{n} {'-'.join(str(x) for x in sx)}" for n, sx in zip(m["names"], m["sets"]))
            print(f"TENNIS LIVE MATCH ({tour}): {m['tournament']} | {score} | {m['detail']}")
            print("TENNIS MATCH KEYS:", sorted(m["raw"].keys()))
            comp0 = (m["raw"].get("competitors") or [{}])[0]
            print("TENNIS PLAYER KEYS:", sorted(comp0.keys()))
            print("TENNIS PLAYER STATS:", str(comp0.get("statistics"))[:600])
            extra = ""
            try:                                           # does ESPN give a per-match summary with player stats?
                sm = _get_json(f"{base}/summary?event={m['id']}")
                extra = f"summary keys: {sorted(sm.keys())}"
                print("TENNIS SUMMARY KEYS:", sorted(sm.keys()))
                print("TENNIS SUMMARY STATS:", str(sm.get("boxscore") or sm.get("statistics"))[:600])
            except Exception as e:
                extra = "no match summary"
                print("TENNIS SUMMARY: none (", e, ")")
            if TENNIS_POST and DISCOVERY_POST and t["posts"] < 4:
                t["posts"] += 1
                _post({"embeds": [{"title": "🎾 Heartbeat — live tennis seen (no picks)", "color": 3447003,
                                   "description": f"• {tour}: {m['tournament']}\n• {score}\n• {m['detail']}\n• Per-player stats on the match: "
                                                  f"{'yes' if comp0.get('statistics') else 'not on the scoreboard'}\n• {extra}"[:1500]}]})


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
        raw = fetch_board()
        board = parse_board(raw)
    except Exception as e:
        print("PrizePicks error:", e)
        return
    _state["board"] = board
    if TENNIS_DISCOVERY:
        try:
            tennis_discovery(raw)
        except Exception as e:                      # tennis can never stop NBA/NFL
            print("tennis error:", e)
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
                if sport == "NBA":
                    update_snapshot(ev, boxes[(sport, ev["id"])])
            except Exception as e:
                print(f"ESPN box score error ({sport} {ev['id']}):", e)
    update_priors(board, live_names)
    if time.time() - _state.get("diag_t", 0) > 600 and any(live.values()):     # every 10 min while games are live
        _state["diag_t"] = time.time()
        for sport in SPORTS:
            names = live_names.get(sport, set())
            mine = [k for k in board if k[0] == sport and k[1] in names]
            with_prior = sum(1 for k in mine if get_prior(k, board[k])[0] is not None)
            print(f"LIVE CHECK {sport}: {len(live.get(sport, []))} live games "
                  f"({', '.join(ev['name'] for ev in live.get(sport, []))[:300]}) | "
                  f"{len(mine)} PrizePicks lines for players in them | {with_prior} with a saved pregame line")
    # 2b) tennis (v1.7): its own scoreboard and model; an error here never stops NFL/NBA
    t_fresh = []
    if TENNIS_ON:
        try:
            t_fresh = tennis_cycle(raw)
        except Exception as e:
            print("tennis error:", e)
    n_names = sum(len(v) for v in _state["names"].values())
    if n_names != _state["names_printed"]:                 # new PrizePicks stat names showed up: log them so we can match them
        _state["names_printed"] = n_names
        for sp_, nm_ in _state["names"].items():
            print(f"STAT NAMES {sp_}:", " | ".join(sorted(nm_))[:1500])
    if first:
        discovery_report(board, live)
    # 3) project every tracked line that belongs to a live game
    sent = checked = 0
    fresh = []
    for key, ln in board.items():
        sport = ln["sport"]
        for ev in live.get(sport, []):
            box = boxes.get((sport, ev["id"]))
            if not box or ln["norm"] not in box:
                continue
            prior, est = get_prior(key, ln)
            ln["prior_est"] = est
            if prior is None:
                if not ALLOW_NO_PRIOR:
                    continue
                prior = ln["line"]
            checked += 1
            if ln["live"] and key not in _state["live_logged"]:     # log the first live rows so we can see what they mean
                _state["live_logged"].add(key)
                if len(_state["live_logged"]) <= 40:
                    print(f"LIVE ROW: {ln['name']} {ln.get('half') or 'full'} {ln['stat']} live line {ln['line']:g} | pregame line {prior:g} | "
                          f"he has {box[ln['norm']].get(ln['stat'])} so far | game {ev['f']:.0%} done | flags {ln['flags']}")
            a = evaluate(ln, ev, box, prior)
            if a:
                fresh.append((key, ln, ev, a))
            break
    fresh += t_fresh
    pre_fresh = []
    if PREGAME_ON:
        try:
            pre_fresh = nba_pregame(board) + (tennis_pregame(_state["tboard"]) if TENNIS_ON else [])
        except Exception as e:                      # the pregame check can never stop live alerts
            print("pregame error:", e)
    fresh += pre_fresh
    sent = process_candidates(fresh)
    n_live = sum(len(v) for v in live.values())
    if n_live or first or t_fresh or sent:
        print(f"cycle: {n_live} live games, {len(board)} lines tracked, {checked} projected, "
              f"{len(t_fresh)} tennis picks, {len(pre_fresh)} pregame picks, {sent} alerts")


def main():
    if not WEBHOOK_URL:
        print("WARNING: WEBHOOK_URL is not set. Alerts will only print in the logs.")
    print(f"Heartbeat v1.9.1 starting. Sports: {SPORTS}{' + TENNIS' if TENNIS_ON else ''}. "
          f"PrizePicks every {PP_POLL:.0f}s, ESPN every {ESPN_POLL:.0f}s.")
    first = True
    while True:
        try:
            cycle(first)
            first = False
        except Exception as e:
            print("cycle error:", e)
        # poll fast only while games (or tennis matches) are live; otherwise relax
        any_live = (any(time.time() - v[0] < 120 for v in _state["summ"].values())
                    or time.time() - _state["tlive"] < 120)
        time.sleep(min(PP_POLL, ESPN_POLL) if any_live else max(PP_POLL, 60))


if __name__ == "__main__":
    main()
