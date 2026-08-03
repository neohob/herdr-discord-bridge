"""Chat-mode stream tests: one user message → one edited bot reply."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.config import BridgeConfig, TerminalConfig
from src.bot.terminal_view import (
    absorb_gateway_window,
    apply_terminal_view,
    begin_prompt_session,
    clear_terminal_state,
    flush_terminal_view,
    get_terminal_state,
    merge_added_lines,
    new_lines_from_window,
    render_chat_reply,
    sanitize_terminal_text,
    status_template_key,
    window_diff_lines,
)
from src.bot.terminal_view import _TurnState


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture(autouse=True)
def _reset():
    clear_terminal_state()
    yield
    clear_terminal_state()


def _bridge(*, edit_cooldown: float = 1.0) -> BridgeConfig:
    return BridgeConfig(terminal=TerminalConfig(edit_cooldown=edit_cooldown))


def _text(call) -> str:
    if not call:
        return ""
    args, kwargs = call
    return str(args[0] if args else kwargs.get("content") or "")


def test_new_lines_no_dupes():
    assert new_lines_from_window(["a", "b"], ["b", "c"]) == ["c"]
    assert new_lines_from_window(["a", "b", "c"], ["a", "b", "c"]) == []


def test_window_diff_captures_scroll_and_inplace():
    assert window_diff_lines(["a", "b", "c"], ["b", "c", "d"]) == ["d"]
    # In-place rewrite of the last line.
    assert window_diff_lines(["a", "b"], ["a", "b2"]) == ["b2"]
    # Full screen replace must not return empty.
    assert window_diff_lines(["old1", "old2"], ["new1", "new2"]) == ["new1", "new2"]


def test_merge_coalesces_spinner_status_frames():
    session: list[str] = []
    frames = [
        " ⠀⠞ Running  6.7k tokens",
        " ⠘⠆ Running  6.7k tokens",
        " ⠠⠜ Running  6.7k tokens",
        " ⠘⠣ Running  6.7k tokens",
        " ⠰⠳ Working  26.39k tokens",
    ]
    merge_added_lines(session, frames)
    assert len(session) == 1
    assert "Working" in session[0]
    assert "26.39k" in session[0]


def test_merge_coalesces_claude_whirlpool_glyph_frames():
    """Claude Code whirlpool spinners cycle ✽✢✻✶✳· on every frame; the glyph
    must not fork the template key or every frame appends a new line."""
    session: list[str] = []
    frames = [
        "✽ Whirlpooling… (8s · thinking)",
        "✢ Whirlpooling… (7s · thinking)",
        "✻ Whirlpooling… (9s · thinking)",
        "✶ Whirlpooling… (13s · thinking)",
        "✳ Whirlpooling… (17s · still thinking)",
        "✽ Whirlpooling… (21s · still thinking)",
    ]
    merge_added_lines(session, frames)
    assert len(session) == 1
    assert "Whirlpooling" in session[0]
    assert "21s" in session[0]


def test_merge_coalesces_phase_transitions_on_same_slot():
    """Whirlpooling→Working→Implementing morphs are the same spinner slot; the
    subject-less ones replace, while a subject-bearing status opens its own slot."""
    session: list[str] = []
    merge_added_lines(session, ["✽ Whirlpooling… (8s · thinking)"])
    merge_added_lines(session, ["✢ Working… (10s · thinking)"])
    merge_added_lines(session, ["✳ Implementing JsonRpcDispatcher… (41s · ↓ 3.4k tokens)"])
    merge_added_lines(session, ["✻ Implementing JsonRpcDispatcher… (44s · ↓ 3.7k tokens)"])
    assert len(session) == 2
    assert "Working" in session[0]
    assert "Implementing JsonRpcDispatcher" in session[1]
    assert "44s" in session[1]


def test_status_slot_replaces_behind_repainted_chrome():
    """A static status bar / task list repaints below the spinner line; the
    spinner slot must still be found in the recent tail and replaced in place."""
    session: list[str] = [
        "  ◻ Task 2: JsonRpcDispatcher",
        "✽ Whirlpooling… (8s · thinking)",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt",
    ]
    merge_added_lines(session, ["✢ Whirlpooling… (9s · thinking)"])
    assert len(session) == 3
    assert "9s" in session[1]
    assert "  ◻ Task 2" in session[0]
    assert session[2].startswith("  ⏵⏵")


def test_chrome_reinsertion_is_deduplicated():
    """Full-window repaints re-insert task lists / status bars verbatim; those
    copies are already displayed and must not append again."""
    session: list[str] = [
        "  ◻ Task 2: JsonRpcDispatcher",
        "   … +4 pending, 1 completed",
        "✽ Whirlpooling… (8s · thinking)",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt",
    ]
    # Repaint: same chrome, only the spinner frame changed.
    merge_added_lines(session, [
        "  ◻ Task 2: JsonRpcDispatcher",
        "   … +4 pending, 1 completed",
        "✢ Whirlpooling… (9s · thinking)",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt",
    ])
    assert len(session) == 4
    assert "9s" in session[2]
    assert session.count("  ◻ Task 2: JsonRpcDispatcher") == 1
    assert session.count("  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt") == 1


def test_chrome_dedup_never_drops_new_chrome_or_content():
    """A chrome-prefixed line that is NOT already in the tail still appends; so
    do ordinary content lines even if chrome surrounds them."""
    session = ["a", "b"]
    first = "◯ task2-impl 你在实现 Task 2 (first appearance)"
    merge_added_lines(session, [first])
    assert session[-1] == first
    # A *different* chrome line, plus ordinary content, still appends.
    second = "◯ task3-impl 你在实现 Task 3 (brand new row)"
    merge_added_lines(session, ["real output line", second])
    assert session[-2:] == ["real output line", second]
    # Re-inserting the same chrome line again is a repaint → deduplicated.
    before = len(session)
    merge_added_lines(session, [first])
    assert len(session) == before


def test_merge_coalesces_progressive_typing_echo():
    session: list[str] = []
    echoes = [
        "  → 有个",
        "  → 有个问",
        "  → 有个问题",
        "  → 有个问题，怎么调用skills呢？",
        "  → 有个问题，怎么调用skills呢？因为 / 都是herdr的命令，那么s",
        "  → 有个问题，怎么调用skills呢？因为 / 都是herdr的命令，",
        "  → 有个问题，怎么调用skills呢？因为 / 都是herdr的命令，那么agent的很多命令也是 / 开头的",
        "  有个问题，怎么调用skills呢？因为 / 都是herdr的命令，那么agent的很多命令也是 / 开头的",
    ]
    merge_added_lines(session, echoes)
    # Prefix-related rewrites collapse; the final non-arrow line is a different stem → append.
    assert any("开头的" in line for line in session)
    assert len(session) <= 2
    assert session[-1].strip().startswith("有个问题")


def test_merge_keeps_distinct_non_prefix_lines():
    session = ["early", "middle"]
    merge_added_lines(session, ["late"])
    assert session == ["early", "middle", "late"]
    # b → b2 is a prefix rewrite and should replace, not append a third copy of history.
    session2 = ["a", "b"]
    merge_added_lines(session2, ["b2"])
    assert session2 == ["a", "b2"]


def test_absorb_coalesces_spinner_across_snapshots():
    state = _TurnState(baseline_text="base", last_window=["base"], last_snapshot="base")
    absorb_gateway_window(state, "base\n ⠀⠞ Running  6.7k tokens")
    absorb_gateway_window(state, "base\n ⠘⠆ Running  6.7k tokens")
    absorb_gateway_window(state, "base\n ⠰⠳ Working  26.39k tokens")
    assert len(state.session_lines) == 1
    assert "Working" in state.session_lines[0]


def test_absorb_coalesces_typing_echo_across_snapshots():
    state = _TurnState(baseline_text="base", last_window=["base"], last_snapshot="base")
    absorb_gateway_window(state, "base\n  → 有个")
    absorb_gateway_window(state, "base\n  → 有个问")
    absorb_gateway_window(state, "base\n  → 有个问题，怎么调用skills呢？")
    assert state.session_lines == ["  → 有个问题，怎么调用skills呢？"]


def test_status_template_key_groups_waiting_shell_countdown():
    a = status_template_key("    Waiting 9m 16s for shell")
    b = status_template_key("    Waiting 9m 15s for shell")
    c = status_template_key(" ⠰⠳ Waiting  52.04k tokens")
    d = status_template_key(" ⠰⠰ Waiting  52.04k tokens")
    assert a is not None and a == b
    assert c is not None and c == d
    assert a != c


def test_status_template_key_normalizes_think_qualifier():
    a = status_template_key("✽ Whirlpooling… (8s · thinking)")
    b = status_template_key("✢ Whirlpooling… (17s · still thinking)")
    c = status_template_key("✶ Whirlpooling… (22s · thinking more)")
    assert a is not None and a == b == c


def test_qualifier_variants_share_one_slot():
    """The trailing parenthetical is transient status; (2m 11s · ↓ 3.6k tokens),
    (2m 15s · thinking), (thinking some more) and (thought for 30s) must all
    collapse to one slot so an Inferring phase never forks into 4 lines."""
    session: list[str] = []
    merge_added_lines(session, [
        "✢ Inferring… (2m 11s · ↓ 3.6k tokens)",
        "✻ Inferring… (2m 15s · thinking)",
        "✽ Inferring… (thinking some more)",
        "✳ Inferring… (thought for 30s)",
    ])
    assert len(session) == 1
    assert "thought for 30s" in session[0]


def test_real_content_trailing_parens_never_collapse():
    """Ordinary prose ending in parens is not a status line; a real line
    "fix the bug (issue #12)" must stay distinct from "(issue #13)"."""
    a = status_template_key("fix the bug (issue #12)")
    b = status_template_key("fix the bug (issue #13)")
    assert a is None and b is None


def test_progress_bar_frames_coalesce():
    """▰▰▰▱▱▱▱… 10% redraws each frame; the bar is not identity, only the
    percent is, and a percent-only line is still a status slot."""
    session: list[str] = []
    for pct in (10, 11, 13, 16, 21, 39):
        bar = ("▰" * pct) + ("▱" * (33 - pct))
        merge_added_lines(session, [f"  {bar} {pct}%"])
    assert len(session) == 1
    assert "39%" in session[0]
    assert status_template_key("  ▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱ 10%") is not None


def test_agent_row_keys_on_name_not_truncated_desc():
    """Agent rows truncate the description as the timer widens (9s → 1m 52s);
    the slot key must be the agent name so all frames coalesce, while distinct
    agents stay distinct rows."""
    session: list[str] = []
    merge_added_lines(session, ["  ◯ task5-review  你在审查一个任务的…  9s"])
    merge_added_lines(session, ["  ◯ task5-review  你在审查一个任…  1m 52s"])
    assert len(session) == 1
    assert "1m 52s" in session[0]
    merge_added_lines(session, ["  ◯ task6-other  另一个任务…  3s"])
    assert len(session) == 2
    assert session[1].startswith("  ◯ task6-other")


def test_sanitize_expands_tabs_at_terminal_tabstops():
    assert sanitize_terminal_text("a\tb") == "a" + " " * 7 + "b"
    assert sanitize_terminal_text("\tlead") == " " * 8 + "lead"
    # tab at a tab stop pads to the next stop (col 8 → pad 8)
    assert sanitize_terminal_text("        \tmid") == " " * 16 + "mid"


def test_sanitize_is_wide_char_aware():
    # 中文 = 4 display cells (2 each) → next tab stop at 8 → 4 spaces. Naive
    # str.expandtabs would count 2 codepoints and pad 6, misaligning CJK output.
    assert sanitize_terminal_text("中文\tx") == "中文" + " " * 4 + "x"
    assert sanitize_terminal_text("中文列\t列") == "中文列" + " " * 2 + "列"


def test_sanitize_strips_control_and_invisible_chars():
    assert sanitize_terminal_text("a\x00b\x1fc") == "abc"
    assert sanitize_terminal_text("a\x7fb") == "ab"
    assert sanitize_terminal_text("a\u200bb\u200dc\ufeff") == "abc"
    assert sanitize_terminal_text("a\u202eb") == "ab"
    assert sanitize_terminal_text("a\r\nb\rc") == "a\nbc"
    assert sanitize_terminal_text("a\tb") == sanitize_terminal_text(sanitize_terminal_text("a\tb"))


def test_absorb_sanitizes_tabbed_snapshots():
    """Sanitization happens at absorb time, so session lines are already final
    display text and nothing downstream sees raw tabs or control chars."""
    state = _TurnState(baseline_text="", last_window=[], last_snapshot="")
    absorb_gateway_window(state, "✽ Whirlpooling… (8s · thinking)\n\tindented\tcode")
    assert state.session_lines == [
        "✽ Whirlpooling… (8s · thinking)",
        "        indented        code",
    ]
    absorb_gateway_window(state, "done\x00\x1fok")
    assert state.session_lines[-1] == "doneok"


def test_sanitized_tabs_keep_status_coalescing():
    """A tabbed spinner line still coalesces in place once sanitized."""
    session: list[str] = []
    merge_added_lines(session, [sanitize_terminal_text("✽ Whirlpooling\t(8s · thinking)")])
    merge_added_lines(session, [sanitize_terminal_text("✢ Whirlpooling\t(9s · thinking)")])
    assert len(session) == 1
    assert "(9s · thinking)" in session[0]


@pytest.mark.asyncio
async def test_apply_terminal_view_sanitizes_state_text():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 3
    reply = MagicMock(id=50)
    prompt = MagicMock()
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    await apply_terminal_view(thread, "p1", "a\tb\x00c", "working", cfg, clock=clock.now)
    state = get_terminal_state(thread, "p1")
    assert state.text == "a" + " " * 7 + "bc"


def _box_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """Build a properly column-aligned box-drawing table (as a terminal prints,
    i.e. aligned by display cells, with CJK wide chars counting 2)."""
    import unicodedata

    def disp(s: str) -> int:
        return sum(2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in s)

    def pad(s: str, width: int) -> str:
        return s + " " * (width - disp(s))

    all_rows = [header, *rows]
    cols = max(len(r) for r in all_rows)
    widths = [
        max(disp(r[i]) if i < len(r) else 0 for r in all_rows) for i in range(cols)
    ]

    def edge(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def data(cells: list[str]) -> str:
        cells = cells + [""] * (cols - len(cells))
        return "│" + "│".join(" " + pad(cells[i], widths[i]) + " " for i in range(cols)) + "│"

    return (
        [edge("┌", "┬", "┐"), data(header), edge("├", "┼", "┤")]
        + [data(r) for r in rows]
        + [edge("└", "┴", "┘")]
    )


def test_box_table_converts_to_markdown():
    """Claude Code option tables: box-drawing rows become a Discord-native
    Markdown table; border rows disappear; wrapped cells merge into one row."""
    lines = _box_table(
        ["", "方法", "权衡"],
        [
            ["A", "继续，门槛 = “对比基准版本（17 个文件）无新增失败”", "保持 SDD 运行；基础设施问题单独追踪"],
            ["B", "现在暂停，修复基础设施（vitest 配置以 stub /", "门槛更清晰；属于战略性调用，会延迟任务"],
            ["", "externalize bun:*）", "2+"],
        ],
    )
    out = render_chat_reply(lines, status="working", continued=False, live=False)
    assert "|  | 方法 | 权衡 |" in out
    assert "| --- | --- | --- |" in out
    assert (
        "| A | 继续，门槛 = “对比基准版本（17 个文件）无新增失败” | 保持 SDD 运行；基础设施问题单独追踪 |"
        in out
    )
    # wrapped B row folded into one logical row, joined with a space
    assert (
        "| B | 现在暂停，修复基础设施（vitest 配置以 stub / externalize bun:*） | 门槛更清晰；属于战略性调用，会延迟任务 2+ |"
        in out
    )
    # no box-drawing rows survive
    assert not any(ch in out for ch in "┌├└│")


def test_box_table_adaptation_is_idempotent():
    lines = _box_table(["", "方法"], [["A", "继续"]])
    once = render_chat_reply(lines, status="working", continued=False, live=False)
    twice = render_chat_reply(
        once.splitlines(), status="working", continued=False, live=False
    )
    assert once == twice


def test_tree_output_never_becomes_table():
    """Tree listings (├── / └── / │ indent) have no whole-line border → passthrough."""
    lines = [
        "src",
        "├── engine",
        "│   └── protocol",
        "└── bot",
    ]
    out = render_chat_reply(lines, status="working", continued=False, live=False)
    assert out == "\n".join(lines)


def test_broken_table_blocks_pass_through():
    # data rows only, no border → not a table
    data_only = ["│ A │ 继续 │ 保持运行 │", "│ B │ 暂停 │ 更清晰  │"]
    assert render_chat_reply(data_only, status="working", continued=False, live=False) == "\n".join(
        data_only
    )
    # border rows only, no data → not a table
    border_only = ["┌──┬──┐", "├──┼──┤", "└──┴──┘"]
    assert render_chat_reply(border_only, status="working", continued=False, live=False) == "\n".join(
        border_only
    )
    # single column → not a table
    one_col = ["┌───┐", "│ 是 │", "└───┘"]
    out = render_chat_reply(one_col, status="working", continued=False, live=False)
    assert "│ 是 │" in out


def test_pure_box_decor_line_becomes_ascii():
    assert render_chat_reply(["──────────────"], status="idle", continued=False, live=False) == "------------"
    assert render_chat_reply(["━━━━━"], status="idle", continued=False, live=False) == "-----"


def test_cell_pipes_escaped_in_markdown():
    lines = [
        "┌───┬────┐",
        "│ a │ b  │",
        "├───┼────┤",
        "│ x │ p|q│",
        "└───┴────┘",
    ]
    out = render_chat_reply(lines, status="working", continued=False, live=False)
    assert "| p\\|q |" in out


def test_wrapped_breadcrumb_chrome_dedup():
    """Wrapped breadcrumbs (⎿  Read / path / (46 lines)) re-enter verbatim on
    repaints; indented path fragments and the (N lines) footer are chrome."""
    session: list[str] = []
    crumbs = [
        "  ⎿  Read",
        "     src/engine/src/protocol/version.ts",
        "     (13 lines)",
    ]
    merge_added_lines(session, crumbs)
    merge_added_lines(session, crumbs + ["✽ Inferring… (thought for 31s)"])
    assert len(session) == 4
    assert session.count("  ⎿  Read") == 1
    assert session.count("     src/engine/src/protocol/version.ts") == 1


def test_status_template_key_keeps_thought_commit_slot():
    a = status_template_key("Thought for 26s")
    b = status_template_key("Thought for 9s")
    assert a is not None and a == b
    assert status_template_key("Thought for 26s") != status_template_key("✽ Whirlpooling… (8s · thinking)")


def test_merge_coalesces_multiline_waiting_status_block():
    session: list[str] = []
    merge_added_lines(
        session,
        [
            " ⠰⠳ Waiting  52.04k tokens",
            "    Waiting 9m 16s for shell",
        ],
    )
    merge_added_lines(
        session,
        [
            " ⠰⠰ Waiting  52.04k tokens",
            "    Waiting 9m 15s for shell",
        ],
    )
    merge_added_lines(
        session,
        [
            " ⠠⠛ Waiting  52.04k tokens",
            "    Waiting 9m 14s for shell",
        ],
    )
    assert len(session) == 2
    assert "52.04k" in session[0]
    assert "9m 14s" in session[1]


def test_absorb_coalesces_waiting_shell_across_snapshots():
    state = _TurnState(baseline_text="base", last_window=["base"], last_snapshot="base")
    absorb_gateway_window(
        state,
        "base\n ⠰⠳ Waiting  52.04k tokens\n    Waiting 9m 16s for shell",
    )
    absorb_gateway_window(
        state,
        "base\n ⠰⠰ Waiting  52.04k tokens\n    Waiting 9m 15s for shell",
    )
    absorb_gateway_window(
        state,
        "base\n ⠠⠛ Waiting  52.04k tokens\n    Waiting 9m 14s for shell",
    )
    assert len(state.session_lines) == 2
    assert "9m 14s" in state.session_lines[1]


def test_render_chat_plain():
    text = render_chat_reply(["hello", "world"], status="working", continued=False, live=True)
    assert "```" not in text
    assert "hello" in text and text.endswith("…")


@pytest.mark.asyncio
async def test_begin_posts_placeholder_then_edits_same_reply():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 1
    reply = MagicMock(id=50, edit=AsyncMock())
    prompt = MagicMock(id=10)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    prompt.reply.assert_awaited_once()
    assert "思考中" in _text(prompt.reply.await_args)
    thread.trigger_typing.assert_awaited()

    state = get_terminal_state(thread, "p1")
    assert state.active
    assert state.message_id == 50

    state.baseline_text = "pre"
    state.session_lines = []
    state.last_window = []
    await apply_terminal_view(
        thread, "p1", "pre\nhello", "working", cfg, clock=clock.now, remote_id="r"
    )
    reply.edit.assert_awaited()
    assert "hello" in _text(reply.edit.await_args)
    assert prompt.reply.await_count == 1


@pytest.mark.asyncio
async def test_no_active_turn_skips_discord_updates():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 2
    thread.send = AsyncMock()
    cfg = _bridge(edit_cooldown=0.0)
    mid = await apply_terminal_view(thread, "p1", "noise", "working", cfg, clock=clock.now)
    thread.send.assert_not_awaited()
    assert mid is None


@pytest.mark.asyncio
async def test_coalesce_flush():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 3
    reply = MagicMock(id=7, edit=AsyncMock())
    prompt = MagicMock(id=1)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=2.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = "x"
    state.session_lines = []
    state.last_window = []
    await apply_terminal_view(thread, "p1", "x\na", "working", cfg, clock=clock.now)
    await apply_terminal_view(thread, "p1", "x\na\nb", "working", cfg, clock=clock.now)
    assert reply.edit.await_count <= 1
    clock.advance(2.5)
    await flush_terminal_view(thread, "p1", cfg, clock=clock.now)
    assert "b" in _text(reply.edit.await_args)


@pytest.mark.asyncio
async def test_deferred_task_flush():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 4
    reply = MagicMock(id=8, edit=AsyncMock())
    prompt = MagicMock(id=2)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    # Cooldown uses max(edit_cooldown, MIN_EDIT_INTERVAL=1.0)
    cfg = _bridge(edit_cooldown=1.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = ""
    state.session_lines = []
    state.last_window = []
    await apply_terminal_view(thread, "p1", "one", "working", cfg, clock=clock.now)
    assert "one" in _text(reply.edit.await_args)
    await apply_terminal_view(thread, "p1", "one\ntwo", "working", cfg, clock=clock.now)
    assert state.pending
    clock.advance(1.1)
    # Drive the scheduled flush with the same clock (task uses real sleep; flush manually).
    await flush_terminal_view(thread, "p1", cfg, clock=clock.now)
    assert "two" in _text(reply.edit.await_args)


@pytest.mark.asyncio
async def test_same_snapshot_keeps_thinking_placeholder():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 5
    reply = MagicMock(id=9, edit=AsyncMock())
    prompt = MagicMock(id=3)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = "line1\nline2"
    state.text = "line1\nline2"
    await apply_terminal_view(
        thread, "p1", "line1\nline2", "working", cfg, clock=clock.now
    )
    reply.edit.assert_not_awaited()
    assert state.message_id == 9


@pytest.mark.asyncio
async def test_sliding_window_still_streams_new_lines():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 6
    reply = MagicMock(id=11, edit=AsyncMock())
    prompt = MagicMock(id=4)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    base = [f"L{i}" for i in range(10)]
    state.baseline_text = "\n".join(base)
    state.session_lines = []
    state.last_window = list(base)

    await apply_terminal_view(
        thread, "p1", "\n".join(base), "working", cfg, clock=clock.now
    )
    reply.edit.assert_not_awaited()

    scrolled = base[2:] + ["agent says hi"]
    await apply_terminal_view(
        thread, "p1", "\n".join(scrolled), "working", cfg, clock=clock.now
    )
    reply.edit.assert_awaited()
    assert "agent says hi" in _text(reply.edit.await_args)
    assert "思考中" not in _text(reply.edit.await_args)


@pytest.mark.asyncio
async def test_lost_baseline_still_shows_window():
    """If pane.read scrolls the prompt baseline away, still update past 思考中."""
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 7
    reply = MagicMock(id=12, edit=AsyncMock())
    prompt = MagicMock(id=5)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = "old-a\nold-b\nold-c"
    state.last_window = state.baseline_text.splitlines()
    state.session_lines = []

    # Completely different window (baseline scrolled/cleared off).
    await apply_terminal_view(
        thread,
        "p1",
        "agent line 1\nagent line 2",
        "working",
        cfg,
        clock=clock.now,
    )
    reply.edit.assert_awaited()
    body = _text(reply.edit.await_args)
    assert "agent line 2" in body
    assert "思考中" not in body


@pytest.mark.asyncio
async def test_append_keeps_earlier_lines_after_scroll():
    """Sliding pane tip must not wipe earlier turn history from Discord buffer."""
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 8
    reply = MagicMock(id=13, edit=AsyncMock())
    cont = MagicMock(id=14, edit=AsyncMock())
    prompt = MagicMock(id=6)
    prompt.reply = AsyncMock(side_effect=[reply, cont])
    thread.fetch_message = AsyncMock(return_value=reply)
    thread.send = AsyncMock(return_value=cont)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = "base"
    state.last_window = ["base"]
    state.session_lines = []

    await apply_terminal_view(thread, "p1", "base\nearly\nmiddle", "working", cfg, clock=clock.now)
    assert state.session_lines == ["early", "middle"]

    # Window scrolled: "early" fell off the pane tip; only middle+late visible.
    clock.advance(2.0)
    await apply_terminal_view(thread, "p1", "middle\nlate", "working", cfg, clock=clock.now)
    assert state.session_lines == ["early", "middle", "late"]
    assert "early" in _text(reply.edit.await_args)
    assert "late" in _text(reply.edit.await_args)


@pytest.mark.asyncio
async def test_long_turn_seals_and_continues_new_message():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 9
    reply = MagicMock(id=20, edit=AsyncMock())
    cont = MagicMock(id=21, edit=AsyncMock())
    prompt = MagicMock(id=7)
    prompt.reply = AsyncMock(side_effect=[reply, cont])
    thread.fetch_message = AsyncMock(side_effect=[reply, cont])
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = ""
    state.last_window = []
    state.session_lines = []

    # Build enough lines to exceed SOFT_LIMIT when rendered.
    lines = [f"line-{i}-" + ("x" * 80) for i in range(40)]
    await apply_terminal_view(thread, "p1", "\n".join(lines), "working", cfg, clock=clock.now)
    assert state.segment_index >= 1 or state.live_start > 0 or prompt.reply.await_count >= 2
    # First bubble was edited (sealed or live); history not discarded from buffer.
    assert len(state.session_lines) == 40


def test_task_panel_repaint_updates_in_place():
    """Pinned task panels repaint as a block every frame; count/checkmark edits
    update the slots in place instead of re-appending the whole panel."""
    session: list[str] = []
    panel = [
        "10 tasks (6 done, 1 in progress, 3 open)",
        "  ◼ Task 7: HttpSseTransport (RPC + SSE + token) (@task7-impl)",
        "    Running Run full test suite, summarize failed files…",
        "  ◻ Task 8: AgentAdapter + Registry + PiAiAdapter + subagent",
        "  ◻ Task 9: EngineClient + @openfde/engine entry",
        "  ◻ Task 10: CLI + P0-2 resource decoupling + acceptance",
        "  ✔ Task 1: JSON-RPC envelope + schema + version",
        "   … +5 completed",
    ]
    assert merge_added_lines(session, panel) == len(panel)

    # Next repaint: done-count went 6→7, Task 7 flipped ◼→✔, everything else same.
    repaint = [
        "10 tasks (7 done, 1 in progress, 2 open)",
        "  ✔ Task 7: HttpSseTransport (RPC + SSE + token) (@task7-impl)",
        "    Running Run full test suite, summarize failed files…",
        "  ◻ Task 8: AgentAdapter + Registry + PiAiAdapter + subagent",
        "  ◻ Task 9: EngineClient + @openfde/engine entry",
        "  ◻ Task 10: CLI + P0-2 resource decoupling + acceptance",
        "  ✔ Task 1: JSON-RPC envelope + schema + version",
        "   … +5 completed",
    ]
    # 2 in-place updates (count line, task-7 row); the rest matched unchanged.
    assert merge_added_lines(session, repaint) == 2
    assert len(session) == len(panel)
    assert session[0] == "10 tasks (7 done, 1 in progress, 2 open)"
    assert session[1] == "  ✔ Task 7: HttpSseTransport (RPC + SSE + token) (@task7-impl)"

    # Identical repaint → zero mutations, still exactly one copy of the panel.
    assert merge_added_lines(session, repaint) == 0
    assert len(session) == len(panel)


def test_collapsed_row_variant_updates_in_place():
    """Folded summaries (… +N word) share one slot across count changes."""
    session = ["… +5 completed"]
    assert merge_added_lines(session, ["… +4 completed"]) == 1
    assert session == ["… +4 completed"]
    assert merge_added_lines(session, ["… +4 completed"]) == 0
    assert len(session) == 1


def test_ui_decor_rows_and_borders_dedup_on_repaint():
    """Rules, prompts, branch labels, agent rows and lone bottom borders are
    repaint chrome: identical copies dedup, only live bits update."""
    session: list[str] = []
    frame = [
        "──────────────────────────────────────────────────────────────────",
        "  ❯",
        "  ⏺ main",
        "◯ task7-impl  实现 Task 7（HttpSseTransport：HTTP RPC + SSE 事件流 + to...  7m 26s",
        "└─────┴─────────────────────────────┴─────────────────────┘",
    ]
    assert merge_added_lines(session, frame) == len(frame)
    # Repaint: identical except the agent-row timer.
    repaint = [
        "──────────────────────────────────────────────────────────────────",
        "  ❯",
        "  ⏺ main",
        "◯ task7-impl  实现 Task 7（HttpSseTransport：HTTP RPC + SSE 事件流 + to...  7m 29s",
        "└─────┴─────────────────────────────┴─────────────────────┘",
    ]
    assert merge_added_lines(session, repaint) == 1
    assert len(session) == 5
    assert "7m 29s" in session[3]


def test_task_like_content_is_never_absorbed():
    """Ordinary prose that merely resembles a task panel must append normally."""
    session: list[str] = ["start"]
    lines = [
        "Task 7: finish the report (remember the notes file)",  # no symbol prefix
        "we have 10 tasks to do today",  # no (counts) suffix
        "  ✔ done with the review",  # no Task N
        "something +5 more to come",  # no leading …
    ]
    before = len(session)
    assert merge_added_lines(session, lines) == 4
    assert len(session) == before + 4
    assert session[-4:] == lines


def test_decor_label_bar_updates_in_place():
    """herdr is_horizontal_rule() treats '─── label ───' lines (≥3 leading
    dashes, herdr src/detect/manifest.rs) as decorative UI; count/label
    changes must update the slot, not append a new line. A single dash +
    prose (rule_chars=1 < 3) is real content and stays untouched."""
    session: list[str] = []
    assert merge_added_lines(session, ["─── 完成 3 项 ───"]) == 1
    assert merge_added_lines(session, ["─── 完成 4 项 ───"]) == 1
    assert session == ["─── 完成 4 项 ───"]
    assert merge_added_lines(session, ["─── 完成 4 项 ───"]) == 0
    assert len(session) == 1
    # Single dash + prose is real content — never absorbed.
    session2 = ["start"]
    assert merge_added_lines(session2, ["─ done with the review"]) == 1
    assert session2[-1] == "─ done with the review"


def _mk_table(rows: list[str]) -> list[str]:
    """A small 2-column box table: top border, header, separator, rows, bottom."""
    return ["┌───┬──────┐", "│ # │ 任务   │", "├───┼──────┤", *rows, "└───┴──────┘"]


def test_table_block_appends_once_then_updates_in_place():
    """A box table is one block: first paint appends every row, a repaint with
    more rows (task list growth) replaces the whole block instead of appending
    rows below the bottom border, and an identical repaint is a no-op."""
    session: list[str] = []
    t1 = _mk_table(["│ 1 │ 修复 A │"])
    assert merge_added_lines(session, t1) == len(t1)
    assert session == t1

    # identical repaint → no mutation
    assert merge_added_lines(session, t1) == 0
    assert session == t1

    # grew by one row → in-place block update, still one contiguous table
    t2 = _mk_table(["│ 1 │ 修复 A │", "│ 2 │ 修复 B │"])
    assert merge_added_lines(session, t2) == 1
    assert session == t2
    assert session.count("└───┴──────┘") == 1

    # shrank → in-place block update
    t3 = _mk_table(["│ 2 │ 修复 B │"])
    assert merge_added_lines(session, t3) == 1
    assert session == t3


def test_table_block_changed_width_appends_conservatively():
    """A table whose top border changed width is a *new* table: append, and the
    old block stays (history). Never splice across different top borders."""
    session: list[str] = []
    t1 = _mk_table(["│ 1 │ A │"])
    merge_added_lines(session, t1)
    t_wide = ["┌────┬────────┐", "│ #  │ 任务    │", "├────┼────────┤", "│ 1  │ A       │", "└────┴────────┘"]
    assert merge_added_lines(session, t_wide) == len(t_wide)
    assert session == t1 + t_wide


def test_incomplete_table_parts_stay_plain_lines():
    """A pane.read window that cut the borders (data rows only, or top border
    without bottom) must not be treated as a block — lines merge as before."""
    session: list[str] = []
    data_only = ["│ 1 │ 修复 A │", "│ 2 │ 修复 B │"]
    assert merge_added_lines(session, data_only) == 2
    assert session == data_only

    top_only = ["┌───┬──────┐", "│ 1 │ 修复 A │"]
    # the data row duplicates an existing chrome line → deduped; only the new
    # top border appends (plain-line behavior, no block splicing)
    assert merge_added_lines(session, top_only) == 1
    assert session == data_only + ["┌───┬──────┐"]


def test_tui_footer_chrome_dropped_pi():
    """pi's fixed footer rows never reach Discord: spinner Working status, token
    stats, extension/MCP status, and the 源码： note between decor bars."""
    session: list[str] = ["line one", "line two"]
    added = [
        " ⠹ Working...",
        "↑2.1M ↓373k R36M CH99.3% 84.7%/128k (auto)                                                    (deepseek) deepseek-v4-flash",
        "🧠 agentmemory 🔌 MCP: 7 servers enabled (1 connected)",
        "源码：https://github.com/earendil-works/pi  https://github.com/anomalyco/opencode   ,claude code 5个月前的源码：https://github.com/neohob/claude-code",
    ]
    assert merge_added_lines(session, added) == 0
    assert session == ["line one", "line two"]


def test_tui_footer_chrome_dropped_opencode_claude():
    """opencode's ╹▀ divider + ctrl+p status bar and claude's bypass-permissions
    prompt band are footer chrome and dropped. The ▣ turn summary is a scrolling
    system row (turn-summary.ts), not footer — it is kept (and coalesced)."""
    session: list[str] = ["ok"]
    added = [
        "     ▣  Plan · DeepSeek V4 Flash Free (New) · 27.9s",
        "   ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀",
        "   /Users/neo/Downloads/MyNextCloud/Work/OpenFDE 151.2K (76%)  ctrl+p commands    • OpenCode 1.18.9",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
    ]
    assert merge_added_lines(session, added) == 1
    assert session == ["ok", "     ▣  Plan · DeepSeek V4 Flash Free (New) · 27.9s"]


def test_opencode_turn_summaries_coalesce_per_turn():
    """Source: turn-summary.ts renders `▣ {agent} · {model} · {duration}` after
    every completed turn. Successive turns of the same agent+model must replace
    in place (duration changes), never stack."""
    session: list[str] = []
    merge_added_lines(session, ["     ▣  Plan · DeepSeek V4 Flash Free (New) · 27.9s"])
    merge_added_lines(session, ["     ▣  Plan · DeepSeek V4 Flash Free (New) · 41.2s"])
    turn_lines = [ln for ln in session if "Plan" in ln]
    assert len(turn_lines) == 1
    assert "41.2s" in turn_lines[0]


def test_footer_chrome_never_absorbs_real_content():
    """Status-like real content is preserved: token-bearing Working lines (info),
    plain Working sentences, paths with percentages, and '源码' in prose."""
    session: list[str] = []
    added = [
        " ⠰⠳ Working  26.39k tokens",            # informative status → slot, kept
        "Working on the merge now",                # prose sentence
        "docs/verify.ts 42% coverage",             # path + percent, no ctrl+p
        "源码里没有这个函数",                        # prose containing 源码
        "build ↓ 12% slower after cache",          # ↓ but no ↑/CH%/model parens
    ]
    assert merge_added_lines(session, added) == len(added)
    assert session == added


def test_pi_stats_with_varying_numbers_still_dropped():
    """The stats line repaints every frame with different numbers; every variant
    is chrome and must be dropped, never appended."""
    session: list[str] = ["content"]
    for frame in (
        "↑2.1M ↓373k R36M CH99.3% 84.7%/128k (auto) (deepseek) deepseek-v4-flash",
        "↑2.2M ↓374k R36M CH99.1% 85.1%/128k (auto) (deepseek) deepseek-v4-flash",
        "↑2.3M ↓375k R37M CH98.9% 85.9%/128k (auto) (deepseek) deepseek-v4-flash",
    ):
        assert merge_added_lines(session, [frame]) == 0
    assert session == ["content"]


def test_pi_stats_without_cache_hit_rate_still_dropped():
    """Source: FooterComponent.render() only pushes CH% when cacheRead>0. A fresh
    session's stats line has no CH — the ↑↓+model structure alone is chrome."""
    session: list[str] = ["content"]
    for frame in (
        "↑1.2k ↓300 12%/32k (auto)                                    (deepseek) deepseek-v4-flash",
        "↑1.3k ↓310 13%/32k (auto)                                    (deepseek) deepseek-v4-flash",
    ):
        assert merge_added_lines(session, [frame]) == 0
    assert session == ["content"]


