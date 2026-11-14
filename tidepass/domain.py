from collections import defaultdict
from decimal import Decimal


def observed_trade_totals(catalog):
    totals = defaultdict(lambda: {"quantity_kg": Decimal(0), "amount": Decimal(0)})
    for record in catalog.rows("trades"):
        if record["cancelled"]:
            continue
        group = record["side"]
        quantity = Decimal(str(record["quantity_kg"]))
        totals[group]["quantity_kg"] += quantity
        totals[group]["amount"] += quantity * Decimal(record["unit_price_kg"])
    return {group: {key: str(value) for key, value in values.items()} for group, values in totals.items()}


def overview(catalog):
    return {"线路数": len(catalog.rows("routes")), "报价数": len(catalog.rows("quotes")), "已成交口径": observed_trade_totals(catalog)}
