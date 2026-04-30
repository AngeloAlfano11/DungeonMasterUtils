import json
import logging
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from config import AUTHORIZED_USERS, BOTS_ID, IGNORED_COMMANDS, SESSIONS_DIR
from handlers.summarize import summarize_job

logger = logging.getLogger(__name__)

# (chat_id, thread_id) -> {"file_path": Path, "started_by": int, "messages": list[dict]}
active_sessions: dict[tuple[int, int | None], dict] = {}

OWN_COMMANDS = {"/summstart", "/summend"}
EDIT_COMMANDS = {"/e", "/edit"}
EDIT_SIMILARITY_THRESHOLD = 0.3


def _sender_name(user) -> str:
    if user.id in BOTS_ID:
        return BOTS_ID[user.id]
    parts = [user.first_name]
    if user.last_name:
        parts.append(user.last_name)
    return " ".join(parts)


def _session_key(update: Update) -> tuple[int, int | None]:
    msg = update.effective_message
    return (msg.chat_id, msg.message_thread_id)


def _rewrite_session(file_path: Path, messages: list[dict]) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


async def start_recording(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in AUTHORIZED_USERS:
        return

    key = _session_key(update)
    msg = update.effective_message

    if key in active_sessions:
        await msg.delete()
        await update.effective_chat.send_message(
            "Recording already active in this thread.",
            message_thread_id=msg.message_thread_id,
        )
        return

    SESSIONS_DIR.mkdir(exist_ok=True)
    chat_id, thread_id = key
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    thread_label = str(thread_id) if thread_id is not None else "main"
    file_path = SESSIONS_DIR / f"chat{chat_id}_thread{thread_label}_{timestamp}.jsonl"

    file_path.touch()
    active_sessions[key] = {
        "file_path": file_path,
        "started_by": user.id,
        "messages": [],
    }

    logger.info("Session started: %s", file_path.name)
    await msg.delete()
    await update.effective_chat.send_message(
        "Listening...",
        message_thread_id=msg.message_thread_id,
    )


async def stop_recording(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in AUTHORIZED_USERS:
        return

    key = _session_key(update)
    msg = update.effective_message
    session = active_sessions.pop(key, None)

    if session is None:
        await msg.delete()
        await update.effective_chat.send_message(
            "No active recording in this thread.",
            message_thread_id=msg.message_thread_id,
        )
        return

    logger.info("Session ended: %s (%d messages)", session["file_path"].name, len(session["messages"]))
    thread_id = msg.message_thread_id
    await msg.delete()

    waiting_msg = await update.effective_chat.send_message(
        "⏳ Generating summary...",
        message_thread_id=thread_id,
    )
    context.job_queue.run_once(
        summarize_job,
        when=0,
        data={
            "file_path": session["file_path"],
            "chat_id": msg.chat_id,
            "message_id": waiting_msg.message_id,
            "attempt": 1,
        },
    )


async def force_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in AUTHORIZED_USERS:
        return

    msg = update.effective_message
    await msg.delete()

    files = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        await update.effective_chat.send_message(
            "No session files found.",
            message_thread_id=msg.message_thread_id,
        )
        return

    latest = files[0]
    logger.info("Force summary requested on: %s", latest.name)

    waiting_msg = await update.effective_chat.send_message(
        "⏳ Generating summary of the last session...",
        message_thread_id=msg.message_thread_id,
    )
    context.job_queue.run_once(
        summarize_job,
        when=0,
        data={
            "file_path": latest,
            "chat_id": msg.chat_id,
            "message_id": waiting_msg.message_id,
            "attempt": 1,
        },
    )


async def record_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not (msg.text or msg.caption):
        return

    key = _session_key(update)
    session = active_sessions.get(key)
    if session is None:
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return
    text_lower = text.lower()

    first_word = text_lower.split()[0] if text_lower else ""
    # Strip bot username suffix (e.g. /summstart@botname)
    base_command = first_word.split("@")[0]
    if base_command in OWN_COMMANDS:
        return

    if base_command in EDIT_COMMANDS:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return
        new_text = parts[1]
        messages = session["messages"]
        if not messages:
            return

        # Use the replied-to message text as the search key, falling back to new_text
        reply = msg.reply_to_message
        search_text = (reply.text or reply.caption or "") if reply else ""
        if not search_text:
            search_text = new_text

        ratios = [
            SequenceMatcher(None, m["text"], search_text).ratio()
            for m in messages
        ]
        best_idx = ratios.index(max(ratios))
        if ratios[best_idx] < EDIT_SIMILARITY_THRESHOLD:
            logger.debug("Edit ignored: no sufficient match (best=%.2f)", ratios[best_idx])
            return

        messages[best_idx]["text"] = new_text
        _rewrite_session(session["file_path"], messages)
        logger.debug("Message %d updated via edit (similarity=%.2f)", best_idx, ratios[best_idx])
        return

    # Handle command messages
    if base_command.startswith("/"):
        if base_command in IGNORED_COMMANDS:
            return
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return  # bare command with no roleplay text (e.g. /roll with no args)
        text = parts[1]  # strip command prefix, keep only the roleplay content

    sender = _sender_name(msg.from_user) if msg.from_user else "Unknown"
    entry = {"sender": sender, "text": text}
    session["messages"].append(entry)

    with open(session["file_path"], "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
