"""Session recorder.

`/SummStart` opens a JSONL file scoped to (chat, thread) and `/SummEnd` closes
it and dispatches the AI summary job. While a session is active, every text
or captioned message in that thread is appended as one JSON line.

In-session shortcuts:
  /e or /edit <new text> (as a reply): fuzzy-match the replied-to message
    against the buffered messages and overwrite the matched entry.
  /d or /delete (as a reply): same fuzzy match, but remove the entry.
"""

import json
import logging
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from config import ALLOWED_CHAT_IDS, AUTHORIZED_USERS, BOTS_ID, IGNORED_COMMANDS, SESSIONS_DIR
from handlers.summarize import summarize_job

logger = logging.getLogger(__name__)

# In-memory session state, keyed by (chat_id, thread_id).
# Lost on bot restart by design: recording is tied to a live human triggering
# /SummStart, so a stale session resuming after a crash isn't desirable.
# (chat_id, thread_id) -> {"file_path": Path, "started_by": int, "messages": list[dict]}
active_sessions: dict[tuple[int, int | None], dict] = {}

OWN_COMMANDS = {"/summstart", "/summend"}
EDIT_COMMANDS = {"/e", "/edit"}
DELETE_COMMANDS = {"/d", "/delete"}
# Minimum SequenceMatcher ratio for an edit/delete to take effect. Below this
# we silently skip — better than mutating the wrong message.
EDIT_SIMILARITY_THRESHOLD = 0.3


def _sender_name(user, command: str | None = None) -> str:
    """Resolve the sender label written into the JSONL.

    Maps the Telegram user to a character name via BOTS_ID. If the user
    controls multiple characters, uses the slash-command prefix to
    disambiguate (e.g. `/Kael` matches "Kael Magdaros"); otherwise falls back
    to the Telegram display name.
    """
    parts = [user.first_name]
    if user.last_name:
        parts.append(user.last_name)
    telegram_name = " ".join(parts)

    if user.id in BOTS_ID:
        characters = BOTS_ID[user.id]
        if len(characters) == 1:
            return characters[0]
        # Multi-character user: pick the one whose name contains the command.
        if command:
            cmd = command.lstrip("/").lower()
            for char_name in characters:
                if cmd in char_name.lower():
                    return char_name
        return telegram_name

    return telegram_name


def _session_key(update: Update) -> tuple[int, int | None]:
    msg = update.effective_message
    return (msg.chat_id, msg.message_thread_id)


def _rewrite_session(file_path: Path, messages: list[dict]) -> None:
    """Truncate the JSONL and write the full list — used by edit/delete since
    we can't rewind appends in place. Plain appends are still used for the
    common case (each new message in record_message)."""
    with open(file_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def _is_allowed_chat(update: Update) -> bool:
    # Empty ALLOWED_CHAT_IDS means "all chats" (setup / development mode).
    return not ALLOWED_CHAT_IDS or update.effective_chat.id in ALLOWED_CHAT_IDS


async def start_recording(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in AUTHORIZED_USERS or not _is_allowed_chat(update):
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

    # Build a unique filename per session: chat + thread + timestamp.
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
    # Drop the /SummStart message and confirm with a clean "Listening...".
    await msg.delete()
    await update.effective_chat.send_message(
        "Listening...",
        message_thread_id=msg.message_thread_id,
    )


async def stop_recording(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in AUTHORIZED_USERS or not _is_allowed_chat(update):
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

    # Send a placeholder; summarize_job will edit it with the final summary
    # (or the failure message after retries are exhausted).
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
    """Re-summarize the most recent JSONL on disk.

    Useful when /SummEnd's summary failed or was lost; the GM can retry
    without re-recording the session.
    """
    user = update.effective_user
    if user.id not in AUTHORIZED_USERS or not _is_allowed_chat(update):
        return

    msg = update.effective_message
    await msg.delete()

    # Most-recent-first, by mtime — the latest session is at index 0.
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
    """Catch-all handler that captures messages while a session is active.

    No-op if the (chat, thread) has no active session — this is wired in
    group=1 so it never preempts command handlers.
    """
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
    # Don't echo our own control commands into the transcript.
    if base_command in OWN_COMMANDS:
        return

    # ---- /e or /edit: fuzzy-replace a previously captured message ----
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

        # Pick the buffered message most similar to the search target.
        ratios = [
            SequenceMatcher(None, m["text"], search_text).ratio()
            for m in messages
        ]
        best_idx = ratios.index(max(ratios))
        if ratios[best_idx] < EDIT_SIMILARITY_THRESHOLD:
            logger.debug("Edit ignored: no sufficient match (best=%.2f)", ratios[best_idx])
            return

        messages[best_idx]["text"] = new_text
        # In-place edits require a full file rewrite (JSONL has no random access).
        _rewrite_session(session["file_path"], messages)
        logger.debug("Message %d updated via edit (similarity=%.2f)", best_idx, ratios[best_idx])
        return

    # ---- /d or /delete: fuzzy-remove a previously captured message ----
    if base_command in DELETE_COMMANDS:
        messages = session["messages"]
        if not messages:
            return

        # Delete requires an explicit reply target; without one we have no
        # way to identify which message to remove.
        reply = msg.reply_to_message
        search_text = (reply.text or reply.caption or "") if reply else ""

        if not search_text:
            logger.debug("Delete ignored: no reply target")
            return

        ratios = [
            SequenceMatcher(None, m["text"], search_text).ratio()
            for m in messages
        ]
        best_idx = ratios.index(max(ratios))
        if ratios[best_idx] < EDIT_SIMILARITY_THRESHOLD:
            logger.debug("Delete ignored: no sufficient match (best=%.2f)", ratios[best_idx])
            return

        removed = messages.pop(best_idx)
        _rewrite_session(session["file_path"], messages)
        logger.debug("Message %d deleted via /d (similarity=%.2f): %s", best_idx, ratios[best_idx], removed["text"][:50])
        return

    # ---- Other slash commands: skip ignored, otherwise capture the args as roleplay ----
    if base_command.startswith("/"):
        if base_command in IGNORED_COMMANDS:
            return
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return  # bare command with no roleplay text (e.g. /roll with no args)
        text = parts[1]  # strip command prefix, keep only the roleplay content

    # Resolve the sender name (character mapping) and persist the entry.
    sender = _sender_name(msg.from_user, base_command) if msg.from_user else "Unknown"
    entry = {"sender": sender, "text": text}
    session["messages"].append(entry)

    # Append-only for normal captures: avoids rewriting the whole file each time.
    with open(session["file_path"], "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
