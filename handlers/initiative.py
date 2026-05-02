"""Initiative tracker for /init.

Per-thread combat state: combatants ordered by initiative, an active pointer
that advances on /init next, and a list of "effects" with per-round or per-
combatant expiration. State is persisted to JSON files under COMBATS_DIR so
restarts don't lose an in-progress fight.

Display flow: the first combatant added to an empty fight pins a fresh
status message in the chat; every subsequent state-mutating command edits
that pinned message in place. Players whose `hidden` flag is true show only
their name in the pin (init/HP omitted).
"""

import html
import json
import logging
import os
from pathlib import Path

from telegram import Message, Update
from telegram.constants import MessageEntityType
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import AUTHORIZED_USERS, COMBATS_DIR

logger = logging.getLogger(__name__)

# (chat_id, thread_id) — same key used by the recorder to scope per-thread state.
Key = tuple[int, int | None]
encounters: dict[Key, dict] = {}

USAGE = (
    "Usage:\n"
    "  /init                       — show current state\n"
    "  /init list                  — show tracked effects\n"
    "  /init <name> <init> [hp[/maxhp]] [@user]\n"
    "  /init next | /init n        — next turn\n"
    "  /init prev | /init p        — previous turn\n"
    "  /init hp <name> <±N|=N>     — modify HP\n"
    "  /init kill <name>           — mark as defeated (skipped, strikethrough)\n"
    "  /init revive <name>         — undo kill\n"
    "  /init rm <name>             — remove combatant\n"
    "  /init track <n|name> <text> — track an effect\n"
    "  /init clear                 — end fight"
)


# ---------- Persistence ----------

def _key_from_msg(msg: Message) -> Key:
    return (msg.chat_id, msg.message_thread_id)


def _combat_path(key: Key) -> Path:
    """One JSON file per (chat, thread). Threadless chats use the literal
    'main' as label so the filename never contains 'None'."""
    chat_id, thread_id = key
    label = str(thread_id) if thread_id is not None else "main"
    return COMBATS_DIR / f"chat{chat_id}_thread{label}.json"


