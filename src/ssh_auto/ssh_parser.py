from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

ARG_OPTIONS = frozenset("Bb cDEeIiJ LlmOopQRSWw".replace(" ", ""))
FLAG_OPTIONS = frozenset("46A aC fGgKkMNnqstTtVvXxYy".replace(" ", ""))
DIAGNOSTIC = frozenset({"G", "Q", "V", "O"})
PATH_OPTIONS = frozenset({"E", "i", "S"})
COMMAND_OPTIONS = frozenset({"proxycommand", "localcommand", "knownhostscommand", "remotecommand"})
SHELL_MARKERS = ("|", ";", "&", ">", "<", "`", "$", "\n", "\r")


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    destination: str
    options: tuple[str, ...]


def validate_destination(destination: str) -> str:
    if not isinstance(destination, str):
        raise ValueError("destination must be a string")
    destination = destination.strip()
    if not destination or destination.startswith("-") or any(ch.isspace() for ch in destination):
        raise ValueError("destination must be one non-option argument")
    if any(marker in destination for marker in SHELL_MARKERS):
        raise ValueError("destination contains shell syntax")
    return destination


def _expand_path(value: str, letter: str, home: Path | None) -> str:
    if letter in PATH_OPTIONS and (value == "~" or value.startswith("~/")):
        base = Path.home() if home is None else home
        return str(base) + value[1:]
    return value


def _parse_options(tokens: tuple[str, ...] | list[str], require_destination: bool, home: Path | None) -> ParsedCommand | tuple[str, ...] | None:
    options: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not isinstance(token, str) or not token or any(marker in token for marker in SHELL_MARKERS):
            return None
        if not token.startswith("-"):
            break
        if token == "--" or len(token) < 2:
            return None
        body = token[1:]
        letter = body[0]
        if letter in ARG_OPTIONS:
            if len(body) == 1:
                index += 1
                if index >= len(tokens) or not isinstance(tokens[index], str) or not tokens[index] or tokens[index].startswith("-"):
                    return None
                value = tokens[index]
            else:
                value = body[1:]
            if not value or any(marker in value for marker in SHELL_MARKERS):
                return None
            if letter == "o":
                key, separator, _ = value.partition("=")
                if not separator or not key.strip() or key.strip().casefold() in COMMAND_OPTIONS:
                    return None
            value = _expand_path(value, letter, home)
            if len(body) == 1:
                options.extend((token, value))
            else:
                options.append(token[:2] + value)
            if letter in DIAGNOSTIC:
                return None
        elif all(char in FLAG_OPTIONS for char in body):
            if any(char in DIAGNOSTIC for char in body):
                return None
            options.append(token)
        else:
            return None
        index += 1

    remaining = tokens[index:]
    if not require_destination:
        return tuple(options) if not remaining else None
    if len(remaining) != 1:
        return None
    try:
        destination = validate_destination(remaining[0])
    except ValueError:
        return None
    return ParsedCommand(destination, tuple(options))


def parse_ssh_command(command: str, home: Path | None = None) -> ParsedCommand | None:
    """Parse one simple interactive ssh command, rejecting shell syntax."""
    if not isinstance(command, str) or not command.strip() or any(marker in command for marker in SHELL_MARKERS):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens or os.path.basename(tokens[0]) != "ssh":
        return None
    result = _parse_options(tokens[1:], True, home)
    return result if isinstance(result, ParsedCommand) else None


def validate_option_tokens(tokens: tuple[str, ...] | list[str], home: Path | None = None) -> tuple[str, ...]:
    result = _parse_options(tokens, False, home)
    if not isinstance(result, tuple):
        raise ValueError("invalid SSH options or positional argument")
    return result


def validate_options(options_text: str, home: Path | None = None) -> tuple[str, ...]:
    if any(marker in options_text for marker in SHELL_MARKERS):
        raise ValueError("shell syntax is not allowed")
    try:
        tokens = shlex.split(options_text, posix=True)
    except ValueError as exc:
        raise ValueError(f"invalid SSH options: {exc}") from exc
    return validate_option_tokens(tokens, home)
