"""Visual countdown timer pinned in the chat/thread.

Sends a bar of █ blocks, then on every TICK_SECONDS it edits one block into a
▒ block to give a moving "fuel gauge" effect. When done, unpins, deletes and
posts a TIME_UP banner.
"""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import ALLOWED_CHAT_IDS, AUTHORIZED_USERS

logger = logging.getLogger(__name__)

# Hard cap on the requested duration (Telegram edit-rate is ~1/sec but we
# refresh every 30s to stay well under it; 60 minutes = 120 edits per timer).
MAX_MINUTES = 60
TICK_SECONDS = 30
TIME_UP_TEXT = "[░Ｔ░ ░ｉ░ ░ｍ░ ░ｅ░ ░'░ ░ｓ░  ░ｕ░ ░ｐ░]"


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

    # Each iteration consumes one █ and grows the ▒ "spent" segment.
    for i in range(ticks):
        await asyncio.sleep(TICK_SECONDS)
        remaining = "█" * (ticks - i - 1)
        spent = "▒" * (i + 1)
        await timer_msg.edit_text(remaining + spent)

    # Cleanup: unpin and delete the bar, then post the time-up banner.
    await timer_msg.unpin()
    await timer_msg.delete()
    await update.effective_chat.send_message(TIME_UP_TEXT, message_thread_id=thread_id)
    logger.info("%d-minute timer ended in chat %d thread %s", minutes, msg.chat_id, thread_id)
