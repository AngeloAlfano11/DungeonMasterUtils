"""Dice roller for /roll.

Grammar:
  input      := rolls (' ' label)?
  rolls      := item (',' item)*
  item       := multiplied | expression
  multiplied := N '(' expression ')'        # 1 ≤ N ≤ MAX_MULT
  expression := term (('+' | '-') term)*    # no spaces inside
  term       := dice_term | constant
  dice_term  := X 'd' Y ([H|L] N)?          # H = highest, L = lowest;
                                            #   N defaults to 1 if omitted
  constant   := integer
  label      := free-form text (used as header)

Roll display: discarded values (from H/L) are shown inside <tg-spoiler>
blocks together with their adjacent `+` connectors, leaving at least one
visible `+` between kept values when possible. The total is rendered in
<b>...</b>.
"""

import html
import logging
import random
import re

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# A single dice term, e.g. "3d12", "3d12H2", "3d12h" (H/L without N → defaults to 1).
DICE_RE = re.compile(r"^(\d+)d(\d+)(?:([hl])(\d+)?)?$", re.IGNORECASE)
# Tokenizer for an expression: optional leading sign, then a dice term or a constant.
TOKEN_RE = re.compile(r"([+\-])?(\d+d\d+(?:[hl]\d*)?|\d+)", re.IGNORECASE)
# Whitelist for the characters allowed inside an expression. Used as a quick
# sanity gate before the more permissive TOKEN_RE walk.
EXPR_RE = re.compile(r"^[\d+\-dhlDHL]+$")
# `N(expression)` syntax used to expand the same expression N times.
MULT_RE = re.compile(r"^(\d+)\((.+)\)$")

MAX_DICE_COUNT = 100
MAX_DICE_FACES = 1000
MAX_MULT = 20

USAGE = (
    "Usage: /roll <expr>[,<expr>...] [label]\n"
    "Examples:\n"
    "  /roll 1d20\n"
    "  /roll 3d12H2+4\n"
    "  /roll 3d12H2-4d20L2\n"
    "  /roll 5(3d12H2+2),2(1d20+5) Test rolls\n"
    "Notes:\n"
    "  - H = highest N, L = lowest N (1 ≤ N < dice count)\n"
    "  - No spaces inside the expression\n"
    "  - N(expr) repeats expr N times (1 ≤ N ≤ 20)"
)


class RollError(Exception):
    """Raised when /roll input is invalid; the message is the user-facing reply."""


def evaluate_term(body: str) -> tuple[int, str, bool]:
    """Roll/evaluate one term. Returns (subtotal, html_fragment, is_constant).

    A "term" is either a constant integer or a dice expression. For dice,
    we roll all dice, decide which ones are kept based on H/L, and let
    `_render_dice_fragment` build the parenthesized display string.
    """
    m = DICE_RE.match(body)
    if not m:
        # Not a dice term → must be a plain integer constant.
        try:
            n = int(body)
        except ValueError:
            raise RollError(USAGE)
        return n, str(n), True

    count = int(m.group(1))
    faces = int(m.group(2))
    mode = m.group(3).lower() if m.group(3) else None
    # H/L without an explicit N defaults to 1 (e.g. 4d20h == 4d20h1).
    if mode is None:
        keep_n = None
    elif m.group(4):
        keep_n = int(m.group(4))
    else:
        keep_n = 1

    if not (1 <= count <= MAX_DICE_COUNT):
        raise RollError(f"Number of dice must be between 1 and {MAX_DICE_COUNT}.")
    if not (2 <= faces <= MAX_DICE_FACES):
        raise RollError(f"Dice faces must be between 2 and {MAX_DICE_FACES}.")
    if mode and not (1 <= keep_n < count):
        raise RollError("With H/L, the keep count must be ≥ 1 and < the number of dice.")

    rolls = [random.randint(1, faces) for _ in range(count)]

    # Pick the kept indices: top N for H, bottom N for L, all otherwise.
    # Sorted is stable, so tied values keep their roll-order position.
    if mode == "h":
        kept = set(sorted(range(count), key=lambda i: rolls[i], reverse=True)[:keep_n])
    elif mode == "l":
        kept = set(sorted(range(count), key=lambda i: rolls[i])[:keep_n])
    else:
        kept = set(range(count))

    subtotal = sum(rolls[i] for i in kept)
    fragment = _render_dice_fragment(rolls, kept)
    return subtotal, fragment, False


