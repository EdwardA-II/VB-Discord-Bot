import asyncio
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ---------------------------------
# 1. Load environment variables
# ---------------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
POLL_CHANNEL_ID = int(os.getenv("POLL_CHANNEL_ID", "0"))
EVENT_VOTE_THRESHOLD = 12

# Optional: send notifications to hosts
HOST_IDS = [
    int(x.strip())
    for x in os.getenv("HOST_IDS", "").split(",")
    if x.strip()
]

if not TOKEN or not POLL_CHANNEL_ID:
    raise ValueError("Missing DISCORD_TOKEN or POLL_CHANNEL_ID in .env")

# ---------------------------------
# 2. Set up SQLite database
# ---------------------------------

DB_PATH = os.getenv("DB_PATH", "volleyball.db")
db = sqlite3.connect(DB_PATH)

# Permanent history of vote additions/removals
db.execute("""
CREATE TABLE IF NOT EXISTS poll_vote_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    answer_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL
)
""")

# Currently active votes
db.execute("""
CREATE TABLE IF NOT EXISTS active_poll_votes (
    poll_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    answer_id INTEGER NOT NULL,
    signed_at TEXT NOT NULL,
    PRIMARY KEY (poll_id, user_id, answer_id)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS sent_event_reminders (
    event_id INTEGER NOT NULL,
    hours_before INTEGER NOT NULL,
    PRIMARY KEY (event_id, hours_before)
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS poll_scheduled_events (
    poll_id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL
)
""")
db.commit()

# ---------------------------------
# 3. Configure Discord
# ---------------------------------

intents = discord.Intents.default()
intents.polls = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
event_creation_lock = asyncio.Lock()

@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user}")
    print(f"Monitoring channel: {POLL_CHANNEL_ID}")
    if not post_event_reminders.is_running():
        post_event_reminders.start()


# ---------------------------------
# 4. Record votes
# ---------------------------------

