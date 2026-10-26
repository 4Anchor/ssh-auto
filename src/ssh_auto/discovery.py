from __future__ import annotations

import glob
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import Candidate
from .ssh_parser import ParsedCommand, parse_ssh_command, validate_destination



def _timestamp(epoch: str | int | None) -> datetime | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _history_path(shell_name: str, home: Path, env: dict[str, str]) -> Path:
    current_shell = Path(env.get("SHELL", "")).name
    if current_shell == shell_name and env.get("HISTFILE"):
        value = env["HISTFILE"]
        if value == "~":
            return home
        if value.startswith("~/"):
            return home / value[2:]
        return Path(os.path.expandvars(value))
    return home / (".bash_history" if shell_name == "bash" else ".zsh_history")


def _read_lines(path: Path, warnings: list[str]) -> Iterator[str]:
    try:
        with path.open(encoding="utf-8") as stream:
            yield from (line.rstrip("\n\r") for line in stream)
    except FileNotFoundError:
        return
    except (OSError, UnicodeError) as exc:
        warnings.append(f"Не удалось прочитать {path}: {exc}")


def _continued(line: str) -> bool:
    return len(line) - len(line.rstrip("\\")) & 1 == 1


def parse_bash_history(path: Path, warnings: list[str], home: Path | None = None) -> Iterator[tuple[ParsedCommand, datetime | None]]:
    timestamp: datetime | None = None
    continuation = False
    for line in _read_lines(path, warnings):
        if continuation:
            continuation = _continued(line)
            continue
        if re.fullmatch(r"#\d+", line):
            timestamp = _timestamp(line[1:])
            continue
        if _continued(line):
            continuation = True
        elif line.strip() and not line[:1].isspace():
            parsed = parse_ssh_command(line, home)
            if parsed:
                yield parsed, timestamp
        timestamp = None


def parse_zsh_history(path: Path, warnings: list[str], home: Path | None = None) -> Iterator[tuple[ParsedCommand, datetime | None]]:
    continuation = False
    for line in _read_lines(path, warnings):
        if continuation:
            continuation = _continued(line)
            continue
        timestamp = None
        command = line
        match = re.match(r":\s*(\d+):\d+;(.*)$", line)
        if match:
            timestamp = _timestamp(match.group(1))
            command = match.group(2)
        if _continued(command):
            continuation = True
        elif command and not command[:1].isspace():
            parsed = parse_ssh_command(command, home)
            if parsed:
                yield parsed, timestamp


def _decode_fish(value: str) -> str:
    placeholder = "\0BACKSLASH\0"
    return value.replace("\\\\", placeholder).replace("\\n", "\n").replace(placeholder, "\\")


def parse_fish_history(path: Path, warnings: list[str], home: Path | None = None) -> Iterator[tuple[ParsedCommand, datetime | None]]:
    command: str | None = None
    when: datetime | None = None
    for line in _read_lines(path, warnings):
        if line.startswith("- cmd: "):
            if command is not None:
                parsed = parse_ssh_command(command, home)
                if parsed:
                    yield parsed, when
            command = _decode_fish(line[7:])
            when = None
        elif command is not None and re.match(r"\s+when:\s*\d+\s*$", line):
            when = _timestamp(line.split(":", 1)[1].strip())
        elif command is not None and not line:
            parsed = parse_ssh_command(command, home)
            if parsed:
                yield parsed, when
            command = None
            when = None
    if command is not None:
        parsed = parse_ssh_command(command, home)
        if parsed:
            yield parsed, when


def _expand_vars(value: str, env: dict[str, str]) -> str:
    return re.sub(r"\$(?:\{([^}]+)\}|([A-Za-z_][A-Za-z0-9_]*))", lambda match: env.get(match.group(1) or match.group(2), match.group(0)), value)


