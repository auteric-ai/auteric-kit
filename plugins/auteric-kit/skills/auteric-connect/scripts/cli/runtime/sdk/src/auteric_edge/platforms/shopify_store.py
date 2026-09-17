"""Local secret cart aliases; keep this file on a private persistent volume."""

import os
import sqlite3
from pathlib import Path
from uuid import uuid4


class ShopifyStore:
    def __init__(self, path, shop):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.path, self.shop = str(path), shop
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS shopify_aliases (shop TEXT, alias TEXT, remote TEXT, "
                "PRIMARY KEY(shop,alias), UNIQUE(shop,remote))"
            )

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def remember(self, remote):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO shopify_aliases VALUES (?,?,?)", (self.shop, "cart_" + uuid4().hex, remote)
            )
            return db.execute(
                "SELECT alias FROM shopify_aliases WHERE shop=? AND remote=?", (self.shop, remote)
            ).fetchone()[0]

    def resolve(self, alias):
        with self.connect() as db:
            row = db.execute(
                "SELECT remote FROM shopify_aliases WHERE shop=? AND alias=?", (self.shop, alias)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown local Shopify cart; restore the connector's persistent state")
        return row[0]
