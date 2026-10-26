from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .ssh_parser import validate_destination, validate_option_tokens


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    destination: str
    options: tuple[str, ...] = ()
    last_used_at: datetime | None = None

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise ValueError("profile name must not be empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "destination", validate_destination(self.destination))
        object.__setattr__(self, "options", validate_option_tokens(self.options))
        object.__setattr__(self, "last_used_at", _utc(self.last_used_at))


@dataclass(frozen=True, slots=True)
class Candidate:
    destination: str
    options: tuple[str, ...] = ()
    sources: frozenset[str] = frozenset()
    use_count: int = 0
    last_seen_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "destination", validate_destination(self.destination))
        object.__setattr__(self, "options", validate_option_tokens(self.options))
        if self.use_count < 0:
            raise ValueError("use_count must not be negative")
        object.__setattr__(self, "sources", frozenset(self.sources))
        object.__setattr__(self, "last_seen_at", _utc(self.last_seen_at))


def ssh_argv(destination: str, options: tuple[str, ...] = ()) -> list[str]:
    """Build argv for the system ssh binary; never use shell parsing here."""
    return ["ssh", *options, destination]
