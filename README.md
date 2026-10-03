# EV Suite

Files
- index.html  : the website (open it in a browser; works offline, or host it on GitHub Pages/Netlify)
- ev_core.py  : the math (American -> decimal -> implied % -> EV%, parlay EV)
- bot.py      : /ev and /parlay Discord commands
- odds_scan.py: daily 9am ET scan (Pinnacle true lines vs FanDuel, DraftKings, BetMGM, Caesars, BetRivers)
- main.py     : start this one file; it runs the daily poster and the bot together

Railway variables
- ODDS_API_KEY       (required) from the-odds-api.com
- WEBHOOK_URL        (required) Discord webhook for the daily post
- DISCORD_BOT_TOKEN  (for /ev and /parlay) from the Discord developer portal
- GUILD_ID           (optional) your server ID, makes the slash commands appear instantly
- RUN_NOW=true       (optional) posts once at startup as a test; delete it after testing
- SPORTS, MARKETS, MIN_EV, MAX_LEG_EV, MIN_TRUE_PROB, MAX_LEGS, HOURS_AHEAD, POST_HOUR (optional tuning)

Start command: python main.py
Bot invite scopes: bot + applications.commands (no privileged intents needed).
