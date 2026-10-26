from pathlib import Path

import pytest

from ssh_auto.app import SshAutoApp
from ssh_auto.models import Candidate, Profile
from ssh_auto.storage import Catalog


@pytest.mark.asyncio
async def test_app_tabs_search_and_saved_candidate_filter(tmp_path: Path):
    catalog = Catalog(tmp_path / "profiles.yaml")
    snapshot = catalog.save([Profile("prod", "prod.example")], None)
    candidate = Candidate("prod.example", (), frozenset({"bash"}), 2, None)
    other = Candidate("other.example", (), frozenset({"ssh_config"}), 0, None)
    app = SshAutoApp(catalog, snapshot, [candidate, other], [])
    async with app.run_test() as pilot:
        assert app.tab == "saved"
        assert len(app._displayed) == 1
        await pilot.press("tab")
        assert app.tab == "found"
        assert [item.destination for item in app._displayed] == ["other.example"]
        await pilot.press("/")
        await pilot.press("o", "t", "h")
        assert [item.destination for item in app._displayed] == ["other.example"]
        await pilot.press("a", "s", "q")
        assert app.search_text == "othasq"
        assert app._displayed == []
        await pilot.press("enter")
        assert app.focused is app.query_one("#table")
        await pilot.press("escape")
        await pilot.press("tab")
        assert app.tab == "saved"

@pytest.mark.asyncio
async def test_date_range_and_sort_on_both_tabs(tmp_path: Path):
    from datetime import datetime, timezone
    from textual.widgets import Input, Static

    def at(day: int) -> datetime:
        return datetime(2026, 10, day, 23, 45, tzinfo=timezone.utc)

    catalog = Catalog(tmp_path / "profiles.yaml")
    snapshot = catalog.save([
        Profile("early", "early.example", last_used_at=at(4)),
        Profile("late", "late.example", last_used_at=at(26)),
        Profile("unused", "unused.example"),
    ], None)
    candidates = [
        Candidate("no-date.example", (), frozenset({"bash"}), 9),
        Candidate("old.example", (), frozenset({"fish"}), 1, at(4)),
        Candidate("new.example", (), frozenset({"fish"}), 1, at(26)),
    ]
    app = SshAutoApp(catalog, snapshot, candidates, [])
    async with app.run_test() as pilot:
        assert [p.name for p in app._displayed] == ["late", "early", "unused"]
        await pilot.click("#date-sort")
        assert [p.name for p in app._displayed] == ["early", "late", "unused"]
        app.query_one("#date-from", Input).value = "2026-10-04"
        app.query_one("#date-to", Input).value = "2026-10-04"
        await pilot.pause()
        assert [p.name for p in app._displayed] == ["early"]
        await pilot.click("#found-tab")
        assert [c.destination for c in app._displayed] == ["old.example"]
        app.query_one("#date-to", Input).value = "2026-10-26"
        await pilot.pause()
        assert [c.destination for c in app._displayed] == ["old.example", "new.example"]
        await pilot.click("#date-sort")
        assert [c.destination for c in app._displayed] == ["new.example", "old.example"]
        app.query_one("#search", Input).value = "old"
        await pilot.pause()
        assert [c.destination for c in app._displayed] == ["old.example"]
        app.query_one("#search", Input).value = ""
        app.query_one("#date-from", Input).value = "2026-10-27"
        await pilot.pause()
        assert app._displayed == []
        assert "Неверный диапазон" in str(app.query_one("#date-error", Static).render())
        app.query_one("#date-from", Input).value = "20261004"
        await pilot.pause()
        assert app._displayed == []
        assert "ГГГГ-ММ-ДД" in str(app.query_one("#date-error", Static).render())
        app.query_one("#date-from", Input).value = ""
        await pilot.pause()
        assert [c.destination for c in app._displayed] == ["new.example", "old.example"]
        app.query_one("#date-from", Input).value = "2026-10-26"
        app.query_one("#date-to", Input).value = ""
        await pilot.pause()
        assert [c.destination for c in app._displayed] == ["new.example"]
        app.query_one("#date-from", Input).value = ""
        await pilot.pause()
        assert [c.destination for c in app._displayed] == ["new.example", "old.example", "no-date.example"]


@pytest.mark.asyncio
async def test_app_add_and_delete_workflow(tmp_path: Path):
    catalog = Catalog(tmp_path / "profiles.yaml")
    snapshot = catalog.load()
    app = SshAutoApp(catalog, snapshot, [Candidate("new.example", (), frozenset({"bash"}), 1, None)], [])
    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.click("#profile-name")
        await pilot.press("ctrl+a")
        await pilot.press("n", "e", "w")
        await pilot.click("#profile-destination")
        await pilot.press("ctrl+a")
        await pilot.press("n", "e", "w", ".", "e", "x", "a", "m", "p", "l", "e")
        await pilot.click("#save")
        assert [profile.name for profile in app.profiles] == ["new"]
        assert app.tab == "saved"
        await pilot.press("d")
        await pilot.click("#confirm")
        assert app.profiles == []
        assert catalog.load().profiles == ()


