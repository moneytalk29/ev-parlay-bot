"""Run this one file. It starts the daily 9am ET poster and (if a token is set) the Discord bot."""
import os
import threading
import time

import odds_scan


def main():
    threading.Thread(target=odds_scan.scheduler_loop, daemon=True).start()
    token = os.getenv("DISCORD_BOT_TOKEN")
    if token:
        import bot
        bot.run(token)
    else:
        print("No DISCORD_BOT_TOKEN set - running the daily poster only.")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
