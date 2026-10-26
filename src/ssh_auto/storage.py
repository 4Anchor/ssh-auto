from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from .ssh_parser import validate_option_tokens
from .models import Profile


class CatalogError(ValueError):
    pass


class CatalogConflict(CatalogError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    profiles: tuple[Profile, ...]
    revision: str | None


def catalog_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = Path(env.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "ssh-auto" / "profiles.yaml"


def _revision(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogError("last_used_at must be null or an ISO-8601 string")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CatalogError(f"invalid last_used_at: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CatalogError("last_used_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _load_bytes(data: bytes | None) -> CatalogSnapshot:
    if data is None:
        return CatalogSnapshot((), None)
    try:
        raw = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CatalogError(f"invalid YAML catalog: {exc}") from exc
    if not isinstance(raw, dict):
        raise CatalogError("catalog root must be a mapping")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise CatalogError("unsupported catalog version")
    if set(raw) != {"version", "profiles"} or not isinstance(raw.get("profiles"), list):
        raise CatalogError("catalog must contain only version and profiles")
    profiles: list[Profile] = []
    names: set[str] = set()
    for index, item in enumerate(raw["profiles"]):
        if not isinstance(item, dict) or set(item) != {"name", "destination", "options", "last_used_at"}:
            raise CatalogError(f"profile {index} has invalid schema")
        if not isinstance(item["name"], str) or not isinstance(item["destination"], str):
            raise CatalogError(f"profile {index} name and destination must be strings")
        if not isinstance(item["options"], list) or not all(isinstance(value, str) for value in item["options"]):
            raise CatalogError(f"profile {index} options must be a list of strings")
        name_key = item["name"].strip().casefold()
        if not name_key or name_key in names:
            raise CatalogError("profile names must be non-empty and unique ignoring case")
        names.add(name_key)
        try:
            options = validate_option_tokens(item["options"])
            profiles.append(Profile(item["name"], item["destination"], options, _parse_timestamp(item["last_used_at"])))
        except (ValueError, TypeError) as exc:
            raise CatalogError(f"invalid profile {index}: {exc}") from exc
    return CatalogSnapshot(tuple(profiles), _revision(data))


class Catalog:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or catalog_path())

    def _read(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CatalogError(f"cannot read catalog {self.path}: {exc}") from exc

    def load(self) -> CatalogSnapshot:
        return _load_bytes(self._read())

    def save(self, profiles: Iterable[Profile], expected_revision: str | None) -> CatalogSnapshot:
        profiles = tuple(profiles)
        _validate_profiles(profiles)
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = os.lstat(self.path.parent)
            if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or directory.st_mode & 0o022:
                raise CatalogError(f"unsafe catalog directory {self.path.parent}")
            os.chmod(self.path.parent, 0o700)
            lock_path = Path(str(self.path) + ".lock")
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                lock = os.fstat(lock_fd)
                if not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.geteuid() or lock.st_nlink != 1:
                    raise CatalogError(f"unsafe catalog lock {lock_path}")
                os.fchmod(lock_fd, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                current = _load_bytes(self._read())
                if current.revision != expected_revision:
                    raise CatalogConflict("catalog changed in another process; reload required")
                payload = _dump(profiles)
                fd, temporary = tempfile.mkstemp(prefix=".profiles.", dir=self.path.parent)
                try:
                    try:
                        os.fchmod(fd, 0o600)
                        with os.fdopen(fd, "wb") as stream:
                            fd = -1
                            stream.write(payload)
                            stream.flush()
                            os.fsync(stream.fileno())
                    finally:
                        if fd != -1:
                            os.close(fd)
                    os.replace(temporary, self.path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                return CatalogSnapshot(profiles, _revision(payload))
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        except OSError as exc:
            raise CatalogError(f"cannot save catalog {self.path}: {exc}") from exc


def _validate_profiles(profiles: tuple[Profile, ...]) -> None:
    seen: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, Profile):
            raise CatalogError("profiles must contain Profile values")
        key = profile.name.casefold()
        if key in seen:
            raise CatalogError("profile names must be unique ignoring case")
        seen.add(key)


def _dump(profiles: tuple[Profile, ...]) -> bytes:
    data = {
        "version": 1,
        "profiles": [
            {
                "name": profile.name,
                "destination": profile.destination,
                "options": list(profile.options),
                "last_used_at": profile.last_used_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if profile.last_used_at else None,
            }
            for profile in profiles
        ],
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False).encode("utf-8")
