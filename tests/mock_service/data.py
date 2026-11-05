"""Invented sample data for the mock service. Nothing here refers to a real person or service."""

from __future__ import annotations

import copy

USERNAME = "testuser"
PASSWORD = "testpass"  # test fixture only; the mock service runs on localhost

INVOICES: list[dict] = [
    {
        "id": f"inv-{n:03d}",
        "date": f"2026-0{(n % 9) + 1}-15",
        "amount": 1000 + n * 250,
        "status": "paid" if n % 2 else "open",
        "owner": {"name": "Testkund Ett", "national_id": "ID-TEST-0001", "address": "Exempelgatan 1"},
    }
    for n in range(1, 6)
]


def fresh_invoices() -> list[dict]:
    return copy.deepcopy(INVOICES)
