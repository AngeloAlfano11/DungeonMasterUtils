import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

MAX_MINUTES = 60
TICK_SECONDS = 30
TIME_UP_TEXT = "[░Ｔ░ ░ｉ░ ░ｍ░ ░ｅ░ ░'░ ░ｓ░  ░ｕ░ ░ｐ░]"


async def start_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

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

    await msg.delete()

    ticks = minutes * 2  # un tick ogni 30 secondi
    timer_msg = await update.effective_chat.send_message("█" * ticks)
    await timer_msg.pin(disable_notification=True)

    for i in range(ticks):
        await asyncio.sleep(TICK_SECONDS)
        remaining = "█" * (ticks - i - 1)
        spent = "▒" * (i + 1)
        await timer_msg.edit_text(remaining + spent)

    await timer_msg.unpin()
    await timer_msg.delete()
    await update.effective_chat.send_message(TIME_UP_TEXT)
    logger.info("%d-minute timer ended in chat %d", minutes, msg.chat_id)
