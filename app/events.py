"""The shape of an order event, checked at the door. Bad data is refused with a reason instead of
being passed on to four other systems."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Customer(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Item(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    qty: int = Field(gt=0, le=1000)
    unit_price: float = Field(ge=0)


class OrderEvent(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    type: Literal["order.created"]
    customer: Customer
    items: list[Item] = Field(min_length=1, max_length=100)
    currency: str = Field(min_length=3, max_length=3)
    total: float = Field(ge=0)

    @field_validator("currency")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def total_matches_items(self):
        expected = round(sum(i.qty * i.unit_price for i in self.items), 2)
        if abs(expected - self.total) > 0.01:
            raise ValueError(f"total {self.total} does not match the items, which add up to {expected}")
        return self
