"""Chat-style Pane replies with append-only history (no silent drops).

Discord has no native token stream. Pattern:
  1. Reply immediately with 「思考中…」
  2. Diff each pane.read snapshot against the previous one; append every
     inserted/replaced line into an in-memory turn buffer (never drop earlier lines)
  3. Throttle-edit the live Discord message; on failure keep pending and retry
  4. Near 2000 chars, freeze the bubble and continue on 「（续）」— sealed text stays

Approve / Yes-No buttons remain separate (choice_ui) when a real prompt appears.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from src.bot.config import BridgeConfig

log = logging.getLogger(__name__)

MSG_LIMIT = 2000
# Leave room so mid-stream edits don't constantly hit the ceiling.
SOFT_LIMIT = 1800
# Default stream cadence if config cooldown is very low.
MIN_EDIT_INTERVAL = 1.0
# Discord typing indicator expires ~10s; refresh while waiting/streaming.
TYPING_REFRESH = 8.0
PLACEHOLDER = "思考中…"
FLUSH_RETRY_DELAY = 1.5

# TUI spinners (braille + common unicode spinners) rewritten in-place each frame.
# Claude Code / Herdr "whirlpool" spinners cycle ✽ ✢ ✻ ✶ ✳ · on every frame, so the
# glyph must be stripped or successive frames of one slot never share a template key.
_BRAILLE_RE = re.compile(r"[\u2800-\u28FF]+")
_SPINNER_GLYPH_RE = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒✽✢✻✶✳✧✦·⏺]+")
# Live status verbs — not exhaustive; template also keys off timers/counters.
_PHASE_RE = re.compile(
    r"\b(?:Running|Working|Thinking|Waiting|Downloading|Uploading|Building|"
    r"Compiling|Installing|Syncing|Loading|Processing|Searching|Indexing|"
    r"Connecting|Retrying|Pending|Whirlpooling|Propagating|Implementing|"
    r"Creating|Extracting|Reviewing|Verifying|Generating|Refactoring|Thought|"
    r"Compacting|Inferring|Compressing|Summarizing|Resolving|Preparing|"
    r"Finalizing|Deciding|Rewriting|Merging|Adjusting)\b",
    re.IGNORECASE,
)
# Claude Code tags slow frames "· still thinking" / "· thinking more" /
# "· thinking some more" — the qualifier must normalize or a slot splits into
# two keys mid-spin.
_THINK_STATE_RE = re.compile(
    r"\bstill\s+thinking\b|\bthinking\s+(?:more|some\s+more)\b", re.IGNORECASE
)
# Progress bars redraw as ▰▰▰▱▱▱▱… on every frame; only the percent is identity.
_BAR_RE = re.compile(r"[▰▱█░▌▐]+")
# A trailing parenthetical on a status line is transient qualifier state
# ("(2m 11s · ↓ 3.6k tokens)", "(thinking some more)", "(thought for 30s)") and
# must collapse so all qualifier variants of one slot share a key.
_QUALIFIER_RE = re.compile(r"\s*\([^()]*\)\s*$")
# Agent rows (◯ <name> <desc>… <timer>) are a live slot keyed by agent name; the
# description truncates as the timer widens, so only the name is stable.
_AGENT_ROW_RE = re.compile(r"^\s*◯\s+([^\s]+)")
# TUI chrome re-inserted on full-window repaints (task lists, status bars, agent
# rows, wrapped breadcrumbs). Identical re-insertions are deduplicated against
# the recent tail.
_CHROME_LINE_RE = re.compile(
    r"^\s*(?:[⎿├└│┌┐┘─╭╮╰╯❯⏺]|◻|◯|◼|▸|▹|▶|⏵|…\s*\+\d+\s+pending|"
    r"\(\d+\s*lines?\)|[\.\w-]+/[\w./-]+(?:\(\d+\s*lines?\))?)"
)
# How far back a live status slot may be found when a repaint interleaves ordinary
# chrome (task list / status bar) between the spinner slot and the window tail.
STATUS_SLOT_WINDOW = 12

# ---- Pinned TUI rows (task panels, folded summaries) ------------------------
# Some agents pin a panel to the bottom of the TUI (task lists, folded counts).
# These rows are neither fully static (done-counts, checkmarks change) nor fully
# dynamic, and they repaint as a block every frame — so exact-match chrome dedup
# misses them (only identical lines dedup) and they re-append every frame. They
# get template slots keyed on structure and update in place like status lines.
_TASK_COUNT_RE = re.compile(r"^\s*\d+\s+tasks?\s*\([^)]*\)\s*$")
_TASK_ROW_RE = re.compile(r"^\s*[◼◻✔✖⬤✓☑☐]\s+Task\s+(\d+)")
_COLLAPSED_RE = re.compile(r"^\s*…\s*\+\d+\s+\w+\s*$")
# Mirrors herdr's is_horizontal_rule() (src/detect/manifest.rs): a line starting
# with ≥3 box-drawing dashes is decorative even when it carries a label
# ("─ 完成 3 项 ─"). Keyed constant → count/label changes update in place.
_DECOR_BAR_RE = re.compile(r"^\s*─{3,}.*─\s*$")
# A task panel is wider than the status window; give its slots more tail to scan.
_UI_SLOT_WINDOW = 40

# ---- TUI footer chrome (per-agent fixed bottom regions) -----------------------
# Every coding-agent TUI pins a fixed footer to the bottom of its screen:
# pi renders pwd + token/context stats + extension statuses (all ``theme.fg(
# "dim")``), opencode renders a spinner row + status bar under a ``╹▀`` divider,
# claude code renders a ``─── ❯ ───`` prompt band with a help line. herdr does
# not filter these — it only detects agent Working/Idle state and forwards the
# raw screen text — and its screen snapshot carries no styling (ScreenTextCell
# holds graphemes only), so the dim marking is lost before the bot sees it.
# These rows repaint every frame and are never part of the reply, so they are
# dropped outright. Patterns are keyed to each agent's *source-verified* render
# structure: pi's FooterComponent.render() (pwd/stats/extension-status lines,
# all ``theme.fg("dim")``), pi's WorkingStatusIndicator (Loader spinner frames
# ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ + ``Working...``), pi's bash tool tail (running
# ``Elapsed 0.0s`` — the completed ``Took 5.2s`` line is reply content and is
# kept), plus observed opencode/claude status bands (opencode is a compiled
# binary; its ╹▀ divider, ctrl+p status bar and ▣ spinner row and claude's
# bypass-permissions prompt band were captured from live screens). Real content
# matching any of these is vanishingly rare.
_PI_STATS_RE = re.compile(
    r"^\s*↑\S+\s+↓\S+"  # pi stats: ↑tok ↓tok [R..] [CH..%] pct%/window (model)
)
_PI_EXT_RE = re.compile(r"^\s*[🧠🔌⚡]\S*\s+.*MCP\s*:\s*\d+\s+servers?", re.IGNORECASE)
_PI_ELAPSED_RE = re.compile(
    r"^\s*Elapsed\s+\d+(?:\.\d+)?\s*s\s*$"  # bash tool still running (muted)
)
_PI_SOURCE_RE = re.compile(r"^\s*源码\s*[:：]")
_WORKING_ONLY_RE = re.compile(r"^\s*[⠁-⣿]\s*Working\.{0,3}\s*$", re.IGNORECASE)
_OC_SPINNER_RE = re.compile(
    r"^\s*[▣◔◑◕◴◵◶◷]\s*\S.*·\s*\d+(?:\.\d+)?\s*s\s*$"  # opencode working row
)
_OC_DIVIDER_RE = re.compile(r"^\s*╹▀+")
_OC_STATUS_RE = re.compile(
    r"^\s*/[\w./-]+\s+\d[\d.,]*\s*[KkMm]?\s*\(\s*\d+\s*%\s*\)\s+ctrl\+p"
)
_CC_HELP_RE = re.compile(r"^\s*⏵+\s*\S.*(?:permissions|shift\+tab|for agents)")


def _is_tui_footer_chrome(line: str) -> bool:
    """True for fixed TUI footer rows that must never reach Discord.

    These are agent-rendered screen chrome (spinner ``Working`` status, pi token
    stats, opencode status bar, claude prompt band), not reply content. They
    repaint every frame; without this gate the status-slot machinery can still
    re-append them when the diff window slides past the previous frame.
    """
    return bool(
        _WORKING_ONLY_RE.match(line)
        or _PI_STATS_RE.match(line)
        or _PI_EXT_RE.match(line)
        or _PI_ELAPSED_RE.match(line)
        or _PI_SOURCE_RE.match(line)
        or _OC_SPINNER_RE.match(line)
        or _OC_DIVIDER_RE.match(line)
        or _OC_STATUS_RE.match(line)
        or _CC_HELP_RE.match(line)
    )


def fixed_ui_key(line: str) -> str | None:
    """Template slot key for pinned TUI rows (task panels / folded summaries).

    ``10 tasks (6 done, …)`` → ``<taskcount>``, ``◼ Task 7: …`` → ``<task:7>``
    (keyed on the number, so checkmark changes ◼→✔ and description edits update
    the same slot in place), ``… +5 completed`` → ``<collapsed>``. Returns
    ``None`` for anything else — real content is never absorbed.
    """
    if _TASK_COUNT_RE.match(line):
        return "<taskcount>"
    m = _TASK_ROW_RE.match(line)
    if m:
        return f"<task:{m.group(1)}>"
    if _COLLAPSED_RE.match(line):
        return "<collapsed>"
    if _DECOR_BAR_RE.match(line):
        return "<decor-bar>"
    return None
_TOKEN_COUNT_RE = re.compile(r"\b[\d.,]+\s*[kKmM]?\s*tokens?\b", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"\b\d+\s*[hH]\s*\d+\s*[mM](?:\s*\d+\s*[sS])?\b|"
    r"\b\d+\s*[mM]\s*\d+\s*[sS]\b|"
    r"\b\d+(?:\.\d+)?\s*[hmsHMS]\b"
)
_PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?%(?=\s|$)")
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
_ELLIPSIS_RE = re.compile(r"\.{2,}|…+")

# ---- Terminal text sanitization (tabs / control / invisible chars) ------------
# Tab stops: terminals default to 8. Discord plain messages collapse tabs, so we
# expand them to spaces at terminal stops — column-aware over wide (CJK) chars.
_TABSTOP = 8
# Invisible zero-width / format / bidi markers: Discord renders them unpredictably
# and counts them toward the 2000-char limit without showing them.
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
# C0/C1 control characters (Discord drops or mis-renders these). \n and \t are
# handled separately; \r is folded away before this runs.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# ---- Box-drawing tables: convert to Discord-native Markdown tables ------------
# Discord renders plain messages in a proportional font, so box-drawing tables
# (┌─┬─┐ │ ├┼┤ └┴┘) can never align there — no amount of padding fixes that.
# Instead we detect the table structure and rewrite it as a Markdown table,
# which Discord renders natively. Tree output (├── src / └── lib / │   dir)
# never becomes a table: conversion requires a whole-line border row.
_BOX_EDGE_RE = re.compile(r"^[┌─┬┐├┼┤└┴┘━]+$")  # whole-line border/separator
_BOX_DATA_RE = re.compile(r"^\s*│")  # data row starting with a vertical bar
_BOX_DECOR_RE = re.compile(r"^[─━]{3,}$")  # pure horizontal rule (no cells)
_TABLE_TOP_RE = re.compile(r"^\s*┌")  # top border starts a box table
_TABLE_BOT_RE = re.compile(r"^\s*└")  # bottom border ends a box table


def _char_cell_width(ch: str) -> int:
    """Display cells a character occupies in a terminal (for tab alignment)."""
    if ch == "\n":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1


def _expand_tabs(text: str) -> str:
    """Expand tabs to spaces at terminal tab stops, column-aware over wide chars.

    Python's ``str.expandtabs`` counts codepoints, so CJK text misaligns; this
    counts display cells (wide/fullwidth = 2) so tabs keep terminal alignment
    once Discord's client renders the message.
    """
    out: list[str] = []
    col = 0
    for ch in text:
        if ch == "\t":
            pad = _TABSTOP - (col % _TABSTOP)
            out.append(" " * pad)
            col += pad
            continue
        out.append(ch)
        if ch == "\n":
            col = 0
        else:
            col += _char_cell_width(ch)
    return "".join(out)


def sanitize_terminal_text(text: str) -> str:
    """Make Pane text render predictably in Discord (idempotent).

    - ``\r\n`` → ``\n``; stray ``\r`` dropped (mid-line redraw artifacts).
    - Tabs expanded to spaces at terminal tab stops (wide-char aware).
    - C0/C1 control chars and invisible zero-width / bidi markers stripped.

    Runs once when a snapshot is absorbed, so session lines, coalescing keys,
    and the 2000-char segmentation all see the final display text.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "")
    text = _INVISIBLE_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return _expand_tabs(text)


