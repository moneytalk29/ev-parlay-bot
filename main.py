"""Heartbeat v3.0: live NFL + NBA + NHL + TENNIS scanner for PrizePicks lines, plus a pregame line check. ALERT ONLY, it never places bets.

What it does
  1. Reads the PrizePicks board (PrizePicks' public feed) for NFL, NBA, NHL, tennis and esports player lines.
  2. v3.0: LIVE picks start from PrizePicks' CURRENT live line (it already includes the score), not the line from
     before kickoff. The bot only posts when the player's pace and the score both push past that line.
     (The pregame channel still reads pregame lines, because that's what those picks are about.)
     LIVE NFL posts only QB Pass Yards and Pass Attempts (HB_NFL_LIVE_STATS). Rushing and receiving are a coin flip.
     CS2 PREGAME: single "MAPS 1-2 Kills" / "MAP 1 Kills" lines from the player's kills per round (bo3.gg, free).
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
     v3.0: picks go out ONLY as PAIRS (two legs from DIFFERENT games, never the same game), live and pregame.
     A pick with no partner is not posted. CAUTION picks (against the score) are not posted.

Honest limits (read these)
  - The script adjustments below are STARTING GUESSES, not proven numbers. Every alert is logged so they can be
    checked and tuned against real results. Treat the first weeks as testing.
  - ESPN's public data is unofficial and can change or lag a few seconds.
  - v3.0: a restart during a game no longer loses picks (no saved pregame line is needed for live picks).
  - It does not know about injuries, a backup QB coming in, or weather.
  - Tennis: it does not know who is serving, and it assumes best-of-3 sets (men's Grand Slams are best-of-5:
    set HB_TENNIS_BEST_OF=5 for those weeks). If a player retires, PrizePicks keeps the stats so far (risk for overs).
Everything is a Railway variable. Only WEBHOOK_URL is required.
"""
import gc
import json
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
# v2.0: pregame picks go to their own Discord channel when this is set (otherwise to WEBHOOK_URL, like before)
PREGAME_WEBHOOK_URL = os.getenv("PREGAME_WEBHOOK_URL")
ESPORTS_WEBHOOK_URL = os.getenv("ESPORTS_WEBHOOK_URL") or next(   # v3.0: also accepts a near-miss name like ESPORT_WEBHOOK
    (v.strip() for k, v in os.environ.items() if k.strip().upper().startswith("ESPORT") and "WEBHOOK" in k.upper()
     and v.strip().startswith("http")), None)   # v3.0: esports picks go to their own channel when this is set
SPORTS = [s.strip().upper() for s in os.getenv("HB_SPORTS", "NFL,NBA,NHL").split(",") if s.strip()]
PP_URL = os.getenv("PP_URL", "https://partner-api.prizepicks.com/projections?per_page=1000&single_stat=true&game_mode=pickem")
# v1.9.2: PrizePicks takes pregame lines down at kickoff. In-game lines may only come from a second feed (in_game=true).
# Its rows count as live only once their game has started, so pregame lines are never mistaken for live ones.
PP_LIVE_URL = os.getenv("PP_LIVE_URL", PP_URL + "&in_game=true")
# v1.9.4: the in-game feed holds every sport (40,000+ rows) and ran the service out of memory. It is now asked for ONE
# league at a time (league_id), only for leagues with live games, and everything else is thrown away right after reading.
PP_LIVE_MAX_AGE = float(os.getenv("HB_PP_LIVE_MAX_AGE", "240"))       # ignore an in-game copy older than 4 minutes
# v1.9.4: saved pregame lines are written to a file so a restart doesn't wipe them (kept 36 hours).
# v1.9.7: if a Railway Volume is attached, Railway tells the bot where (RAILWAY_VOLUME_MOUNT_PATH) and the file goes there,
# so it survives new deploys too. Without a Volume the file is lost on every deploy.
STATE_FILE = os.getenv("HB_STATE_FILE") or os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "/tmp", "heartbeat_state.json")
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
PASS_TRAIL, PASS_TRAIL_CAP = float(os.getenv("HB_PASS_TRAIL", "0.16")), float(os.getenv("HB_PASS_TRAIL_CAP", "0.40"))
# v2.3: a trailing team passes on ~70-80% of plays (normal ~58%), so its passing volume is boosted by about
# +25% when down 10 in the 2nd half and up to +40% when down 14+ late.
# v2.2: a team down 10+ from late in the 2nd quarter on throws on almost every play. Never post a passing/receiving
# UNDER for that team (not even as CAUTION).
TRAIL_BLOCK_MARGIN = float(os.getenv("HB_TRAIL_BLOCK_MARGIN", "10"))
TRAIL_BLOCK_FROM = float(os.getenv("HB_TRAIL_BLOCK_FROM", "0.25"))     # v2.9: from the start of the 2nd quarter
# v2.9: NFL picks that go AGAINST the game script are not posted (not even as CAUTION) when:
#   - the score gap is 10+ from the 2nd quarter on (any stat), or
#   - the gap is 7+ and it's a VOLUME stat (attempts, completions, catches, carries), which follow the script closely.
NFL_NO_AGAINST_MARGIN = float(os.getenv("HB_NFL_NO_AGAINST_MARGIN", "10"))
NFL_NO_AGAINST_VOLUME_MARGIN = float(os.getenv("HB_NFL_NO_AGAINST_VOLUME_MARGIN", "7"))
NFL_VOLUME = ("pass_att", "pass_cmp", "rec_cnt", "rush_att")
# v3.0: when the live line has moved this much (or 5%) since pregame, the bot never picks the other way.
# v3.0: live picks no longer use the line from before kickoff. The baseline is PrizePicks' current live line,
# which already includes the score. Set HB_USE_PREGAME_LINE=true to go back to the old way.
# v3.0: live NFL picks only on these stats (QB volume is the most predictable). Empty = all stats.
NFL_LIVE_STATS = tuple(x.strip() for x in os.getenv("HB_NFL_LIVE_STATS", "pass_yds,pass_att").split(",") if x.strip())
USE_PREGAME_LINE = os.getenv("HB_USE_PREGAME_LINE", "false").lower() == "true"
NFL_MOVE_MIN = float(os.getenv("HB_NFL_MOVE_MIN", "1.5"))
PASS_LEAD, PASS_LEAD_CAP = float(os.getenv("HB_PASS_LEAD", "0.08")), float(os.getenv("HB_PASS_LEAD_CAP", "0.25"))
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

# v1.9.8: picks where the player's pace and the game script disagree are posted alone with a CAUTION tag.
# HB_CAUTION=false hides them completely.
CAUTION_ON = os.getenv("HB_CAUTION", "true").lower() == "true"
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
            "receptions": ("rec_cnt", "rec"),
            # v1.9.5: combo lines, used only by the pregame check
            "pass yds": ("pass_yds", "pass"), "rush yds": ("rush_yds", "rush"), "receiving yds": ("rec_yds", "rec"),
            "rush+rec yds": ("rr_yds", "combo"), "rush + rec yds": ("rr_yds", "combo"),
            "rushing+receiving yards": ("rr_yds", "combo"), "pass+rush yds": ("pq_yds", "combo"),
            "pass + rush yds": ("pq_yds", "combo"), "passing+rushing yards": ("pq_yds", "combo")},
    "NBA": {"points": ("pts", "nba"), "rebounds": ("reb", "nba"), "assists": ("ast", "nba"),
            "3-pt made": ("fg3", "nba"), "3-pointers made": ("fg3", "nba"), "three pointers made": ("fg3", "nba"),
            "3pt made": ("fg3", "nba"), "3ptm": ("fg3", "nba"), "3pm": ("fg3", "nba"),
            "pts+rebs+asts": ("pra", "nba"), "points+rebounds+assists": ("pra", "nba"), "pra": ("pra", "nba"),
            "fantasy score": ("fant", "nba"), "fantasy points": ("fant", "nba"),
            "pts+rebs": ("pr", "nba"), "points+rebounds": ("pr", "nba"), "pts+asts": ("pa", "nba"),
            "points+assists": ("pa", "nba"), "rebs+asts": ("ra", "nba"), "rebounds+assists": ("ra", "nba")},
    "NHL": {"shots on goal": ("sog", "shots"), "shots": ("sog", "shots"), "sog": ("sog", "shots"),
            "goalie saves": ("saves", "goalie"), "saves": ("saves", "goalie"),
            "blocked shots": ("nhl_blk", "def"), "hits": ("hits", "hits"),
            # v1.9.5: used only by the pregame check (Points vs Goals + Assists)
            "goals": ("nhl_g", "pre"), "assists": ("nhl_a", "pre"), "points": ("nhl_pts", "pre"),
            # v1.9.9: two goalies' saves added together (pregame check only)
            "goalie saves (combo)": ("saves_combo", "pre"), "goalie saves combo": ("saves_combo", "pre"),
            "saves (combo)": ("saves_combo", "pre"), "goalie saves (combos)": ("saves_combo", "pre"),
            # v2.0: pregame recent-form only
            "faceoffs won": ("fow", "pre"), "time on ice": ("toi", "pre"), "fantasy score": ("nhl_fant", "pre")},
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
              "sog": "Shots On Goal", "saves": "Goalie Saves", "nhl_blk": "Blocked Shots", "hits": "Hits",
              "nhl_g": "Goals", "nhl_a": "Assists", "nhl_pts": "Points", "rr_yds": "Rush+Rec Yds", "pq_yds": "Pass+Rush Yds",
              "saves_combo": "Goalie Saves (Combo)", "fow": "Faceoffs Won", "toi": "Time On Ice",
              "nhl_fant": "Fantasy Score"}
MIN_ABS_EDGE = {"pass_yds": 12, "pass_att": 3, "pass_cmp": 2.5, "rush_yds": 8, "rush_att": 2.5, "rec_yds": 12, "rec_cnt": 1.5,
                "pts": 3.5, "reb": 1.5, "ast": 1.5, "fg3": 1.0, "pra": 4.0, "fant": 5.0, "pr": 3.0, "pa": 3.0, "ra": 2.0,
                "sog": 1.0, "saves": 3.0, "nhl_blk": 1.0, "hits": 1.0}
YARD_DAMP = {"pass_yds": 0.8, "rush_yds": 0.8, "rec_yds": 0.8, "pass_cmp": 0.9, "rec_cnt": 0.9}   # efficiency changes with the script, so shrink these
# v1.3: VOLUME props (attempts, carries, catches) follow the game script more reliably than YARD props (a trailing QB can
# throw 42 times and still miss his yards). Yard props need a bigger gap to alert, and volume props rank first when pairing.
YARD_STATS = ("pass_yds", "rec_yds")      # rush yards are NOT here: a leading team runs on purpose, so they follow the script closely
YARD_EDGE_MULT = float(os.getenv("HB_YARD_EDGE_MULT", "1.3"))
# v2.3: a yards UNDER needs a cushion bigger than one big play (OVERs are not affected)
BIG_PLAY_CUSHION = {"pass_yds": float(os.getenv("HB_UNDER_CUSHION_PASS", "45")),
                    "rec_yds": float(os.getenv("HB_UNDER_CUSHION_REC", "30")),
                    "rush_yds": float(os.getenv("HB_UNDER_CUSHION_RUSH", "25"))}
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


def _post(payload, url=None):
    url = url or WEBHOOK_URL
    if not url:
        print("no WEBHOOK_URL set, would have posted:", str(payload)[:300])
        return
    for _ in range(4):
        try:
            r = requests.post(url, json=payload, timeout=15)
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
def _pp_get(which, url):
    """One PrizePicks call, with a pause after "429 Too Many Requests". Returns fresh data or None (then the last copy is used)."""
    now = time.time()
    if now < _state.get("pp_block_until", 0):
        return None
    try:
        r = _session.get(url, timeout=20)
        if r.status_code == 429:
            wait = _num(r.headers.get("Retry-After"), 0) or min(300, max(60, 2 * _state.get("pp_backoff", 30)))
            _state["pp_backoff"] = wait
            _state["pp_block_until"] = now + wait
            print(f"PrizePicks asked us to slow down (429): pausing PrizePicks calls for {wait:.0f}s, using the last board meanwhile")
            return None
        r.raise_for_status()
        _state["pp_backoff"] = 30
        data = r.json()
        _state.setdefault("pp_cache", {})[which] = (now, data)
        return data
    except Exception as e:
        if which == "main" or time.time() - _state.get("ppl_err_t", 0) > 600:
            _state["ppl_err_t"] = time.time()
            print(f"PrizePicks {'live feed ' if which.startswith('live') else ''}error:", e)
        return None


