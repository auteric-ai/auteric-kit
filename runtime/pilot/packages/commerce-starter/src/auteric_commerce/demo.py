import sqlite3
from contextlib import closing
from pathlib import Path

from .domain import DomainError, Variant


class Demo:
    """Persistent synthetic catalog. Never uses Shopify or an LLM."""
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS variants(id TEXT PRIMARY KEY, price TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO variants VALUES('demo-variant-1','100.00')")
            db.execute("INSERT OR IGNORE INTO variants VALUES('summer-shoes','100.00')")

    def get(self, variant_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT price FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not row:
            raise DomainError("Demo variant not found", 404)
        if variant_id == 'summer-shoes':
            return Variant(id=variant_id, product_id="summer-shoes-product", title="Summer Shoes",
                           sku="SUMMER-SHOES-01", price=row[0], currency="USD", stock=4200)
        return Variant(id=variant_id, product_id="demo-product-1", title="Demo Bag", sku="DEMO-BAG-1", price=row[0], currency="USD", stock=10)

    def set_price(self, variant, new_price):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE variants SET price=? WHERE id=?", (format(new_price, '.2f'), variant.id))
