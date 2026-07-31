"""Discord bot package for Herdr Discord Bridge."""

__all__ = ["BridgeBot", "main"]


def __getattr__(name: str):
    if name in {"BridgeBot", "main"}:
        from src.bot.bot import BridgeBot, main

        return BridgeBot if name == "BridgeBot" else main
    raise AttributeError(name)