def _render_dice_fragment(rolls: list[int], kept: set[int]) -> str:
    """Build the parenthesized roll display, grouping discarded rolls into
    spoiler blocks and absorbing adjacent `+` connectors. Leaves at least one
    `+` visible between kept values when possible to convey summation."""
    n = len(rolls)
    if n == 0:
        return "()"
    if n == 1:
        val = str(rolls[0])
        return f"({val})" if 0 in kept else f"(<tg-spoiler>{val}</tg-spoiler>)"

    # right_kept[i] = True if there's any kept index strictly greater than i
    right_kept = [False] * n
    found = False
    for i in range(n - 1, -1, -1):
        right_kept[i] = found
        if i in kept:
            found = True

    # Decide whether the "+" connector at position k (between rolls[k] and rolls[k+1]) is visible.
    visible_plus = [False] * (n - 1)
    for k in range(n - 1):
        a_kept = k in kept
        b_kept = (k + 1) in kept
        if a_kept and b_kept:
            visible_plus[k] = True
        elif a_kept and not b_kept and right_kept[k + 1]:
            # kept → discarded with another kept later: keep this "+" visible
            visible_plus[k] = True

    # Token stream as (text, hidden) pairs: each die value plus its trailing "+".
    tokens: list[tuple[str, bool]] = []
    for i in range(n):
        tokens.append((str(rolls[i]), i not in kept))
        if i < n - 1:
            tokens.append((" + ", not visible_plus[i]))

    # Merge adjacent same-state tokens so each spoiler block is one tag.
    merged: list[list] = []
    for text, hidden in tokens:
        if merged and merged[-1][1] == hidden:
            merged[-1][0] += text
        else:
            merged.append([text, hidden])

    # Render: hidden chunks become <tg-spoiler>...</tg-spoiler>; surrounding
    # whitespace is moved outside the tag so the spoiler block tightly wraps
    # only the content the user shouldn't see.
    out: list[str] = []
    for text, hidden in merged:
        if hidden:
            stripped = text.strip()
            leading_ws = text[: len(text) - len(text.lstrip())]
            trailing_ws = text[len(text.rstrip()):]
            out.append(leading_ws)
            out.append(f"<tg-spoiler>{stripped}</tg-spoiler>")
            out.append(trailing_ws)
        else:
            out.append(text)

    return "(" + "".join(out) + ")"


def parse_and_evaluate_expression(expr: str) -> list[tuple[int, int, str, bool]]:
    """Parse one expression and evaluate each term. Returns (sign, subtotal, fragment, is_constant) per term.

    The parser walks TOKEN_RE matches and verifies they cover the whole
    string contiguously — any gap means an invalid character snuck in.
    """
    if not EXPR_RE.match(expr):
        raise RollError(USAGE)

    resolved: list[tuple[int, int, str, bool]] = []
    pos = 0
    expected_sign = False
    for match in TOKEN_RE.finditer(expr):
        sign_char = match.group(1)
        body = match.group(2)
        # Reject any gap between the previous match and this one.
        if pos != match.start():
            raise RollError(USAGE)
        if not expected_sign:
            # First term must NOT carry a leading sign (no "-1d20" at the front).
            if sign_char is not None:
                raise RollError(USAGE)
            sign = +1
            expected_sign = True
        else:
            # Subsequent terms MUST carry a sign — that's the operator.
            if sign_char is None:
                raise RollError(USAGE)
            sign = +1 if sign_char == "+" else -1
        subtotal, fragment, is_const = evaluate_term(body)
        resolved.append((sign, subtotal, fragment, is_const))
        pos = match.end()

    # After the loop the matches must have covered the full string.
    if pos != len(expr) or not resolved:
        raise RollError(USAGE)

    return resolved


