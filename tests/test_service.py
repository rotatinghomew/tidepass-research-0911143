import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tidepass.catalog import Catalog, DataError
from tidepass.service import Application, make_server
from tidepass.domain import overview


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.application = Application("data", self.root / "state")

    def test_tables_have_unique_ids(self):
        for name, schema in self.application.catalog.manifest.items():
            identifiers = [record[schema["key"]] for record in self.application.catalog.rows(name)]
            self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_copy_does_not_mutate_source(self):
        name = next(iter(self.application.catalog.tables))
        records = self.application.catalog.rows(name)
        records[0].clear()
        self.assertTrue(self.application.catalog.rows(name)[0])

    def test_fingerprint_tracks_content(self):
        original = self.application.catalog
        shutil.copytree("data", self.root / "data")
        name = next(iter(original.tables))
        target = self.root / "data" / (name + ".csv")
        target.write_bytes(target.read_bytes() + b"\n")
        changed = Catalog(self.root / "data")
        self.assertNotEqual(original.fingerprint, changed.fingerprint)

    def test_invalid_number_rejected(self):
        with self.assertRaises(ValueError):
            Catalog.convert("nan", "number")
        with self.assertRaises(ValueError):
            Catalog.convert("Infinity", "money")

    def test_timestamp_requires_zone(self):
        with self.assertRaises(ValueError):
            Catalog.convert("2026-09-01T10:00:00", "time")

    def test_empty_and_zero_differ(self):
        self.assertIsNone(Catalog.convert("", "number?"))
        self.assertEqual(Catalog.convert("0", "number?"), 0)

    def test_pagination_and_missing_table(self):
        name = next(iter(self.application.catalog.tables))
        status, result = self.application.dispatch("GET", "/tables/" + name + "?offset=1&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(result["rows"], self.application.catalog.rows(name)[1:3])
        self.assertEqual(self.application.dispatch("GET", "/tables/absent")[0], 404)

    def test_invalid_pagination(self):
        name = next(iter(self.application.catalog.tables))
        self.assertEqual(self.application.dispatch("GET", "/tables/" + name + "?limit=0")[0], 400)

    def test_case_round_trip(self):
        status, created = self.application.dispatch("POST", "/cases", {"label": "经营讨论", "options": {"period": "2026-09"}})
        self.assertEqual(status, 201)
        self.assertEqual(self.application.dispatch("GET", "/cases/" + created["id"])[1], created)
        self.assertEqual(created["source_fingerprint"], self.application.catalog.fingerprint)
        self.assertTrue(created["tables"])

    def test_case_rejects_unknown_fields(self):
        self.assertEqual(self.application.dispatch("POST", "/cases", {"label": "讨论", "extra": 2})[0], 400)
        self.assertEqual(self.application.dispatch("POST", "/cases", {"label": ""})[0], 400)

    def test_case_integrity(self):
        record = self.application.cases.create(self.application.catalog, "复核")
        target = self.root / "state" / (record["id"] + ".json")
        envelope = json.loads(target.read_text())
        envelope["content"]["label"] = "被改动"
        target.write_text(json.dumps(envelope))
        with self.assertRaises(ValueError):
            self.application.cases.read(record["id"])

    def test_case_path_is_not_filename(self):
        self.assertEqual(self.application.dispatch("GET", "/cases/../schema.json")[0], 404)

    def test_current_view(self):
        status, result = self.application.dispatch("GET", "/view")
        self.assertEqual(status, 200)
        self.assertTrue(result["view"])
        self.assertEqual(result["view"], overview(self.application.catalog))

    def test_profiles_separate_missing_values(self):
        status, result = self.application.dispatch("GET", "/profiles")
        self.assertEqual(status, 200)
        for name, fields in result["profiles"].items():
            for values in fields.values():
                self.assertEqual(values["observed"] + values["missing"], len(self.application.catalog.rows(name)))

    def test_real_http(self):
        server = make_server(self.application, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urlopen(f"http://127.0.0.1:{server.server_port}/view", timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.load(response)["view"])

    def test_bad_json_over_http(self):
        server = make_server(self.application, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = Request(f"http://127.0.0.1:{server.server_port}/cases", data=b"{", method="POST")
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 400)

    def test_transmission_dimensions_endpoint(self):
        status, result = self.application.dispatch("GET", "/transmission/dimensions")
        self.assertEqual(status, 200)
        self.assertEqual(result["source_fingerprint"], self.application.catalog.fingerprint)
        self.assertIn("带鱼", result["dimensions"]["species"])

    def test_transmission_series_endpoint_and_validation(self):
        status, result = self.application.dispatch(
            "GET", "/transmission/series?route_id=R0&match=window&window=2&evidence=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["series"]), 1)
        self.assertTrue(result["series"][0]["trade_evidence"])
        self.assertEqual(self.application.dispatch(
            "GET", "/transmission/series?match=unknown")[0], 400)
        self.assertEqual(self.application.dispatch(
            "GET", "/transmission/series?window=9")[0], 400)
        self.assertEqual(self.application.dispatch(
            "GET", "/transmission/series?route_id=NOPE")[0], 400)

    def test_transmission_analyze_endpoint(self):
        payload = {"route_ids": ["R0"], "max_lag": 2, "bootstrap_replications": 40,
                   "models": ["controlled"], "windows": ["full"]}
        status, result = self.application.dispatch("POST", "/transmission/analyze", payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["routes_analyzed"], 1)
        self.assertTrue(result["interpretation"])
        model = result["results"][0]["windows"][0]["models"][0]
        self.assertEqual(model["model"], "controlled")
        self.assertTrue(model["cumulative"])
        self.assertEqual(self.application.dispatch(
            "POST", "/transmission/analyze", {"max_lag": 99})[0], 400)
        self.assertEqual(self.application.dispatch(
            "POST", "/transmission/analyze", {"species": "不存在"})[0], 400)
        self.assertEqual(self.application.dispatch(
            "POST", "/transmission/analyze", ["not", "object"])[0], 400)

    def test_transmission_analyze_over_http(self):
        server = make_server(self.application, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        payload = json.dumps({"route_ids": ["R0"], "max_lag": 1,
                              "bootstrap_replications": 40}).encode()
        request = Request(f"http://127.0.0.1:{server.server_port}/transmission/analyze",
                          data=payload, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            result = json.load(response)
        self.assertEqual(result["routes_analyzed"], 1)


if __name__ == "__main__":
    unittest.main()