def _row_cell_cols(row: str) -> list[int]:
    """Display-cell column of every character (CJK wide chars occupy 2 cells)."""
    cols: list[int] = []
    col = 0
    for ch in row:
        cols.append(col)
        col += _char_cell_width(ch)
    return cols


def _box_anchors(data_rows: list[str]) -> list[int]:
    """Column anchor display-columns: cluster the │ positions across rows.

    Anchors are display cells, not codepoint indexes — the terminal aligns
    pipes by cell, so CJK content makes codepoint positions drift per row
    while the cell columns stay identical (e.g. [0, 6, 75, 116] for all rows
    while raw indexes are 54/60/60…).
    """
    positions: list[int] = []
    for row in data_rows:
        cols = _row_cell_cols(row)
        positions.extend(cols[i] for i, ch in enumerate(row) if ch == "│")
    if not positions:
        return []
    positions.sort()
    clusters: list[list[int]] = []
    for p in positions:
        if clusters and p - clusters[-1][-1] <= 1:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [round(sum(c) / len(c)) for c in clusters]


def _box_split(row: str, anchors: list[int]) -> list[str]:
    """Slice one data row into cells at the display-column anchors."""
    cols = _row_cell_cols(row)
    cells: list[str] = []
    for idx, a in enumerate(anchors):
        end = anchors[idx + 1] if idx + 1 < len(anchors) else None
        buf = [
            ch
            for i, ch in enumerate(row)
            if a < cols[i] and (end is None or cols[i] < end)
        ]
        # strip stray edge pipes too: rows that drift off the anchor grid would
        # otherwise smuggle a │ into the cell text.
        cells.append("".join(buf).strip(" │"))
    return cells


