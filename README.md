# World Cup Score Notifier — Discord Bot

Posts the final score of every FIFA World Cup 2026 match into a chosen
Discord channel as soon as it finishes, including the stage of the
competition (Group Stage, Round of 16, Quarter-final, Semi-final, Final,
Third Place Play-off, etc.).

## How it works

The bot only polls for live scores **during match windows**, to minimise API
usage and run comfortably on free hosting tiers / free API quotas:

1. **Once a day**, it fetches the day's full fixture schedule (1 API request)
   and notes each match's kickoff time.
2. **Outside match hours**, it just checks the clock every
   `IDLE_CHECK_INTERVAL_SECONDS` (default 30 min) — no result-polling, almost
   no API usage.
3. **From `PRE_MATCH_BUFFER_MINUTES` before kickoff** until
   `MATCH_WINDOW_MINUTES` after kickoff (default 150 min, covering 90 mins +
   stoppage + extra time + penalties), it polls for finished matches every
   `LIVE_POLL_INTERVAL_SECONDS` (default 2 min).
4. Any newly-finished match it hasn't posted before gets sent as an embed to
   your configured channel, e.g.:

> **France** 2 - 1 Spain
> Full time
> Stage: Semi-final
> Venue: MetLife Stadium

Already-posted matches are remembered in `seen_matches.json` so restarting
the bot won't cause duplicate posts.

### API usage estimate

On a day with matches, the bot makes:
- 1 request for the daily schedule
- ~75 requests per match window (150 min ÷ 2 min interval) — overlapping
  windows (common during group stage, when multiple matches kick off close
  together) share polling, so total requests scale with the number of
  *distinct, non-overlapping* windows in a day, not the number of matches

If that's still too high for your API plan, increase
`LIVE_POLL_INTERVAL_SECONDS` (e.g. to 300 for 5-minute polling) or reduce
`MATCH_WINDOW_MINUTES`.

## Setup

### 1. Create a Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. New Application -> give it a name -> Bot tab -> Add Bot
3. Copy the **bot token**
4. Under "Privileged Gateway Intents" you don't need to enable anything extra
   for this bot (it only sends messages)
5. Go to OAuth2 -> URL Generator, select scope `bot`, and permission
   `Send Messages` (and `Embed Links`), then use the generated URL to invite
   the bot to your server

### 2. Get an API-Football key

1. Sign up at [api-football.com](https://www.api-football.com/) (free tier
   gives 100 requests/day, which is plenty for polling every 2 minutes during
   match windows — see notes below on rate limits)
2. Copy your API key

### 3. Get your channel ID

1. In Discord, enable Developer Mode: Settings -> Advanced -> Developer Mode
2. Right-click the channel you want results posted to -> Copy Channel ID

### 4. Configure the bot

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `DISCORD_TOKEN`
- `CHANNEL_ID`
- `API_FOOTBALL_KEY`

### 5. Install dependencies and run

```bash
pip install -r requirements.txt
python bot.py
```

## Notes on rate limits

Because the bot now only polls during match windows (see "How it works"
above), API usage is far lower than a constant 24/7 poll. On most days this
comfortably fits within the API-Football free tier (100 requests/day) as
long as you don't have too many overlapping match windows at once. If you do
exceed the limit on a heavy multi-match day:

- Increase `LIVE_POLL_INTERVAL_SECONDS` (e.g. to 300 for 5-minute polling)
- Reduce `MATCH_WINDOW_MINUTES` (e.g. to 120 if you're not fussed about
  posting penalty-shootout results that run very long)
- Upgrade to a paid API-Football plan for higher limits

## Customisation

- **Different stages**: edit `normalise_stage()` in `bot.py` to change how
  round names are displayed.
- **Different data source**: replace `fetch_finished_matches()` /
  `fetch_fixtures_for_date()` with calls to another provider (e.g.
  football-data.org, TheSportsDB) — just make sure the returned data is
  reshaped to match what `build_embed()` and `parse_kickoff()` expect, or
  adjust those functions accordingly.
- **Multiple channels** (e.g. one per group): you can extend
  `check_and_post_results` to pick a channel based on
  `fixture["league"]["round"]`.
- **Match window length**: tune `PRE_MATCH_BUFFER_MINUTES` and
  `MATCH_WINDOW_MINUTES` in `.env` if matches in your timezone tend to run
  long (extra time + penalties) or you want tighter polling.

## Running continuously

For 24/7 operation, run this on a small VPS, Raspberry Pi, or with a process
manager like `pm2` or `systemd`, or inside a Docker container with a restart
policy.