def _save(key: Key, encounter: dict) -> None:
    """Atomic write: serialize to a `.tmp` sibling then rename onto the
    target. os.replace is atomic on both POSIX and Windows so a crash mid-
    write can't leave a half-written JSON file."""
    COMBATS_DIR.mkdir(exist_ok=True)
    path = _combat_path(key)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(encounter, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _delete(key: Key) -> None:
    _combat_path(key).unlink(missing_ok=True)


def load_all_encounters() -> None:
    """Called once at bot startup. Each parseable file becomes an entry in
    the in-memory `encounters` dict. Corrupted files are logged and skipped
    so one bad JSON doesn't prevent the bot from starting."""
    if not COMBATS_DIR.exists():
        return
    for path in COMBATS_DIR.glob("chat*_thread*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                enc = json.load(f)
            encounters[(enc["chat_id"], enc["thread_id"])] = enc
        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("Skipping corrupted combat file %s: %s", path.name, e)


# ---------- Generic helpers ----------

async def _send_chat(update: Update, text: str, **kwargs) -> Message:
    """Send a message into the originating chat/thread.

    Used everywhere instead of `msg.reply_text` because the dispatcher
    deletes the trigger message: a `reply_text` after deletion would either
    fail or render as a reply to a missing message.
    """
    msg = update.effective_message
    return await update.effective_chat.send_message(
        text=text,
        message_thread_id=msg.message_thread_id,
        **kwargs,
    )


def _new_encounter(chat_id: int, thread_id: int | None) -> dict:
    return {
        "chat_id": chat_id,
        "thread_id": thread_id,
        "combatants": [],
        "active_idx": 0,
        "round": 1,
        "effects": [],
        "pinned_message_id": None,
    }


def _parse_hp_spec(s: str) -> tuple[int, int]:
    """Parse the HP literal at /init add time: `30` → (30, 30); `25/30` → (25, 30)."""
    if "/" in s:
        cur, mx = s.split("/", 1)
        return int(cur), int(mx)
    n = int(s)
    return n, n


def _parse_hp_change(spec: str, current: int) -> int | None:
    """Returns new HP from a `=N` / `+N` / `-N` spec, or None if invalid.

    The leading sign or `=` is mandatory: a bare number would be ambiguous
    between "set to N" and "deal N damage" so we force the user to be
    explicit.
    """
    if spec.startswith("="):
        body = spec[1:]
    elif spec[:1] in "+-":
        body = spec
    else:
        return None
    try:
        value = int(body)
    except ValueError:
        return None
    return value if spec.startswith("=") else current + value


def _find_combatant(enc: dict, name: str) -> int | None:
    """Case-insensitive name lookup → list index or None."""
    name_l = name.lower()
    return next(
        (i for i, c in enumerate(enc["combatants"]) if c["name"].lower() == name_l),
        None,
    )


def is_spoiler_message(msg: Message) -> bool:
    """True if the message contains any spoiler entity. Used at add time to
    flag a combatant as `hidden` (init/HP omitted from the pinned view)."""
    return any(e.type == MessageEntityType.SPOILER for e in (msg.entities or []))


def is_defeated(c: dict) -> bool:
    return c["defeated"]


async def _require_encounter(
    update: Update, *, allow_empty: bool = False
) -> tuple[Key | None, dict | None]:
    """Fetch the encounter for this chat/thread or send an error.

    `allow_empty=True` is used by /init clear, which needs to operate on an
    encounter even after every combatant has been removed.
    """
    key = _key_from_msg(update.effective_message)
    enc = encounters.get(key)
    if enc is None or (not allow_empty and not enc["combatants"]):
        await _send_chat(update, "No active fight.")
        return None, None
    return key, enc


async def _require_combatant(update: Update, enc: dict, name: str) -> int | None:
    """Resolve a combatant by name; send an error reply when missing."""
    idx = _find_combatant(enc, name)
    if idx is None:
        await _send_chat(update, f"Combatant '{name}' not found.")
    return idx


def _active_ping(enc: dict) -> str | None:
    """Build the @mention ping line for the active combatant, or None when
    they have no linked Telegram user."""
    active = enc["combatants"][enc["active_idx"]]
    return f"{active['mention']}'s turn" if active["mention"] else None


# ---------- Render ----------

def _wrap_strike(s: str, defeated: bool) -> str:
    return f"<s>{s}</s>" if defeated else s


def render(enc: dict) -> str:
    """Build the HTML for the pinned status message.

    Two layouts depending on whether any combatant tracks HP:
      - Compact inline (`ROUND N : Kael(18) — Goblin(14)`) when no one has HP
      - Vertical list with "⚔ Round N" header when at least one has HP

    A combatant's `hidden` flag suppresses both their initiative and HP, so
    the pinned message only ever leaks the name. The active combatant's
    name is underlined; defeated combatants are rendered in strikethrough.
    """
    if not enc["combatants"]:
        return "No active fight."

    round_n = enc["round"]
    active = enc["active_idx"]
    cs = enc["combatants"]

    has_hp = any(c["hp_current"] is not None for c in cs)
    if not has_hp:
        # Compact one-line layout.
        parts = []
        for i, c in enumerate(cs):
            name = html.escape(c["name"])
            wrapped = f"<u>{name}</u>" if i == active else name
            parts.append(wrapped if c["hidden"] else f"{wrapped}({c['init']})")
        return f"ROUND {round_n} : " + " — ".join(parts)

    # Vertical layout: one combatant per line, marker for the active one.
    lines = [f"⚔ Round {round_n}"]
    for i, c in enumerate(cs):
        marker = "▶ " if i == active else "  "
        name = html.escape(c["name"])
        body = f"<u>{name}</u>" if i == active else name
        if not c["hidden"]:
            body += f" — init {c['init']}"
            if c["hp_current"] is not None:
                hp = c["hp_current"]
                body += f" — HP {hp}/{c['hp_max']}" if c["hp_max"] is not None else f" — HP {hp}"
        # Marker stays outside the strikethrough so the ▶ keeps standing out.
        lines.append(marker + _wrap_strike(body, is_defeated(c)))
    return "\n".join(lines)


def render_effects(enc: dict) -> str:
    """Build the HTML body for /init list — the active effects with rounds
    remaining (or, for combatant-bound effects, the target turn)."""
    if not enc["effects"]:
        return "No active effects."
    lines = ["Active effects:"]
    for e in enc["effects"]:
        text = html.escape(e["text"])
        target = e["target_combatant"]
        if target:
            lines.append(f"- {text} (until {html.escape(target)}'s turn in R{e['expires_at_round']})")
        else:
            rounds_left = e["expires_at_round"] - enc["round"]
            lines.append(f"- {text} ({rounds_left} rounds left)")
    return "\n".join(lines)


def _format_expired_line(e: dict) -> str:
    """One line in the "Expired this round:" notification — append the
    target name in parens for combatant-bound effects so it's clear which
    combatant the expiration belongs to."""
    text = html.escape(e["text"])
    target = e["target_combatant"]
    return f"- {text} ({html.escape(target)})" if target else f"- {text}"


# ---------- Pin update ----------

async def update_state_view(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    enc: dict,
    *,
    ping_text: str | None = None,
) -> None:
    """Refresh the pinned status message after a state change.

    Tries `edit_message_text` first; falls back to sending a new message and
    re-pinning if the original was deleted (BadRequest). If a `ping_text`
    is provided (turn change with an @mention), it's posted as a separate
    message so the @user actually receives a notification.
    """
    msg = update.effective_message
    text = render(enc)
    pinned_id = enc["pinned_message_id"]

    if pinned_id:
        try:
            await context.bot.edit_message_text(
                chat_id=msg.chat_id,
                message_id=pinned_id,
                text=text,
                parse_mode="HTML",
            )
        except BadRequest:
            # The pinned message was probably deleted by a user. Recreate it.
            new_msg = await _send_chat(update, text, parse_mode="HTML")
            try:
                await new_msg.pin(disable_notification=True)
                enc["pinned_message_id"] = new_msg.message_id
                _save(_key_from_msg(msg), enc)
            except BadRequest:
                # Bot lacks pin permission — leave it unpinned, still visible.
                pass
    else:
        # No pin yet (encounter created via /init add then handled here on
        # subsequent commands): send a fresh message without pinning.
        await _send_chat(update, text, parse_mode="HTML")

    if ping_text:
        await _send_chat(update, ping_text)


# ---------- Advance & expiration ----------

def _advance(enc: dict, delta: int) -> tuple[bool, list[int]]:
    """Move the active pointer by `delta` (1 = next turn, -1 = previous).

    Defeated combatants are skipped automatically. Returns:
      - advanced: True if a non-defeated combatant was found
      - skipped_indices: positions of defeated combatants stepped over

    The `skipped_indices` list is used by `_expire_effects` to fire effects
    whose target was passed without ever becoming the active combatant.
    """
    n = len(enc["combatants"])
    skipped: list[int] = []
    if n == 0 or all(is_defeated(c) for c in enc["combatants"]):
        return False, skipped

    new_idx = enc["active_idx"]
    new_round = enc["round"]
    # Walk at most `n` positions: if everyone but the current is defeated
    # this still terminates on the wrap-around to the active.
    for _ in range(n):
        new_idx += delta
        if new_idx >= n:
            # Wrapped past the end: round +1 and back to the top.
            new_idx = 0
            new_round += 1
        elif new_idx < 0:
            # Wrapped before the start: round -1 (but never below 1) and to the bottom.
            new_idx = n - 1
            if new_round > 1:
                new_round -= 1
        if not is_defeated(enc["combatants"][new_idx]):
            enc["active_idx"] = new_idx
            enc["round"] = new_round
            return True, skipped
        skipped.append(new_idx)
    return False, skipped


def _expire_effects(enc: dict, skipped_indices: list[int]) -> list[dict]:
    """Remove and return effects whose timing has elapsed.

    Round-based: fires when current round >= expires_at_round.
    Combatant-bound: fires when active is target OR target was skipped this turn,
    in the target round (or any later round, as a fallback)."""
    expired, remaining = [], []
    active_name = enc["combatants"][enc["active_idx"]]["name"] if enc["combatants"] else None
    skipped_names = {enc["combatants"][i]["name"] for i in skipped_indices}
    cur_r = enc["round"]

    for e in enc["effects"]:
        target = e["target_combatant"]
        target_r = e["expires_at_round"]
        if target is None:
            # Pure round counter.
            should_expire = cur_r >= target_r
        elif cur_r > target_r:
            # Late catch-up: the target round has passed entirely.
            should_expire = True
        else:
            # Exactly on target round: fire iff target is now active or got skipped this advance.
            should_expire = cur_r == target_r and (
                active_name == target or target in skipped_names
            )
        (expired if should_expire else remaining).append(e)

    enc["effects"] = remaining
    return expired


# ---------- Subcommand handlers ----------

async def handle_add(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    """Add a combatant (creating the encounter on first call).

    Args order is flexible: any token starting with '@' is treated as the
    Telegram mention regardless of position. The remaining tokens must be:
      <name> <init> [hp[/maxhp]]

    On the first add to an empty/new encounter, the rendered state is
    pinned in the chat; subsequent adds edit that pinned message.
    """
    msg = update.effective_message
    key = _key_from_msg(msg)
    chat_id, thread_id = key

    # Pull out the @mention from anywhere in the args; only the first one wins.
    mention = None
    positional: list[str] = []
    for a in args:
        if a.startswith("@") and len(a) > 1 and mention is None:
            mention = a
        else:
            positional.append(a)

    if len(positional) < 2:
        await _send_chat(update, "Usage: /init <name> <init> [hp[/maxhp]] [@user]")
        return

    name = positional[0]
    try:
        init_val = int(positional[1])
    except ValueError:
        await _send_chat(update, "Initiative must be an integer.")
        return

    hp_current: int | None = None
    hp_max: int | None = None
    if len(positional) >= 3:
        try:
            hp_current, hp_max = _parse_hp_spec(positional[2])
        except ValueError:
            await _send_chat(update, "HP format must be N or N/M.")
            return
        if hp_current < 0 or hp_max <= 0 or hp_current > hp_max:
            await _send_chat(update, "Invalid HP values.")
            return

    # Lazily create the encounter the first time anyone is added.
    enc = encounters.setdefault(key, _new_encounter(chat_id, thread_id))

    if _find_combatant(enc, name) is not None:
        await _send_chat(update, f"Combatant '{name}' already exists.")
        return

    new_combatant = {
        "name": name,
        "init": init_val,
        "hp_current": hp_current,
        "hp_max": hp_max,
        "mention": mention,
        # If the trigger message contained any spoiler entity, the new
        # combatant's init/HP are hidden in the pinned view.
        "hidden": is_spoiler_message(msg),
        "defeated": False,
    }

    # Re-sort by initiative descending; preserve the active combatant by name
    # so the active pointer keeps pointing at the right person after the sort.
    active_name = (
        enc["combatants"][enc["active_idx"]]["name"] if enc["combatants"] else None
    )
    enc["combatants"].append(new_combatant)
    enc["combatants"].sort(key=lambda c: -c["init"])

    if active_name:
        enc["active_idx"] = next(
            i for i, c in enumerate(enc["combatants"]) if c["name"] == active_name
        )

    _save(key, enc)

    # First combatant in a fresh encounter: send the status message and pin it.
    if enc["pinned_message_id"] is None:
        pinned = await _send_chat(update, render(enc), parse_mode="HTML")
        try:
            await pinned.pin(disable_notification=True)
        except BadRequest:
            pass
        enc["pinned_message_id"] = pinned.message_id
        _save(key, enc)
        return

    await update_state_view(update, context, enc)


async def handle_next(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    """Advance to the next combatant and run the per-turn expiration sweep."""
    key, enc = await _require_encounter(update)
    if enc is None:
        return

    advanced, skipped = _advance(enc, +1)
    if not advanced:
        await _send_chat(update, "No eligible combatants to advance to.")
        return

    expired = _expire_effects(enc, skipped)
    _save(key, enc)
    await update_state_view(update, context, enc, ping_text=_active_ping(enc))

    # Surface expirations as a separate message so the GM can see what
    # ended right at the moment it ended.
    if expired:
        txt = "Expired this round:\n" + "\n".join(_format_expired_line(e) for e in expired)
        await _send_chat(update, txt)


async def handle_prev(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    """Move the active pointer backwards. Effects are NOT un-expired:
    going back is meant to fix a misclick on /init next, not to rewind time."""
    key, enc = await _require_encounter(update)
    if enc is None:
        return

    advanced, _skipped = _advance(enc, -1)
    if not advanced:
        await _send_chat(update, "No eligible combatants to go back to.")
        return

    _save(key, enc)
    await update_state_view(update, context, enc, ping_text=_active_ping(enc))


async def handle_hp(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    """Apply an HP delta or absolute value. Negative HP is allowed (some
    systems care about how far below 0 you are); only the upper bound is
    clamped to hp_max so over-healing past max doesn't happen."""
    key, enc = await _require_encounter(update)
    if enc is None:
        return
    if len(args) < 2:
        await _send_chat(update, "Usage: /init hp <name> <±N|=N>")
        return

    idx = await _require_combatant(update, enc, args[0])
    if idx is None:
        return

    c = enc["combatants"][idx]
    if c["hp_current"] is None:
        await _send_chat(update, f"Combatant '{c['name']}' has no HP tracking.")
        return

    new_hp = _parse_hp_change(args[1], c["hp_current"])
    if new_hp is None:
        await _send_chat(update, "HP requires a sign: use +N, -N, or =N.")
        return

    # Cap at max if defined; allow negative values for "very dead" tracking.
    if c["hp_max"] is not None:
        new_hp = min(c["hp_max"], new_hp)
    c["hp_current"] = new_hp

    _save(key, enc)
    await update_state_view(update, context, enc)


async def handle_remove(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    """Remove a combatant entirely and clean up effects bound to them."""
    key, enc = await _require_encounter(update)
    if enc is None:
        return
    if not args:
        await _send_chat(update, "Usage: /init rm <name>")
        return

    idx = await _require_combatant(update, enc, args[0])
    if idx is None:
        return

    removed_name = enc["combatants"][idx]["name"]
    enc["combatants"].pop(idx)

    # Keep `active_idx` pointing at the same combatant after the pop.
    if enc["combatants"]:
        if idx < enc["active_idx"]:
            enc["active_idx"] -= 1
        elif enc["active_idx"] >= len(enc["combatants"]):
            # The active was last and got removed; clamp to new last.
            enc["active_idx"] = len(enc["combatants"]) - 1
    else:
        enc["active_idx"] = 0

    # Drop effects targeting this combatant: they'd never fire otherwise.
    enc["effects"] = [e for e in enc["effects"] if e["target_combatant"] != removed_name]

    _save(key, enc)
    await update_state_view(update, context, enc)


async def _set_defeated(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    args: list[str],
    *,
    defeated: bool,
    cmd_name: str,
) -> None:
    """Shared implementation for /init kill and /init revive — they only
    differ in the boolean and the help string."""
    key, enc = await _require_encounter(update)
    if enc is None:
        return
    if not args:
        await _send_chat(update, f"Usage: /init {cmd_name} <name>")
        return
    idx = await _require_combatant(update, enc, args[0])
    if idx is None:
        return
    enc["combatants"][idx]["defeated"] = defeated
    _save(key, enc)
    await update_state_view(update, context, enc)


async def handle_kill(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    await _set_defeated(update, context, args, defeated=True, cmd_name="kill")


async def handle_revive(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    await _set_defeated(update, context, args, defeated=False, cmd_name="revive")


async def handle_track(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    """Add a tracked effect.

    The first arg is overloaded:
      - integer N (positive) → round-based effect, fires after N rounds.
      - combatant name      → combatant-bound, fires when that combatant's
                              next turn comes up (or is skipped because
                              they're defeated).
    """
    key, enc = await _require_encounter(update)
    if enc is None:
        return
    if len(args) < 2:
        await _send_chat(update, "Usage: /init track <n|name> <text>")
        return

    first = args[0]
    text = " ".join(args[1:]).strip()
    if not text:
        await _send_chat(update, "Effect text cannot be empty.")
        return

    target: str | None
    try:
        # Try the integer interpretation first; ValueError → fall back to name.
        n = int(first)
        if n <= 0:
            await _send_chat(update, "Round count must be a positive integer.")
            return
        target = None
        expires_at = enc["round"] + n
        confirm = f"Tracking '{text}' (expires R{expires_at})"
    except ValueError:
        idx = await _require_combatant(update, enc, first)
        if idx is None:
            return
        target = enc["combatants"][idx]["name"]
        # Combatant-bound effects always target their next turn (the round
        # after the current one).
        expires_at = enc["round"] + 1
        confirm = f"Tracking '{text}' (until {target}'s next turn in R{expires_at})"

    enc["effects"].append({
        "text": text,
        "expires_at_round": expires_at,
        "target_combatant": target,
    })
    _save(key, enc)
    await _send_chat(update, confirm)


async def handle_list_effects(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    """/init list — read-only effect dump, doesn't touch the pinned message."""
    enc = (await _require_encounter(update))[1]
    if enc is None:
        return
    await _send_chat(update, render_effects(enc), parse_mode="HTML")


async def handle_clear(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    """End the encounter: unpin the status message, drop the file, forget the state."""
    msg = update.effective_message
    key, enc = await _require_encounter(update, allow_empty=True)
    if enc is None:
        return

    pinned_id = enc["pinned_message_id"]
    if pinned_id:
        try:
            await context.bot.unpin_chat_message(chat_id=msg.chat_id, message_id=pinned_id)
        except BadRequest:
            # Pin already gone or no permission — not worth surfacing.
            pass

    encounters.pop(key, None)
    _delete(key)
    await _send_chat(update, "Fight cleared.")


async def _show_state(update: Update) -> None:
    """Bare /init: print the current state without pinning. Used as the
    fallback when no subcommand and no add args are provided."""
    key = _key_from_msg(update.effective_message)
    enc = encounters.get(key)
    if enc is None or not enc["combatants"]:
        await _send_chat(update, "No active fight. Use /init <name> <init> to start.")
        return
    await _send_chat(update, render(enc), parse_mode="HTML")


# ---------- Dispatch ----------

# Subcommand keyword → handler. The dispatcher falls back to handle_add when
# the first token is none of these (treating `/init Kael 18 60` as add shorthand).
SUBCOMMANDS = {
    "add": handle_add,
    "list": handle_list_effects,
    "next": handle_next, "n": handle_next,
    "prev": handle_prev, "p": handle_prev, "back": handle_prev,
    "hp": handle_hp,
    "rm": handle_remove, "remove": handle_remove,
    "kill": handle_kill,
    "revive": handle_revive,
    "track": handle_track,
    "clear": handle_clear, "end": handle_clear,
}


async def initiative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level /init dispatcher.

    1. AUTHORIZED_USERS gate — non-GMs are silently ignored.
    2. Best-effort delete of the trigger message to keep the chat clean.
    3. Bare /init → show state; first arg matches a subcommand → dispatch;
       otherwise treat the whole arg list as a `/init add` shorthand.
    """
    user = update.effective_user
    if user is None or user.id not in AUTHORIZED_USERS:
        return

    msg = update.effective_message
    try:
        await msg.delete()
    except BadRequest:
        # No delete permission — ignore, the rest of the handler still works.
        pass

    if not context.args:
        await _show_state(update)
        return

    sub = context.args[0].lower()
    handler = SUBCOMMANDS.get(sub)
    if handler is not None:
        await handler(update, context, list(context.args[1:]))
    else:
        # Unknown first token → assume the user meant to add a combatant.
        await handle_add(update, context, list(context.args))
