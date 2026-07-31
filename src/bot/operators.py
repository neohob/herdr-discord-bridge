"""Operator permission checks for structural /herdr operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

    from src.bot.config import OperatorsConfig


def is_operator(member: discord.Member, cfg: OperatorsConfig) -> bool:
    if cfg.user_ids and member.id not in cfg.user_ids:
        return False

    if member.guild_permissions.manage_guild:
        return True

    if cfg.role_ids:
        member_role_ids = {role.id for role in member.roles}
        if member_role_ids.intersection(cfg.role_ids):
            return True

    if not cfg.require_manage_guild and not cfg.role_ids:
        return True

    return False
