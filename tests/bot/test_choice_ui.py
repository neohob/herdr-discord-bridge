from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.choice_ui import BlockedChoiceView, _choice_custom_id
from src.bot.config import OperatorsConfig


def _interaction(*, member) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(config=SimpleNamespace(operators=OperatorsConfig(require_manage_guild=True))),
        user=member,
        response=SimpleNamespace(send_message=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_choice_view_allows_operator_interaction() -> None:
    member = SimpleNamespace(
        id=1,
        guild_permissions=SimpleNamespace(manage_guild=True),
        roles=[],
    )
    from src.bot.choice_ui import _check_operator

    allowed = await _check_operator(_interaction(member=member))

    assert allowed is True


@pytest.mark.asyncio
async def test_choice_view_rejects_non_operator_interaction() -> None:
    member = SimpleNamespace(
        id=1,
        guild_permissions=SimpleNamespace(manage_guild=False),
        roles=[],
    )
    interaction = _interaction(member=member)
    from src.bot.choice_ui import _check_operator

    allowed = await _check_operator(interaction)

    assert allowed is False
    interaction.response.send_message.assert_awaited_once_with(
        "Operator permission is required.",
        ephemeral=True,
    )


def test_choice_view_is_persistent_and_encodes_target_in_custom_ids() -> None:
    view = BlockedChoiceView("lab", "w1:p1")

    assert view.timeout is None
    assert all(
        child.custom_id == _choice_custom_id("lab", "w1:p1", action)
        for child, action in zip(view.children, ("yes", "no", "custom"), strict=True)
    )
    # Layout-only buttons: no custom action attribute (clicks go to PersistentChoiceButton).
    assert all(not hasattr(child, "action") for child in view.children)
