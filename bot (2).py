import os
import json
import aiohttp
import discord

from pathlib import Path
from dotenv import load_dotenv
from discord.ext import tasks

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

SEEN_FILE = Path("seen_matches.json")

intents = discord.Intents.default()
client = discord.Client(intents=intents)

WORLD_CUP_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/"
    "soccer/fifa.world/scoreboard"
)

# -------------------------------------------------
# Persistence
# -------------------------------------------------

def load_seen():

    if SEEN_FILE.exists():

        try:

            return set(
                json.loads(
                    SEEN_FILE.read_text()
                )
            )

        except Exception:

            return set()

    return set()


def save_seen(data):

    SEEN_FILE.write_text(
        json.dumps(
            list(data)
        )
    )


seen_matches = load_seen()

# -------------------------------------------------
# ESPN API
# -------------------------------------------------

async def fetch_matches():

    async with aiohttp.ClientSession() as session:

        async with session.get(WORLD_CUP_URL) as response:

            data = await response.json()

            return data.get("events", [])


# -------------------------------------------------
# Embed
# -------------------------------------------------

def build_embed(match):

    competition = match["competitions"][0]

    competitors = competition["competitors"]

    home = None
    away = None

    for team in competitors:

        if team["homeAway"] == "home":
            home = team

        if team["homeAway"] == "away":
            away = team

    home_name = home["team"]["displayName"]
    away_name = away["team"]["displayName"]

    home_score = home["score"]
    away_score = away["score"]

    status = competition["status"]["type"]["description"]

    embed = discord.Embed(
        title=f"{home_name} {home_score}-{away_score} {away_name}",
        description=f"🏆 {status}",
        color=0x2ECC71
    )

    embed.set_footer(
        text="FIFA World Cup 2026"
    )

    return embed


# -------------------------------------------------
# Polling
# -------------------------------------------------

@tasks.loop(seconds=60)
async def check_results():

    channel = client.get_channel(CHANNEL_ID)

    if channel is None:
        return

    try:

        matches = await fetch_matches()

        for match in matches:

            match_id = match["id"]

            status = (
                match["competitions"][0]
                ["status"]
                ["type"]
                ["state"]
            )

            if status != "post":
                continue

            if match_id in seen_matches:
                continue

            embed = build_embed(match)

            await channel.send(embed=embed)

            seen_matches.add(match_id)

            save_seen(seen_matches)

            print(
                f"Posted result "
                f"{match_id}"
            )

    except Exception as e:

        print("Error:", e)


@check_results.before_loop
async def before_loop():

    await client.wait_until_ready()


@client.event
async def on_ready():

    print(
        f"Logged in as {client.user}"
    )

    if not check_results.is_running():

        check_results.start()


client.run(DISCORD_TOKEN)
