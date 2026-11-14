import unittest
from tidepass.catalog import Catalog
from tidepass.domain import observed_trade_totals


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")

    def test_cancelled_trades_are_excluded(self):
        totals = observed_trade_totals(self.catalog)
        self.assertGreater(float(totals["retail"]["quantity_kg"]), 0)
        self.assertLess(sum(float(value["quantity_kg"]) for value in totals.values()), sum(record["quantity_kg"] for record in self.catalog.rows("trades")))


    def test_decimal_volume_weighted_amount(self):
        result = observed_trade_totals(FakeCatalog({"trades": [
            {"side": "retail", "cancelled": False, "quantity_kg": .1, "unit_price_kg": "0.20"},
            {"side": "retail", "cancelled": False, "quantity_kg": .2, "unit_price_kg": "0.30"}]}))
        self.assertEqual(result["retail"]["quantity_kg"], "0.3")
        self.assertEqual(result["retail"]["amount"], "0.080")

    def test_cancelled_only_does_not_create_zero_market(self):
        result = observed_trade_totals(FakeCatalog({"trades": [
            {"side": "wholesale", "cancelled": True, "quantity_kg": 100, "unit_price_kg": "2"}]}))
        self.assertEqual(result, {})

    def test_separate_sides(self):
        result = observed_trade_totals(FakeCatalog({"trades": [
            {"side": side, "cancelled": False, "quantity_kg": 1, "unit_price_kg": "2"} for side in ("wholesale", "retail")]}))
        self.assertEqual(set(result), {"wholesale", "retail"})

    def test_empty_trade_stream(self):
        self.assertEqual(observed_trade_totals(FakeCatalog({"trades": []})), {})


class FakeCatalog:
    def __init__(self, tables):
        self.tables = tables

    def rows(self, name):
        return self.tables[name]
