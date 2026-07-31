from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.choice_ui import BlockedChoiceView
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

    allowed = await BlockedChoiceView("lab", "w1:p1").interaction_check(_interaction(member=member))

    assert allowed is True


@pytest.mark.asyncio
async def test_choice_view_rejects_non_operator_interaction() -> None:
    member = SimpleNamespace(
        id=1,
        guild_permissions=SimpleNamespace(manage_guild=False),
        roles=[],
    )
    interaction = _interaction(member=member)

    allowed = await BlockedChoiceView("lab", "w1:p1").interaction_check(interaction)

    assert allowed is False
    interaction.response.send_message.assert_awaited_once_with(
        "Operator permission is required.",
        ephemeral=True,
    )
