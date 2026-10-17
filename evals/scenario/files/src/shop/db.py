"""Access to the shop's SQLite database (data/shop.db)."""

import logging
import sqlite3
from pathlib import Path

DATABASE = Path(__file__).resolve().parents[2] / "data" / "shop.db"
log = logging.getLogger("shop.db")


def connect(path: Path = DATABASE) -> sqlite3.Connection:
    log.info("opening %s", path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection
