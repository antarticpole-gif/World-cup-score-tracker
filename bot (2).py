"""
World Cup Score Notifier — Discord Bot
----------------------------------------
Polls a football data API for World Cup 2026 matches and, whenever a match
finishes, posts the final score AND the stage of the competition
(Group Stage, Round of 16, Quarter-final, Semi-final, Final, etc.)
into a designated Discord channel.

To stay within free API rate limits, the bot is "schedule aware":
- Once a day it fetches the day's fixture list (1 request/day).
- It only polls for live results during active match windows (kickoff time
  through a configurable buffer afterwards), and sleeps the rest of the time.

Setup:
1. pip install -r requirements.txt
2. Copy .env.example to .env and fill in your values
3. Run: python bot.py

Data source: API-Football (api-football.com via RapidAPI or direct).
You can swap out the API calls to use a different provider (e.g. TheSportsDB,
football-data.org) if you prefer — just keep the shapes used in
`build_embed` and `fetch_finished_matches` consistent.
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

import aiohttp
import discord
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
WORLD_CUP_LEAGUE_ID = int(os.getenv("WORLD_CUP_LEAGUE_ID", "1"))   # 1 = World Cup in API-Football
WORLD_CUP_SEASON = int(os.getenv("WORLD_CUP_SEASON", "2026"))

# How often to check for results WHILE a match is live/recently finished
LIVE_POLL_INTERVAL_SECONDS = int(os.getenv("LIVE_POLL_INTERVAL_SECONDS", "120"))

# How often to check for "is it match time yet" / refresh schedule while idle
IDLE_CHECK_INTERVAL_SECONDS = int(os.getenv("IDLE_CHECK_INTERVAL_SECONDS", "1800"))

# How long after a scheduled kickoff to keep polling, to cover 90 mins +
# stoppage + extra time + penalties (knockout matches can run ~2.5 hrs)
MATCH_WINDOW_MINUTES = int(os.getenv("MATCH_WINDOW_MINUTES", "150"))

# How many minutes BEFORE kickoff to start polling (in case of early starts)
PRE_MATCH_BUFFER_MINUTES = int(os.getenv("PRE_MATCH_BUFFER_MINUTES", "5"))

API_BASE_URL = "https://v3.football.api-sports.io"

# File used to remember which finished matches we've already announced,
# so we don't post duplicates if the bot restarts.
SEEN_MATCHES_FILE = Path(__file__).parent / "seen_matches.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("wc-bot")

# ---------------------------------------------------------------------------
# Stage name mapping
# ---------------------------------------------------------------------------
# API-Football returns a free-text "round" string, e.g. "Group Stage - 1",
# "Round of 16", "Quarter-finals", "Semi-finals", "Final", "3rd Place Final".
# We normalise these into clean display labels.
def normalise_stage(round_name: str) -> str:
    if not round_name:
        return "Unknown stage"

    name = round_name.strip().lower()

    if "group" in name:
        # e.g. "Group Stage - 1" -> "Group Stage (Matchday 1)"
        parts = round_name.split("-")
        if len(parts) == 2 and parts[1].strip().isdigit():
            return f"Group Stage (Matchday {parts[1].strip()})"
        return "Group Stage"

    if "16" in name:
        return "Round of 16"

    if "quarter" in name:
        return "Quarter-final"

    if "semi" in name:
        return "Semi-final"

    if "3rd" in name or "third" in name:
        return "Third Place Play-off"

    if "final" in name:
        return "Final"

    # Fallback: title-case whatever the API gave us
    return round_name.strip()


# ---------------------------------------------------------------------------
# Persistence helpers (avoid duplicate posts across restarts)
# ---------------------------------------------------------------------------
def load_seen_matches() -> set[int]:
    if SEEN_MATCHES_FILE.exists():
        try:
            return set(json.loads(SEEN_MATCHES_FILE.read_text()))
        except (json.JSONDecodeError, ValueError):
            log.warning("Could not parse %s, starting fresh.", SEEN_MATCHES_FILE)
    return set()


def save_seen_matches(seen: set[int]) -> None:
    SEEN_MATCHES_FILE.write_text(json.dumps(sorted(seen)))


# ---------------------------------------------------------------------------
# API fetching
# ---------------------------------------------------------------------------
async def fetch_fixtures_for_date(session: aiohttp.ClientSession, day: date) -> list[dict]:
    """
    Fetch ALL fixtures (any status) for the World Cup on a given date.
    Used once a day to build today's match schedule.
    """
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {
        "league": WORLD_CUP_LEAGUE_ID,
        "season": WORLD_CUP_SEASON,
        "date": day.isoformat(),
    }

    async with session.get(f"{API_BASE_URL}/fixtures", headers=headers, params=params) as resp:
        if resp.status != 200:
            text = await resp.text()
            log.error("API request failed (%s): %s", resp.status, text)
            return []
        data = await resp.json()
        return data.get("response", [])


async def fetch_finished_matches(session: aiohttp.ClientSession) -> list[dict]:
    """
    Fetch all fixtures for the World Cup that have a 'finished' status.
    Returns a list of raw fixture dicts from API-Football.
    """
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {
        "league": WORLD_CUP_LEAGUE_ID,
        "season": WORLD_CUP_SEASON,
        "status": "FT-AET-PEN",  # Full Time, After Extra Time, Penalties
    }

    async with session.get(f"{API_BASE_URL}/fixtures", headers=headers, params=params) as resp:
        if resp.status != 200:
            text = await resp.text()
            log.error("API request failed (%s): %s", resp.status, text)
            return []
        data = await resp.json()
        return data.get("response", [])


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------
def build_embed(fixture: dict) -> discord.Embed:
    teams = fixture["teams"]
    goals = fixture["goals"]
    fixture_info = fixture["fixture"]
    league_info = fixture["league"]

    home_name = teams["home"]["name"]
    away_name = teams["away"]["name"]
    home_goals = goals["home"]
    away_goals = goals["away"]
    home_winner = teams["home"].get("winner")
    away_winner = teams["away"].get("winner")

    stage = normalise_stage(league_info.get("round", ""))
    status_long = fixture_info["status"]["long"]  # e.g. "Match Finished", "Penalties"

    # Bold the winning team's name (or both if it's a draw)
    def fmt(name: str, winner) -> str:
        return f"**{name}**" if winner else name

    title = f"{fmt(home_name, home_winner)} {home_goals} - {away_goals} {fmt(away_name, away_winner)}"

    embed = discord.Embed(
        title=title,
        description=f"Full time ({status_long})" if status_long != "Match Finished" else "Full time",
        color=0x2ECC71,
    )
    embed.add_field(name="Stage", value=stage, inline=True)
    embed.add_field(name="Venue", value=fixture_info.get("venue", {}).get("name") or "Unknown", inline=True)

    # Penalty shootout score if applicable
    pen = fixture.get("score", {}).get("penalty")
    if pen and (pen.get("home") is not None or pen.get("away") is not None):
        embed.add_field(name="Penalties", value=f"{pen['home']} - {pen['away']}", inline=True)

    home_logo = teams["home"].get("logo")
    if home_logo:
        embed.set_thumbnail(url=home_logo)

    embed.set_footer(text="FIFA World Cup 2026")
    return embed


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------
def parse_kickoff(fixture: dict) -> datetime:
    """Parse a fixture's kickoff time into a timezone-aware UTC datetime."""
    ts = fixture["fixture"]["timestamp"]
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def is_within_match_window(now: datetime, kickoffs: list[datetime]) -> bool:
    """
    Return True if `now` falls within any match's active window:
    [kickoff - PRE_MATCH_BUFFER_MINUTES, kickoff + MATCH_WINDOW_MINUTES]
    """
    for kickoff in kickoffs:
        window_start = kickoff - timedelta(minutes=PRE_MATCH_BUFFER_MINUTES)
        window_end = kickoff + timedelta(minutes=MATCH_WINDOW_MINUTES)
        if window_start <= now <= window_end:
            return True
    return False


def next_window_start(now: datetime, kickoffs: list[datetime]):
    """Return the start time of the next upcoming match window, if any."""
    upcoming = [
        k - timedelta(minutes=PRE_MATCH_BUFFER_MINUTES)
        for k in kickoffs
        if (k - timedelta(minutes=PRE_MATCH_BUFFER_MINUTES)) > now
    ]
    return min(upcoming) if upcoming else None


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)

seen_matches: set[int] = set()

# Cache of today's kickoff times (UTC), refreshed once a day (or when empty)
todays_kickoffs: list[datetime] = []
todays_kickoffs_date = None


async def refresh_todays_schedule(session: aiohttp.ClientSession) -> None:
    """Fetch today's fixtures (1 API request) and cache their kickoff times."""
    global todays_kickoffs, todays_kickoffs_date

    today = datetime.now(tz=timezone.utc).date()
    fixtures = await fetch_fixtures_for_date(session, today)

    kickoffs = [parse_kickoff(f) for f in fixtures]
    todays_kickoffs = kickoffs
    todays_kickoffs_date = today

    log.info(
        "Refreshed schedule for %s: %d match(es) found.",
        today.isoformat(),
        len(kickoffs),
    )
    for k in sorted(kickoffs):
        log.info("  Kickoff: %s UTC", k.strftime("%H:%M"))


async def check_and_post_results(session: aiohttp.ClientSession, channel) -> int:
    """Fetch finished matches and post any new ones. Returns count posted."""
    fixtures = await fetch_finished_matches(session)

    new_count = 0
    for fixture in fixtures:
        fixture_id = fixture["fixture"]["id"]
        if fixture_id in seen_matches:
            continue

        embed = build_embed(fixture)
        try:
            await channel.send(embed=embed)
            new_count += 1
        except discord.DiscordException as exc:
            log.error("Failed to send message for fixture %s: %s", fixture_id, exc)
            continue

        seen_matches.add(fixture_id)

    if new_count:
        save_seen_matches(seen_matches)
        log.info("Posted %d new result(s).", new_count)

    return new_count


@tasks.loop(seconds=30)
async def scheduler_loop():
    """
    Master loop. Decides whether to:
    - refresh the daily schedule
    - poll for live results (if inside a match window)
    - sleep (if idle)

    The loop's own interval is dynamically adjusted: short while a match is
    live, long while idle — this is what keeps API usage low outside match
    hours.
    """
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        log.error("Could not find channel with ID %s. Check CHANNEL_ID and bot permissions.", CHANNEL_ID)
        return

    now = datetime.now(tz=timezone.utc)
    today = now.date()

    async with aiohttp.ClientSession() as session:
        # Refresh schedule once per day (or if we don't have one yet)
        if todays_kickoffs_date != today:
            await refresh_todays_schedule(session)

        if is_within_match_window(now, todays_kickoffs):
            log.info("Inside a match window — polling for results.")
            await check_and_post_results(session, channel)
            scheduler_loop.change_interval(seconds=LIVE_POLL_INTERVAL_SECONDS)
        else:
            nxt = next_window_start(now, todays_kickoffs)
            if nxt:
                wait_secs = max(0, (nxt - now).total_seconds())
                log.info(
                    "No live matches. Next match window starts at %s UTC (in %.0f min).",
                    nxt.strftime("%H:%M"),
                    wait_secs / 60,
                )
            else:
                log.info("No more matches scheduled today.")

            # Sleep longer while idle, but cap so we don't miss a midnight
            # schedule refresh or a same-day window we hadn't accounted for.
            scheduler_loop.change_interval(seconds=IDLE_CHECK_INTERVAL_SECONDS)


@scheduler_loop.before_loop
async def before_scheduler():
    await client.wait_until_ready()


@client.event
async def on_ready():
    global seen_matches
    seen_matches = load_seen_matches()
    log.info("Logged in as %s", client.user)
    log.info("Loaded %d previously-seen match IDs.", len(seen_matches))
    if not scheduler_loop.is_running():
        scheduler_loop.start()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Add it to your .env file.")
    if not CHANNEL_ID:
        raise SystemExit("CHANNEL_ID is not set. Add it to your .env file.")
    if not API_FOOTBALL_KEY:
        raise SystemExit("API_FOOTBALL_KEY is not set. Add it to your .env file.")

    client.run(DISCORD_TOKEN)