def test_bash_elapsed_dropped_but_took_kept():
    """Source: core/tools/bash.js appends `Elapsed 0.0s` (muted, repaints every
    frame while the tool runs) vs `Took 5.2s` once on completion. Elapsed is
    running-state chrome; Took is reply content."""
    session: list[str] = []
    assert merge_added_lines(session, [" Elapsed 0.0s"]) == 0
    assert merge_added_lines(session, [" Elapsed 12.3s"]) == 0
    assert session == []
    assert merge_added_lines(session, [" Took 5.2s"]) == 1
    assert session == [" Took 5.2s"]


def test_pwd_line_kept():
    """The footer pwd line (~path (branch)) is dim chrome but its shape is too
    close to real content to filter — it is stable across frames anyway, so the
    diff window rarely re-sends it. Assert we deliberately keep it."""
    session: list[str] = []
    assert merge_added_lines(session, ["~/Downloads/MyNextCloud/Work/herdr-discord-bridge (main)"]) == 1
    assert session == ["~/Downloads/MyNextCloud/Work/herdr-discord-bridge (main)"]


def test_custom_status_word_frames_coalesce():
    """User's real case: pi WorkingStatusIndicator with a custom message
    'Booping…' and custom spinner frames ✽·✢✶✳. The word is not in any phase
    list — the leading spinner glyph + short text is the source-verified shape
    (setWorkingMessage is free-form). All six animation frames collapse into one
    slot instead of appending six lines."""
    session: list[str] = ["ok"]
    frames = [
        "✽ Booping…",
        "· Booping…",
        "✽ Booping…",
        "✢ Booping…",
        "✶ Booping…",
        "✳ Booping…",
    ]
    for f in frames:
        merge_added_lines(session, [f])
    # every frame replaced the previous one → single status line, content intact
    status_lines = [ln for ln in session if "Booping" in ln]
    assert len(status_lines) == 1
    assert session[0] == "ok"


