"""Discord slash commands: /ev and /parlay."""
import os
import discord
from discord import app_commands
import ev_core as core

GREEN, RED = 3066993, 15158332


class EvBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        gid = os.getenv("GUILD_ID")          # set GUILD_ID so commands appear instantly
        if gid:
            guild = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()           # global sync can take up to an hour

    async def on_ready(self):
        print(f"Bot online as {self.user}")


bot = EvBot()


@bot.tree.command(name="ev", description="Compare books' odds against Pinnacle's true probability")
@app_commands.describe(book_odds="Books and American odds, e.g. DraftKings +150, FanDuel +145",
                       true_prob="Pinnacle no-vig win probability in %, e.g. 42.5")
async def ev_cmd(interaction: discord.Interaction, book_odds: str, true_prob: str):
    try:
        p = core.parse_prob(true_prob)
        rows = core.evaluate_books(core.parse_book_odds(book_odds), p)
    except ValueError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    best = rows[0]
    lines = []
    for r in rows:
        mark = "🟢" if r["ev"] > 0 else "🔴"
        tag = "PLAY ✅" if r["verdict"] == "PLAY" else "SKIP"
        lines.append(f"{mark} **{r['book']}** {core.fmt_american(r['american'])} • dec {r['decimal']:.3f} • "
                     f"implied {r['implied']*100:.1f}% • EV **{r['ev']:+.2f}%** → {tag}")
    embed = discord.Embed(
        title=f"+EV check — true prob {p*100:.1f}% (fair {core.fmt_american(core.decimal_to_american(1/p))})",
        description="\n".join(lines),
        color=GREEN if best["ev"] > 0 else RED)
    embed.add_field(name="BEST BOOK",
                    value=f"**{best['book']}** {core.fmt_american(best['american'])} • EV {best['ev']:+.2f}% → {best['verdict']}")
    embed.set_footer(text="PLAY = EV above 2%. EV is an estimate, not a guarantee.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="parlay", description="EV of a 2-5 leg parlay")
@app_commands.describe(legs="Each leg as odds@true%, comma separated, e.g. +150@45, -110@55, +200@36")
async def parlay_cmd(interaction: discord.Interaction, legs: str):
    try:
        res = core.parlay(core.parse_legs(legs))
    except ValueError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    lines = [f"{'🟢' if l['ev'] > 0 else '🔴'} Leg {i}: {core.fmt_american(l['american'])} • true "
             f"{l['true_prob']*100:.1f}% • EV {l['ev']:+.2f}%" for i, l in enumerate(res["legs"], start=1)]
    embed = discord.Embed(
        title=f"{len(res['legs'])}-leg parlay: {core.fmt_american(res['american'])} → {res['verdict']}",
        description="\n".join(lines),
        color=GREEN if res["ev"] > 0 else RED)
    embed.add_field(name="Total EV", value=f"**{res['ev']:+.2f}%**", inline=True)
    embed.add_field(name="Win chance", value=f"{res['win_prob']*100:.1f}%", inline=True)
    embed.add_field(name="Decimal", value=f"{res['decimal']:.3f}", inline=True)
    embed.set_footer(text="Assumes independent legs. Parlays must be placed at one book.")
    await interaction.response.send_message(embed=embed)


def run(token):
    bot.run(token)
