from __future__ import annotations

from .app import SshAutoApp
from .discovery import discover
from .storage import Catalog, CatalogError, CatalogSnapshot, catalog_path


def main() -> None:
    path = catalog_path()
    catalog = Catalog(path)
    try:
        snapshot = catalog.load()
        catalog_error = None
    except CatalogError as exc:
        snapshot = CatalogSnapshot((), None)
        catalog_error = str(exc)
    candidates, warnings = discover()
    app = SshAutoApp(catalog, snapshot, candidates, warnings, catalog_error)
    app.run()
