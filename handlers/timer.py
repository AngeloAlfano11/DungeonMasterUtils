"""Visual countdown timer pinned in the chat/thread.

Sends a bar of █ blocks, then on every TICK_SECONDS it edits one block into a
▒ block to give a moving "fuel gauge" effect. When done, unpins, deletes and
posts a TIME_UP banner.

`/timer <minutes>` starts a timer; `/timerstop` cancels the active one in the
current thread (unpins + deletes the bar, no TIME_UP banner).
"""

import asyncio
import logging

from telegram import Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import ALLOWED_CHAT_IDS, AUTHORIZED_USERS

logger = logging.getLogger(__name__)

# Hard cap on the requested duration (Telegram edit-rate is ~1/sec but we
# refresh every 30s to stay well under it; 60 minutes = 120 edits per timer).
MAX_MINUTES = 60
TICK_SECONDS = 30
TIME_UP_TEXT = "[░Ｔ░ ░ｉ░ ░ｍ░ ░ｅ░ ░'░ ░ｓ░  ░ｕ░ ░ｐ░]"

# Per-thread tracking of the currently-running timer task so /timerstop can
# cancel it. Lost on bot restart by design — a restarted bot can't recover a
# mid-flight asyncio loop; the pinned message would remain until manually
# unpinned, which is acceptable.
_active_timers: dict[tuple[int, int | None], asyncio.Task] = {}


async def _run_timer(
    update: Update,
    timer_msg: Message,
    minutes: int,
    key: tuple[int, int | None],
) -> None:
    """Background task: tick the bar, then post TIME_UP. Cancellable.

    Cancellation (from /timerstop) skips TIME_UP but still runs cleanup.
    """
    thread_id = timer_msg.message_thread_id
    ticks = minutes * 2
    cancelled = False
    try:
        for i in range(ticks):
            await asyncio.sleep(TICK_SECONDS)
            remaining = "█" * (ticks - i - 1)
            spent = "▒" * (i + 1)
            try:
                await timer_msg.edit_text(remaining + spent)
            except BadRequest:
                # Bar message was deleted externally — abort silently.
                return
    except asyncio.CancelledError:
        cancelled = True
        # Don't re-raise: we still want the finally cleanup to run, and
        # there's nothing above us that cares about the cancellation status.
    finally:
        # Always try to unpin + delete; both can fail (already gone, no
        # permission) — neither failure is worth surfacing.
        try:
            await timer_msg.unpin()
        except Exception:
            pass
        try:
            await timer_msg.delete()
        except Exception:
            pass
        _active_timers.pop(key, None)

    if not cancelled:
        await update.effective_chat.send_message(TIME_UP_TEXT, message_thread_id=thread_id)
        logger.info("%d-minute timer ended in chat %d thread %s", minutes, key[0], thread_id)
    else:
        logger.info("Timer cancelled in chat %d thread %s", key[0], thread_id)


async def start_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    if update.effective_user.id not in AUTHORIZED_USERS:
        return
    if ALLOWED_CHAT_IDS and update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    if not context.args:
        await msg.reply_text(f"Usage: /timer <minutes> (max {MAX_MINUTES})")
        return

    try:
        minutes = int(context.args[0])
    except ValueError:
        await msg.reply_text("Please provide a whole number of minutes.")
        return

    if not (1 <= minutes <= MAX_MINUTES):
        await msg.reply_text(f"Timer must be between 1 and {MAX_MINUTES} minutes.")
        return

    key = (msg.chat_id, msg.message_thread_id)
    if key in _active_timers:
        await msg.reply_text("Timer already active in this thread. Use /timerstop first.")
        return

    # Remove the trigger message so the chat shows only the live timer.
    await msg.delete()

    thread_id = msg.message_thread_id
    # Two ticks per minute (one every 30s).
    ticks = minutes * 2
    timer_msg = await update.effective_chat.send_message(
        "█" * ticks,
        message_thread_id=thread_id,
    )
    # Pin without notification so we don't spam everyone in the group.
    await timer_msg.pin(disable_notification=True)

    # Spawn the tick loop as a background task so /timerstop can cancel it.
    # If we awaited here, the update handler would block for `minutes` and
    # there'd be no point to release control back to PTB.
    task = asyncio.create_task(_run_timer(update, timer_msg, minutes, key))
    _active_timers[key] = task
    logger.info("%d-minute timer started in chat %d thread %s", minutes, msg.chat_id, thread_id)


async def stop_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the active timer in this thread and clean up the pinned bar."""
    msg = update.effective_message

    if update.effective_user.id not in AUTHORIZED_USERS:
        return
    if ALLOWED_CHAT_IDS and update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    await msg.delete()

    key = (msg.chat_id, msg.message_thread_id)
    task = _active_timers.get(key)
    if task is None:
        await update.effective_chat.send_message(
            "No active timer in this thread.",
            message_thread_id=msg.message_thread_id,
        )
        return

    # The task's finally block will unpin/delete the bar and pop from
    # _active_timers. We don't await it: just signal cancellation and return.
    task.cancel()
