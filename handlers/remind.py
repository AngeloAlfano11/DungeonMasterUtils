"""Session reminder for /remind.

One reminder per (chat, thread). The GM configures the days of the week and
time when sessions happen plus a player roster; the bot pings everyone 24h
before each session via APScheduler-backed run_daily jobs.

Setting a new reminder on a thread that already has one silently overwrites
the previous schedule. /remind clear removes it. /remind alone shows the
current configuration.
"""

import datetime
import json
import logging
import os
import re
from pathlib import Path

from telegram import Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import AUTHORIZED_USERS, REMINDERS_DIR

logger = logging.getLogger(__name__)

Key = tuple[int, int | None]
# In-memory mirror of the JSON files; one entry per (chat, thread).
reminders: dict[Key, dict] = {}

DAY_ABBR = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

USAGE = (
    "Usage:\n"
    "  /remind                                — show current reminder\n"
    "  /remind <days> <HH:MM> <@user> [...] [text]\n"
    "  /remind clear                          — remove the reminder\n"
    "Days: comma-separated (mon,tue,wed,thu,fri,sat,sun). Time: 24h.\n"
    "Players: one or more @mentions. Trailing words are an optional message."
)


# ---------- Persistence ----------

def _key_from_msg(msg: Message) -> Key:
    return (msg.chat_id, msg.message_thread_id)


def _path(key: Key) -> Path:
    chat_id, thread_id = key
    label = str(thread_id) if thread_id is not None else "main"
    return REMINDERS_DIR / f"chat{chat_id}_thread{label}.json"


def _save(key: Key, reminder: dict) -> None:
    """Atomic write through a `.tmp` sibling + os.replace."""
    REMINDERS_DIR.mkdir(exist_ok=True)
    path = _path(key)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reminder, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _delete(key: Key) -> None:
    _path(key).unlink(missing_ok=True)


# ---------- Generic helpers ----------

async def _send_chat(update: Update, text: str, **kwargs) -> Message:
    """Send into the originating chat/thread (the trigger message gets deleted
    so a normal reply_text would dangle)."""
    msg = update.effective_message
    return await update.effective_chat.send_message(
        text=text,
        message_thread_id=msg.message_thread_id,
        **kwargs,
    )


# ---------- Scheduling ----------

def _job_name(key: Key, session_day: int) -> str:
    chat_id, thread_id = key
    label = thread_id if thread_id is not None else "main"
    return f"remind_{chat_id}_{label}_{session_day}"


async def _fire_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """run_daily callback: posts the ping message in the configured thread."""
    data = context.job.data
    pings = " ".join(data["players"])
    body = data["text"] or f"🎲 Session tomorrow at {data['session_time']}!"
    try:
        await context.bot.send_message(
            chat_id=data["chat_id"],
            message_thread_id=data["thread_id"],
            text=f"{body}\n{pings}".strip(),
        )
    except Exception as e:
        # Errors here are typically transient (rate limit, permissions changed,
        # bot kicked). Log and keep the schedule alive — next week may work.
        logger.warning(
            "Reminder fire failed for chat=%s thread=%s: %s",
            data["chat_id"], data["thread_id"], e,
        )


def _schedule(job_queue, key: Key, reminder: dict) -> None:
    """One run_daily job per session_day, firing on (day-1)%7 at session_time."""
    hh, mm = map(int, reminder["session_time"].split(":"))
    fire_time = datetime.time(hh, mm)
    chat_id, thread_id = key
    for d in reminder["session_days"]:
        # 24h before the session: shift the weekday back by one.
        reminder_day = (d - 1) % 7
        job_queue.run_daily(
            _fire_reminder,
            time=fire_time,
            days=(reminder_day,),
            name=_job_name(key, d),
            data={
                "chat_id": chat_id,
                "thread_id": thread_id,
                "session_time": reminder["session_time"],
                "players": reminder["players"],
                "text": reminder["text"],
            },
        )


def _unschedule(job_queue, key: Key) -> None:
    """Remove every scheduled reminder job for this key.

    Walks all 7 possible day slots (we don't need to know which were active)
    so the same call works to wipe before a replace and to handle clear.
    """
    for d in range(7):
        for job in job_queue.get_jobs_by_name(_job_name(key, d)):
            job.schedule_removal()