async def get_voter_name(payload):
    guild = bot.get_guild(payload.guild_id) if payload.guild_id is not None else None
    if guild is None:
        return f"Unknown user ({payload.user_id})"

    member = guild.get_member(payload.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return f"Unknown user ({payload.user_id})"

    return member.display_name


async def fetch_poll_message(payload):
    channel = bot.get_partial_messageable(payload.channel_id, guild_id=payload.guild_id)
    try:
        return await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return None


def get_poll_details(payload, message):
    poll_name = f"Unknown poll ({payload.message_id})"
    answer_text = f"Unknown option ({payload.answer_id})"

    poll = message.poll if message is not None else None
    if poll is None:
        return poll_name, answer_text

    answer = poll.get_answer(payload.answer_id)
    if answer is not None:
        answer_text = answer.text or str(answer.emoji or answer_text)

    return poll.question, answer_text


async def record_vote(payload, action):

    # Ignore votes outside #polls
    if payload.channel_id != POLL_CHANNEL_ID:
        return

    poll_id = payload.message_id
    user_id = payload.user_id
    answer_id = payload.answer_id

    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()

    # Save the event and update current signups
    with db:
        db.execute("""
            INSERT INTO poll_vote_events
            (poll_id, user_id, answer_id, action, timestamp_utc)
            VALUES (?, ?, ?, ?, ?)
        """, (
            poll_id, user_id, answer_id, action, timestamp
        ))

        if action == "JOINED":
            db.execute("""
                INSERT OR IGNORE INTO active_poll_votes
                (poll_id, user_id, answer_id, signed_at)
                VALUES (?, ?, ?, ?)
            """, (
                poll_id, user_id, answer_id, timestamp
            ))

        elif action == "LEFT":
            db.execute("""
                DELETE FROM active_poll_votes
                WHERE poll_id = ?
                  AND user_id = ?
                  AND answer_id = ?
            """, (
                poll_id, user_id, answer_id
            ))

    voter_name = await get_voter_name(payload)
    message = await fetch_poll_message(payload)
    poll_name, answer_text = get_poll_details(payload, message)
    local_time = now.astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z")
    print(
        f"{action} | Poll: {poll_name} | "
        f"User: {voter_name} | "
        f"Answer: {answer_text} | "
        f"Time: {local_time}"
    )

    if action == "JOINED" and message is not None:
        await maybe_create_poll_event(message)

    # ---------------------------------
    # 5. Notify hosts (optional)
    # ---------------------------------

    poll_link = (
        f"https://discord.com/channels/"
        f"{payload.guild_id}/{payload.channel_id}/{poll_id}"
    )

    msg = (
        f"**Volleyball Vote {action}**\n"
        f"User: <@{user_id}>\n"
        f"Answer ID: {answer_id}\n"
        f"Time: <t:{int(now.timestamp())}:F>\n"
        f"Poll: {poll_link}"
    )

    for host_id in HOST_IDS:
        try:
            user = await bot.fetch_user(host_id)
            await user.send(msg)
        except discord.HTTPException as error:
            print(f"Could not DM {host_id}: {error}")




# ---------------------------------
# 6. Listen for Discord events
# ---------------------------------

@bot.event
async def on_raw_poll_vote_add(payload):
    await record_vote(payload, "JOINED")


@bot.event
async def on_raw_poll_vote_remove(payload):
    await record_vote(payload, "LEFT")



# ---------------------------------
# 7. Bot Commands
# ---------------------------------

# Retrieve polls from the Discord channel
async def find_polls(max_results=None, *, history_limit=500):
    channel = bot.get_channel(POLL_CHANNEL_ID)

    if channel is None:
        channel = await bot.fetch_channel(POLL_CHANNEL_ID)

    if not isinstance(channel, discord.abc.Messageable):
        raise commands.CommandError(
            "POLL_CHANNEL_ID must point to a channel with message history, "
            "such as a text channel or a forum post's thread."
        )

    polls = []

    async for message in channel.history(limit=history_limit):
        if message.poll is None:
            continue

        polls.append(message)

        if max_results and len(polls) >= max_results:
            break

    return polls


# Send longer responses without hitting Discord's limit
async def send_lines(ctx, lines):
    chunk = ""

    for line in lines:
        next_chunk = f"{chunk}\n{line}" if chunk else line

        if len(next_chunk) > 1900 and chunk:
            await ctx.send(
                chunk,
                allowed_mentions=discord.AllowedMentions.none()
            )
            chunk = line
        else:
            chunk = next_chunk

    if chunk:
        await ctx.send(
            chunk,
            allowed_mentions=discord.AllowedMentions.none()
        )


# ---------------------------------
# 7.1 !polls - Show 5 newest polls
# ---------------------------------

@bot.command()
async def polls(ctx):

    recent_polls = await find_polls(max_results=5)

    if not recent_polls:
        await ctx.send("No polls found in this channel.")
        return

    lines = ["**Last 5 Volleyball Polls**", ""]

    for index, message in enumerate(recent_polls, 1):
        title = discord.utils.escape_markdown(
            message.poll.question
        )

        lines.append(
            f"{index}. **{title}** [View Poll]({message.jump_url})"
        )

    await send_lines(ctx, lines)


# ---------------------------------
# 7.2 !poll - Search by poll title
# ---------------------------------

@bot.command()
async def poll(ctx, *, name: str | None = None):

    if not name or not name.strip():
        await ctx.send(
            "Usage: `!poll <part of poll title or posting date>`\n"
            "Examples: `!poll friday`, `!poll 9/29`"
        )
        return

    search = name.strip().casefold()

    date_match = re.fullmatch(r"([0-9]{1,2})/([0-9]{1,2})", search)
    posting_date = tuple(map(int, date_match.groups())) if date_match else None
    all_polls = await find_polls(history_limit=None if posting_date else 500)

    matches = []
    for message in all_polls:
        if search in message.poll.question.casefold():
            matches.append(message)
        elif posting_date:
            posted = message.created_at.astimezone()
            if (posted.month, posted.day) == posting_date:
                matches.append(message)

    if not matches:
        await ctx.send(
            f"No polls found matching `{name}`."
        )
        return

    # Prefer a unique exact title match
    exact_matches = [
        message for message in matches
        if message.poll.question.casefold() == search
    ]

    if len(exact_matches) == 1:
        selected = exact_matches[0]

    elif len(matches) == 1:
        selected = matches[0]

    else:
        lines = [
            f"**Multiple polls match `{name}`:**",
            ""
        ]

        for message in matches[:5]:
            title = discord.utils.escape_markdown(
                message.poll.question
            )

            lines.append(
                f"• **{title}** [View Poll]({message.jump_url})"
            )

        lines.append("")
        lines.append(
            "Try searching with the date or a more "
            "specific part of the title."
        )

        await send_lines(ctx, lines)
        return

    # ---------------------------------
    # Retrieve current recorded signups
    # ---------------------------------

    poll_id = selected.id

    rows = db.execute("""
        SELECT user_id, answer_id, signed_at
        FROM active_poll_votes
        WHERE poll_id = ?
        ORDER BY signed_at ASC, rowid ASC
    """, (poll_id,)).fetchall()

    title = discord.utils.escape_markdown(
        selected.poll.question
    )

    lines = [
        f"**{title}**",
        f"[View Original Poll]({selected.jump_url})",
        f"**Recorded active votes: {len(rows)}**",
        ""
    ]

    if not rows:
        lines.append(
            "No active signups recorded by the bot yet."
        )

    else:
        for position, (user_id, answer_id, signed_at) in enumerate(rows, 1):

            # Convert stored UTC datetime to Unix timestamp
            dt = datetime.fromisoformat(signed_at)
            unix_time = int(dt.timestamp())

            # Retrieve the actual answer text
            answer = selected.poll.get_answer(answer_id)

            answer_text = (
                answer.text or str(answer.emoji or answer_id)
                if answer is not None
                else f"Option {answer_id}"
            )

            lines.append(
                f"{position}. <@{user_id}> — "
                f"{answer_text}\n"
                f"   <t:{unix_time}:D> at <t:{unix_time}:T>"
            )

    await send_lines(ctx, lines)



# ---------------------------------
# 8. Volleyball event reminders
# ---------------------------------

EVENT_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b")
EVENT_TIME_RANGE = re.compile(
    r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*(AM|PM)?\s*[-–—]\s*"
    r"(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*(AM|PM)\b",
    re.IGNORECASE,
)
EVENT_SETTING = re.compile(r"\b(indoor|outdoor)s?\b", re.IGNORECASE)


def clean_poll_title(title):
    title = re.sub(r"<a?:\w+:\d+>|[#*0-9]\ufe0f?\u20e3", " ", title)
    characters = (
        " " if unicodedata.category(char) == "So" else char
        for char in title
        if char not in "\u200d\ufe0e\ufe0f"
        and not "\U0001f3fb" <= char <= "\U0001f3ff"
    )
    return " ".join("".join(characters).split())


def poll_event_name(title):
    settings = {match.group(1).lower() for match in EVENT_SETTING.finditer(title)}
    if len(settings) != 1:
        return title[:100]

    setting = settings.pop()
    details = re.sub(
        rf"^{setting}s?\b(?:\s+(?:volleyball|vb)\b)?[\s:–—-]*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return f"{setting.title()} {details}"[:100]


def parse_poll_event(title, posted_at):
    date = EVENT_DATE.search(title)
    if date is None:
        return None
    times = EVENT_TIME_RANGE.search(title, date.end())
    if times is None:
        return None

    before = title[date.end():times.start()].strip(" ,@-–—")
    after = title[times.end():].strip(" ,@-–—")
    before = re.sub(r"\bfrom$", "", before, flags=re.IGNORECASE).strip()
    after = re.sub(r"^at\s+", "", after, flags=re.IGNORECASE)
    if bool(before) == bool(after):
        return None
    location = before or after
    if len(location) > 100 or not any(char.isalpha() for char in location):
        return None

    month, day, year_text = date.groups()
    posted = posted_at.astimezone()
    year = int(year_text) if year_text else posted.year
    if year_text and len(year_text) == 2:
        year += 2000
    elif not year_text and posted.month == 12 and int(month) == 1:
        year += 1

    start_hour, start_minute, start_period, end_hour, end_minute, end_period = times.groups()
    start_pm = (start_period or end_period).upper() == "PM"
    end_pm = end_period.upper() == "PM"
    try:
        start = datetime(year, int(month), int(day),
        int(start_hour) % 12 + 12 * start_pm, int(start_minute or 0))
    except ValueError:
        return None
    end = start.replace(hour=int(end_hour) % 12 + 12 * end_pm, minute=int(end_minute or 0))
    if end <= start:
        if start_period is None:
            start -= timedelta(hours=12)
        else:
            end += timedelta(days=1)
    if end - start > timedelta(hours=12):
        return None

    return start.astimezone(), end.astimezone(), location


async def maybe_create_poll_event(message):
    poll = message.poll
    guild = message.guild
    if poll is None or guild is None:
        return
    if sum(answer.vote_count for answer in poll.answers) < EVENT_VOTE_THRESHOLD:
        return

    async with event_creation_lock:
        existing = db.execute(
            "SELECT event_id FROM poll_scheduled_events WHERE poll_id = ?", (message.id,)
        ).fetchone()
        if existing:
            return existing[0]

        title = clean_poll_title(poll.question)
        details = parse_poll_event(title, message.created_at)
        if details is None:
            print(f"Poll {message.id} reached 12 votes but needs a date, time range, and location.")
            return
        start, end, location = details
        if start <= datetime.now(timezone.utc):
            return

        description = f"Scheduled from the volleyball poll: {message.jump_url}"
        try:
            events = await guild.fetch_scheduled_events(with_counts=False)
            event = next((event for event in events if event.description == description), None)
            created = event is None
            if event is None:
                event = await guild.create_scheduled_event(
                    name=poll_event_name(title),
                    description=description,
                    entity_type=discord.EntityType.external,
                    privacy_level=discord.PrivacyLevel.guild_only,
                    start_time=start,
                    end_time=end,
                    location=location,
                )
        except discord.HTTPException as error:
            print(f"Could not schedule event for poll {message.id}: {error}")
            return

        with db:
            db.execute(
                "INSERT INTO poll_scheduled_events (poll_id, event_id) VALUES (?, ?)",
                (message.id, event.id),
            )
        print(f"Scheduled volleyball event for poll {message.id}: {event.url}")
        if created:
            general = discord.utils.get(guild.text_channels, name="general")
            if general is not None:
                try:
                    await send_event_notice(general, event, "New volleyball event")
                except discord.HTTPException as error:
                    print(f"Could not announce event {event.id} in #general: {error}")
        return event.id


@bot.command()
@commands.guild_only()
async def pollevent(ctx, poll_reference: str = ""):
    if not ctx.author.guild_permissions.create_events:
        await ctx.send("You need the Create Events permission to run this command.")
        return

    reference = poll_reference.strip("<>")
    link = re.fullmatch(r"https://discord\.com/channels/(\d+)/(\d+)/(\d+)", reference)
    if link:
        guild_id, channel_id, message_id = map(int, link.groups())
        if guild_id != ctx.guild.id or channel_id != POLL_CHANNEL_ID:
            await ctx.send("Use a poll link from this server's configured poll channel.")
            return
    elif re.fullmatch(r"[0-9]{17,20}", reference):
        message_id = int(reference)
    else:
        await ctx.send("Usage: `!pollevent <poll message link or ID>`")
        return

    try:
        channel = bot.get_channel(POLL_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(POLL_CHANNEL_ID)
        if not isinstance(channel, discord.abc.Messageable) or getattr(channel, "guild", None) != ctx.guild:
            await ctx.send("The configured poll channel isn't in this server.")
            return
        message = await channel.fetch_message(message_id)
    except discord.HTTPException:
        await ctx.send("I couldn't load that poll from the configured poll channel.")
        return

    if message.poll is None or message.guild != ctx.guild:
        await ctx.send("That message isn't a poll in this server.")
        return
    if sum(answer.vote_count for answer in message.poll.answers) < EVENT_VOTE_THRESHOLD:
        await ctx.send(f"The poll needs at least {EVENT_VOTE_THRESHOLD} votes.")
        return

    event_id = await maybe_create_poll_event(message)
    if event_id is None:
        await ctx.send(
            "I couldn't create the event. Check that the poll title has a future start time, "
            "date, time range, and location, and that the bot has Create Events permission."
        )
        return
    await ctx.send(f"[View Volleyball Event](https://discord.com/events/{ctx.guild.id}/{event_id})")


async def get_volleyball_events(guild):
    events = await guild.fetch_scheduled_events(with_counts=False)
    return sorted(events, key=lambda event: event.start_time)


def format_event_reminder(event):
    title = discord.utils.escape_markdown(event.name)
    start = int(event.start_time.timestamp())
    lines = [f"🏐 **{title}**", f"Starts <t:{start}:F> (<t:{start}:R>)"]
    if event.location:
        lines.append(f"Location: {discord.utils.escape_markdown(event.location)}")
    lines.append(f"[View Event]({event.url})")
    return "\n".join(lines)


async def send_event_notice(channel, event, heading="Volleyball reminder"):
    await channel.send(
        f"@everyone\n**{heading}**\n{format_event_reminder(event)}",
        allowed_mentions=discord.AllowedMentions(
            everyone=True, users=False, roles=False, replied_user=False
        ),
    )


@bot.command()
@commands.guild_only()
async def reminder(ctx):
    general = discord.utils.get(ctx.guild.text_channels, name="general")
    if general is None:
        await ctx.send("I couldn't find a text channel named #general.")
        return

    try:
        events = await get_volleyball_events(ctx.guild)
    except discord.HTTPException as error:
        print(f"Could not load volleyball events: {error}")
        await ctx.send("I couldn't load this server's events. Try again shortly.")
        return

    now = datetime.now(timezone.utc)
    today = now.astimezone().date()
    tomorrow = today + timedelta(days=1)
    for event in events:
        if event.start_time.astimezone().date() not in (today, tomorrow):
            continue
        upcoming = event.status == discord.EventStatus.scheduled and event.start_time > now
        ongoing = event.status == discord.EventStatus.active and (
            event.end_time is None or event.end_time > now
        )
        if upcoming or ongoing:
            try:
                await send_event_notice(general, event)
            except discord.HTTPException as error:
                print(f"Could not post reminder for event {event.id}: {error}")
                await ctx.send("I couldn't post the reminder in #general.")
            return

    await ctx.send("No volleyball event is scheduled for today or tomorrow.")


@tasks.loop(minutes=1)
async def post_event_reminders():
    await bot.wait_until_ready()
    try:
        poll_channel = bot.get_channel(POLL_CHANNEL_ID)
        if poll_channel is None:
            poll_channel = await bot.fetch_channel(POLL_CHANNEL_ID)
        guild = getattr(poll_channel, "guild", None)
        if guild is None:
            return
        general = discord.utils.get(guild.text_channels, name="general")
        if general is None:
            return
        events = await get_volleyball_events(guild)
    except discord.HTTPException as error:
        print(f"Could not check volleyball reminders: {error}")
        return

    now = datetime.now(timezone.utc)
    for event in events:
        remaining = event.start_time - now
        if event.status != discord.EventStatus.scheduled or not (
            timedelta(0) < remaining <= timedelta(hours=24)
        ):
            continue

        # If both thresholds passed while offline, send the latest one only.
        hours_before = 10 if remaining <= timedelta(hours=10) else 24
        if db.execute(
            "SELECT 1 FROM sent_event_reminders WHERE event_id = ? AND hours_before = ?",
            (event.id, hours_before),
        ).fetchone():
            continue

        try:
            await send_event_notice(general, event)
        except discord.HTTPException as error:
            print(f"Could not post reminder for event {event.id}: {error}")
            continue

        with db:
            db.execute(
                "INSERT OR IGNORE INTO sent_event_reminders VALUES (?, ?)",
                (event.id, hours_before),
            )

# ---------------------------------
# 9. Logging for disconnects and reconnects
# ---------------------------------

@bot.event
async def on_disconnect():
    global disconnect_time

    if disconnect_time is None:
        disconnect_time = datetime.now(timezone.utc)
        print(f"DISCONNECTED at {disconnect_time.astimezone()}")

@bot.event
async def on_resumed():
    global disconnect_time

    now = datetime.now(timezone.utc)

    if disconnect_time is not None:
        duration = (now - disconnect_time).total_seconds()

        print(
            f"RECONNECTED after approximately "
            f"{duration:.2f} seconds"
        )

        disconnect_time = None

    else:
        print(f"Discord session resumed at {now.astimezone()}")

# ---------------------------------
# 10. Start the bot
# ---------------------------------

bot.run(TOKEN)