def _mem_mb():
    """Memory the bot is using right now, in MB (Linux). 0 if unknown."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except Exception:
        return 0.0


def _wanted_league_ids(main_data):
    """PrizePicks league ids for our sports (from the normal board), only for sports with live games right now."""
    live_sports = _state.get("live_sports", set())
    tennis_live = time.time() - _state.get("tlive", 0) < 180
    ids = []
    for inc in main_data.get("included", []) or []:
        if inc.get("type") != "league":
            continue
        name = ((inc.get("attributes") or {}).get("name") or "").upper()
        if name in live_sports or (TENNIS_ON and tennis_live and _is_tennis_league(name)):
            ids.append((str(inc.get("id")), name))
    return sorted(set(ids))


def _slim(data, league_id):
    """Keep only this league's rows and the players/games they point to; drop the rest straight away."""
    rows = [it for it in (data.get("data") or [])
            if str(((it.get("relationships") or {}).get("league") or {}).get("data", {}).get("id")) == league_id]
    need = set()
    for it in rows:
        for rel in ("new_player", "game", "league"):
            d = ((it.get("relationships") or {}).get(rel) or {}).get("data") or {}
            if d.get("id") is not None:
                need.add((rel if rel != "new_player" else "new_player", str(d["id"])))
    inc = [x for x in (data.get("included") or []) if (x.get("type"), str(x.get("id"))) in need]
    return {"data": rows, "included": inc}


def fetch_board():
    """ONE PrizePicks call per cycle. While games are live, the normal board is refreshed about every 90 seconds and the
    cycles in between read the in-game lines of ONE live league each (v1.9.4), so memory and the call rate stay low."""
    cache = _state.setdefault("pp_cache", {})
    now = time.time()
    main = cache.get("main")
    leagues = _wanted_league_ids(main[1]) if main else []
    use_live = bool(PP_LIVE_URL) and PP_LIVE_URL != PP_URL and bool(leagues)
    if not main or not use_live or now - main[0] >= 90:
        _pp_get("main", PP_URL)
    else:
        turn = _state["pp_turn"] = _state.get("pp_turn", 0) + 1
        lid, lname = leagues[turn % len(leagues)]
        got = _pp_get("live_tmp", f"{PP_LIVE_URL}&league_id={lid}")
        cache.pop("live_tmp", None)
        if got is not None:
            raw_n = len(got.get("data") or [])
            slim = _slim(got, lid)
            del got
            gc.collect()
            cache["live:" + lid] = (now, slim)
            if raw_n > 5000 and now - _state.get("ppl_big_t", 0) > 600:
                _state["ppl_big_t"] = now
                print(f"PP LIVE FEED {lname}: PrizePicks ignored the league filter ({raw_n} rows), kept {len(slim['data'])}")
    if "main" not in cache:
        raise RuntimeError("no PrizePicks board yet")
    data = cache["main"][1]
    rows, inc = [], []
    for key in list(cache):
        if not key.startswith("live:"):
            continue
        t, lv = cache[key]
        if now - t > PP_LIVE_MAX_AGE or key[5:] not in {l for l, _ in leagues}:
            del cache[key]                                   # stale, or that league has no live games any more
            continue
        for item in lv.get("data", []) or []:
            item["_live_feed"] = True
        rows += lv.get("data", []) or []
        inc += lv.get("included", []) or []
    if not rows:
        return data
    if now - _state.get("ppl_log_t", 0) > 600:              # every 10 min: what the in-game lines hold
        _state["ppl_log_t"] = now
        n = _now()
        started = sum(1 for it in rows if (_to_dt((it.get("attributes") or {}).get("start_time")) or n) < n)
        print(f"PP LIVE FEED: {len(rows)} in-game rows for {', '.join(nm for _, nm in leagues)}, "
              f"{started} for games already started | memory {_mem_mb():.0f} MB")
    return {"data": (data.get("data") or []) + rows, "included": (data.get("included") or []) + inc}


# ====================== SAVED PREGAME LINES (v1.9.4) ======================
def save_state(force=False):
    """Writes the saved pregame lines to a file, at most once a minute."""
    now = time.time()
    if not force and now - _state.get("save_t", 0) < 60:
        return
    _state["save_t"] = now
    pt = _state.setdefault("prior_t", {})
    keep = [[list(k), v, pt.get(k, now)] for k, v in _state["prior"].items() if now - pt.get(k, now) < 36 * 3600]
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            used = [[list(k)] + list(v) for k, v in _state.get("used_props", {}).items() if now - v[0] < PROP_ONCE_H * 3600]
            json.dump({"v": 1, "prior": keep, "used": used}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        if now - _state.get("save_err_t", 0) > 3600:
            _state["save_err_t"] = now
            print("could not save pregame lines:", e)


def load_state():
    """Reads saved pregame lines back after a restart. Lines older than 36 hours are dropped."""
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
    except FileNotFoundError:
        print(f"SAVED LINES: none found in {STATE_FILE} (first start, or no Railway Volume attached)")
        return
    except Exception as e:
        print("SAVED LINES: could not read the file:", e)
        return
    now, n = time.time(), 0
    pt = _state.setdefault("prior_t", {})
    for k, v, t in d.get("prior", []):
        if now - t < 36 * 3600:
            key = tuple(k)
            _state["prior"][key] = v
            pt[key] = t
            n += 1
    for row in d.get("used", []):                   # v3.0: props already posted stay "used" after a restart
        k, v = row[0], (list(row[1:]) + [None, None])[:3]
        if v[0] and now - v[0] < PROP_ONCE_H * 3600:
            _state.setdefault("used_props", {})[tuple(k)] = tuple(v)
    print(f"SAVED LINES: restored {n} pregame lines from before the restart ({STATE_FILE})")


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
                stat = STAT_MAP.get(sport, {}).get(stat_name)
                if not stat and any(b in stat_name for b in BAD_WORDS):
                    continue
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
                        or str(a.get("status") or "").lower() in ("in_game", "live", "in_progress")
                        or bool(item.get("_live_feed") and (_to_dt(start) or _now()) < _now()))
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
                        or str(a.get("status") or "").lower() in ("in_game", "live", "in_progress")
                        or bool(item.get("_live_feed") and (_to_dt(start) or _now()) < _now()))
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
    """Full-game expectation: the pregame line, nudged toward the player's pace so far.
    v1.9.6: the pace only starts to count after a quarter of the game and grows slowly (one slow quarter is noise)."""
    if current is not None and f > 0.25:
        w = min(PACE_WEIGHT_MAX, 0.8 * (f - 0.25))
        return (1 - w) * prior + w * (current / f)
    return prior


def nfl_script(group, stat, margin, f):
    """Volume multiplier for the rest of the game, from the team's score margin (+ = leading)."""
    scale = 0.4 + 0.6 * f                         # an early deficit says less than a late one
    units = abs(margin) / 7.0
    if group in ("pass", "rec") and margin < 0:   # v2.2: trailing passing grows faster as the game goes on
        m = 1 + min(PASS_TRAIL_CAP, PASS_TRAIL * units * min(1.0, 0.6 + 0.6 * f))
        damp = 0.9 if stat in ("pass_yds", "rec_yds") else 1.0   # yards follow the extra throws almost fully
        return 1 + (m - 1) * damp
    if group in ("pass", "rec"):                  # receivers ride the same team passing volume as the QB
        m = (1 + min(PASS_TRAIL_CAP, PASS_TRAIL * units * scale)) if margin < 0 else \
            (1 - min(PASS_LEAD_CAP, PASS_LEAD * units * scale)) if margin > 0 else 1.0
    else:
        m = (1 + min(RUSH_LEAD_CAP, RUSH_LEAD * units * scale)) if margin > 0 else \
            (1 - min(RUSH_TRAIL_CAP, RUSH_TRAIL * units * scale)) if margin < 0 else 1.0
    damp = YARD_DAMP.get(stat, 1.0)
    return 1 + (m - 1) * damp


QB_TRAIL_ATT = float(os.getenv("HB_QB_TRAIL_ATT", "44"))      # full-game pass attempt pace for a team down 7 (about 22 a half)
QB_LEAD_ATT = float(os.getenv("HB_QB_LEAD_ATT", "31"))        # full-game pass attempt pace for a team up 10
QB_LEAGUE_YPA = float(os.getenv("HB_QB_LEAGUE_YPA", "6.6"))   # yards per attempt the QB's own number is pulled toward


def project_qb_script(stat, cur, f, margin):
    """v3.0: QB passing when the score decides the game plan. Instead of trusting the line, count the throws:
    a team down 7+ throws about 38-45 times a game, a team up 10+ about 25-31. Remaining throws x his yards per throw."""
    r = 1 - f
    if margin <= -7:
        per_game = QB_TRAIL_ATT + 0.5 * min(-margin - 7, 14)          # down 7: 44, down 14: 47.5, down 21: 51
    elif margin >= 10:
        per_game = QB_LEAD_ATT - 0.4 * min(margin - 10, 14)           # up 10: 31, up 17: 28, up 24: 25
    else:
        return None
    att_left = per_game * r
    att = cur.get("pass_att") or 0.0
    yds = cur.get("pass_yds") or 0.0
    ypa = (yds + 15 * QB_LEAGUE_YPA) / (att + 15)                       # his own yards per throw, pulled toward average
    if stat == "pass_att":
        proj = att + att_left
    else:
        proj = yds + att_left * ypa
    side = "trails" if margin < 0 else "leads"
    notes = [f"Team {side} by {abs(margin)}: about {per_game:.0f} throws a game at this score, {att_left:.0f} left",
             f"{ypa:.1f} yards per throw (his {att:g} throws so far, pulled toward the league average)"]
    return proj, (1.2 if margin < 0 else 0.8), notes