def render_expression(resolved: list[tuple[int, int, str, bool]]) -> tuple[str, int]:
    """Compose the textual display of an expression's resolved terms.

    Constants render compactly (`+4`, `-1`); dice fragments keep spaces
    around their operator (`+ (...)`, `- (...)`) for readability.
    """
    total = sum(sign * sub for sign, sub, _, _ in resolved)
    parts: list[str] = []
    for i, (sign, _, frag, is_const) in enumerate(resolved):
        if i == 0:
            parts.append(frag)
        elif is_const:
            parts.append(f" {'+' if sign == +1 else '-'}{frag}")
        else:
            parts.append(f" {'+' if sign == +1 else '-'} {frag}")
    return "".join(parts), total


def expand_items(rolls_part: str) -> list[list[str]]:
    """Split rolls_part on commas into groups; each group is a list of expressions
    (multipliers expand inline within their group)."""
    items = rolls_part.split(",")
    # Reject empty items: leading/trailing/duplicate commas.
    if any(not item for item in items):
        raise RollError(USAGE)
    groups: list[list[str]] = []
    for item in items:
        m = MULT_RE.match(item)
        if m:
            count = int(m.group(1))
            inner = m.group(2)
            if not (1 <= count <= MAX_MULT):
                raise RollError(f"Multiplier must be between 1 and {MAX_MULT}.")
            # `5(1d20)` → ["1d20", "1d20", "1d20", "1d20", "1d20"]
            groups.append([inner] * count)
        else:
            groups.append([item])
    return groups


def build_roll_response(args: list[str]) -> str:
    """Top-level orchestrator: parses args, rolls, and formats the final message.

    Output layouts:
      - Single result, no label: expression on one line, total on next (bold).
      - Single result, with label: label header + indented expression + total.
      - Multiple results: numbered `N# ...` lines, with a blank line between
        comma-separated groups so visually distinct rolls stay grouped.
    """
    if not args:
        raise RollError(USAGE)
    rolls_part = args[0]
    label = " ".join(args[1:]) if len(args) > 1 else None

    groups = expand_items(rolls_part)
    safe_label = html.escape(label) if label else None

    total_count = sum(len(g) for g in groups)
    if total_count == 1:
        # Single-roll layout: keep the original two-line format with the
        # total on its own line.
        expr = groups[0][0]
        expr_line, total = render_expression(parse_and_evaluate_expression(expr))
        if safe_label:
            return f"{safe_label}:\n  {expr_line} =\n<b>{total}</b>"
        return f"{expr_line} =\n<b>{total}</b>"

    # Multi-roll layout: a numbered list, with comma-separated groups
    # separated by a blank line.
    rendered_groups: list[str] = []
    counter = 1
    for group in groups:
        lines = []
        for expr in group:
            expr_line, total = render_expression(parse_and_evaluate_expression(expr))
            lines.append(f"{counter}# {expr_line} = <b>{total}</b>")
            counter += 1
        rendered_groups.append("\n".join(lines))

    body = "\n\n".join(rendered_groups)
    if safe_label:
        return f"{safe_label}:\n{body}"
    return body


async def roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram entry point.

    Reply with a "rolling…" placeholder first, then edit it with the result.
    On RollError we surface the message to the user; on any other exception
    we log and show a generic error.
    """
    msg = update.effective_message
    waiting_msg = await msg.reply_text("\U0001f3b2 rolling...")

    try:
        text = build_roll_response(context.args or [])
    except RollError as e:
        await waiting_msg.edit_text(str(e))
        return
    except Exception:
        logger.exception("Unexpected error in /roll")
        await waiting_msg.edit_text("Unexpected error. Try again.")
        return

    await waiting_msg.edit_text(text, parse_mode="HTML")