@pytest.mark.asyncio
async def test_app_launch_passes_argv_and_records_use(tmp_path: Path, monkeypatch):
    from contextlib import nullcontext
    import subprocess

    catalog = Catalog(tmp_path / "profiles.yaml")
    snapshot = catalog.save([Profile("jump", "ops@example.test", ("-J", "bastion", "-p", "2222"))], None)
    app = SshAutoApp(catalog, snapshot, [], [])
    captured = {}

    def fake_run(argv, check=False):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 23)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(app, "suspend", lambda: nullcontext())
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert captured["argv"] == ["ssh", "-J", "bastion", "-p", "2222", "ops@example.test"]
        assert "23" in str(app.query_one("#status").render())
    assert catalog.load().profiles[0].last_used_at is not None


@pytest.mark.asyncio
async def test_inline_collision_edit_and_delete_cancel(tmp_path: Path):
    from textual.widgets import DataTable, Input, Static
    catalog = Catalog(tmp_path / "profiles.yaml")
    snapshot = catalog.save([Profile("Alpha", "alpha.example"), Profile("Beta", "beta.example")], None)
    app = SshAutoApp(catalog, snapshot, [], ["Include missing.conf"])
    async with app.run_test() as pilot:
        row = app.query_one("#table", DataTable).get_row_at(0)
        assert [str(cell) for cell in row[:4]] == ["Alpha — alpha.example", "", "", ""]
        assert "missing.conf" in str(app.query_one("#warnings", Static).render())
        await pilot.press("e")
        name = app.screen.query_one("#profile-name", Input)
        name.value = "beta"
        await pilot.click("#save")
        assert "Имя уже используется" in str(app.screen.query_one("#form-error", Static).render())
        await pilot.click("#profile-name")
        name.value = ""
        await pilot.press("A", "s", "t", "e", "r")
        assert name.value == "Aster"
        await pilot.click("#save")
        assert [p.name for p in app.profiles] == ["Aster", "Beta"], str(app.screen.query_one("#form-error", Static).render())
        assert "Профиль сохранён" in str(app.query_one("#status", Static).render())
        await pilot.press("d")
        await pilot.click("#cancel")
        assert len(app.profiles) == 2
        await pilot.press("d")
        await pilot.click("#confirm")
        assert [p.name for p in app.profiles] == ["Beta"]
        assert "Профиль удалён" in str(app.query_one("#status", Static).render())


@pytest.mark.asyncio
async def test_dynamic_ui_content_is_literal(tmp_path: Path):
    from rich.text import Text
    from textual.widgets import DataTable, Static

    catalog = Catalog(tmp_path / "profiles.yaml")
    snapshot = catalog.save([Profile("[red]prod[/red]", "prod.example")], None)
    app = SshAutoApp(catalog, snapshot, [], ["Ошибка [red]"])
    async with app.run_test() as pilot:
        row = app.query_one("#table", DataTable).get_row_at(0)
        assert all(isinstance(cell, Text) and not cell.spans for cell in row)
        assert str(row[0]) == "[red]prod[/red] — prod.example"
        warnings = app.query_one("#warnings", Static).render()
        assert str(warnings) == "Ошибка [red]"
        app._set_status("Ошибка [red]")
        assert str(app.query_one("#status", Static).render()) == "Ошибка [red]"
        await pilot.press("tab")
        assert app.tab == "found"


@pytest.mark.asyncio
async def test_corrupt_reload_blocks_mutation_until_repaired(tmp_path: Path, monkeypatch):
    from textual.widgets import Static
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    catalog = Catalog(tmp_path / "profiles.yaml")
    snapshot = catalog.save([Profile("one", "one.example")], None)
    app = SshAutoApp(catalog, snapshot, [], [])
    async with app.run_test() as pilot:
        catalog.path.write_text("version: true\nprofiles: []\n")
        await pilot.press("r")
        assert app.catalog_error
        await pilot.press("a")
        assert app.screen is app._screen_stack[0]
        assert "Каталог повреждён" in str(app.query_one("#status", Static).render())
        catalog.path.write_text("version: 1\nprofiles: []\n")
        await pilot.press("r")
        assert app.catalog_error is None
        await pilot.press("a")
        assert app.screen.query_one("#profile-name")
        await pilot.click("#cancel")


@pytest.mark.asyncio
async def test_stale_save_preserves_newer_catalog_and_candidate_returns(tmp_path: Path):
    from textual.widgets import Static
    catalog = Catalog(tmp_path / "profiles.yaml")
    snapshot = catalog.load()
    candidate = Candidate("host.example", (), frozenset({"fish"}), 1)
    app = SshAutoApp(catalog, snapshot, [candidate], [])
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.click("#save")
        assert app.tab == "saved"
        await pilot.press("tab")
        assert app._displayed == []
        await pilot.press("tab")
        catalog.save([Profile("other", "other.example")], app.snapshot.revision)
        await pilot.press("d")
        await pilot.click("#confirm")
        assert "Не удалено" in str(app.query_one("#status", Static).render())
        assert [p.name for p in catalog.load().profiles] == ["other"]
