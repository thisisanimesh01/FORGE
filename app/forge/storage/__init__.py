"""FORGE storage package: SQLite database and persistent store."""

from typing import Any
from forge.storage.database import get_db_connection, init_database, SCHEMA_VERSION

__all__ = [
    "get_db_connection",
    "init_database",
    "SCHEMA_VERSION",
    "ForgeStore",
    "StoreError",
]


def __getattr__(name: str) -> Any:
    if name in ("ForgeStore", "StoreError"):
        from forge.storage.store import ForgeStore, StoreError
        return {"ForgeStore": ForgeStore, "StoreError": StoreError}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