def _box_merge(cells_rows: list[list[str]]) -> list[list[str]]:
    """Fold continuation rows (empty first cell) into the previous logical row.

    A wrapped table cell in the terminal becomes a full │…│…│ physical row with
    an empty first column; Discord table cells cannot hold newlines, so we join
    those pieces with a space instead of emitting a broken extra row.
    """
    merged: list[list[str]] = []
    for row in cells_rows:
        if merged and not row[0]:
            prev = merged[-1]
            for i, cell in enumerate(row):
                if i < len(prev) and cell:
                    prev[i] = (prev[i] + " " + cell).strip() if prev[i] else cell
            continue
        merged.append(list(row))
    return merged


def _table_block_to_markdown(block: list[str]) -> list[str]:
    """Rewrite a contiguous box-drawing table block as Markdown (best-effort).

    Returns the block untouched unless it has both a whole-line border row and
    at least two columns, so tree output and stray box glyphs pass through.
    """
    if not any(_BOX_EDGE_RE.match(line) for line in block):
        return block
    data_rows = [line for line in block if _BOX_DATA_RE.match(line)]
    if not data_rows:
        return block
    anchors = _box_anchors(data_rows)
    # need at least 3 pipes (= 2 columns); a single-column box banner is not a
    # table and passes through untouched.
    if len(anchors) < 3:
        return block
    rows = _box_merge([_box_split(row, anchors) for row in data_rows])
    # Drop all-empty edge columns: every data row ends with a closing │ that
    # adds a spurious trailing cell. Keep the header's empty first cell when
    # data rows fill it (option tables), but drop a column empty in every row.
    while len(rows[0]) > 1 and all(r[-1] == "" for r in rows):
        for r in rows:
            r.pop()
    while len(rows[0]) > 1 and all(r[0] == "" for r in rows):
        for r in rows:
            r.pop(0)
    header = rows[0]
    out = [
        "| " + " | ".join(c.replace("|", "\\|") for c in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in rows[1:]:
        cells = (row + [""] * len(header))[: len(header)]
        out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return out


def _adapt_rendered_lines(lines: list[str]) -> list[str]:
    """Rendering-side adaptations (idempotent): box tables → Markdown, pure
    horizontal rules → ASCII dashes. Session lines keep the original text so
    dedup/coalescing keys stay stable; this only shapes the Discord output."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # pure horizontal rule first: it is a subset of the table-edge charset,
        # so it must win before the table-block branch absorbs it.
        if _BOX_DECOR_RE.match(line):
            out.append("-" * min(len(line), 12))
            i += 1
            continue
        if _BOX_EDGE_RE.match(line) or _BOX_DATA_RE.match(line):
            j = i
            while j < n and (_BOX_EDGE_RE.match(lines[j]) or _BOX_DATA_RE.match(lines[j])):
                j += 1
            out.extend(_table_block_to_markdown(lines[i:j]))
            i = j
            continue
        out.append(line)
        i += 1
    return out


@dataclass
class _TurnState:
    message_id: int | None = None
    last_edit: float = 0.0
    pending: bool = False
    text: str = ""
    status: str = "unknown"
    remote_id: str = ""
    flush_task: asyncio.Task[None] | None = None
    typing_task: asyncio.Task[None] | None = None
    needs_new_message: bool = False
    anchor_message: Any | None = None
    anchor_message_id: int | None = None
    baseline_text: str | None = None
    # Append-only lines for this user turn (full history, not just the pane tip)
    session_lines: list[str] = field(default_factory=list)
    live_start: int = 0
    segment_index: int = 0
    last_window: list[str] = field(default_factory=list)
    last_snapshot: str = ""
    last_rendered: str = ""
    active: bool = False  # True after begin_prompt_session until next prompt
    choice_message_id: int | None = None
    choice_fingerprint: str | None = None
    bridge_cfg: BridgeConfig | None = None


_states: dict[tuple[int, str], _TurnState] = {}


def _key(thread: Any, pane_id: str) -> tuple[int, str]:
    return (int(thread.id), pane_id)


def _get(thread: Any, pane_id: str) -> _TurnState:
    k = _key(thread, pane_id)
    state = _states.get(k)
    if state is None:
        state = _TurnState()
        _states[k] = state
    return state


def get_terminal_state(thread: Any, pane_id: str) -> _TurnState:
    return _get(thread, pane_id)


def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is not None and not task.done():
        task.cancel()


def clear_terminal_state(thread_id: int | None = None, pane_id: str | None = None) -> None:
    if thread_id is None and pane_id is None:
        for state in _states.values():
            _cancel_task(state.flush_task)
            _cancel_task(state.typing_task)
        _states.clear()
        return
    drop = [
        k
        for k in _states
        if (thread_id is None or k[0] == thread_id) and (pane_id is None or k[1] == pane_id)
    ]
    for k in drop:
        state = _states.pop(k, None)
        if state is not None:
            _cancel_task(state.flush_task)
            _cancel_task(state.typing_task)


def new_lines_from_window(session: list[str], window: list[str]) -> list[str]:
    if not window:
        return []
    if not session:
        return list(window)
    if len(window) <= len(session) and session[-len(window) :] == window:
        return []
    max_n = min(len(session), len(window))
    for n in range(max_n, 0, -1):
        if session[-n:] == window[:n]:
            return list(window[n:])
    for start in range(max(0, len(session) - len(window)), len(session)):
        chunk = session[start:]
        if chunk and window[: len(chunk)] == chunk:
            return list(window[len(chunk) :])
    last = session[-1]
    try:
        idx = len(window) - 1 - window[::-1].index(last)
    except ValueError:
        return []
    return list(window[idx + 1 :])


def window_diff_lines(prev: list[str], window: list[str]) -> list[str]:
    """Every inserted/replaced line when the sliding window advances.

    This is the primary anti-leak path: scroll, append, and in-place rewrites all
    show up as insert/replace opcodes.
    """
    if not window:
        return []
    if not prev:
        return list(window)
    if prev == window:
        return []
    matcher = difflib.SequenceMatcher(a=prev, b=window, autojunk=False)
    out: list[str] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            out.extend(window[j1:j2])
    return out


def _filter_duplicate_prefix(session: list[str], added: list[str]) -> list[str]:
    if not added:
        return []
    if not session:
        return list(added)
    max_n = min(len(session), len(added))
    for n in range(max_n, 0, -1):
        if session[-n:] == added[:n]:
            return list(added[n:])
    return list(added)


def _strip_spinner_glyphs(line: str) -> str:
    text = _BRAILLE_RE.sub("", line)
    text = _SPINNER_GLYPH_RE.sub("", text)
    return text.strip()


def status_template_key(line: str) -> str | None:
    """Stable key for live-updating TUI status lines; ``None`` if ordinary text.

    Spinners, phase verbs (Running/Waiting/…), token counters, durations, and
    other numbers are normalized so successive frames of the same status slot
    share a key and can replace in place — including multi-line status blocks.
    Generalized over raw glyphs and qualifiers: progress bars collapse to
    ``<pct>``, trailing parentheticals to ``<q>``, and agent rows to
    ``<agent:name>``.
    """
    agent = _AGENT_ROW_RE.match(line)
    if agent:
        return f"<agent:{agent.group(1).lower()}>"
    text = _strip_spinner_glyphs(line)
    if not text:
        return None
    text = _THINK_STATE_RE.sub("thinking", text)
    text = _BAR_RE.sub("", text)
    # A trailing parenthetical is transient status (time · tokens · thinking);
    # collapse it so "(2m 11s · ↓ 3.6k tokens)", "(2m 15s · thinking)",
    # "(thinking some more)" and "(thought for 30s)" share one slot. Only applied
    # to lines that already look like status (leading glyph, "·" separator, or a
    # phase verb) so real content ending in parens is never collapsed.
    if _SPINNER_GLYPH_RE.match(line) or "·" in line or _PHASE_RE.search(text):
        text = _QUALIFIER_RE.sub(" <Q>", text)
    normalized = _PHASE_RE.sub("<PHASE>", text)
    normalized = _TOKEN_COUNT_RE.sub("<TOKENS>", normalized)
    normalized = _DURATION_RE.sub("<TIME>", normalized)
    normalized = _PERCENT_RE.sub("<PCT>", normalized)
    normalized = _NUMBER_RE.sub("<NUM>", normalized)
    normalized = _ELLIPSIS_RE.sub("…", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    strong = ("<phase>", "<tokens>", "<time>", "<pct>", "<q>")
    has_strong = any(token in normalized for token in strong)
    # pi's WorkingStatusIndicator renders ``{spinner} {message}`` where message
    # is free-form via setWorkingMessage (Working/Booping/Thinking/... — any
    # word, not enumerable). A leading spinner glyph + short residual text with
    # no other strong marker is exactly that status row: give every frame of
    # every word one shared slot so animation frames replace instead of append.
    # Long lines (>40) are real content even if they start with a glyph; lines
    # carrying tokens/phases/percent keep their informative key.
    if (
        not has_strong
        and _SPINNER_GLYPH_RE.match(line)
        and len(text) <= 40
        and "\n" not in text
    ):
        return "<spinner-status>"
    # Bare numbers alone are too greedy (e.g. ``line-0-xxx`` / ``line-1-xxx``).
    # Allow only short ratio-style counters: ``3/10``, ``2 of 5``.
    if not has_strong:
        if "<num>" not in normalized or len(normalized) > 40:
            return None
        if not re.search(r"<num>\s*/\s*<num>|<num>\s+of\s+<num>", normalized):
            return None
    if not re.search(r"[a-z\u4e00-\u9fff]", normalized) and "<pct>" not in normalized:
        return None
    return normalized


def _is_prefix_rewrite(previous: str, current: str) -> bool:
    """True when one line is a progressive rewrite of the other (typing echo)."""
    if not previous or not current or previous == current:
        return False
    return current.startswith(previous) or previous.startswith(current)


def _split_table_blocks(added: list[str]) -> list[list[str]]:
    """Group added lines into single-line items and complete box-table blocks.

    A complete block starts with a top border (``┌``), ends with a bottom
    border (``└``), and contains only box-drawing lines. Anything else stays
    as individual lines so the existing per-line merge rules apply unchanged.
    """
    items: list[list[str]] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        chunk: list[str] = []
        for line in run:
            chunk.append(line)
            if _TABLE_BOT_RE.match(line):
                items.append(chunk)
                chunk = []
        if chunk:
            items.extend([ln] for ln in chunk)  # incomplete tail → plain lines
        run.clear()

    for line in added:
        if _BOX_EDGE_RE.match(line) or _BOX_DATA_RE.match(line) or _BOX_DECOR_RE.match(line):
            run.append(line)
        else:
            flush()
            items.append([line])
    flush()

    out: list[list[str]] = []
    for item in items:
        if len(item) > 1 and _table_block_key(item) is not None:
            out.append(item)
        else:
            out.extend([ln] for ln in item)
    return out


def _table_block_key(lines: list[str]) -> str | None:
    """Identity of a complete box-table block = its top border line.

    Tables repaint with the same top border while their body grows/shrinks, so
    the top border is a stable template key; width changes (different border)
    produce a new key and append conservatively.
    """
    if not lines or not _TABLE_TOP_RE.match(lines[0]) or not _TABLE_BOT_RE.match(lines[-1]):
        return None
    for line in lines[1:]:
        if not (_BOX_DATA_RE.match(line) or _BOX_EDGE_RE.match(line)):
            return None
    return lines[0]


def _merge_table_block(session: list[str], block: list[str]) -> int:
    """Replace a previously displayed table with the same top border, or append.

    A table that grew/shrunk by rows (new tasks, collapsed groups) otherwise
    appends its extra rows *below the bottom border*, splitting the block.
    Returns mutation count (1 for an in-place block update, len(block) for an
    append, 0 when the identical block is already displayed).
    """
    key = _table_block_key(block)
    assert key is not None
    window = max(_UI_SLOT_WINDOW, len(block) + 8)
    start = max(0, len(session) - window)
    for index in range(len(session) - 1, start - 1, -1):
        if session[index] != key:
            continue
        end = index + 1
        while end < len(session) and (
            _BOX_DATA_RE.match(session[end]) or _BOX_EDGE_RE.match(session[end])
        ):
            end += 1
        old = session[index:end]
        if old == block:
            return 0
        session[index:end] = block
        return 1
    session.extend(block)
    return len(block)


def _slot_key(line: str) -> str | None:
    """Any template key: live status lines first, then pinned TUI rows."""
    return status_template_key(line) or fixed_ui_key(line)


def _replace_in_trailing_status_block(
    session: list[str], line: str, key: str, window: int = STATUS_SLOT_WINDOW
) -> bool | None:
    """Update a matching live status slot in the recent tail.

    Scans the last ``window`` lines, skipping ordinary lines, so a status frame
    still replaces in place even when repainted chrome (status bar / task list)
    sits between the spinner slot and the tail of the history. Only
    slot-keyed lines are ever touched; real content is left alone.

    Returns ``True`` if replaced, ``False`` if matched but unchanged, ``None``
    if no slot matched (caller should append).
    """
    start = max(0, len(session) - window)
    for index in range(len(session) - 1, start - 1, -1):
        prev_key = _slot_key(session[index])
        if prev_key is None:
            continue
        if prev_key != key:
            continue
        if session[index] == line:
            return False
        session[index] = line
        return True
    return None


def merge_added_lines(session: list[str], added: list[str]) -> int:
    """Merge ``added`` into ``session`` with status-slot / prefix coalesce.

    Live TUI status lines (spinners, token counters, ``Waiting Nm Ns for shell``,
    etc.) update in place by template key so multi-line status blocks stay one
    row per slot. Typing echoes still coalesce via prefix rewrite.

    Returns the number of mutations (append or in-place replace). A pure replace
    still counts so Discord can refresh the live bubble.
    """
    mutations = 0
    for item in _split_table_blocks(added):
        if len(item) > 1:
            mutations += _merge_table_block(session, item)
            continue
        line = item[0]
        if _is_tui_footer_chrome(line):
            # TUI footer rows (spinner Working, pi stats, opencode status bar,
            # claude prompt band) are fixed screen chrome, never reply content.
            continue
        if not session:
            session.append(line)
            mutations += 1
            continue
        key = status_template_key(line)
        window = STATUS_SLOT_WINDOW
        if key is None:
            key = fixed_ui_key(line)
            window = _UI_SLOT_WINDOW
        if key is not None:
            replaced = _replace_in_trailing_status_block(session, line, key, window)
            if replaced is True:
                mutations += 1
                continue
            if replaced is False:
                continue
            session.append(line)
            mutations += 1
            continue
        last = session[-1]
        if _is_prefix_rewrite(last, line):
            session[-1] = line
            mutations += 1
            continue
        if _is_chrome_reinsertion(session, line):
            continue
        session.append(line)
        mutations += 1
    return mutations


def _is_chrome_reinsertion(session: list[str], line: str) -> bool:
    """True when a repainted TUI chrome line duplicates a recent session line.

    Full-window repaints re-insert static chrome (task lists, status bars, agent
    rows) verbatim; they are already displayed, so another copy only floods the
    bubble. Real content is never chrome-prefixed and is never dropped here.
    """
    if not _CHROME_LINE_RE.match(line):
        return False
    return line in session[-STATUS_SLOT_WINDOW:]


def _delta_from_baseline(base: list[str], window: list[str]) -> list[str]:
    """New lines after a prompt baseline, tolerant of pane.read sliding windows."""
    if not window:
        return []
    if not base:
        return list(window)
    if window == base:
        return []
    if len(window) >= len(base) and window[: len(base)] == base:
        return list(window[len(base) :])
    max_n = min(len(base), len(window))
    for n in range(max_n, 0, -1):
        if base[-n:] == window[:n]:
            return list(window[n:])
    last = base[-1]
    try:
        idx = len(window) - 1 - window[::-1].index(last)
    except ValueError:
        return []
    return list(window[idx + 1 :])


def turn_lines_since_baseline(baseline: str | None, current: str) -> list[str]:
    """Best-effort first seed of post-prompt lines (used only when session is empty)."""
    window = str(current or "").splitlines()
    if not window:
        return []
    base = str(baseline or "").splitlines()
    if not base:
        return list(window)
    if window == base:
        return []
    delta = _delta_from_baseline(base, window)
    if delta:
        return delta
    for line in reversed(base):
        if line == "":
            continue
        try:
            idx = len(window) - 1 - window[::-1].index(line)
        except ValueError:
            continue
        after = list(window[idx + 1 :])
        if after:
            return after
    return list(window)


def absorb_gateway_window(state: _TurnState, snapshot: str) -> int:
    """Append newly visible Pane lines into this turn's history.

    Prefer difflib window diffs so scrolled / rewritten tips cannot silently vanish.
    Never replaces earlier session_lines with only the current tip.
    """
    snap = sanitize_terminal_text(str(snapshot or ""))
    window = snap.splitlines()
    if snap == state.last_snapshot and window == state.last_window:
        return 0
    prev = list(state.last_window)
    state.last_window = list(window)
    state.last_snapshot = snap

    added: list[str] = []
    if not state.session_lines:
        base = str(state.baseline_text or "").splitlines()
        baseline_s = str(state.baseline_text or "")
        if window == base or snap == baseline_s:
            return 0
        added = _delta_from_baseline(base, window)
        if not added and prev:
            added = window_diff_lines(prev, window)
        if not added:
            # First post-prompt snapshot lost the baseline — seed once.
            added = turn_lines_since_baseline(state.baseline_text, snap)
    else:
        added = window_diff_lines(prev, window)
        if not added:
            added = new_lines_from_window(state.session_lines, window)
        if not added:
            tip = turn_lines_since_baseline(state.baseline_text, snap)
            if tip and len(tip) > len(state.session_lines) and tip[: len(state.session_lines)] == state.session_lines:
                added = tip[len(state.session_lines) :]
            elif tip:
                added = new_lines_from_window(state.session_lines, tip)

    added = _filter_duplicate_prefix(state.session_lines, added)
    if not added:
        return 0
    return merge_added_lines(state.session_lines, added)


def session_body(baseline: str | None, current: str, max_lines: int) -> str:
    lines = turn_lines_since_baseline(baseline, current)
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])


def render_chat_reply(
    lines: list[str],
    *,
    status: str,
    continued: bool,
    live: bool,
) -> str:
    """Plain chatbot-style body — no code fences, no heavy chrome."""
    lines = _adapt_rendered_lines(list(lines))
    body = "\n".join(lines).rstrip()
    if continued and body:
        body = f"（续）\n{body}"
    if not body:
        # Never replace an on-screen 「思考中…」 with a bare ellipsis via edit.
        body = PLACEHOLDER if live else "…"
    elif live and str(status).lower() in {"working", "unknown", ""}:
        if not body.endswith("…"):
            body = f"{body}\n…"
    if len(body) > MSG_LIMIT:
        body = "…" + body[-(MSG_LIMIT - 1) :]
    return body


def _fit_prefix(lines: list[str], *, continued: bool) -> int:
    if not lines:
        return 0
    for count in range(len(lines), 0, -1):
        text = render_chat_reply(lines[:count], status="idle", continued=continued, live=False)
        if len(text) <= SOFT_LIMIT:
            return count
    return 1


async def _trigger_typing(thread: Any) -> None:
    """Show Discord's native 「正在输入…」 indicator (the channel typing dots)."""
    trigger = getattr(thread, "trigger_typing", None)
    if not callable(trigger):
        return
    try:
        await trigger()
    except Exception:  # noqa: BLE001
        log.debug("trigger_typing failed", exc_info=True)


async def _typing_keepalive(thread: Any, state: _TurnState) -> None:
    """Refresh typing while the turn is active and still working."""
    try:
        while state.active:
            status = str(state.status).lower()
            if status not in {"working", "unknown", ""}:
                break
            await _trigger_typing(thread)
            await asyncio.sleep(TYPING_REFRESH)
    except asyncio.CancelledError:
        raise
    finally:
        if state.typing_task is asyncio.current_task():
            state.typing_task = None


def _start_typing(thread: Any, state: _TurnState) -> None:
    _cancel_task(state.typing_task)
    state.typing_task = None
    try:
        state.typing_task = asyncio.create_task(
            _typing_keepalive(thread, state),
            name=f"chat-typing-{getattr(thread, 'id', '?')}",
        )
    except RuntimeError:
        # No running loop in some unit tests — typing is best-effort.
        pass


def _stop_typing(state: _TurnState) -> None:
    _cancel_task(state.typing_task)
    state.typing_task = None


async def begin_prompt_session(
    thread: Any,
    pane_id: str,
    prompt_message: Any,
    *,
    remote_id: str = "",
) -> None:
    """Start a chat turn: reply under the user message with a streaming placeholder."""
    from src.bot.config import BridgeConfig

    state = _get(thread, pane_id)

    # Flush any unflushed previous-turn lines to Discord before resetting — no silent drop.
    if state.active and state.session_lines[state.live_start :]:
        cfg = state.bridge_cfg or BridgeConfig()
        state.pending = True
        try:
            await _flush(thread, pane_id, cfg, state, clock=time.time)
        except Exception:  # noqa: BLE001
            log.exception("failed flushing previous turn before new prompt %s", pane_id)

    _cancel_task(state.flush_task)
    state.flush_task = None
    _stop_typing(state)
    state.pending = False

    # Previous live bubble will not receive more edits.
    old_id = state.message_id
    old_rendered = state.last_rendered
    if old_id is not None and (
        old_rendered == PLACEHOLDER or old_rendered.endswith("\n…") or old_rendered.endswith("…")
    ):
        try:
            old = await thread.fetch_message(old_id)
            note = "（已结束，见下方新回复）" if old_rendered != PLACEHOLDER else "（已取消）"
            if str(getattr(old, "content", "") or "") in {PLACEHOLDER, old_rendered}:
                await old.edit(content=note)
        except Exception:  # noqa: BLE001
            log.debug("failed marking previous chat bubble %s", old_id, exc_info=True)

    state.anchor_message = prompt_message
    state.anchor_message_id = int(getattr(prompt_message, "id", 0) or 0) or None
    state.baseline_text = state.text
    state.session_lines = []
    state.live_start = 0
    state.segment_index = 0
    state.last_window = str(state.text or "").splitlines()
    state.last_snapshot = str(state.text or "")
    state.last_rendered = PLACEHOLDER
    state.active = True
    state.status = "working"
    state.choice_fingerprint = None
    if remote_id:
        state.remote_id = remote_id

    await _trigger_typing(thread)
    _start_typing(thread, state)
    try:
        if hasattr(prompt_message, "reply"):
            msg = await prompt_message.reply(PLACEHOLDER)
        else:
            msg = await thread.send(PLACEHOLDER)
    except Exception:  # noqa: BLE001
        log.exception("failed posting chat placeholder")
        state.message_id = None
        state.needs_new_message = True
        return
    state.message_id = int(msg.id)
    state.needs_new_message = False
    state.last_edit = 0.0


async def fail_prompt_session(thread: Any, pane_id: str, note: str) -> None:
    """Replace the live 「思考中…」 bubble with an error and end the turn."""
    state = _get(thread, pane_id)
    _cancel_task(state.flush_task)
    state.flush_task = None
    _stop_typing(state)
    state.pending = False
    state.active = False
    mid = state.message_id
    if mid is None:
        return
    content = str(note or "").strip() or "❌ 发送失败"
    if len(content) > MSG_LIMIT:
        content = content[: MSG_LIMIT - 1] + "…"
    try:
        msg = await thread.fetch_message(mid)
        await msg.edit(content=content)
        state.last_rendered = content
    except Exception:  # noqa: BLE001
        log.debug("failed editing failed-prompt bubble %s", mid, exc_info=True)


async def _send_new(thread: Any, state: _TurnState, content: str) -> Any:
    msg: Any = None
    anchor = state.anchor_message
    if anchor is not None and hasattr(anchor, "reply"):
        try:
            msg = await anchor.reply(content)
        except Exception:  # noqa: BLE001
            log.debug("anchor.reply failed; falling back to thread.send", exc_info=True)
            msg = None
    if msg is None:
        msg = await thread.send(content)
    state.message_id = int(msg.id)
    state.needs_new_message = False
    return msg


async def _edit_live(thread: Any, state: _TurnState, content: str) -> Any:
    if state.message_id is None or state.needs_new_message:
        return await _send_new(thread, state, content)
    try:
        msg = await thread.fetch_message(state.message_id)
        await msg.edit(content=content)
        return msg
    except discord.NotFound:
        state.message_id = None
        state.needs_new_message = True
        return await _send_new(thread, state, content)


def _schedule_flush(
    thread: Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    state: _TurnState,
    delay: float,
    clock: Callable[[], float],
) -> None:
    state.pending = True
    if state.flush_task is None or state.flush_task.done():
        state.flush_task = asyncio.create_task(
            _flush_after(thread, pane_id, bridge_cfg, state, delay, clock),
            name=f"chat-stream-{getattr(thread, 'id', '?')}-{pane_id}",
        )


async def apply_terminal_view(
    thread: Any,
    pane_id: str,
    text: str,
    status: str,
    bridge_cfg: BridgeConfig,
    *,
    remote_id: str = "",
    force: bool = False,
    message_id: int | None = None,
    clock: Callable[[], float] = time.time,
) -> int | None:
    """Stream Pane output into the active chat reply (no-op without an active turn)."""
    state = _get(thread, pane_id)
    state.bridge_cfg = bridge_cfg
    if remote_id:
        state.remote_id = remote_id
    state.text = sanitize_terminal_text(str(text or ""))
    state.status = status or "unknown"
    status_l = str(state.status).lower()
    if state.active and status_l not in {"working", "unknown", ""}:
        _stop_typing(state)
    elif state.active and status_l in {"working", "unknown", ""} and (
        state.typing_task is None or state.typing_task.done()
    ):
        _start_typing(thread, state)

    # Chatbot mode: ignore background Pane noise until the user sends a message.
    if not state.active and not force:
        state.last_window = str(text or "").splitlines()
        state.last_snapshot = str(text or "")
        state.session_lines = []
        return state.message_id

    if message_id is not None and not state.needs_new_message and state.message_id is None:
        state.message_id = message_id

    changed = absorb_gateway_window(state, text)
    if not changed and not force:
        return state.message_id
    if not state.session_lines and not force:
        return state.message_id

    now = clock()
    cooldown = max(float(bridge_cfg.terminal.edit_cooldown), MIN_EDIT_INTERVAL)
    if (
        not force
        and state.message_id is not None
        and not state.needs_new_message
        and (now - state.last_edit) < cooldown
    ):
        delay = cooldown - (now - state.last_edit)
        _schedule_flush(thread, pane_id, bridge_cfg, state, delay, clock)
        return state.message_id

    return await _flush(thread, pane_id, bridge_cfg, state, clock=clock)


async def _flush_after(
    thread: Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    state: _TurnState,
    delay: float,
    clock: Callable[[], float],
) -> None:
    try:
        await asyncio.sleep(max(0.0, delay))
        if state.pending:
            await flush_terminal_view(thread, pane_id, bridge_cfg, clock=clock)
    except asyncio.CancelledError:
        raise
    finally:
        if state.flush_task is asyncio.current_task():
            state.flush_task = None


async def flush_terminal_view(
    thread: Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    *,
    clock: Callable[[], float] = time.time,
) -> int | None:
    state = _get(thread, pane_id)
    if not state.pending and not state.session_lines[state.live_start :]:
        return state.message_id
    return await _flush(thread, pane_id, bridge_cfg, state, clock=clock)


async def _flush(
    thread: Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    state: _TurnState,
    *,
    clock: Callable[[], float],
) -> int | None:
    state.pending = False
    if not state.active:
        return state.message_id

    if not state.session_lines and state.text:
        absorb_gateway_window(state, state.text)

    if not state.session_lines[state.live_start :]:
        return state.message_id

    try:
        while True:
            live_lines = state.session_lines[state.live_start :]
            if not live_lines:
                return state.message_id
            continued = state.segment_index > 0
            content = render_chat_reply(
                live_lines,
                status=state.status,
                continued=continued,
                live=True,
            )
            if content == state.last_rendered and not state.needs_new_message:
                state.last_edit = clock()
                return state.message_id

            if len(content) <= SOFT_LIMIT or len(live_lines) <= 1:
                await _edit_live(thread, state, content)
                state.last_rendered = content
                state.last_edit = clock()
                return state.message_id

            fit = _fit_prefix(live_lines, continued=continued)
            if fit >= len(live_lines):
                fit = max(1, len(live_lines) - 1)
            sealed = render_chat_reply(
                live_lines[:fit],
                status=state.status,
                continued=continued,
                live=False,
            )
            await _edit_live(thread, state, sealed)
            state.last_rendered = sealed
            state.live_start += fit
            state.segment_index += 1
            state.message_id = None
            state.needs_new_message = True
            state.last_edit = clock()
    except discord.HTTPException as exc:
        log.warning(
            "chat stream update failed %s/%s (will retry): %s",
            state.remote_id,
            pane_id,
            exc,
        )
        # Keep unflushed live lines; retry so Discord does not permanently miss them.
        _schedule_flush(thread, pane_id, bridge_cfg, state, FLUSH_RETRY_DELAY, clock)
        return state.message_id
    except Exception:  # noqa: BLE001
        log.exception("chat stream update crashed %s/%s (will retry)", state.remote_id, pane_id)
        _schedule_flush(thread, pane_id, bridge_cfg, state, FLUSH_RETRY_DELAY, clock)
        return state.message_id
