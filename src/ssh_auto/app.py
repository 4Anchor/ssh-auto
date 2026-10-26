from __future__ import annotations

import subprocess
import shlex
from datetime import datetime, timezone

from rich.text import Text

from textual.app import App, ComposeResult, SuspendNotSupported
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.binding import Binding
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from .discovery import discover
from .ssh_parser import validate_options, validate_destination
from .models import Candidate, Profile, ssh_argv
from .storage import Catalog, CatalogConflict, CatalogError, CatalogSnapshot


class ProfileModal(ModalScreen[tuple[str, str, str] | None]):
    def __init__(self, profile: Profile | None = None, candidate: Candidate | None = None, profiles: tuple[Profile, ...] = ()):
        super().__init__()
        self.profile = profile
        self.candidate = candidate
        self.profiles = profiles

    def compose(self) -> ComposeResult:
        title = "Редактировать профиль" if self.profile else "Добавить профиль"
        name = self.profile.name if self.profile else (self.candidate.destination if self.candidate else "")
        destination = self.profile.destination if self.profile else (self.candidate.destination if self.candidate else "")
        options = self.profile.options if self.profile else (self.candidate.options if self.candidate else ())
        yield Container(
            Label(title, id="modal-title"),
            Label("Имя"), Input(value=name, id="profile-name"),
            Label("Destination"), Input(value=destination, id="profile-destination"),
            Label("SSH options"), Input(value=shlex.join(options), id="profile-options"),
            Static("", id="form-error", markup=False),
            Horizontal(Button("Сохранить", id="save"), Button("Отмена", id="cancel")),
            id="profile-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        try:
            name = self.query_one("#profile-name", Input).value.strip()
            destination = validate_destination(self.query_one("#profile-destination", Input).value)
            options = validate_options(self.query_one("#profile-options", Input).value)
            if not name:
                raise ValueError("Имя не должно быть пустым")
            if any(existing.name.casefold() == name.casefold() and existing is not self.profile for existing in self.profiles):
                raise ValueError("Имя уже используется")
        except ValueError as exc:
            self.query_one("#form-error", Static).update(str(exc))
            return
        self.dismiss((name, destination, shlex.join(options)))


class ConfirmModal(ModalScreen[bool]):
    def compose(self) -> ComposeResult:
        yield Container(
            Label("Удалить выбранный профиль?"),
            Horizontal(Button("Удалить", id="confirm"), Button("Отмена", id="cancel")),
            id="confirm-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

class SearchInput(Input):
    can_focus = False

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.app.query_one("#table", DataTable).focus()


class HelpModal(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        yield Container(
            Label("Клавиши"),
            Static("a добавить · s сохранить найденное · e редактировать · d удалить\nEnter запустить · r обновить · Tab вкладка · / поиск · q выход"),
            Button("Закрыть", id="close"),
            id="help-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class SshAutoApp(App[None]):
    TITLE = "ssh-auto"
    CSS = """
    Screen { layout: vertical; }
    #search { margin: 0 1; }
    #tabs { height: 3; align: center middle; }
    #tabs Button { margin: 0 1; }
    #content { height: 1fr; margin: 0 1; }
    #status { height: auto; min-height: 2; margin: 1 1; color: $text; }
    #empty { height: auto; margin: 0 1; }
    #warnings { height: auto; margin: 0 1; color: $warning; }
    #profile-dialog, #confirm-dialog, #help-dialog { width: 70; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    #profile-dialog Input { margin: 1 0; }
    #form-error { color: $error; height: 2; }
    """
    BINDINGS = [
        Binding("q", "quit", "Выйти"),
        Binding("a", "add", "Добавить"),
        Binding("s", "save_candidate", "Сохранить найденное"),
        Binding("e", "edit", "Изменить"),
        Binding("d", "delete", "Удалить"),
        Binding("r", "reload", "Обновить"),
        Binding("tab", "toggle_tab", "Вкладка"),
        Binding("/", "focus_search", "Поиск"),
        Binding("?", "help", "Помощь"),
        Binding("enter", "launch", "Запустить"),
    ]

    def __init__(self, catalog: Catalog, snapshot: CatalogSnapshot, candidates: list[Candidate], warnings: list[str], catalog_error: str | None = None):
        super().__init__()
        self.catalog = catalog
        self.snapshot = snapshot
        self.profiles = list(snapshot.profiles)
        self.candidates = candidates
        self.warnings = warnings
        self.catalog_error = catalog_error
        self.tab = "saved" if self.profiles else "found"
        self.search_text = ""
        self._displayed: list[Profile | Candidate] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield SearchInput(placeholder="Поиск по отображаемым полям", id="search")
        yield Horizontal(Button("Сохранённые", id="saved-tab"), Button("Найденные", id="found-tab"), id="tabs")
        yield DataTable(id="table", cursor_type="row")
        yield Static("", id="empty", markup=False)
        yield Static("", id="status", markup=False)
        yield Static("", id="warnings", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("Имя / Destination", "Options", "Источники", "Использований", "Последнее использование")
        self._render()
        search = self.query_one("#search", SearchInput)
        search.can_focus = True
        self.call_after_refresh(table.focus)
        if self.catalog_error:
            self._set_status(f"Каталог повреждён: {self.catalog_error}. Нажмите r после исправления файла.")

    def _mutations_allowed(self) -> bool:
        if self.catalog_error:
            self._set_status(f"Каталог повреждён: {self.catalog_error}. Нажмите r после исправления файла.")
            return False
        return True

    def _render(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        self._displayed = []
        needle = self.search_text.casefold()
        saved_keys = {(item.destination, item.options) for item in self.profiles}
        if self.tab == "saved":
            values: list[Profile | Candidate] = sorted(self.profiles, key=lambda p: (-(p.last_used_at.timestamp() if p.last_used_at else float("-inf")), p.name.casefold()))
            for profile in values:
                options = shlex.join(profile.options)
                fields = (profile.name, profile.destination, options, profile.last_used_at.isoformat() if profile.last_used_at else "")
                if needle and not any(needle in field.casefold() for field in fields):
                    continue
                table.add_row(*(Text(value, end="") for value in (f"{profile.name} — {profile.destination}", options, "", "", _date(profile.last_used_at))))
                self._displayed.append(profile)
        else:
            values = [candidate for candidate in self.candidates if (candidate.destination, candidate.options) not in saved_keys]
            for candidate in values:
                options = shlex.join(candidate.options)
                sources = ",".join(sorted(candidate.sources))
                fields = (candidate.destination, options, sources, str(candidate.use_count), candidate.last_seen_at.isoformat() if candidate.last_seen_at else "")
                if needle and not any(needle in field.casefold() for field in fields):
                    continue
                table.add_row(*(Text(value, end="") for value in (candidate.destination, options, sources, str(candidate.use_count), _date(candidate.last_seen_at))))
                self._displayed.append(candidate)
        self.query_one("#empty", Static).update(
            ("Нет сохранённых профилей. Нажмите a для добавления или r для обновления истории."
             if self.tab == "saved" else "Ничего не найдено. Нажмите a для добавления профиля или r для обновления истории.")
            if not self._displayed else ""
        )
        warning_text = "\n".join(self.warnings) if self.warnings else "Предупреждений нет."
        self.query_one("#warnings", Static).update(warning_text)
        self._update_tabs()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "table":
            self.action_launch()

    def _update_tabs(self) -> None:
        self.query_one("#saved-tab", Button).variant = "primary" if self.tab == "saved" else "default"
        self.query_one("#found-tab", Button).variant = "primary" if self.tab == "found" else "default"

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _selected(self) -> Profile | Candidate | None:
        table = self.query_one("#table", DataTable)
        if 0 <= table.cursor_row < len(self._displayed):
            return self._displayed[table.cursor_row]
        return None

    def action_focus_search(self) -> None:
        self.query_one("#search", SearchInput).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.search_text = event.value
            self._render()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.query_one("#table", DataTable).focus()

    def action_toggle_tab(self) -> None:
        self.tab = "found" if self.tab == "saved" else "saved"
        self._render()
        self.query_one("#table", DataTable).focus()

    def action_focus_next(self) -> None:
        if isinstance(self.screen, ModalScreen):
            super().action_focus_next()
        else:
            self.action_toggle_tab()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "saved-tab":
            self.tab = "saved"
            self._render()
        elif event.button.id == "found-tab":
            self.tab = "found"
            self._render()

    def action_add(self) -> None:
        if self._mutations_allowed():
            self.push_screen(ProfileModal(profiles=tuple(self.profiles)), self._profile_submitted)

    def action_save_candidate(self) -> None:
        if not self._mutations_allowed():
            return
        selected = self._selected()
        if self.tab == "found" and isinstance(selected, Candidate):
            self.push_screen(ProfileModal(candidate=selected, profiles=tuple(self.profiles)), self._profile_submitted)

    def action_edit(self) -> None:
        if not self._mutations_allowed():
            return
        selected = self._selected()
        if self.tab == "saved" and isinstance(selected, Profile):
            self.push_screen(ProfileModal(profile=selected, profiles=tuple(self.profiles)), lambda result: self._profile_submitted(result, selected))

    def action_delete(self) -> None:
        if not self._mutations_allowed():
            return
        selected = self._selected()
        if self.tab == "saved" and isinstance(selected, Profile):
            self.push_screen(ConfirmModal(), lambda confirmed: self._delete_confirmed(confirmed, selected))

    def _profile_submitted(self, result: tuple[str, str, str] | None, editing: Profile | None = None) -> None:
        if result is None or not self._mutations_allowed():
            return
        name, destination, options_text = result
        try:
            options = validate_options(options_text)
            if any(profile.name.casefold() == name.casefold() and profile is not editing for profile in self.profiles):
                raise ValueError("Имя уже используется")
            replacement = Profile(name, destination, options, editing.last_used_at if editing else None)
            profiles = [replacement if profile is editing else profile for profile in self.profiles] if editing else [*self.profiles, replacement]
            self.snapshot = self.catalog.save(profiles, self.snapshot.revision)
            self.profiles = list(self.snapshot.profiles)
            self.tab = "saved"
            self._set_status("Профиль сохранён.")
            self._render()
        except (CatalogConflict, CatalogError, ValueError) as exc:
            self._set_status(f"Не сохранено: {exc}. Нажмите r для перечитывания.")

    def _delete_confirmed(self, confirmed: bool, selected: Profile) -> None:
        if not confirmed or not self._mutations_allowed():
            return
        try:
            self.snapshot = self.catalog.save([profile for profile in self.profiles if profile is not selected], self.snapshot.revision)
            self.profiles = list(self.snapshot.profiles)
            self._set_status("Профиль удалён.")
            self._render()
        except (CatalogConflict, CatalogError) as exc:
            self._set_status(f"Не удалено: {exc}. Нажмите r для перечитывания.")

    def action_reload(self) -> None:
        try:
            self.snapshot = self.catalog.load()
            self.catalog_error = None
            self.profiles = list(self.snapshot.profiles)
            self.candidates, self.warnings = discover()
            self._set_status("Каталог и источники обновлены.")
            self._render()
        except (CatalogError, OSError) as exc:
            self.catalog_error = str(exc)
            self._set_status(f"Ошибка обновления: {exc}. Нажмите r после исправления файла.")

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_launch(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        destination = selected.destination
        options = selected.options
        command = ssh_argv(destination, options)
        try:
            with self.suspend():
                result = subprocess.run(command, check=False)
        except OSError as exc:
            self._set_status(f"Ошибка запуска ssh: {exc}")
            return
        except SuspendNotSupported:
            self._set_status("Терминал не поддерживает временную передачу управления ssh.")
            return
        status = f"ssh завершился с кодом {result.returncode}." if result.returncode else "ssh завершён."
        if isinstance(selected, Profile):
            updated = Profile(selected.name, selected.destination, selected.options, datetime.now(timezone.utc))
            try:
                self.snapshot = self.catalog.save([updated if profile is selected else profile for profile in self.profiles], self.snapshot.revision)
                self.profiles = list(self.snapshot.profiles)
            except (CatalogConflict, CatalogError) as exc:
                status += f" Время использования не сохранено: {exc}. Нажмите r."
        self._render()
        self._set_status(status)


def _date(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if value else "—"
