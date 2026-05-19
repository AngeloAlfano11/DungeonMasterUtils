"""User-scoped roll macros.

Three commands:
  /macro                          → list saved macros
  /macro <expression> <name>      → save (or overwrite) a macro
  /macrodel <name>                → remove one macro
  /macroreset                     → remove all macros for the calling user

Macros are per-Telegram-user and global across chats/threads — a player's
shortcuts follow them anywhere the bot is. Lookup is exposed via
`resolve_macro`, imported by `roll.py` to intercept `/roll <macroName>`.
"""

import json
import logging
import os
import re
from pathlib import Path

from telegram import Message, Update
from telegram.ext import ContextTypes

from config import MACROS_DIR
from handlers.roll import validate_roll_input

logger = logging.getLogger(__name__)

# Names must start with a letter or underscore so they never collide with a
# dice expression (which always starts with a digit). 1–32 chars, alphanumeric
# plus dash/underscore.
MACRO_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")

# user_id → file dict; lazy-loaded on first access.
_cache: dict[int, dict] = {}


# ---------- Persistence ----------

def _path(user_id: int) -> Path:
    return MACROS_DIR / f"user{user_id}.json"


def _save(user_id: int) -> None:
    """Atomic write through a `.tmp` sibling + os.replace."""
    MACROS_DIR.mkdir(exist_ok=True)
    path = _path(user_id)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache[user_id], f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _delete(user_id: int) -> None:
    _path(user_id).unlink(missing_ok=True)


def _ensure_loaded(user_id: int) -> dict:
    """Return the user's macro dict, lazy-loading from disk if needed.

    Always returns a usable dict (with `macros` key) — never None — so callers
    can read/write without extra null checks. A brand-new user gets a stub
    that isn't persisted until they actually save something.
    """
    if user_id in _cache:
        return _cache[user_id]
    path = _path(user_id)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _cache[user_id] = data
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Corrupted macros file for user %d (%s); starting fresh.", user_id, e)
    stub = {"user_id": user_id, "macros": {}}
    _cache[user_id] = stub
    return stub


# ---------- Generic helpers ----------

async def _send_chat(update: Update, text: str, **kwargs) -> Message:
    """Send into the originating chat/thread."""
    msg = update.effective_message
    return await update.effective_chat.send_message(
        text=text,
        message_thread_id=msg.message_thread_id,
        **kwargs,
    )


# ---------- Lookup (exported to roll.py) ----------

def resolve_macro(args: list[str], user_id: int) -> tuple[list[str], str | None]:
    """Try to expand args[0] as a macro name for `user_id`.

    Returns (new_args, error_msg):
      - If args[0] looks like a name (letters-leading) and matches a saved
        macro → returns the substituted args with macro name as default label.
      - If args[0] looks like a name but no macro found → returns args
        unchanged plus a "macro not found" error message.
      - Otherwise → returns args unchanged with no error.
    """
    if not args:
        return args, None
    first = args[0]
    if not MACRO_NAME_RE.match(first):
        return args, None
    data = _ensure_loaded(user_id)
    name_l = first.lower()
    for stored_name, expression in data["macros"].items():
        if stored_name.lower() == name_l:
            rest = args[1:]
            # No extra args → use the macro name as the label. Otherwise, the
            # user-provided trailing tokens act as a custom label.
            return ([expression] + (rest or [first])), None
    return args, (
        f"Macro '{first}' not found. Use /macro <expression> {first} to create one."
    )


# ---------- Subcommand handlers ----------

async def macro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/macro` (no args) → list; `/macro <expr> <name>` → save."""
    user = update.effective_user
    if user is None:
        return
    args = list(context.args or [])

    if not args:
        await _list(update, user.id)
        return

    if len(args) != 2:
        await _send_chat(update, "Usage: /macro <expression> <name>")
        return

    expression, name = args
    if not MACRO_NAME_RE.match(name):
        await _send_chat(
            update,
            "Invalid macro name. Use letters/digits/_/- (must start with a letter or underscore).",
        )
        return

    err = validate_roll_input([expression])
    if err:
        await _send_chat(update, f"Invalid expression: {err}")
        return

    data = _ensure_loaded(user.id)
    # Case-insensitive overwrite: if a macro with the same name (any case)
    # exists, replace it in place to preserve the user's original casing key.
    existing_key = next((k for k in data["macros"] if k.lower() == name.lower()), None)
    if existing_key is not None:
        previous = data["macros"][existing_key]
        del data["macros"][existing_key]
        data["macros"][name] = expression
        _save(user.id)
        await _send_chat(update, f"Macro '{name}' updated: {previous} → {expression}")
    else:
        data["macros"][name] = expression
        _save(user.id)
        await _send_chat(update, f"Macro '{name}' saved: {expression}")


async def _list(update: Update, user_id: int) -> None:
    data = _ensure_loaded(user_id)
    macros = data["macros"]
    if not macros:
        await _send_chat(
            update,
            "No macros saved yet. Use /macro <expression> <name> to create one.",
        )
        return
    lines = ["Your macros:"]
    for name in sorted(macros, key=str.lower):
        lines.append(f"- {name} → {macros[name]}")
    await _send_chat(update, "\n".join(lines))


async def macro_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/macrodel <name>` → remove one macro."""
    user = update.effective_user
    if user is None:
        return
    args = list(context.args or [])
    if len(args) != 1:
        await _send_chat(update, "Usage: /macrodel <name>")
        return
    name = args[0]
    data = _ensure_loaded(user.id)
    actual = next((k for k in data["macros"] if k.lower() == name.lower()), None)
    if actual is None:
        await _send_chat(update, f"Macro '{name}' not found.")
        return
    del data["macros"][actual]
    if data["macros"]:
        _save(user.id)
    else:
        # Last macro gone: drop the on-disk file too so the macros/ dir
        # doesn't accumulate empty stubs.
        _delete(user.id)
        _cache.pop(user.id, None)
    await _send_chat(update, f"Macro '{actual}' removed.")


async def macro_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/macroreset` → wipe everything for the calling user."""
    user = update.effective_user
    if user is None:
        return
    data = _ensure_loaded(user.id)
    if not data["macros"]:
        await _send_chat(update, "No macros to reset.")
        return
    data["macros"].clear()
    _delete(user.id)
    _cache.pop(user.id, None)
    await _send_chat(update, "All macros cleared.")
