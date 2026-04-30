import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import AUTHORIZED_USERS

logger = logging.getLogger(__name__)


def _bare_chat_id(chat_id: int) -> str:
    """Strip the -100 prefix from supergroup IDs for use in t.me/c/ links."""
    return str(abs(chat_id))[3:]


def _msg_link(chat_id: int, message_id: int, thread_id: int | None = None) -> str:
    bare = _bare_chat_id(chat_id)
    if thread_id:
        return f"https://t.me/c/{bare}/{thread_id}/{message_id}"
    return f"https://t.me/c/{bare}/{message_id}"


async def getids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in AUTHORIZED_USERS:
        return

    msg = update.effective_message
    await msg.delete()

    user_label = user.full_name
    if user.username:
        user_label += f" (@{user.username})"

    group_label = msg.chat.title or str(msg.chat_id)

    context_link = _msg_link(msg.chat_id, msg.message_id, msg.message_thread_id)

    lines = [
        f"Message generated for getting user and group data. Here's where it was sent: <a href=\"{context_link}\">jump to message</a>",
        "",
        f"<b>User ID:</b> <code>{user.id}</code> — {user_label}",
        f"<b>Group ID:</b> <code>{msg.chat_id}</code> — {group_label}",
    ]

    if msg.message_thread_id:
        thread_link = f"https://t.me/c/{_bare_chat_id(msg.chat_id)}/{msg.message_thread_id}"
        lines.append(f"<b>Thread ID:</b> <code>{msg.message_thread_id}</code> — <a href=\"{thread_link}\">open thread</a>")

    lines.append(f"<b>Message ID:</b> <code>{msg.message_id}</code>")

    reply = msg.reply_to_message
    if reply:
        reply_context_link = _msg_link(msg.chat_id, reply.message_id, msg.message_thread_id)
        lines.append("")
        lines.append(f"<b>Reply target:</b> <a href=\"{reply_context_link}\">jump to message</a>")
        lines.append(f"  Message ID: <code>{reply.message_id}</code>")

        if reply.from_user:
            sender = reply.from_user
            sender_label = sender.full_name
            if sender.username:
                sender_label += f" (@{sender.username})"
            lines.append(f"  Sent by: <code>{sender.id}</code> — {sender_label}")
        else:
            lines.append("  Sent by: unknown (anonymous/channel)")

        lines.append(f"  Chat: <code>{reply.chat.id}</code> — {reply.chat.title or 'N/A'}")

        reply_text = reply.text or reply.caption or ""
        if not reply_text:
            lines.append("  Type: thread header message (topic creation)")
        elif msg.chat.is_forum:
            lines.append("  Type: message inside a forum thread")
            lines.append(f"  Thread ID: <code>{msg.message_thread_id}</code>")

    info = "\n".join(lines)

    try:
        await context.bot.send_message(chat_id=user.id, text=info, parse_mode="HTML")
    except Exception as e:
        logger.warning("Could not send private message to %d: %s", user.id, e)
        await update.effective_chat.send_message(
            "Could not send you a private message. Please start a conversation with the bot first.",
            message_thread_id=msg.message_thread_id,
        )
