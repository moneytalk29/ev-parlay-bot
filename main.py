"""Heartbeat v1.4: live NFL + NBA game-script scanner for PrizePicks lines. ALERT ONLY, it never places bets.

What it does
  1. Reads the PrizePicks board (same public feed Captain Hook uses) for NFL and NBA player lines.
  2. Remembers each line from BEFORE kickoff (the "pregame line"), which is the market's expectation for the player.
  3. While a game is live, reads the score, clock and player box score from ESPN's free public data.
  4. Projects the player's final total using the game script:
       NFL: a team that trails throws more and runs less; a team that leads runs more and throws less.
       NBA: in a blowout starters sit (fewer minutes); in a tight finish starters play more.
            v1.4 adds 1st-half / 2nd-half props (1H and 2H points, rebounds, assists, PRA, fantasy score) and full-game
            PRA / fantasy score. 2H props are judged at HALFTIME using the score, first-half minutes and foul trouble.
  5. Posts to your Discord webhook when the projection is far enough from PrizePicks' current line.
     Picks go out as PAIRS (two legs from DIFFERENT games, never the same game, same game-shape preferred) so you can
     lock both fast. A lone pick waits HB_PAIR_WAIT_SECONDS for a partner, then posts alone, so nothing is lost.

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
# Pairs: live lines move fast, so picks go out two at a time (PrizePicks needs 2+ legs).
PAIR_MODE = os.getenv("HB_PAIR_MODE", "true").lower() == "true"
PAIR_WAIT = float(os.getenv("HB_PAIR_WAIT_SECONDS", "75"))     # how long a lone pick waits for a partner before posting alone
PAIR_REQUIRE_SAME_SHAPE = os.getenv("HB_PAIR_REQUIRE_SAME_SHAPE", "false").lower() == "true"  # true = only pair lopsided with lopsided, tight with tight
PAIR_SAME_SHAPE_BONUS = float(os.getenv("HB_PAIR_SAME_SHAPE_BONUS", "0.3"))   # preference for same-shape partners
LOPSIDED = {"NFL": float(os.getenv("HB_NFL_LOPSIDED", "10")), "NBA": float(os.getenv("HB_NBA_LOPSIDED", "15"))}  # margin that makes a game "lopsided"
# NFL game-script strength: change in volume per 7 points of margin (a "score"), and the cap
PASS_TRAIL, PASS_TRAIL_CAP = float(os.getenv("HB_PASS_TRAIL", "0.07")), float(os.getenv("HB_PASS_TRAIL_CAP", "0.30"))
PASS_LEAD, PASS_LEAD_CAP = float(os.getenv("HB_PASS_LEAD", "0.05")), float(os.getenv("HB_PASS_LEAD_CAP", "0.20"))
RUSH_LEAD, RUSH_LEAD_CAP = float(os.getenv("HB_RUSH_LEAD", "0.08")), float(os.getenv("HB_RUSH_LEAD_CAP", "0.30"))
RUSH_TRAIL, RUSH_TRAIL_CAP = float(os.getenv("HB_RUSH_TRAIL", "0.09")), float(os.getenv("HB_RUSH_TRAIL_CAP", "0.35"))
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

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
           "Origin": "https://app.prizepicks.com", "Referer": "https://app.prizepicks.com/"}
ESPN = {"NFL": "https://site.api
