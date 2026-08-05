"""Read-only indexed access to the Olist CSV files."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any


class OlistRepository:
    """Loads only the CSV tables required by the dispute policy."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.orders: dict[str, dict[str, str]] = {}
        self.items_by_order: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.payments_by_order: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.seller_ids: set[str] = set()

    def load(self) -> None:
        self.orders = {
            row["order_id"]: row
            for row in self._read_csv("olist_orders_dataset.csv")
        }
        self.items_by_order = defaultdict(list)
        for row in self._read_csv("olist_order_items_dataset.csv"):
            self.items_by_order[row["order_id"]].append(row)

        self.payments_by_order = defaultdict(list)
        for row in self._read_csv("olist_order_payments_dataset.csv"):
            self.payments_by_order[row["order_id"]].append(row)

        self.seller_ids = {
            row["seller_id"]
            for row in self._read_csv("olist_sellers_dataset.csv")
        }

    def _read_csv(self, filename: str) -> list[dict[str, str]]:
        path = self.data_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Required dataset is missing: {path}")
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            return list(csv.DictReader(file))

    def order(self, order_id: str) -> dict[str, str] | None:
        return self.orders.get(order_id)

    def items(self, order_id: str) -> list[dict[str, str]]:
        return list(self.items_by_order.get(order_id, []))

    def payments(self, order_id: str) -> list[dict[str, str]]:
        return list(self.payments_by_order.get(order_id, []))

    def has_seller(self, seller_id: str) -> bool:
        return seller_id in self.seller_ids

    def summary(self) -> dict[str, Any]:
        return {
            "orders": len(self.orders),
            "order_item_orders": len(self.items_by_order),
            "payment_orders": len(self.payments_by_order),
            "sellers": len(self.seller_ids),
        }