def test_status_word_with_elapsed_parens_coalesce():
    """Qualified frames (39s · thinking some more / 44s · ↓ tokens) share the
    <Q> slot: same word, different qualifiers → one line."""
    session: list[str] = []
    merge_added_lines(session, ["✢ Booping… (39s · thinking some more)"])
    merge_added_lines(session, ["✽ Booping… (44s · ↓ 2.9k tokens)"])
    booping = [ln for ln in session if "Booping" in ln]
    assert len(booping) == 1


def test_thinking_paragraph_lines_never_absorbed():
    """Long glyph-prefixed thinking lines (⏺/❯ prefixes are in the spinner glyph
    set) must stay: the guard is short-text-only, so real prose is untouched."""
    session: list[str] = []
    thinking = (
        "⏺ 用户让我检查 task 完成状态并继续。先说明:Phase 0 的 10 个实现 task 全部完成(impl + review + final review 全"
        " Approved)。TaskList 显示 Task 8/9/10 pending 是我没同步(双轨)。"
    )
    prompt = "❯ 我看你task还有没有完成的？继续呀我看你task还有没有完成的？继续呀"
    assert merge_added_lines(session, [thinking]) == 1
    assert merge_added_lines(session, [prompt]) == 1
    assert session == [thinking, prompt]


def test_claude_any_status_word_coalesces():
    """Claude Code's braille spinner + arbitrary status word (Working/Thinking/
    reasoning/…) repaints each frame. The <spinner-status> slot collapses every
    frame of every word — the word list must never be the mechanism."""
    session: list[str] = ["ok"]
    for frame in ("⠋ Working…", "⠙ Working…", "⠹ Working…", "⠸ Thinking…"):
        merge_added_lines(session, [frame])
    status = [ln for ln in session if ln.strip().startswith(("⠋", "⠙", "⠹", "⠸"))]
    assert len(status) <= 1
    assert session[0] == "ok"