def _include_patterns(raw: str, root: Path, env: dict[str, str] | None = None, warnings: list[str] | None = None) -> list[Path]:
    try:
        pieces = shlex.split(raw, posix=True)
    except ValueError:
        raise ValueError("unterminated Include pattern")
    paths: list[Path] = []
    for piece in pieces:
        if "%" in piece:
            raise ValueError("unsupported % token in Include")
        expanded = piece
        if env is not None and piece.startswith("~/"):
            expanded = str(root.parent / piece[2:])
        else:
            expanded = os.path.expanduser(piece)
            expanded = os.path.expandvars(expanded) if env is None else _expand_vars(expanded, env)
        path = Path(expanded)
        if not path.is_absolute():
            path = root / path
        matches = sorted(glob.glob(str(path)))
        if not matches and not glob.has_magic(str(path)) and warnings is not None:
            warnings.append(f"Не найден Include {path}")
        paths.extend(Path(item) for item in matches)
    return paths


def parse_ssh_config(path: Path, warnings: list[str], visited: set[Path] | None = None, root: Path | None = None, env: dict[str, str] | None = None) -> list[str]:
    visited = set() if visited is None else visited
    root = root or path.parent
    try:
        real = path.resolve(strict=True)
    except FileNotFoundError:
        return []
    except OSError as exc:
        warnings.append(f"Не удалось открыть SSH config {path}: {exc}")
        return []
    if real in visited:
        return []
    visited.add(real)
    aliases: list[str] = []
    for line in _read_lines(path, warnings):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            parts = shlex.split(stripped, posix=True)
        except ValueError as exc:
            warnings.append(f"Ошибка SSH config {path}: {exc}")
            continue
        if not parts:
            continue
        keyword = parts[0].lower()
        if keyword == "include":
            if len(parts) < 2:
                warnings.append(f"Пустой Include в {path}")
                continue
            try:
                includes = _include_patterns(stripped[len(parts[0]):].strip(), root, env, warnings)
            except ValueError as exc:
                warnings.append(f"Ошибка Include в {path}: {exc}")
                continue
            for include in includes:
                aliases.extend(parse_ssh_config(include, warnings, visited, root, env))
        elif keyword == "host" and len(parts) > 1:
            for alias in parts[1:]:
                if not any(char in alias for char in "*?!"):
                    try:
                        aliases.append(validate_destination(alias))
                    except ValueError:
                        warnings.append(f"Недопустимый Host {alias!r} в {path}")
    return aliases


@dataclass(slots=True)
class _Aggregate:
    sources: set[str] = field(default_factory=set)
    count: int = 0
    latest: datetime | None = None


def discover(env: dict[str, str] | None = None, home: Path | None = None) -> tuple[list[Candidate], list[str]]:
    env = dict(os.environ if env is None else env)
    home = Path(home or env.get("HOME", str(Path.home()))).expanduser()
    warnings: list[str] = []
    merged: dict[tuple[str, tuple[str, ...]], _Aggregate] = {}

    def add(parsed: ParsedCommand, source: str, timestamp: datetime | None, count: int) -> None:
        entry = merged.setdefault((parsed.destination, parsed.options), _Aggregate())
        entry.sources.add(source)
        entry.count += count
        if timestamp is not None and (entry.latest is None or timestamp > entry.latest):
            entry.latest = timestamp

    for source, path, parser in (
        ("bash", _history_path("bash", home, env), parse_bash_history),
        ("zsh", _history_path("zsh", home, env), parse_zsh_history),
    ):
        for parsed, timestamp in parser(path, warnings, home):
            add(parsed, source, timestamp, 1)
    data_home = env.get("XDG_DATA_HOME", str(home / ".local" / "share"))
    if data_home == "~":
        data_home = str(home)
    elif data_home.startswith("~/"):
        data_home = str(home / data_home[2:])
    fish_path = Path(os.path.expandvars(data_home)) / "fish" / "fish_history"
    for parsed, timestamp in parse_fish_history(fish_path, warnings, home):
        add(parsed, "fish", timestamp, 1)
    for alias in parse_ssh_config(home / ".ssh" / "config", warnings, env=env):
        add(ParsedCommand(alias, ()), "ssh_config", None, 0)

    candidates = sorted(
        (Candidate(destination, options, frozenset(entry.sources), entry.count, entry.latest)
         for (destination, options), entry in merged.items()),
        key=lambda item: (
            -item.use_count,
            -(item.last_seen_at.timestamp() if item.last_seen_at else float("-inf")),
            item.destination,
            item.options,
        ),
    )
    return candidates, warnings