def load_all_reminders(job_queue) -> None:
    """Boot-time: rebuild the in-memory dict and re-register all jobs."""
    if not REMINDERS_DIR.exists():
        return
    for path in REMINDERS_DIR.glob("chat*_thread*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            key = (data["chat_id"], data["thread_id"])
            reminders[key] = data
            _schedule(job_queue, key, data)
        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("Skipping corrupted reminder file %s: %s", path.name, e)


# ---------- Argument parsing ----------

def _parse_days(raw: str) -> list[int] | None:
    """Comma-separated abbreviations → sorted unique list of weekday ints.
    Returns None on any unknown token."""
    out: set[int] = set()
    for token in raw.split(","):
        t = token.strip().lower()
        if t not in DAY_ABBR:
            return None
        out.add(DAY_ABBR[t])
    return sorted(out) if out else None


def _format_days(days: list[int]) -> str:
    return ",".join(DAY_NAMES[d] for d in days)


def _parse_set_args(args: list[str]) -> tuple[list[int], str, list[str], str | None] | None:
    """Returns (session_days, session_time, players, text) or None on error.

    Layout: <days> <time> <@user1> [@user2 ...] [free text].
    Players are required to start with '@' so plain words after them fall into the text.
    """
    if len(args) < 3:
        return None

    days = _parse_days(args[0])
    if days is None:
        return None

    if not TIME_RE.match(args[1]):
        return None
    session_time = args[1]

    players: list[str] = []
    i = 2
    while i < len(args) and args[i].startswith("@") and len(args[i]) > 1:
        players.append(args[i])
        i += 1

    if not players:
        return None

    text = " ".join(args[i:]).strip() or None
    return days, session_time, players, text


# ---------- Subcommand handlers ----------

async def handle_set(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    """Create or replace the reminder for this thread."""
    parsed = _parse_set_args(args)
    if parsed is None:
        await _send_chat(update, USAGE)
        return

    session_days, session_time, players, text = parsed
    key = _key_from_msg(update.effective_message)
    chat_id, thread_id = key

    # Replacing: drop old jobs first (no-op if none scheduled).
    _unschedule(context.job_queue, key)

    reminder = {
        "chat_id": chat_id,
        "thread_id": thread_id,
        "session_days": session_days,
        "session_time": session_time,
        "players": players,
        "text": text,
    }
    reminders[key] = reminder
    _save(key, reminder)
    _schedule(context.job_queue, key, reminder)

    await _send_chat(
        update,
        f"Reminder set: {_format_days(session_days)} @ {session_time} → "
        f"ping 24h before to {', '.join(players)}.",
    )


async def handle_show(update: Update) -> None:
    key = _key_from_msg(update.effective_message)
    r = reminders.get(key)
    if r is None:
        await _send_chat(update, "No reminder set. Use /remind <days> <HH:MM> <@users> to schedule one.")
        return
    lines = [
        "Active reminder:",
        f"- Days: {_format_days(r['session_days'])}",
        f"- Time: {r['session_time']} (ping 24h before)",
        f"- Players: {', '.join(r['players'])}",
    ]
    if r["text"]:
        lines.append(f"- Text: {r['text']}")
    await _send_chat(update, "\n".join(lines))


async def handle_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = _key_from_msg(update.effective_message)
    if key not in reminders:
        await _send_chat(update, "No reminder set.")
        return
    _unschedule(context.job_queue, key)
    reminders.pop(key, None)
    _delete(key)
    await _send_chat(update, "Reminder cleared.")


# ---------- Dispatch ----------

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level /remind dispatcher.

    Bare /remind → show; first arg "clear" → clear; otherwise → set.
    """
    user = update.effective_user
    if user is None or user.id not in AUTHORIZED_USERS:
        return

    msg = update.effective_message
    try:
        await msg.delete()
    except BadRequest:
        # No delete permission — keep the trigger message, the rest still works.
        pass

    args = list(context.args or [])
    if not args:
        await handle_show(update)
        return
    if args[0].lower() == "clear":
        await handle_clear(update, context)
        return
    await handle_set(update, context, args)