def project_nfl(line, cur, prior, f, margin):
    if line["stat"] in ("pass_att", "pass_yds") and f >= 0.25:
        q = project_qb_script(line["stat"], cur, f, margin)
        if q:
            return q
    r = 1 - f
    total = _blend_total(prior, cur.get(line["stat"]), f)
    m = nfl_script(line["group"], line["stat"], margin, f)
    c = cur.get(line["stat"], 0.0)
    proj = c + total * r * m
    notes = [f"Pace baseline {total:.1f} for the full game (PrizePicks line {prior:g})",
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
    notes = [f"Pace baseline {total:.1f} for the full game (PrizePicks line {prior:g})",
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
    if line["sport"] == "NFL" and NFL_LIVE_STATS and line["stat"] not in NFL_LIVE_STATS:
        return None                                  # v3.0: live NFL = QB passing yards and pass attempts only
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
    if gap < 0 and line["stat"] in BIG_PLAY_CUSHION:
        need = max(need, BIG_PLAY_CUSHION[line["stat"]])   # v2.3: one long play can erase a yards UNDER
    if abs(gap) < need:
        return None
    if half == 1:
        need *= float(os.getenv("HB_HALF1_EDGE_MULT", "1.25"))     # 1st-half props have no game-script edge, only pace, so ask for a bigger gap
        if abs(gap) < need:
            return None
    elif SCRIPT_ONLY and abs(factor - 1) < SCRIPT_MIN:
        return None
    if (line["sport"] == "NFL" and line.get("group") in ("pass", "rec") and gap < 0
            and margin <= -TRAIL_BLOCK_MARGIN and f >= TRAIL_BLOCK_FROM):
        return None                                  # v2.2: down 10+ late = they throw every play: no passing UNDER
    against = abs(factor - 1) >= SCRIPT_MIN and (gap > 0) != (factor > 1)
    if line["sport"] == "NFL" and against and (
            (abs(margin) >= NFL_NO_AGAINST_MARGIN and f >= TRAIL_BLOCK_FROM)
            or (line["stat"] in NFL_VOLUME and abs(margin) >= NFL_NO_AGAINST_VOLUME_MARGIN)):
        return None                                  # v2.9: never against the score in a 7+/10+ point game
    if (line["sport"] == "NFL" and line.get("group") in ("pass", "rec") and gap > 0
            and margin >= TRAIL_BLOCK_MARGIN and f >= TRAIL_BLOCK_FROM):
        return None                                  # v2.9 mirror: up 10+ = they run the clock: no passing OVER
    moved_now = L - prior
    if (line["sport"] == "NFL" and line["live"] and abs(moved_now) >= max(NFL_MOVE_MIN, 0.05 * prior)
            and (gap > 0) != (moved_now > 0)):
        return None                                  # v3.0: PrizePicks moved the live line for the score: never fade that move
    caution = half != 1 and abs(factor - 1) >= SCRIPT_MIN and (gap > 0) != (factor > 1)
    if caution and not CAUTION_ON:
        return None
    if caution:                                      # v1.9.8: against the game script -> posted alone, tagged CAUTION
        notes.insert(0, f"⚠️ CAUTION: the player's pace says {'OVER' if gap > 0 else 'UNDER'}, but the score "
                        f"(script ×{factor:.2f}) points the other way. Your call.")
    moved = L - prior
    if abs(moved) >= 0.01:
        notes.append(f"PrizePicks line has moved {moved:+g} since pregame (some of the script may already be priced in)")
    if line["live"]:
        notes.append("PrizePicks live line" + (" (rest-of-game, converted to a full-game number)" if LIVE_LINE_MODE == "rest" else ""))
    shape = "lopsided" if abs(margin) >= LOPSIDED[line["sport"]] else "tight"
    return {"direction": "OVER" if gap > 0 else "UNDER", "proj": proj, "gap": gap, "cur": c, "line": L,
            "margin": margin, "team": team, "opp": opp, "notes": notes, "factor": factor, "moved": moved,
            "strength": abs(gap) / need * PRIORITY.get(line["stat"], 1.0), "shape": f"{line['sport']}-{shape}", "game": event["id"],
            "sport": line["sport"], "caution": caution}


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


# ====================== TENNIS PREGAME (v2.1) ======================
# Two reasons must agree before a tennis pregame pick posts:
#   1) a simulation of the match built from PrizePicks' OWN Games Won + Total Games lines, and
#   2) the players' recent form, from the final scores of their last 10 singles matches (free ESPN scoreboards).
# Props: Total Games Won, Fantasy Score, Total Sets, Total Tie Breaks, 1st Set Total Games.
# (Total Games itself is what the simulation is built from, so it can't be checked against it. Aces, Double Faults and
#  Break Points have no free data.)
T_HIST_DAYS = int(os.getenv("HB_TENNIS_HISTORY_DAYS", "56"))
T_HIST_STEP = 4                                 # one scoreboard every 4 days catches every tournament
T_FORM_RATE = float(os.getenv("HB_TENNIS_FORM_RATE", "0.6"))
T_FORM_GAP = {"t_gw": 1.0, "t_fant": 2.0, "t_sets": 0.1, "t_tb": 0.15, "t_set1": 0.6}
T_MATCH_STATS = ("t_sets", "t_tb", "t_set1")


def _tennis_history_update(budget=2):
    """Reads past ESPN tennis scoreboards a couple at a time and keeps every finished singles match (compact)."""
    hist = _state.setdefault("t_hist", {})
    done = _state.setdefault("t_hist_dates", {})
    today = datetime.now(_ET) if _ET is not timezone.utc else _now()
    todo = []
    for back in range(0, T_HIST_DAYS + 1, T_HIST_STEP):
        d = (today - timedelta(days=back)).strftime("%Y%m%d")
        for tour in TENNIS_ESPN:
            t = done.get((tour, d))
            fresh = 6 * 3600 if back <= 2 else 10 ** 9      # recent days get re-read; older ones never change
            if t is None or time.time() - t > fresh:
                todo.append((back, tour, d))
    for back, tour, d in sorted(todo)[:budget]:
        try:
            data = _get_json(f"{TENNIS_ESPN[tour]}/scoreboard?dates={d}")
        except Exception as e:
            print(f"tennis history error ({tour} {d}):", e)
            done[(tour, d)] = time.time() - 6 * 3600 + 900     # try again in 15 minutes
            continue
        for ev in data.get("events", []) or []:
            comps = list(ev.get("competitions") or [])
            for g in ev.get("groupings") or []:
                if "singles" not in str((g.get("grouping") or {}).get("slug", "")):
                    continue
                comps += g.get("competitions") or []
            for c in comps:
                mid = str(c.get("id") or "")
                st = ((c.get("status") or {}).get("type") or {})
                if not mid or mid in hist or st.get("name") != "STATUS_FINAL":
                    continue                                   # finished normally only (no retirements/walkovers)
                cs = c.get("competitors") or []
                if len(cs) != 2 or any(x.get("type") != "athlete" for x in cs):
                    continue
                names = [_norm((x.get("athlete") or {}).get("displayName", "")) for x in cs]
                ls = [[int(_num(v.get("value"))) for v in (x.get("linescores") or [])] for x in cs]
                n = min(len(ls[0]), len(ls[1]))
                if n < 2 or n > 3:
                    continue                                   # best-of-3 only (the model assumes it)
                sets = [(ls[0][i], ls[1][i]) for i in range(n)]
                hist[mid] = {"t": _to_dt(c.get("date")) or _now(), "p": names, "sets": sets}
        done[(tour, d)] = time.time()
        del data
    return len(todo) - min(len(todo), budget)


def _tennis_player_matches(norm):
    """This player's last 10 finished singles matches, newest first, from his own side: list of stat dicts."""
    idx = _state.get("t_name_idx")
    hist = _state.get("t_hist", {})
    if not idx or idx[0] != len(hist):
        by = {}
        for m in hist.values():
            for nm in m["p"]:
                by.setdefault(nm, None)
        last = {}
        for nm in by:
            if nm.split():
                last.setdefault(nm.split()[-1], []).append(nm)
        idx = (len(hist), set(by), last)
        _state["t_name_idx"] = idx
    _, names, last = idx
    me = norm if norm in names else None
    if not me and norm.split():
        cands = [c for c in last.get(norm.split()[-1], []) if c[:1] == norm[:1]]
        me = cands[0] if len(cands) == 1 else None
    if not me:
        return []
    rows = []
    for m in sorted(hist.values(), key=lambda m: m["t"], reverse=True):
        if me not in m["p"]:
            continue
        side = m["p"].index(me)
        sets = [(a, b) if side == 0 else (b, a) for a, b in m["sets"]]
        gw, gl = sum(a for a, _ in sets), sum(b for _, b in sets)
        sw, sl = sum(1 for a, b in sets if a > b), sum(1 for a, b in sets if b > a)
        rows.append({"t_gw": gw, "t_games": gw + gl, "t_sets": len(sets),
                     "t_tb": sum(1 for a, b in sets if max(a, b) == 7 and min(a, b) == 6),
                     "t_set1": sets[0][0] + sets[0][1], "t_fant": 10 + gw - gl + 3 * (sw - sl)})
        if len(rows) >= 10:
            break
    return rows


def _t_form_reason(stat, direction, L, vals, who):
    """(ok, text) for the recent-form reason."""
    if not vals:
        return False, ""
    n = len(vals)
    avg = sum(vals) / n
    good = sum(1 for v in vals if (v > L if direction == "OVER" else v < L))
    side_ok = (avg - L) >= T_FORM_GAP[stat] if direction == "OVER" else (L - avg) >= T_FORM_GAP[stat]
    ok = side_ok and good / n >= T_FORM_RATE
    label = {"t_gw": "games won", "t_fant": "fantasy (without aces)", "t_sets": "sets", "t_tb": "tie breaks",
             "t_set1": "1st-set games"}[stat]
    return ok, f"{'✅' if ok else '❌'} Recent form ({who}): {label} averaging {avg:.2f}, {direction} {L:g} in {good} of {n}"


def tennis_pregame(tb):
    """Tennis pregame picks: the simulation AND recent form must both point the same way. One pick per match."""
    try:
        _tennis_history_update()
    except Exception as e:
        print("tennis history error:", e)
    groups = {}
    for key, ln in tb.items():
        if ln.get("gid"):
            groups.setdefault(ln["gid"], {}).setdefault(ln["norm"], {})[ln["stat"]] = (key, ln)
    out, checked, no_form = [], 0, 0
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
        if ck not in _state["tcal"] or _state["tcal"][ck][:3] != (tg, gwA, gwB):
            _state["tcal"][ck] = (tg, gwA, gwB, _calibrate_hold(share, tg))
        base = _state["tcal"][ck][3]
        d = share - 0.5
        st0 = {"sw": (0, 0), "g_done": (0, 0), "tbs": 0, "ns": 0, "set1": None, "cur": (0, 0)}
        sims = _sim(st0, _cl(base + d), _cl(base - d), _cl(0.5 + d, 0.1, 0.9), T_BEST_OF, T_SIMS)
        name_a, name_b = A[next(iter(A))][1]["name"], B[next(iter(B))][1]["name"]
        form = {0: _tennis_player_matches(pa), 1: _tennis_player_matches(pb)}
        checked += 1
        if len(form[0]) < 5 or len(form[1]) < 5:
            no_form += 1
        cands = []                                              # (side, stat, key, ln)
        for side, D in ((0, A), (1, B)):
            for stat in ("t_gw", "t_fant"):
                if stat in D:
                    cands.append((side, stat) + D[stat])
        for stat in T_MATCH_STATS:                              # same number for both players: check once
            if stat in A:
                cands.append((0, stat) + A[stat])
            elif stat in B:
                cands.append((1, stat) + B[stat])
        best = None
        for side, stat, key, ln in cands:
            adj = 0.0
            if stat == "t_fant":
                adj = _serve_adj(ln["norm"])
                if adj is None:
                    continue                                    # no Aces line: can't price the serve part fairly
            Lv = ln["line"]
            vals = [_t_val(stat, side, s_, adj) for s_ in sims]
            n = len(vals)
            p_over = sum(1 for v in vals if v > Lv) / n
            p_under = sum(1 for v in vals if v < Lv) / n
            direction, prob = ("OVER", p_over) if p_over >= p_under else ("UNDER", p_under)
            need = PRE_MIN_PROB + (T_FANT_EXTRA if stat == "t_fant" else 0.0)
            if prob < need:
                continue
            # reason 2: recent form
            if stat in T_MATCH_STATS:
                if len(form[0]) < 4 or len(form[1]) < 4:
                    continue
                fv = [r[stat] for r in form[0]] + [r[stat] for r in form[1]]
                ok, ftxt = _t_form_reason(stat, direction, Lv, fv, f"both players' last {len(fv)} matches combined")
            else:
                mine = form[side]
                if len(mine) < 5:
                    continue
                fv = [r[stat] + (adj if stat == "t_fant" else 0) for r in mine]
                ok, ftxt = _t_form_reason(stat, direction, Lv, fv, f"{ln['name']}'s last {len(fv)}")
            if not ok:
                continue
            proj = sum(vals) / n
            notes = [f"✅ Simulation from PrizePicks' own lines: {direction} {Lv:g} in {prob:.0%} of {n} matches", ftxt]
            if stat == "t_gw":
                notes.append(f"Games Won lines: {name_a} {gwA:g} + {name_b} {gwB:g} = {gwA + gwB:g}, Total Games {tg:g}")
            elif stat == "t_fant":
                notes.append(f"Fantasy = 10 + games won − games lost + 3 × (sets won − sets lost) + aces/double faults "
                             f"({adj:+.1f} from the Aces line)")
            elif stat == "t_sets":
                notes.append("Over 2.5 sets = the match goes to a 3rd set")
            elif stat == "t_tb":
                notes.append("A tie break = a set that reaches 6-6")
            if stat not in T_MATCH_STATS:
                notes.append("Last matches: " + ", ".join(f"{v:g}" for v in fv))
            notes.append("Recent form is against different opponents, so it's a guide, not a promise")
            notes.append("If a player retires, PrizePicks keeps the stats so far: a risk for overs")
            a = {"direction": direction, "proj": proj, "gap": proj - Lv, "cur": 0, "line": Lv, "margin": 0,
                 "team": {}, "opp": {}, "notes": notes, "factor": 1.0, "moved": 0.0, "prob": prob,
                 "strength": prob / need, "shape": "PRE-pregame", "game": "TP" + str(gid), "pre": True,
                 "sport": "TENNIS", "score_txt": f"Starts {_start_txt(start)}"}
            ev = {"id": "TP" + str(gid), "name": f"{name_a} vs {name_b} — 2 reasons agree", "f": 0.0, "period": 0,
                  "clock_txt": ""}
            if best is None or prob > best[3]["prob"]:
                best = (key, ln, ev, a)
        if best:
            out.append(best)
    if time.time() - _state.get("t_pre_log_t", 0) > 3600:
        _state["t_pre_log_t"] = time.time()
        print(f"TENNIS PREGAME CHECK: {checked} upcoming matches, {no_form} without 5+ recent matches for both players, "
              f"{len(out)} picks | {len(_state.get('t_hist', {}))} past matches loaded")
    return out


NBA_COMBOS = (("pra", ("pts", "reb", "ast")), ("pr", ("pts", "reb")), ("pa", ("pts", "ast")), ("ra", ("reb", "ast")))
# v1.9.5: (combos, smallest gap, gap as a share of the line) per sport
PRE_COMBOS = {
    "NBA": (NBA_COMBOS, PRE_NBA_GAP, 0.08),
    "NFL": ((("rr_yds", ("rush_yds", "rec_yds")), ("pq_yds", ("pass_yds", "rush_yds"))),
            float(os.getenv("HB_PRE_NFL_GAP", "10")), 0.10),
    # NHL lines are mostly 0.5 / 1.5, and Goals 0.5 + Assists 0.5 vs Points 0.5 is normal, so only a gap of a full
    # point or more counts.
    "NHL": ((("nhl_pts", ("nhl_g", "nhl_a")),), float(os.getenv("HB_PRE_NHL_GAP", "1.0")), 0.0),
}


def all_combo_pregame(board):
    out = []
    for sport, (combos, gap_min, pct) in PRE_COMBOS.items():
        if sport in SPORTS:
            out += nba_pregame(board, sport, combos, gap_min, pct)
    return out


def nba_pregame(board, sport="NBA", combos=NBA_COMBOS, gap_min=None, pct=0.08):
    """A combo line (NBA PRA etc., NFL Rush+Rec Yds, NHL Points) that is far from the sum of the player's single lines."""
    gap_min = PRE_NBA_GAP if gap_min is None else gap_min
    players = {}
    for key, ln in board.items():
        if ln["sport"] == sport and ln.get("half") is None and ln["line"] > 0 and not ln["live"]:
            players.setdefault(ln["norm"], {})[ln["stat"]] = (key, ln)
    out = []
    for norm, D in players.items():
        start = _upcoming([ln for _, ln in D.values()])
        if not start:
            continue
        best = None
        for combo, parts in combos:
            if combo not in D or not all(p in D for p in parts):
                continue
            key, ln = D[combo]
            total = sum(D[p][1]["line"] for p in parts)
            gap = total - ln["line"]
            need = max(gap_min, pct * ln["line"])
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
                 "sport": sport,
                 "pre": True, "score_txt": f"{ln['opp'] or 'Game'} • starts {_start_txt(start)}"}
            ev = {"id": a["game"], "name": f"{ln['name']} ({ln['team'] or sport})", "f": 0.0, "period": 0, "clock_txt": ""}
            if best is None or a["strength"] > best[3]["strength"]:
                best = (key, ln, ev, a)
        if best:
            out.append(best)
    return out


# ====================== NHL PREGAME GOALIE SAVES (v1.9.9) ======================
NHL_SAVES_MIN_PROB = float(os.getenv("HB_SAVES_MIN_PROB", "0.66"))   # pick must hit in 66%+ by the model
NHL_SAVES_MIN_GAP = float(os.getenv("HB_SAVES_MIN_GAP", "3.0"))      # and be 3+ saves away from the line
NHL_SV_LEAGUE = 0.902                                               # league save percentage
NHL_TEAM_ALIAS = {"LAK": "LA", "NJD": "NJ", "SJS": "SJ", "TBL": "TB", "WAS": "WSH", "MON": "MTL", "NAS": "NSH",
                  "CLS": "CBJ", "UTA": "UTAH", "UTH": "UTAH", "VEG": "VGK", "LV": "VGK", "WIN": "WPG", "CAL": "CGY",
                  "PHX": "UTAH", "ARI": "UTAH", "NYIS": "NYI"}


def _nhl_season_year():
    n = datetime.now(_ET) if _ET is not timezone.utc else _now()
    return n.year + 1 if n.month >= 9 else n.year                 # the 2026-27 season is "2027" at ESPN


def nhl_team_stats():
    """Shots taken / shots allowed per game and save % for every NHL team: this season blended with last season
    (early in the season a few games say little). Read from ESPN at most every 6 hours."""
    cached = _state.get("nhl_ts")
    if cached and time.time() - cached[0] < (6 * 3600 if cached[1] else 1800):
        return cached[1]
    out = {}
    try:
        teams = _get_json(ESPN["NHL"] + "/teams")["sports"][0]["leagues"][0]["teams"]
        y = _nhl_season_year()
        for t in teams:
            tm = t.get("team") or {}
            tid = str(tm.get("id"))
            per = {}
            for yr in (y, y - 1):
                try:
                    d = _get_json(f"{ESPN['NHL']}/teams/{tid}/statistics?season={yr}&seasontype=2")
                except Exception:
                    continue
                vals = {}
                for cat in ((d.get("results") or {}).get("stats") or {}).get("categories", []) or []:
                    for st in cat.get("stats", []) or []:
                        vals[st.get("name")] = _num(st.get("value"))
                gp = vals.get("games", 0)
                if gp > 0:
                    per[yr] = (gp, vals.get("shotsTotal", 0) / gp, vals.get("shotsAgainst", 0) / gp, vals.get("savePct", 0))
            if not per:
                continue
            cur, last = per.get(y), per.get(y - 1)
            if cur and last:
                w = cur[0] / (cur[0] + 15.0)                     # this season counts fully after ~15+ games
                sf, sa = w * cur[1] + (1 - w) * last[1], w * cur[2] + (1 - w) * last[2]
                sv = w * cur[3] + (1 - w) * last[3]
            else:
                one = cur or last
                sf, sa, sv = one[1], one[2], one[3]
            names = {tm.get("abbreviation", ""), tm.get("shortDisplayName", ""), tm.get("name", ""),
                     tm.get("location", ""), tm.get("displayName", "")}
            out[tid] = {"abbr": tm.get("abbreviation", ""), "name": tm.get("displayName", ""), "sf": sf, "sa": sa,
                        "sv": sv or NHL_SV_LEAGUE, "keys": {re.sub(r"[^A-Z]", "", n.upper()) for n in names if n},
                        "gp": cur[0] if cur else 0}
    except Exception as e:
        print("NHL team stats error:", e)
    _state["nhl_ts"] = (time.time(), out)
    if out:
        print(f"NHL TEAM STATS: {len(out)} teams loaded (season {_nhl_season_year()} blended with last season)")
    return out


def _nhl_team(ts, txt):
    k = re.sub(r"[^A-Z]", "", str(txt or "").upper())
    if not k:
        return None
    k = NHL_TEAM_ALIAS.get(k, k)
    for tid, t in ts.items():
        if k == t["abbr"] or k in t["keys"]:
            return tid
    for tid, t in ts.items():                                       # "Tampa Bay Lightning", "TB Lightning" ...
        if any(len(x) > 3 and x in k for x in t["keys"]):
            return tid
    return None


def _norm_cdf(x):
    import math
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def nhl_saves_pregame(board):
    """Goalie Saves before puck drop: expected shots on the goalie = (his team's shots allowed) x (the other team's
    shots taken) / league average, times a save %. Posts only when the line is clearly off."""
    lines = [(k, ln) for k, ln in board.items() if ln["sport"] == "NHL" and ln["stat"] == "saves"
             and not ln["live"] and ln["line"] > 0 and _upcoming([ln])]
    if not lines and not any(ln["sport"] == "NHL" and ln["stat"] == "saves_combo" for ln in board.values()):
        return []
    ts = nhl_team_stats()
    if not ts:
        return []
    lg_sf = sum(t["sf"] for t in ts.values()) / len(ts)
    out, miss = [], []

    def model(gl):
        """(expected saves, spread, expected shots, his team, other team) for one goalie's line, or None."""
        mine, opp = _nhl_team(ts, gl["team"]), _nhl_team(ts, gl["opp"])
        if not mine or not opp or mine == opp:
            return None
        A, B = ts[mine], ts[opp]
        shots = A["sa"] * B["sf"] / lg_sf
        sv = 0.7 * NHL_SV_LEAGUE + 0.3 * A["sv"]                    # team save % counts a little; one bad week says little
        mu = shots * sv
        return mu, max(5.0, 0.22 * mu), shots, A, B

    for key, ln in lines:
        m = model(ln)
        if not m:
            miss.append(f"{ln['name']} ({ln['team']} vs {ln['opp']})")
            continue
        mu, sd, shots, A, B = m
        _state.setdefault("saves_model", {})[ln["norm"]] = (mu, shots, A, B, lg_sf)
        continue                                        # v2.0: singles post through the recent-form check (2+ reasons)
        L = ln["line"]
        p_over = 1 - _norm_cdf((L - mu) / sd)
        direction, prob = ("OVER", p_over) if mu > L else ("UNDER", 1 - p_over)
        gap = mu - L
        if prob < NHL_SAVES_MIN_PROB or abs(gap) < NHL_SAVES_MIN_GAP:
            continue
        start = _upcoming([ln])
        notes = [f"{B['abbr']} take {B['sf']:.1f} shots a game, {A['abbr']} allow {A['sa']:.1f} (league average {lg_sf:.1f})",
                 f"Expected about {shots:.0f} shots on him and {mu:.1f} saves (hits {direction} about {prob:.0%} by this model)",
                 "Pregame check: confirm he is the STARTING goalie before you lock",
                 "If he gets pulled early, saves stop counting: a risk for overs"]
        if A.get("gp", 0) < 10:
            notes.append(f"Early season: this year's numbers ({int(A.get('gp', 0))} games) are blended with last season")
        a = {"direction": direction, "proj": mu, "gap": gap, "cur": 0, "line": L, "margin": 0, "team": {}, "opp": {},
             "notes": notes, "factor": 1.0, "moved": 0.0, "prob": prob, "strength": prob / NHL_SAVES_MIN_PROB,
             "shape": "PRE-pregame", "game": "GS" + str(ln.get("gid") or ln["norm"]), "pre": True, "sport": "NHL",
             "score_txt": f"{A['abbr']} vs {B['abbr']} • starts {_start_txt(start)}"}
        ev = {"id": a["game"], "name": f"{ln['name']} ({A['name']}) — Goalie Saves", "f": 0.0, "period": 0, "clock_txt": ""}
        out.append((key, ln, ev, a))
    # v1.9.9: Goalie Saves (Combo): two goalies added together. Each goalie needs his own single saves line on the
    # board (that's how the bot knows his team and opponent).
    singles = {ln["norm"]: ln for _, ln in lines}
    singles.update({ln["norm"]: ln for k, ln in board.items() if ln["sport"] == "NHL" and ln["stat"] == "saves"})
    combos = [(k, ln) for k, ln in board.items() if ln["sport"] == "NHL" and ln["stat"] == "saves_combo"
              and not ln["live"] and ln["line"] > 0 and _upcoming([ln])]
    n_combo_ok = 0
    for key, ln in combos:
        parts = [_norm(x) for x in re.split(r"\s*(?:\+|/|&|,)\s*", ln["name"]) if x.strip()]
        ms = [model(singles[p]) if p in singles else None for p in parts]
        if len(parts) < 2 or not all(ms):
            miss.append(f"combo {ln['name']}")
            continue
        n_combo_ok += 1
        mu = sum(m[0] for m in ms)
        sd = sum(m[1] ** 2 for m in ms) ** 0.5
        L = ln["line"]
        forms = [_nhl_form_values(p, _nhl_team(ts, singles[p]["team"]), "saves") for p in parts]
        if not all(f and len(f) >= 4 for f in forms):
            continue                                    # v2.0: need each goalie's recent starts too (2nd reason)
        form_sum = sum(sum(f) / len(f) for f in forms)
        if (form_sum - L) * (mu - L) <= 0 or abs(form_sum - L) < NHL_SAVES_MIN_GAP:
            continue                                    # recent starts don't agree with the matchup model
        p_over = 1 - _norm_cdf((L - mu) / sd)
        direction, prob = ("OVER", p_over) if mu > L else ("UNDER", 1 - p_over)
        gap = mu - L
        if prob < NHL_SAVES_MIN_PROB or abs(gap) < NHL_SAVES_MIN_GAP:
            continue
        start = _upcoming([ln])
        notes = [" + ".join(f"{singles[p]['name']} about {m[0]:.1f} ({m[3]['abbr']} vs {m[4]['abbr']}, ~{m[2]:.0f} shots)"
                            for p, m in zip(parts, ms)) + f" = {mu:.1f} saves",
                 f"✅ Recent starts agree: their averages add up to {form_sum:.1f}",
                 f"Hits {direction} about {prob:.0%} by this model",
                 "Pregame check: BOTH must be the starting goalies, or the combo can go DNP",
                 "If either gets pulled early, his saves stop counting: a risk for overs"]
        a = {"direction": direction, "proj": mu, "gap": gap, "cur": 0, "line": L, "margin": 0, "team": {}, "opp": {},
             "notes": notes, "factor": 1.0, "moved": 0.0, "prob": prob, "strength": prob / NHL_SAVES_MIN_PROB,
             "shape": "PRE-pregame", "game": "GC" + str(ln.get("gid") or ln["norm"]), "pre": True, "sport": "NHL",
             "score_txt": f"starts {_start_txt(start)}"}
        ev = {"id": a["game"], "name": f"{ln['name']} — Goalie Saves (Combo)", "f": 0.0, "period": 0, "clock_txt": ""}
        out.append((key, ln, ev, a))
    if time.time() - _state.get("saves_log_t", 0) > 3600:
        _state["saves_log_t"] = time.time()
        print(f"NHL SAVES CHECK: {len(lines)} goalie lines + {len(combos)} combos before puck drop, "
              f"{n_combo_ok} combos matched, "
              f"{len(out)} picks" + (f" | could not match: {', '.join(miss)[:400]}" if miss else ""))
    return out


# ====================== NHL PREGAME RECENT FORM (v2.0) ======================
FORM_STATS = ("sog", "hits", "nhl_blk", "fow", "toi", "saves", "nhl_fant")
FORM_MIN_GAP = {"sog": 0.6, "hits": 0.8, "nhl_blk": 0.6, "fow": 1.5, "toi": 1.2, "saves": 3.0, "nhl_fant": 2.5}
FORM_RATE = float(os.getenv("HB_FORM_RATE", "0.7"))         # 7 of the last 10 games over (or under) the line
FORM_GAMES = 10
FORM_MIN_PLAYED = 5
FORM_FETCH_PER_CYCLE = int(os.getenv("HB_FORM_FETCH_PER_CYCLE", "12"))   # box scores read per cycle (keeps live fast)


def _nhl_team_games(tid):
    """A team's last 10 finished games (newest first) from its ESPN schedule. Refreshed every 3 hours."""
    sc = _state.setdefault("nhl_sched", {})
    hit = sc.get(tid)
    if hit and time.time() - hit[0] < 3 * 3600:
        return hit[1]
    games = []
    try:
        d = _get_json(f"{ESPN['NHL']}/teams/{tid}/schedule")
        now = _now()
        for ev in d.get("events", []) or []:
            dt = _to_dt(ev.get("date"))
            if dt and (now - dt).total_seconds() > 4 * 3600:       # started 4+ hours ago: finished
                games.append((dt, str(ev.get("id"))))
    except Exception as e:
        print(f"NHL schedule error ({tid}):", e)
        return hit[1] if hit else []
    games.sort(reverse=True)
    out = [eid for _, eid in games[:FORM_GAMES]]
    sc[tid] = (time.time(), out)
    return out


def _nhl_game_box(eid):
    """Every player's line from one finished NHL game (cached; finished games don't change). None if not read yet."""
    cache = _state.setdefault("nhl_box", {})
    if eid in cache:
        return cache[eid]
    if _state.get("form_budget", 0) <= 0:
        return None
    _state["form_budget"] -= 1
    try:
        d = _get_json(f"{ESPN['NHL']}/summary?event={eid}")
    except Exception:
        return None
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
        return None
    winner = next((str((c.get("team") or {}).get("id")) for c in comp.get("competitors", []) if c.get("winner")), None)
    players = {}
    for team in (d.get("boxscore") or {}).get("players", []) or []:
        tid = str((team.get("team") or {}).get("id"))
        for cat in team.get("statistics", []) or []:
            keys = cat.get("keys") or cat.get("labels") or []
            goalie = (cat.get("name") or "").lower() == "goalies"
            for ath in cat.get("athletes", []) or []:
                nm = _norm((ath.get("athlete") or {}).get("displayName", ""))
                vals = ath.get("stats") or []
                if not nm or len(vals) != len(keys):
                    continue
                row = dict(zip(keys, vals))
                if goalie:
                    toi = _clock_seconds(row.get("timeOnIce")) / 60.0
                    win = 1.0 if (winner == tid and toi >= 40) else 0.0
                    sv, ga = _num(row.get("saves")), _num(row.get("goalsAgainst"))
                    players[nm] = {"goalie": True, "start": toi >= 40, "saves": sv,
                                   "nhl_fant": 6 * win + 0.6 * sv - 3 * ga, "team": tid}
                else:
                    g, a_ = _num(row.get("goals")), _num(row.get("assists"))
                    sog, hits, blk = _num(row.get("shotsTotal")), _num(row.get("hits")), _num(row.get("blockedShots"))
                    toi = _clock_seconds(row.get("timeOnIce")) / 60.0
                    if toi <= 0:
                        continue
                    players[nm] = {"sog": sog, "hits": hits, "nhl_blk": blk, "fow": _num(row.get("faceoffsWon")),
                                   "toi": toi, "nhl_fant": 6 * g + 4 * a_ + sog + 0.5 * hits + blk, "team": tid}
    cache[eid] = players
    return players


def _nhl_form_values(norm, tid, stat):
    """The player's numbers for this stat in his team's last 10 games (newest first). Goalies: starts only.
    None if the box scores aren't all read yet."""
    if not tid:
        return None
    vals = []
    for eid in _nhl_team_games(tid):
        box = _nhl_game_box(eid)
        if box is None:
            return None                                       # still reading box scores: try next cycle
        r = box.get(norm)
        if not r or stat not in r:
            continue
        _state.setdefault("is_goalie", {})[norm] = bool(r.get("goalie"))
        if r.get("goalie") and not r.get("start"):
            continue
        vals.append(r[stat])
    return vals


def nhl_form_pregame(board):
    """Pregame NHL picks from recent form. Posts only when 2+ reasons agree:
    must have BOTH (a) 7+ of the last 10 games on the same side of the line and (b) a clear average gap,
    plus at least one of (c) the matchup and (d) the last 5 games."""
    lines = [(k, ln) for k, ln in board.items() if ln["sport"] == "NHL" and ln["stat"] in FORM_STATS
             and not ln["live"] and ln["line"] > 0 and _upcoming([ln])]
    if not lines:
        return []
    ts = nhl_team_stats()
    if not ts:
        return []
    lg_sa = sum(t["sa"] for t in ts.values()) / len(ts)
    lg_sf = sum(t["sf"] for t in ts.values()) / len(ts)
    out, waiting, checked = [], 0, 0
    for key, ln in lines:
        mine, opp = _nhl_team(ts, ln["team"]), _nhl_team(ts, ln["opp"])
        if not mine:
            continue
        stat, L = ln["stat"], ln["line"]
        vals = _nhl_form_values(ln["norm"], mine, stat)
        if vals is None:
            waiting += 1
            continue
        checked += 1
        if len(vals) < (4 if stat in ("saves",) else FORM_MIN_PLAYED):
            continue
        is_goalie = stat == "saves" or bool(_state.get("is_goalie", {}).get(ln["norm"]))
        n = len(vals)
        avg = sum(vals) / n
        direction = "OVER" if avg > L else "UNDER"
        hits_n = sum(1 for v in vals if (v > L if direction == "OVER" else v < L))
        pushes = sum(1 for v in vals if v == L)
        rate = hits_n / n
        need = max(FORM_MIN_GAP[stat], 0.12 * L)
        reasons = []
        r1 = rate >= FORM_RATE
        r2 = abs(avg - L) >= need
        if not (r1 and r2):
            continue
        reasons.append(f"✅ {hits_n} of his last {n} {'starts' if is_goalie else 'games'} went {direction} {L:g}")
        reasons.append(f"✅ Averaging {avg:.1f} vs a line of {L:g}")
        r3 = False
        against = False
        mline = None                                    # v2.4: the matchup is always shown (✅ / ➖ / ❌)
        if opp:
            O = ts[opp]
            good_hi = direction == "OVER"
            if stat in ("sog",) or (stat == "nhl_fant" and not is_goalie):
                ratio = O["sa"] / lg_sa
                what = f"{O['abbr']} allow {O['sa']:.1f} shots a game (league {lg_sa:.1f})"
            elif stat == "nhl_blk":
                ratio = O["sf"] / lg_sf
                what = f"{O['abbr']} take {O['sf']:.1f} shots a game (league {lg_sf:.1f}), the shots there are to block"
            elif is_goalie and stat == "saves":
                sm = _state.get("saves_model", {}).get(ln["norm"])
                ratio = (sm[0] / L) if sm and L else None
                what = (f"about {sm[1]:.0f} shots expected on him ({sm[3]['abbr']} take {sm[3]['sf']:.1f}, "
                        f"{sm[2]['abbr']} allow {sm[2]['sa']:.1f})") if sm else ""
            else:
                ratio, what = None, ""                  # hits, faceoffs, time on ice: no matchup number
            if ratio is not None:
                hi, lo = (1.06, 0.94) if stat != "saves" else (1.07, 0.93)
                if (good_hi and ratio >= hi) or (not good_hi and ratio <= lo):
                    r3 = True
                    mline = f"✅ Matchup helps: {what}"
                elif (good_hi and ratio <= lo) or (not good_hi and ratio >= hi):
                    against = True
                    mline = f"❌ Matchup is against it: {what}"
                else:
                    mline = f"➖ Matchup is neutral: {what}"
        if against:
            continue                                    # v2.4: never post a pick the matchup works against
        if mline:
            reasons.append(mline)
        last5 = vals[:5]
        l5 = sum(last5) / len(last5)
        r4 = len(last5) >= 4 and ((l5 - L) if direction == "OVER" else (L - l5)) >= 0.5 * need
        if r4:
            reasons.append(f"✅ Last {len(last5)}: averaging {l5:.1f}")
        if not (r3 or r4):
            continue
        notes = reasons + [f"Last {n}: " + ", ".join(f"{v:g}" if stat != "toi" else f"{v:.1f}" for v in vals)]
        if is_goalie:
            notes.append("Confirm he is the STARTING goalie (a goalie who doesn't start is DNP)")
        if stat == "nhl_fant" and not is_goalie:
            notes.append("Fantasy from free box scores leaves out power play points (0.5 each): a tiny bit low")
        if float(L).is_integer():
            notes.append(f"Whole-number line: exactly {L:g} is a push")
        if pushes:
            notes.append(f"{pushes} of the last {n} landed exactly on {L:g}")
        start = _upcoming([ln])
        n_reasons = 2 + int(r3) + int(r4)
        a = {"direction": direction, "proj": avg, "gap": avg - L, "cur": 0, "line": L, "margin": 0, "team": {},
             "opp": {}, "notes": notes, "factor": 1.0, "moved": 0.0, "prob": rate,
             "strength": rate / FORM_RATE * (1 + 0.15 * (n_reasons - 3)), "shape": "PRE-pregame",
             "game": "NG" + str(ln.get("gid") or ln["team"]), "pre": True, "sport": "NHL", "form": True,
             "score_txt": f"{ts[mine]['abbr']} vs {ts[opp]['abbr'] if opp else ln['opp']} • starts {_start_txt(start)}"}
        ev = {"id": a["game"], "name": f"{ln['name']} ({ts[mine]['name']}) — {n_reasons} reasons agree", "f": 0.0,
              "period": 0, "clock_txt": ""}
        out.append((key, ln, ev, a))
    if time.time() - _state.get("form_log_t", 0) > 3600 or (waiting == 0 and not _state.get("form_logged_ready")):
        _state["form_log_t"] = time.time()
        _state["form_logged_ready"] = waiting == 0
        print(f"NHL FORM CHECK: {len(lines)} pregame lines, {checked} checked, {waiting} still loading box scores, "
              f"{len(out)} picks ({len(_state.get('nhl_box', {}))} box scores cached)")
    return out


# ====================== ESPORTS PREGAME (v2.6, free) ======================
# Valorant, LoL and CS2: a "(Combo)" kills line (2-3 players added together) is checked against the sum of the
# same players' single lines. Only PrizePicks' own lines are used (no paid esports data).
ES_LEAGUES = ("CS2", "CSGO", "CS", "LOL", "LEAGUE OF LEGENDS", "VAL", "VALORANT")
ES_ON = os.getenv("HB_ESPORTS", "true").lower() == "true"
ES_GAP_MIN = float(os.getenv("HB_ES_GAP_MIN", "5"))          # combo must be 5+ kills away from the sum
ES_GAP_PCT = float(os.getenv("HB_ES_GAP_PCT", "0.035"))      # ...and 3.5%+ of the line


def _es_league(name):
    n = (name or "").upper()
    if n in ("CS2", "CSGO", "CS"):
        return "CS2"
    if n in ("LOL", "LEAGUE OF LEGENDS"):
        return "LOL"
    if n in ("VAL", "VALORANT"):
        return "VAL"
    return None


def esports_pregame(raw):
    players, leagues = {}, {}
    for inc in raw.get("included", []) or []:
        a = inc.get("attributes", {}) or {}
        if inc.get("type") == "new_player":
            players[inc["id"]] = (a.get("name", ""), a.get("team") or a.get("team_name") or "")
        elif inc.get("type") == "league":
            leagues[inc["id"]] = (a.get("name") or "").upper()
    singles, combos = {}, []
    for item in raw.get("data", []) or []:
        try:
            a = item["attributes"]
            rel = item.get("relationships", {})
            lg = _es_league(leagues.get((rel.get("league", {}).get("data") or {}).get("id"), ""))
            if not lg or str(a.get("odds_type") or "standard").lower() != "standard":
                continue
            if item.get("_live_feed"):
                continue
            stat_raw = re.sub(r"\s+", " ", str(a.get("stat_display_name") or a.get("stat_type") or "")).strip()
            name, team = players.get((rel.get("new_player", {}).get("data") or {}).get("id"), ("", ""))
            line = _num(a.get("line_score"))
            if not name or line <= 0:
                continue
            is_combo = "(combo)" in stat_raw.lower()
            base = re.sub(r"\s*\(combo\)\s*", "", stat_raw, flags=re.I).strip()
            key = "es_" + re.sub(r"[^a-z0-9]+", "_", (lg + " " + base).lower()).strip("_")
            STAT_LABEL.setdefault(key, base + (" (Combo)" if is_combo else ""))
            if is_combo:
                STAT_LABEL.setdefault(key + "_c", base + " (Combo)")
            gid = (rel.get("game", {}).get("data") or {}).get("id")
            ln = {"sport": lg, "name": name, "norm": _norm(name), "team": team, "pos": "",
                  "stat": key + ("_c" if is_combo else ""), "group": "es", "half": None, "line": line,
                  "start": _to_dt(a.get("start_time")), "opp": a.get("description") or "", "flags": {},
                  "live": False, "gid": gid}
            if is_combo:
                combos.append((lg, key, ln))
            else:
                singles[(lg, key, _norm(name))] = ln
        except Exception:
            continue
    out, matched = [], 0
    for lg, key, ln in combos:
        start = _upcoming([ln])
        if not start:
            continue
        parts = [x.strip() for x in re.split(r"\s*\+\s*", ln["name"]) if x.strip()]
        legs = [singles.get((lg, key, _norm(x))) for x in parts]
        if len(parts) < 2 or not all(legs):
            if len(_state.setdefault("es_miss", [])) < 3:       # v3.0: show why a combo didn't match (for fixing)
                near = [ln2["name"] for (l2, k2, _), ln2 in singles.items() if l2 == lg and k2 == key][:4]
                _state["es_miss"].append(1)
                print(f"ESPORTS COMBO NOT MATCHED: name '{ln['name']}' stat '{STAT_LABEL.get(key + '_c', key)}' "
                      f"parts {parts} found {[bool(x) for x in legs]} | singles with that stat: {near}")
            continue
        matched += 1
        total = sum(x["line"] for x in legs)
        gap = total - ln["line"]
        need = max(ES_GAP_MIN, ES_GAP_PCT * ln["line"])
        if abs(gap) < need:
            continue
        direction = "OVER" if gap > 0 else "UNDER"
        parts_txt = " + ".join(f"{x['name']} {x['line']:g}" for x in legs)
        notes = [f"PrizePicks' own lines: {parts_txt} = {total:g}, but the combo is {ln['line']:g}",
                 "Each line is a middle guess, so the sum is a fair check but not exact",
                 "If a map isn't played or a player is benched, check PrizePicks' DNP rules before you lock"]
        a = {"direction": direction, "proj": total, "gap": gap, "cur": 0, "line": ln["line"], "margin": 0,
             "team": {}, "opp": {}, "notes": notes, "factor": 1.0, "moved": 0.0, "prob": 0.0,
             "strength": abs(gap) / need, "shape": "PRE-pregame", "game": "ES" + str(ln.get("gid") or ln["name"]),
             "pre": True, "sport": lg, "score_txt": f"{ln['team'] or lg} {ln['opp'] and 'vs ' + ln['opp']} • starts {_start_txt(start)}"}
        ev = {"id": a["game"], "name": f"{ln['name']} ({lg})", "f": 0.0, "period": 0, "clock_txt": ""}
        out.append(((lg, ln["norm"], ln["stat"], None), ln, ev, a))
    if CS2_FORM_ON:
        try:
            out += cs2_form_pregame(singles)
        except Exception as e:                       # bo3.gg trouble never stops anything else
            print("cs2 form error:", e)
    if time.time() - _state.get("es_log_t", 0) > 3600:
        _state["es_log_t"] = time.time()
        print(f"ESPORTS PREGAME CHECK: {len(singles)} single lines, {len(combos)} combos, {matched} combos matched to "
              f"their players, {len(out)} picks")
    return out


# ====================== CS2 PLAYER FORM (v3.0, free: bo3.gg) ======================
# Single CS2 kill lines ("MAPS 1-2 Kills", "MAP 1 Kills"): the player's kills per round from bo3.gg's free public
# stats, over the last 45 days AND the last 150 days, times the rounds those maps usually last. Both must land on the
# same side of the line, the recent one by a clear gap. Unofficial source: if it fails, CS2 form picks just stop.
CS2_FORM_ON = os.getenv("HB_CS2_FORM", "true").lower() == "true"
CS2_ROUNDS_PER_MAP = float(os.getenv("HB_CS2_ROUNDS_PER_MAP", "21.5"))
CS2_FETCH_PER_CYCLE = int(os.getenv("HB_CS2_FETCH_PER_CYCLE", "6"))
CS2_GAP_PCT = float(os.getenv("HB_CS2_GAP_PCT", "0.10"))      # recent projection must be 10%+ away from the line
CS2_RECENT_DAYS, CS2_LONG_DAYS = 45, 150
BO3 = "https://api.bo3.gg/api/v1"
_bo3 = requests.Session()
_bo3.headers.update({"accept": "application/json, text/plain, */*", "origin": "https://bo3.gg", "referer": "https://bo3.gg/",
                     "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/122.0.0.0 Safari/537.36"})


def _bo3_get(path, params):
    if _state.get("cs2_budget", 0) <= 0 or time.time() < _state.get("bo3_block_until", 0):
        return None
    _state["cs2_budget"] -= 1
    try:
        r = _bo3.get(BO3 + path, params=params, timeout=12)
        if r.status_code in (403, 429):
            _state["bo3_block_until"] = time.time() + 900
            print(f"bo3.gg said {r.status_code}: pausing CS2 form for 15 minutes")
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _state["bo3_err"] = _state.get("bo3_err", 0) + 1
        if _state["bo3_err"] <= 3:
            print("bo3.gg error:", e)
        return None


def _cs2_slug(name):
    cache = _state.setdefault("cs2_slug", {})
    k = name.lower()
    if k in cache:
        return cache[k]
    d = _bo3_get("/filters/players", {"page[limit]": 5, "filter[discipline_id][eq]": 1, "search_text": name})
    if d is None:
        return None
    exact = [x for x in d.get("results", []) if (x.get("nickname") or "").lower() == k]
    exact.sort(key=lambda x: (x.get("rank") is None, x.get("rank") or 0))     # the ranked (active pro) player first
    cache[k] = exact[0]["slug"] if exact else ""
    return cache[k]


def _cs2_kpr(slug, days):
    """(kills per round, rounds played) over the last `days` days, cached 12 hours."""
    cache = _state.setdefault("cs2_stats", {})
    hit = cache.get((slug, days))
    if hit and time.time() - hit[0] < 12 * 3600:
        return hit[1]
    today = _now().date()
    d = _bo3_get(f"/players/{slug}/general_stats", {"filter[start_date_from]": str(today - timedelta(days=days)),
                                                    "filter[start_date_to]": str(today)})
    if d is None:
        return None
    rounds = _num(d.get("rounds_count"), 0)
    val = (_num(d.get("kills_sum"), 0) / rounds, rounds) if rounds > 0 else (0.0, 0)
    cache[(slug, days)] = (time.time(), val)
    return val


def _cs2_maps(label):
    """'MAPS 1-2 Kills' -> 2, 'MAP 1 Kills' -> 1. Map 3 is not always played, so lines that need it are skipped."""
    t = label.lower()
    if "kill" not in t or "headshot" in t or "hs" in t.split():
        return None
    m = re.search(r"maps?\s*(\d)(?:\s*-\s*(\d))?", t)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    if b > 2 or b < a:
        return None
    return b - a + 1


def cs2_form_pregame(singles):
    _state["cs2_budget"] = CS2_FETCH_PER_CYCLE
    out, lines, waiting = [], 0, 0
    for (lg, key, norm), ln in singles.items():
        if lg != "CS2":
            continue
        nmaps = _cs2_maps(STAT_LABEL.get(key, ""))
        start = _upcoming([ln]) if nmaps else None
        if not start:
            continue
        lines += 1
        slug = _cs2_slug(ln["name"])
        if slug is None:
            waiting += 1
            continue
        if not slug:
            continue                                   # not found on bo3.gg
        rec, lon = _cs2_kpr(slug, CS2_RECENT_DAYS), _cs2_kpr(slug, CS2_LONG_DAYS)
        if rec is None or lon is None:
            waiting += 1
            continue
        if rec[1] < 120 or lon[1] < 300:
            continue                                   # too few rounds played to trust
        L = ln["line"]
        rounds = CS2_ROUNDS_PER_MAP * nmaps
        p_rec, p_lon = rec[0] * rounds, lon[0] * rounds
        need = max(1.5 * nmaps, CS2_GAP_PCT * L)
        g_rec, g_lon = p_rec - L, p_lon - L
        if abs(g_rec) < need or g_lon * g_rec <= 0 or abs(g_lon) < 0.5 * need:
            continue
        direction = "OVER" if g_rec > 0 else "UNDER"
        maps_txt = f"{nmaps} maps" if nmaps > 1 else "1 map"
        notes = [f"✅ Last {CS2_RECENT_DAYS} days: {rec[0]:.2f} kills a round x {rounds:g} rounds ({maps_txt}) = {p_rec:.1f}",
                 f"✅ Last {CS2_LONG_DAYS} days: {lon[0]:.2f} kills a round = {p_lon:.1f}",
                 f"Line {L:g} → {direction}",
                 "Short maps (a stomp) mean fewer kills for everyone, long maps (overtime) mean more",
                 "Stats from bo3.gg (free, unofficial). Check the lineup before you lock"]
        a = {"direction": direction, "proj": p_rec, "gap": g_rec, "cur": 0, "line": L, "margin": 0, "team": {},
             "opp": {}, "notes": notes, "factor": 1.0, "moved": 0.0, "prob": 0.0,
             "strength": abs(g_rec) / need, "shape": "PRE-pregame", "game": "ES" + str(ln.get("gid") or ln["team"]),
             "pre": True, "sport": "CS2", "form": True,
             "score_txt": f"{ln['team'] or 'CS2'} {ln['opp'] and 'vs ' + ln['opp']} • starts {_start_txt(start)}"}
        ev = {"id": a["game"], "name": f"{ln['name']} (CS2) — recent and long-term form agree", "f": 0.0,
              "period": 0, "clock_txt": ""}
        out.append(((lg, norm, ln["stat"], None), ln, ev, a))
    if time.time() - _state.get("cs2_log_t", 0) > 3600:
        _state["cs2_log_t"] = time.time()
        print(f"CS2 FORM CHECK: {lines} kill lines, {waiting} still loading from bo3.gg, {len(out)} picks")
    return out


# ====================== NBA PREGAME RECENT FORM (v2.7) ======================
# Like NHL: the player's last 10 games (this season first, topped up with last season's games for the SAME team),
# plus the opponent's points allowed as the matchup. Posts only when 2+ reasons agree and the matchup isn't against it.
NBA_FORM_STATS = ("pts", "reb", "ast", "fg3", "pra", "pr", "pa", "ra", "fant")
NBA_FORM_GAP = {"pts": 3.0, "reb": 1.5, "ast": 1.5, "fg3": 0.8, "pra": 4.0, "pr": 3.0, "pa": 3.0, "ra": 2.0, "fant": 5.0}
NBA_SCORING = ("pts", "fg3", "pra", "pr", "pa", "fant")       # stats the opponent's points allowed says something about
NBA_MIN_MINUTES = float(os.getenv("HB_NBA_FORM_MIN_MINUTES", "28"))
NBA_TEAM_ALIAS = {"NYK": "NY", "GSW": "GS", "SAS": "SA", "NOP": "NO", "NOR": "NO", "UTA": "UTAH", "UTH": "UTAH",
                  "WAS": "WSH", "PHO": "PHX", "BRK": "BKN", "BRO": "BKN", "CHO": "CHA", "NJN": "BKN"}


def _nba_season_year():
    n = datetime.now(_ET) if _ET is not timezone.utc else _now()
    return n.year + 1 if n.month >= 9 else n.year                 # the 2026-27 season is "2027" at ESPN


def nba_teams():
    hit = _state.get("nba_teams")
    if hit and time.time() - hit[0] < 24 * 3600:
        return hit[1]
    out = {}
    try:
        for t in _get_json(ESPN["NBA"] + "/teams")["sports"][0]["leagues"][0]["teams"]:
            tm = t.get("team") or {}
            names = {tm.get("abbreviation", ""), tm.get("shortDisplayName", ""), tm.get("name", ""),
                     tm.get("location", ""), tm.get("displayName", "")}
            out[str(tm.get("id"))] = {"abbr": tm.get("abbreviation", ""), "name": tm.get("displayName", ""),
                                      "keys": {re.sub(r"[^A-Z]", "", n.upper()) for n in names if n}}
    except Exception as e:
        print("NBA teams error:", e)
    if out:
        _state["nba_teams"] = (time.time(), out)
    return out


def _nba_team(teams, txt):
    k = re.sub(r"[^A-Z]", "", str(txt or "").upper())
    if not k:
        return None
    k = NBA_TEAM_ALIAS.get(k, k)
    for tid, t in teams.items():
        if k == t["abbr"] or k in t["keys"]:
            return tid
    for tid, t in teams.items():
        if any(len(x) > 3 and x in k for x in t["keys"]):
            return tid
    return None


def _score_val(x):
    if isinstance(x, dict):
        return _num(x.get("value", x.get("displayValue")))
    return _num(x)


def _nba_team_games(tid):
    """A team's last 10 finished regular-season games, newest first: [(event id, points allowed)].
    This season first, topped up with last season's games. Refreshed every 3 hours."""
    sc = _state.setdefault("nba_sched", {})
    hit = sc.get(tid)
    if hit and time.time() - hit[0] < 3 * 3600:
        return hit[1]
    games, y, now = [], _nba_season_year(), _now()
    for yr in (y, y - 1):
        try:
            d = _get_json(f"{ESPN['NBA']}/teams/{tid}/schedule?season={yr}&seasontype=2")
        except Exception as e:
            print(f"NBA schedule error ({tid} {yr}):", e)
            continue
        for ev in d.get("events", []) or []:
            dt = _to_dt(ev.get("date"))
            comp = (ev.get("competitions") or [{}])[0]
            done = (((comp.get("status") or {}).get("type") or {}).get("completed"))
            if not dt or (now - dt).total_seconds() < 4 * 3600 or done is False:
                continue
            allowed = None
            for c in comp.get("competitors", []) or []:
                if str((c.get("team") or {}).get("id")) != str(tid):
                    allowed = _score_val(c.get("score"))
            games.append((dt, str(ev.get("id")), allowed))
        if len(games) >= FORM_GAMES:
            break
    games.sort(reverse=True)
    out = [(eid, al) for _, eid, al in games[:FORM_GAMES]]
    sc[tid] = (time.time(), out)
    return out


def _nba_game_box(eid):
    cache = _state.setdefault("nba_box", {})
    if eid in cache:
        return cache[eid]
    if _state.get("form_budget", 0) <= 0:
        return None
    _state["form_budget"] -= 1
    try:
        d = _get_json(f"{ESPN['NBA']}/summary?event={eid}")
    except Exception:
        return None
    players = {}
    for team in (d.get("boxscore") or {}).get("players", []) or []:
        btid = str((team.get("team") or {}).get("id") or "")
        for cat in team.get("statistics", []) or []:
            labels = cat.get("labels") or []
            for ath in cat.get("athletes", []) or []:
                nm = _norm((ath.get("athlete") or {}).get("displayName", ""))
                vals = ath.get("stats") or []
                if not nm or not vals or len(vals) != len(labels):
                    continue
                row = dict(zip(labels, vals))
                mins = _num(row.get("MIN"))
                if mins <= 0:
                    continue
                made, _ = _split_pair(row.get("3PT"))
                p = {"minutes": mins, "pts": _num(row.get("PTS")), "reb": _num(row.get("REB")),
                     "ast": _num(row.get("AST")), "fg3": float(made), "stl": _num(row.get("STL")),
                     "blk": _num(row.get("BLK")), "tov": _num(row.get("TO")), "tid": btid}
                players[nm] = derive(p)
    cache[eid] = players
    return players


NBA_SOFT_STATS = ("fant", "pra", "pts", "pr", "pa")
NBA_SOFT_MIN = float(os.getenv("HB_NBA_SOFT_MINUTES", "30"))    # "main player" for the soft-matchup play
NBA_SOFT_RATIO = float(os.getenv("HB_NBA_SOFT_RATIO", "1.04"))  # opponent allows 4%+ more points than average
NBA_STARTER_MIN = float(os.getenv("HB_NBA_STARTER_MINUTES", "28"))   # v3.0: NBA picks only for heavy-minute players
NBA_MATCHUP_EDGE = float(os.getenv("HB_NBA_MATCHUP_EDGE", "0.02"))     # opponent must allow 2%+ more (OVER) / less (UNDER)


def _nba_season_allowed(tid):
    """(this season's points allowed a game, games this season, last season's full points allowed a game). 6h cache."""
    sc = _state.setdefault("nba_season_al", {})
    hit = sc.get(tid)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    y, now, res = _nba_season_year(), _now(), []
    for yr in (y, y - 1):
        vals = []
        try:
            d = _get_json(f"{ESPN['NBA']}/teams/{tid}/schedule?season={yr}&seasontype=2")
            for ev in d.get("events", []) or []:
                dt = _to_dt(ev.get("date"))
                comp = (ev.get("competitions") or [{}])[0]
                if not dt or (now - dt).total_seconds() < 4 * 3600 or \
                        (((comp.get("status") or {}).get("type") or {}).get("completed")) is False:
                    continue
                for c in comp.get("competitors", []) or []:
                    if str((c.get("team") or {}).get("id")) != str(tid):
                        v = _score_val(c.get("score"))
                        if v:
                            vals.append(v)
        except Exception as e:
            print(f"NBA schedule error ({tid} {yr}):", e)
        res.append(vals)
    this, last = res
    out = (sum(this) / len(this) if this else None, len(this), sum(last) / len(last) if last else None)
    sc[tid] = (time.time(), out)
    return out


def _nba_fant_allowed(tid):
    """Fantasy score and PRA the team's OPPONENTS put up a game, over its last 10 games (box scores). None = loading."""
    f, pra, n = 0.0, 0.0, 0
    for eid, _al in _nba_team_games(tid):
        box = _nba_game_box(eid)
        if box is None:
            return None
        opp = [r for r in box.values() if r.get("tid") and r["tid"] != str(tid)]
        if opp:
            f += sum(r["fant"] for r in opp)
            pra += sum(r["pra"] for r in opp)
            n += 1
    return (f / n, pra / n) if n >= 5 else None


def _nba_allowed(teams):
    """v3.0 'how soft is this defense', as a points-allowed number (league average = normal):
    points allowed (last season's full season, blended with this season as games come in: by game 15 they count
    equally), plus fantasy score and PRA allowed over the last 10 games once those box scores are loaded."""
    hit = _state.get("nba_allowed")
    if hit and time.time() - hit[0] < 3 * 3600 and hit[1]:
        return hit[1]
    pts, fan, det = {}, {}, {}
    for tid in teams:
        this, n, last = _nba_season_allowed(tid)
        if this is None and last is None:
            continue
        pts[tid] = this if last is None else last if this is None else (n * this + 15 * last) / (n + 15)
        fa = _nba_fant_allowed(tid)
        if fa:
            fan[tid] = fa
    if len(pts) < 20:
        return {}
    lgp = sum(pts.values()) / len(pts)
    use_f = len(fan) >= 20
    lgf = sum(v[0] for v in fan.values()) / len(fan) if use_f else 0
    lgr = sum(v[1] for v in fan.values()) / len(fan) if use_f else 0
    out = {}
    for tid, p in pts.items():
        r = p / lgp
        if use_f and tid in fan:
            r = (r + fan[tid][0] / lgf + fan[tid][1] / lgr) / 3
        out[tid] = lgp * r
        det[tid] = (p, fan.get(tid))
    _state["nba_soft_detail"] = (det, lgp, (lgf, lgr) if use_f else None)
    if use_f:                                   # full table: keep it 3 hours. Points-only table: rebuild next cycle
        _state["nba_allowed"] = (time.time(), out)
    return out


def _nba_matchup_txt(teams, opp):
    det, lgp, lgfr = _state.get("nba_soft_detail", ({}, 0, None))
    p, fa = det.get(opp, (None, None))
    abbr = teams.get(opp, {}).get("abbr", "?")
    if p is None:
        return abbr
    txt = f"{abbr} allow {p:.1f} points a game (league {lgp:.1f})"
    if fa and lgfr:
        txt += f", {fa[0]:.0f} fantasy pts (league {lgfr[0]:.0f}) and {fa[1]:.0f} PRA (league {lgfr[1]:.0f}) to opponents"
    return txt


def _nba_minutes(tid, norm):
    """His minutes in his last 5 games for his CURRENT team (newest first). None = box scores still loading."""
    mins = []
    for eid, _al in _nba_team_games(tid):
        box = _nba_game_box(eid)
        if box is None:
            return None
        r = box.get(norm)
        if r:
            mins.append(r["minutes"])
        if len(mins) >= 5:
            break
    return mins


def nba_gate(cands):
    """v3.0: every NBA pregame pick must be (1) a heavy-minute player (28+ minutes a game lately for his current team)
    and (2) facing a defense that helps: OVER only vs a team that allows more points than average, UNDER only vs one
    that allows fewer. Bench players and neutral/bad matchups are dropped."""
    out, dropped = [], 0
    teams = None
    for c in cands:
        key, ln, ev, a = c
        if (a.get("sport") or ln.get("sport")) != "NBA":
            out.append(c)
            continue
        teams = teams or nba_teams()
        mine, opp = _nba_team(teams, ln.get("team")), _nba_team(teams, ln.get("opp"))
        if not mine or not opp:
            dropped += 1
            continue
        mins = _nba_minutes(mine, ln["norm"])
        if not mins or len(mins) < 2 or sum(mins) / len(mins) < NBA_STARTER_MIN:
            dropped += 1
            continue
        allowed = _nba_allowed(teams)
        if opp not in allowed:
            dropped += 1
            continue
        lg = sum(allowed.values()) / len(allowed)
        ratio = allowed[opp] / lg
        if (a["direction"] == "OVER" and ratio < 1 + NBA_MATCHUP_EDGE) or \
                (a["direction"] == "UNDER" and ratio > 1 - NBA_MATCHUP_EDGE):
            dropped += 1
            continue
        notes = [n for n in a.get("notes", []) if "Matchup" not in n]
        a["notes"] = [f"⭐ Starter: {sum(mins) / len(mins):.0f} minutes a game lately",
                      f"✅ Matchup: {_nba_matchup_txt(teams, opp)}"] + notes
        out.append(c)
    if dropped and time.time() - _state.get("nba_gate_log_t", 0) > 3600:
        _state["nba_gate_log_t"] = time.time()
        print(f"NBA GATE: {dropped} NBA pregame picks dropped (bench minutes, unknown team, or matchup not helping)")
    return out


def nba_form_pregame(board):
    lines = [(k, ln) for k, ln in board.items() if ln["sport"] == "NBA" and ln.get("half") is None
             and ln["stat"] in NBA_FORM_STATS and not ln["live"] and ln["line"] > 0 and _upcoming([ln])]
    if not lines:
        return []
    teams = nba_teams()
    if not teams:
        return []
    allowed = {}
    out, waiting, checked = [], 0, 0
    for key, ln in lines:
        mine, opp = _nba_team(teams, ln["team"]), _nba_team(teams, ln["opp"])
        if not mine:
            continue
        games = _nba_team_games(mine)
        vals, mins, ready = [], [], True
        for eid, _al in games:
            box = _nba_game_box(eid)
            if box is None:
                ready = False
                break
            r = box.get(ln["norm"])
            if r:
                vals.append(r[ln["stat"]])
                mins.append(r["minutes"])
        if not ready:
            waiting += 1
            continue
        checked += 1
        if not vals or sum(mins) / len(mins) < NBA_MIN_MINUTES:
            continue
        stat, L = ln["stat"], ln["line"]
        n = len(vals)
        avg = sum(vals) / n
        direction = "OVER" if avg > L else "UNDER"
        good = sum(1 for v in vals if (v > L if direction == "OVER" else v < L))
        rate = good / n
        need = max(NBA_FORM_GAP[stat], 0.10 * L)
        # v3.0 SOFT-MATCHUP PLAY: a main player (30+ min) facing one of the softest defenses -> his OVER needs less
        # proof (60%+ of games over, average above the line, 3+ games with his current team).
        soft = False
        if opp and stat in NBA_SOFT_STATS and direction == "OVER" and sum(mins) / len(mins) >= NBA_SOFT_MIN:
            al = _nba_allowed(teams)
            if opp in al:
                lg0 = sum(al.values()) / len(al)
                soft = (al[opp] / lg0 >= NBA_SOFT_RATIO and n >= 3 and rate >= 0.6 and avg - L >= 0.5 * need)
        if not soft and (n < FORM_MIN_PLAYED or rate < FORM_RATE or abs(avg - L) < need):
            continue
        reasons = [f"✅ {good} of his last {n} games went {direction} {L:g}",
                   f"✅ Averaging {avg:.1f} vs a line of {L:g} ({sum(mins) / len(mins):.0f} minutes a game)"]
        if soft:
            reasons.insert(0, "🎯 Soft-matchup play: main player vs one of the softest defenses")
        r3 = False
        if opp and stat in NBA_SCORING:
            if not allowed:
                allowed = _nba_allowed(teams)
            if opp in allowed and allowed:
                lg = sum(allowed.values()) / len(allowed)
                ratio = allowed[opp] / lg
                what = _nba_matchup_txt(teams, opp)
                if (direction == "OVER" and ratio >= 1.04) or (direction == "UNDER" and ratio <= 0.96):
                    r3 = True
                    reasons.append(f"✅ Matchup helps: {what}")
                elif (direction == "OVER" and ratio <= 0.96) or (direction == "UNDER" and ratio >= 1.04):
                    continue                            # matchup is against it: skip
                else:
                    reasons.append(f"➖ Matchup is neutral: {what}")
        last5 = vals[:5]
        l5 = sum(last5) / len(last5)
        r4 = len(last5) >= 4 and ((l5 - L) if direction == "OVER" else (L - l5)) >= 0.5 * need
        if r4:
            reasons.append(f"✅ Last {len(last5)}: averaging {l5:.1f}")
        if not (r3 or r4):
            continue
        notes = reasons + [f"Last {n}: " + ", ".join(f"{v:g}" for v in vals),
                           "Check injuries and minutes news before you lock (the bot can't see them)"]
        if float(L).is_integer():
            notes.append(f"Whole-number line: exactly {L:g} is a push")
        start = _upcoming([ln])
        n_reasons = 2 + int(r3) + int(r4)
        a = {"direction": direction, "proj": avg, "gap": avg - L, "cur": 0, "line": L, "margin": 0, "team": {},
             "opp": {}, "notes": notes, "factor": 1.0, "moved": 0.0, "prob": rate,
             "strength": rate / FORM_RATE * (1 + 0.15 * (n_reasons - 3)), "shape": "PRE-pregame",
             "game": "NB" + str(ln.get("gid") or ln["team"]), "pre": True, "sport": "NBA", "form": True,
             "score_txt": f"{teams[mine]['abbr']} vs {teams[opp]['abbr'] if opp else ln['opp']} • starts {_start_txt(start)}"}
        ev = {"id": a["game"], "name": f"{ln['name']} ({teams[mine]['name']}) — {n_reasons} reasons agree", "f": 0.0,
              "period": 0, "clock_txt": ""}
        out.append((key, ln, ev, a))
    if time.time() - _state.get("nba_form_log_t", 0) > 3600 or (waiting == 0 and not _state.get("nba_form_ready")):
        _state["nba_form_log_t"] = time.time()
        _state["nba_form_ready"] = waiting == 0
        print(f"NBA FORM CHECK: {len(lines)} pregame lines, {checked} checked, {waiting} still loading box scores, "
              f"{len(out)} picks ({len(_state.get('nba_box', {}))} box scores cached)")
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


def _can_alert(key, direction, game=None, line=None):
    """v1.9.8: the same player + stat + lean posts again only when PrizePicks shows a DIFFERENT number
    (each new line is a new pick), and never twice at the same number in one game."""
    if not _room():
        return False
    last = _state["alerts"].get(key)
    if last and last[1] == direction:
        if game is not None and len(last) > 3 and last[2] == game:
            return line is not None and line != last[3]
        if time.time() - last[0] < COOLDOWN:
            return False
    return True


def _emoji(a):
    return "🔥" if a["direction"] == "OVER" else "🧊"


def _score_text(event, a):
    if a.get("score_txt"):
        return a["score_txt"]
    t, o = a["team"], a["opp"]
    return f"{t['abbr'] or t['name']} {t['score']} - {o['score']} {o['abbr'] or o['name']} • {'P' if a.get('sport') == 'NHL' else 'Q'}{event['period']} {event['clock_txt']}"


def _pick_line(line, a):
    """v2.1: the pick in PrizePicks words, first line of every pregame post."""
    word = "MORE" if a["direction"] == "OVER" else "LESS"
    arrow = "⬆️" if a["direction"] == "OVER" else "⬇️"
    return f"👉 **PICK: {word} ({a['direction']}) {line['line']:g} {stat_label(line)}** {arrow}\n"


def _leg_text(line, a):
    if a.get("form"):
        return (_pick_line(line, a) + f"**{line['name']}** — {stat_label(line)} **{line['line']:g}** → **{a['direction']}**\n"
                f"Recent average **{a['proj']:.1f}** (gap {a['gap']:+.1f})\n"
                + "\n".join(f"• {n}" for n in a["notes"]))
    if a.get("pre"):
        return (_pick_line(line, a) + f"**{line['name']}** — {stat_label(line)} **{line['line']:g}** → **{a['direction']}**\n"
                f"Fair value from the other lines **{a['proj']:.1f}** (gap {a['gap']:+.1f})\n"
                + "\n".join(f"• {n}" for n in a["notes"]))
    return (f"**{line['name']}** — {stat_label(line)} **{line['line']:g}** → **{a['direction']}**\n"
            f"Now **{a['cur']:g}** • Projected **{a['proj']:.1f}** (gap {a['gap']:+.1f})\n"
            + "\n".join(f"• {n}" for n in a["notes"]))


PROP_ONCE_H = float(os.getenv("HB_PROP_ONCE_HOURS", "20"))   # v3.0: a prop is used in ONE post only (no reuse with a new partner)


def _mark_sent(key, a):
    _state.setdefault("used_props", {})[key] = (time.time(), a["direction"], a.get("line"))
    _state["alerts"][key] = (time.time(), a["direction"], a.get("game"), a.get("line"))
    _state["pending"].pop(key, None)
    if a.get("pre"):
        _state["pre_sent"][key] = a["line"]                   # posted once at this line; again only if the line changes
        _state["pre_times"].append(time.time())


def _channel(c):
    """Esports -> ESPORTS channel (if set), other pregame -> PREGAME channel, live -> the main channel."""
    if not c[3].get("pre"):
        return None
    if (c[3].get("sport") or c[1].get("sport")) in ("CS2", "LOL", "VAL") and ESPORTS_WEBHOOK_URL:
        return ESPORTS_WEBHOOK_URL
    return PREGAME_WEBHOOK_URL


PP_BOARD_URL = os.getenv("HB_PP_LINK", "https://app.prizepicks.com/board")
PP_LINK_TXT = f"👉 [Open PrizePicks](<{PP_BOARD_URL}>) (search the player name)"   # v3.0: one tap to the board


def send_single(cand, label="no pair found"):
    key, line, event, a = cand
    _mark_sent(key, a)
    _state["sent"].append(time.time())
    footer = "Model v3.0 • alert only • projection, not a guarantee" + (f" • {label}" if label else "")
    tag = " PREGAME" if a.get("pre") else (" LIVE" if line["live"] else "")
    lean = ("⚠️ CAUTION — LEAN " if a.get("caution") else "LEAN ") + a["direction"]
    color = 15105570 if a.get("caution") else (3066993 if a["direction"] == "OVER" else 3447003)
    embed = {"title": f"{_emoji(a)} Heartbeat — {line['sport']}{tag} — {lean}",
             "description": _leg_text(line, a), "color": color,
             "fields": [{"name": event["name"], "value": _score_text(event, a), "inline": False}],
             "footer": {"text": footer}}
    txt = f"💓{tag} SINGLE\n**{line['name']} — {stat_label(line)} {a['direction']} {line['line']:g}**\n{PP_LINK_TXT}"
    _post({"content": txt[:1900], "embeds": [embed]}, _channel(cand))
    print(f"ALERT(single{', CAUTION' if a.get('caution') else ''}) {line['sport']} {line['name']} {line.get('half') or 'full'} {line['stat']} line {line['line']} proj {a['proj']:.1f} "
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
             "footer": {"text": "Model v3.0 • alert only • projections, not guarantees"}}
    picks = "\n".join(f"**{i}. {l['name']} — {stat_label(l)} {a['direction']} {l['line']:g}**"
                       for i, (_, l, _, a) in enumerate((c1, c2), 1))
    # v3.0: the two picks also go in the plain message text, so they show even if Discord doesn't load the box
    _post({"content": f"💓 {tag}PAIR\n{picks}\n{PP_LINK_TXT}"[:1900], "embeds": [embed]}, _channel(c1))
    print(f"ALERT(pair) {' + '.join(legs)}")


PRE_PAIR_WAIT = float(os.getenv("HB_PRE_PAIR_WAIT_MINUTES", "45")) * 60
# v2.6: pregame picks post ONLY as pairs (two juicy picks from different games). A pick with no partner is not posted.
PRE_PAIRS_ONLY = os.getenv("HB_PRE_PAIRS_ONLY", "true").lower() == "true"
# v3.0: live picks post ONLY as pairs too (two legs from different live games). No live singles, no CAUTION singles.
# A live pick waits for a partner; if it stops qualifying first, it is dropped. Set HB_LIVE_PAIRS_ONLY=false to go back.
LIVE_PAIRS_ONLY = os.getenv("HB_LIVE_PAIRS_ONLY", "true").lower() == "true"


ES_SPORTS = ("CS2", "LOL", "VAL")
# v3.0: esports pregame picks pair ONLY with other esports picks. With no esports partner, one posts alone after
# HB_ES_SOLO_WAIT_MINUTES (10) so nobody waits for tonight's NHL/NBA games.
ES_SOLO_WAIT = float(os.getenv("HB_ES_SOLO_WAIT_MINUTES", "10")) * 60


def _is_es(c):
    return c[3].get("sport") in ES_SPORTS


def _pair_wait(c):
    """v2.5: live picks wait 75 seconds for a partner. Pregame picks have hours, so they wait up to 45 minutes,
    but always post alone by 1 hour before their game starts."""
    if not c[3].get("pre"):
        return PAIR_WAIT
    if _is_es(c):
        st = c[1].get("start")
        left = (st - _now()).total_seconds() - 300 if st else ES_SOLO_WAIT
        return max(0.0, min(ES_SOLO_WAIT, left))
    st = c[1].get("start")
    if not st:
        return PAIR_WAIT
    left = (st - _now()).total_seconds() - 3600
    return max(PAIR_WAIT, min(PRE_PAIR_WAIT, left))


def process_candidates(fresh):
    """fresh = [(key, line, event, alert)] that qualify on THIS cycle's data. Pairs them across different games."""
    now = time.time()
    live_keys = {c[0] for c in fresh}
    for k in list(_state["pending"]):                 # a pick that stopped qualifying is dropped
        if k not in live_keys:
            del _state["pending"][k]
    ready = []
    nowdt = _now()
    fresh = [c for c in fresh if not (c[3].get("pre") and (c[1].get("live")
             or (c[1].get("start") is not None and nowdt >= c[1]["start"])))]   # v3.0: pregame picks ONLY before kickoff
    used = _state.setdefault("used_props", {})
    for k in [k for k, v in used.items() if time.time() - v[0] > PROP_ONCE_H * 3600]:
        del used[k]

    def _flipped(c):                                   # only a NEW line where the bot now likes the OTHER side counts
        v = used[c[0]]
        return v[1] is not None and c[3]["direction"] != v[1] and c[3].get("line") != v[2]
    fresh = [c for c in fresh if c[0] not in used or _flipped(c)]   # v3.0: a prop posts ONCE (restarts included)
    for c in fresh:
        if c[3].get("pre"):
            if _state["pre_sent"].get(c[0]) == c[3]["line"] or not _pre_room():
                continue                               # already posted at this line, or the pregame hourly cap is reached
        if not _can_alert(c[0], c[3]["direction"], None if c[3].get("pre") else c[3].get("game"), c[3].get("line")):
            continue
        _state["pending"].setdefault(c[0], now)
        ready.append(c)
    posted = 0
    if LIVE_PAIRS_ONLY and PAIR_MODE:
        ready = [c for c in ready if not c[3].get("caution")]   # v3.0: CAUTION picks only posted as singles, so they are skipped
    for c in [c for c in ready if c[3].get("caution")]:      # CAUTION picks: alone, right away, never in a pair
        if not _room():
            break
        send_single(c, label="caution: pace vs score")
        posted += 1
    ready = [c for c in ready if not c[3].get("caution")]
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
            if c1[3].get("pre") and _is_es(c1) != _is_es(c2):        # v3.0: esports pair only with esports
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
        if PRE_PAIRS_ONLY and c[3].get("pre") and not _is_es(c):
            continue                                   # v2.6: pregame picks only ever post as a PAIR (esports: see ES_SOLO_WAIT)
        if LIVE_PAIRS_ONLY and not c[3].get("pre"):
            continue                                   # v3.0: live picks only ever post as a PAIR
        if c[0] not in used and now - _state["pending"].get(c[0], now) >= _pair_wait(c):
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
    lines.append("Pregame line check: " + (f"ON (max {PRE_MAX_LEGS} picks an hour)" if PREGAME_ON else "off")
                 + (" → posts to the PREGAME channel" if PREGAME_WEBHOOK_URL else " → same channel (no PREGAME_WEBHOOK_URL yet)"))
    lines.append("Esports picks → " + ("their own ESPORTS channel" if ESPORTS_WEBHOOK_URL else
                                       "the PREGAME channel (no ESPORTS_WEBHOOK_URL found)"))
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
            if _state["prior"].get(key) != ln["line"]:
                _state["prior"][key] = ln["line"]
                _state.setdefault("prior_t", {})[key] = time.time()


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
    _state["live_sports"] = {sp for sp in SPORTS if live.get(sp)}
    save_state()
    if time.time() - _state.get("diag_t", 0) > 600 and any(live.values()):     # every 10 min while games are live
        _state["diag_t"] = time.time()
        for sport in SPORTS:
            names = live_names.get(sport, set())
            mine = [k for k in board if k[0] == sport and k[1] in names]
            with_prior = sum(1 for k in mine if get_prior(k, board[k])[0] is not None)
            if sport == SPORTS[0]:
                print(f"LIVE CHECK board: {len(board)} lines, {sum(1 for v in board.values() if v['live'])} marked live | "
                      f"memory {_mem_mb():.0f} MB")
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
            if not USE_PREGAME_LINE:                 # v3.0: live picks start from PrizePicks' CURRENT line, not the pregame one
                prior, est = ln["line"], False
            else:
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
            _state["form_budget"] = FORM_FETCH_PER_CYCLE
            pre_fresh = all_combo_pregame(board) + (nhl_saves_pregame(board) + nhl_form_pregame(board)
                                                    if "NHL" in SPORTS else []) + (tennis_pregame(_state["tboard"]) if TENNIS_ON else [])
            if ES_ON:
                pre_fresh += esports_pregame(raw)
            if "NBA" in SPORTS:
                pre_fresh += nba_form_pregame(board)
        except Exception as e:                      # the pregame check can never stop live alerts
            print("pregame error:", e)
        try:
            pre_fresh = nba_gate(pre_fresh)        # v3.0: NBA = starters only, and only with a matchup that helps
        except Exception as e:
            print("nba gate error:", e)
            pre_fresh = [c for c in pre_fresh if (c[3].get("sport") or c[1].get("sport")) != "NBA"]
    fresh += pre_fresh
    sent = process_candidates(fresh)
    n_live = sum(len(v) for v in live.values())
    if n_live or first or t_fresh or sent:
        print(f"cycle: {n_live} live games, {len(board)} lines tracked, {checked} projected, "
              f"{len(t_fresh)} tennis picks, {len(pre_fresh)} pregame picks, {sent} alerts")


def main():
    if not WEBHOOK_URL:
        print("WARNING: WEBHOOK_URL is not set. Alerts will only print in the logs.")
    load_state()
    print(f"Heartbeat v3.0 starting. Sports: {SPORTS}{' + TENNIS' if TENNIS_ON else ''}. "
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
