import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ssh_auto.models import Profile
from ssh_auto.storage import Catalog, CatalogConflict, CatalogError


def test_empty_catalog_save_round_trip_and_modes(tmp_path: Path):
    path = tmp_path / "config" / "ssh-auto" / "profiles.yaml"
    catalog = Catalog(path)
    empty = catalog.load()
    assert empty.profiles == ()
    saved = catalog.save(
        [Profile("Production", "deploy@example.com", ("-p", "2222"), datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc))],
        empty.revision,
    )
    assert saved.profiles[0].last_used_at == datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(Path(str(path) + ".lock").stat().st_mode & 0o777) == "0o600"
    text = path.read_text(encoding="utf-8")
    assert "last_used_at:" in text and "2026-10-26T08:30:00Z" in text


def test_lock_symlink_does_not_change_target(tmp_path: Path):
    path = tmp_path / "profiles.yaml"
    target = tmp_path / "executable"
    original = b"#!/bin/sh\nexit 0\n"
    target.write_bytes(original)
    target.chmod(0o755)
    Path(str(path) + ".lock").symlink_to(target)
    with pytest.raises(CatalogError):
        Catalog(path).save([Profile("one", "one.example")], None)
    assert target.read_bytes() == original
    assert target.stat().st_mode & 0o777 == 0o755
    assert not path.exists()


def test_unsafe_catalog_directory_is_not_chmodded(tmp_path: Path):
    directory = tmp_path / "config"
    directory.mkdir(mode=0o770)
    with pytest.raises(CatalogError, match="unsafe catalog directory"):
        Catalog(directory / "profiles.yaml").save([], None)
    assert directory.stat().st_mode & 0o777 == 0o770


def test_invalid_catalog_is_not_overwritten_and_stale_save_conflicts(tmp_path: Path):
    path = tmp_path / "profiles.yaml"
    path.write_text("version: 99\nprofiles: []\n", encoding="utf-8")
    catalog = Catalog(path)
    with pytest.raises(CatalogError):
        catalog.load()
    original = path.read_bytes()
    with pytest.raises(CatalogError):
        catalog.save([], None)
    assert path.read_bytes() == original

    path.unlink()
    first = catalog.load()
    one = catalog.save([Profile("one", "one.example")], first.revision)
    second_reader = Catalog(path)
    stale = second_reader.load()
    newer = catalog.save([Profile("two", "two.example")], one.revision)
    with pytest.raises(CatalogConflict):
        second_reader.save([Profile("stale", "stale.example")], stale.revision)
    assert catalog.load().profiles == newer.profiles


def test_schema_rejects_duplicate_and_remote_like_values(tmp_path: Path):
    path = tmp_path / "profiles.yaml"
    path.write_text(
        "version: 1\nprofiles:\n"
        "  - {name: One, destination: one.example, options: [], last_used_at: null}\n"
        "  - {name: one, destination: two.example, options: [], last_used_at: null}\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        Catalog(path).load()
    path.write_text(
        "version: 1\nprofiles:\n"
        "  - {name: One, destination: '$HOST', options: [], last_used_at: null}\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        Catalog(path).load()


def test_bool_version_and_unsafe_option_do_not_load(tmp_path: Path):
    path = tmp_path / "profiles.yaml"
    path.write_text("version: true\nprofiles: []\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="version"):
        Catalog(path).load()
    path.write_text("version: 1\nprofiles:\n  - {name: unsafe, destination: prod, options: ['-o', 'ProxyCommand=echo'], last_used_at: null}\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="invalid profile"):
        Catalog(path).load()


@pytest.mark.parametrize("options", [
    ["-o", "RemoteCommand=id"],
    ["-oReMoTeCoMmAnD = id"],
    ["-F", "/tmp/config"],
    ["-F/tmp/config"],
])
def test_existing_catalog_rejects_unsafe_options_without_rewriting(tmp_path: Path, options: list[str]):
    import yaml

    path = tmp_path / "profiles.yaml"
    original = yaml.safe_dump({"version": 1, "profiles": [
        {"name": "unsafe", "destination": "prod", "options": options, "last_used_at": None}
    ]}).encode()
    path.write_bytes(original)
    with pytest.raises(CatalogError, match="invalid profile"):
        Catalog(path).load()
    with pytest.raises(CatalogError):
        Catalog(path).save([], None)
    assert path.read_bytes() == original


def test_microseconds_and_failed_replace_preserve_catalog(tmp_path: Path, monkeypatch):
    path = tmp_path / "profiles.yaml"
    catalog = Catalog(path)
    moment = datetime(2026, 10, 26, 8, 30, 12, 123456, tzinfo=timezone.utc)
    saved = catalog.save([Profile("one", "one.example", last_used_at=moment)], None)
    assert catalog.load().profiles[0].last_used_at == moment
    assert "2026-10-26T08:30:12.123456Z" in path.read_text()
    original = path.read_bytes()
    def fail_replace(*args):
        raise OSError("replace denied")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(CatalogError, match="replace denied"):
        catalog.save([Profile("two", "two.example")], saved.revision)
    assert path.read_bytes() == original
    assert list(path.parent.glob(".profiles.*")) == []
