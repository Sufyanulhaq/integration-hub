import pytest
from pydantic import ValidationError

from app import mapping
from app.events import OrderEvent
from app.main import order_body


def make(**over):
    return {**order_body("ORD-1"), **over}


def test_a_valid_order_is_accepted_and_the_currency_is_upper_cased():
    assert OrderEvent.model_validate(make(currency="gbp")).currency == "GBP"


@pytest.mark.parametrize("over, fragment", [
    ({"total": 999.0}, "does not match the items"),
    ({"items": []}, "at least 1"),
    ({"items": [{"sku": "A", "name": "B", "qty": 0, "unit_price": 1}], "total": 0}, "greater than 0"),
    ({"customer": {"name": "A", "email": "not-an-email"}}, "pattern"),
    ({"id": "bad id with spaces"}, "pattern"),
    ({"type": "order.deleted"}, "order.created"),
    ({"currency": "POUNDS"}, "at most 3"),
])
def test_bad_orders_are_refused_with_a_reason(over, fragment):
    with pytest.raises(ValidationError) as e:
        OrderEvent.model_validate(make(**over))
    assert fragment in str(e.value)


def test_a_total_off_by_a_rounding_cent_is_accepted():
    assert OrderEvent.model_validate(make(total=362.004))


def test_each_system_gets_the_fields_it_expects(order):
    crm = mapping.to_crm(order)
    assert (crm["first_name"], crm["last_name"], crm["last_order_id"]) == ("Priya", "Nair", "ORD-1")
    acc = mapping.to_accounting(order)
    assert acc["status"] == "draft" and acc["lines"][1] == {"code": "CBL-02", "description": "Cable kit", "quantity": 2, "unit_amount": 6.5}
    assert mapping.to_slack(order)["text"] == "New order ORD-1: 3 items, 362.00 GBP, from Priya Nair"
    assert mapping.to_sheet(order)["row"] == ["ORD-1", "Priya Nair", "priya.nair@example.com", 3, 362.0, "GBP"]


def test_a_single_name_and_a_single_item_read_correctly():
    one = OrderEvent.model_validate(make(customer={"name": "Cher", "email": "c@x.co"}, items=[{"sku": "A", "name": "B", "qty": 1, "unit_price": 5}], total=5))
    assert mapping.to_crm(one)["last_name"] is None and mapping.to_slack(one)["text"].startswith("New order ORD-1: 1 item,")
