"""Turns one order into the payload each system expects. Each system names things differently, so
this is where the integration work actually lives."""
from __future__ import annotations

from .events import OrderEvent


def to_crm(o: OrderEvent) -> dict:
    first, _, last = o.customer.name.partition(" ")
    return {"email": o.customer.email, "first_name": first, "last_name": last or None,
            "last_order_id": o.id, "lifetime_value_delta": o.total, "currency": o.currency}


def to_accounting(o: OrderEvent) -> dict:
    return {"reference": o.id, "contact": {"name": o.customer.name, "email": o.customer.email}, "currency": o.currency,
            "lines": [{"code": i.sku, "description": i.name, "quantity": i.qty, "unit_amount": i.unit_price} for i in o.items],
            "status": "draft"}


def to_slack(o: OrderEvent) -> dict:
    count = sum(i.qty for i in o.items)
    return {"text": f"New order {o.id}: {count} item{'s' if count != 1 else ''}, {o.total:,.2f} {o.currency}, from {o.customer.name}"}


def to_sheet(o: OrderEvent) -> dict:
    return {"row": [o.id, o.customer.name, o.customer.email, sum(i.qty for i in o.items), o.total, o.currency]}


MAPPERS = {"crm": to_crm, "accounting": to_accounting, "slack": to_slack, "sheets": to_sheet}
