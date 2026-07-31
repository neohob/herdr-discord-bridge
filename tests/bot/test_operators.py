from types import SimpleNamespace

from src.bot.config import OperatorsConfig
from src.bot.operators import is_operator


def _member(*, user_id: int, manage_guild: bool = False, role_ids: tuple[int, ...] = ()):
    return SimpleNamespace(
        id=user_id,
        guild_permissions=SimpleNamespace(manage_guild=manage_guild),
        roles=[SimpleNamespace(id=rid) for rid in role_ids],
    )


def test_manage_guild_is_operator():
    cfg = OperatorsConfig(require_manage_guild=True, user_ids=[], role_ids=[])
    assert is_operator(_member(user_id=1, manage_guild=True), cfg)


def test_no_manage_guild_not_operator():
    cfg = OperatorsConfig(require_manage_guild=True, user_ids=[], role_ids=[])
    assert not is_operator(_member(user_id=2, manage_guild=False), cfg)


def test_role_id_grants_operator():
    cfg = OperatorsConfig(require_manage_guild=True, user_ids=[], role_ids=[42])
    assert is_operator(_member(user_id=3, manage_guild=False, role_ids=(42,)), cfg)


def test_user_allowlist_blocks_non_member():
    cfg = OperatorsConfig(require_manage_guild=True, user_ids=[1], role_ids=[])
    assert not is_operator(_member(user_id=2, manage_guild=True), cfg)


def test_user_allowlist_allows_member_with_admin():
    cfg = OperatorsConfig(require_manage_guild=True, user_ids=[1], role_ids=[])
    assert is_operator(_member(user_id=1, manage_guild=True), cfg)
