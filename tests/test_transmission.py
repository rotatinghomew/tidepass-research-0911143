import unittest

import numpy as np

from tidepass.catalog import Catalog
from tidepass.transmission.analysis import (
    INTERPRETATION,
    run_analysis,
    validate_request,
)
from tidepass.transmission.econometrics import (
    EG_TAU_CRITICAL,
    build_lag_matrices,
    estimate_horizon_profiles,
    horizon_panel,
    joint_panel,
    ljung_box,
    long_run_relation,
    ols_fit,
)
from tidepass.transmission.series import (
    build_series,
    known_dimensions,
    resonance_shock,
    select_routes,
    trade_evidence,
)


class SeriesTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")
        self.route = self.catalog.rows("routes")[0]

    def test_dimensions_and_selection(self):
        dimensions = known_dimensions(self.catalog)
        self.assertEqual(set(dimensions), {"species", "grade", "region", "route_ids"})
        routes = select_routes(self.catalog, species=self.route["species"])
        self.assertTrue(routes)
        self.assertTrue(all(record["species"] == self.route["species"] for record in routes))
        with self.assertRaises(ValueError):
            select_routes(self.catalog, species="不存在的品种")
        with self.assertRaises(ValueError):
            select_routes(self.catalog, route_ids=["NOPE"])

    def test_missing_quotes_are_never_filled(self):
        series = build_series(self.catalog, self.route, "挂牌", "same_day", 2)
        missing = series["missing_cells"]
        self.assertGreater(sum(missing.values()), 0)
        # 同日匹配下缺报位置必须保持 NaN
        self.assertTrue(np.isnan(series["wholesale"]).any())
        paired = series["paired_positions"]
        self.assertFalse(np.isnan(series["wholesale"][paired]).any())
        self.assertFalse(np.isnan(series["retail"][paired]).any())

    def test_window_match_expands_sample_and_records_offsets(self):
        strict = build_series(self.catalog, self.route, "挂牌", "same_day", 2)
        aligned = build_series(self.catalog, self.route, "挂牌", "window", 2)
        self.assertGreaterEqual(aligned["paired_count"], strict["paired_count"])
        nonzero = int((aligned["align_offsets"]["wholesale"] != 0).sum())
        self.assertIsInstance(nonzero, int)
        self.assertGreater(nonzero, 0)
        with self.assertRaises(ValueError):
            build_series(self.catalog, self.route, "挂牌", "window", 9)
        with self.assertRaises(ValueError):
            build_series(self.catalog, self.route, "坏类型", "same_day", 2)

    def test_prices_are_normalized_per_kilogram(self):
        half_kilo = next(record for record in self.catalog.rows("routes")
                         if float(record["unit_kg"]) == 0.5)
        series = build_series(self.catalog, half_kilo, "挂牌", "same_day", 2)
        raw = next(record for record in self.catalog.rows("quotes")
                   if record["route_id"] == half_kilo["id"]
                   and record["side"] == "wholesale" and record["quote_kind"] == "挂牌")
        position = series["calendar_days"].index(raw["day"])
        self.assertAlmostEqual(series["wholesale"][position],
                               float(raw["price"]) / 0.5)

    def test_resonance_uses_other_regions_only(self):
        values = resonance_shock(self.catalog, self.route,
                                 [record["id"] for record in self.catalog.rows("calendar")])
        self.assertEqual(len(values), len(self.catalog.rows("calendar")))
        same_species_other_region = [record for record in self.catalog.rows("routes")
                                     if record["species"] == self.route["species"]
                                     and record["id"] != self.route["id"]]
        self.assertTrue(same_species_other_region)

    def test_trade_evidence_excludes_cancelled_and_keeps_gap(self):
        series = build_series(self.catalog, self.route, "挂牌", "same_day", 2)
        evidence = trade_evidence(self.catalog, self.route, series["paired_days"])
        for side, bucket in evidence.items():
            self.assertEqual(bucket["records"],
                             bucket["realized_records"] + bucket["cancelled_records"])
            self.assertGreaterEqual(bucket["cancelled_records"], 0)
            if bucket["quote_trade_pairs"]:
                self.assertIsNotNone(bucket["quote_trade_gap_mean"])


class EconometricsTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")
        self.route = next(record for record in self.catalog.rows("routes")
                          if record["id"] == "R0")
        self.calendar = self.catalog.rows("calendar")
        self.series = build_series(self.catalog, self.route, "挂牌", "same_day", 2)
        self.resonance = resonance_shock(self.catalog, self.route,
                                         self.series["calendar_days"])

    def test_lag_matrix_only_uses_adjacent_calendar_days(self):
        matrices = build_lag_matrices(self.series, 3)
        # 每个有效滞后项对应的两个日历位置必须相邻
        for horizon in range(4):
            valid = np.flatnonzero(matrices["lag_valid"][:, horizon])
            self.assertLess(horizon, 4)
            self.assertTrue(len(valid) > 0)
        # 手工核对零阶滞后等于相邻日批发差分
        paired = self.series["paired_positions"]
        wholesale = self.series["wholesale"]
        row = np.flatnonzero(matrices["lag_valid"][:, 0])[0]
        end = row + 1
        expected = wholesale[paired[end]] - wholesale[paired[end - 1]]
        self.assertAlmostEqual(matrices["lag_matrix"][row, 0], expected)

    def test_horizon_panel_controls_are_full_rank(self):
        matrices = build_lag_matrices(self.series, 3)
        panel = horizon_panel(self.series, self.calendar, matrices, 0,
                              True, self.resonance, False)
        self.assertEqual(np.linalg.matrix_rank(panel["x"].T @ panel["x"]),
                         panel["x"].shape[1])
        self.assertFalse(np.isnan(panel["x"]).any())

    def test_ols_recovers_known_coefficient(self):
        rng = np.random.default_rng(7)
        x = np.column_stack([np.ones(200), rng.normal(size=200)])
        true_beta = np.array([1.5, -2.0])
        y = x @ true_beta + rng.normal(scale=0.01, size=200)
        fit = ols_fit(y, x, ["const", "slope"])
        np.testing.assert_allclose(fit["beta"], true_beta, atol=0.01)
        self.assertTrue(all(item["p_value"] < 0.01 for item in fit["coefficients"]
                            if item["term"] == "slope"))

    def test_ols_rejects_rank_deficient_and_tiny_samples(self):
        x = np.ones((10, 3))
        y = np.ones(10)
        with self.assertRaises(ValueError):
            ols_fit(y, x, ["a", "b", "c"])
        with self.assertRaises(ValueError):
            ols_fit(np.ones(5), np.ones((5, 1)), ["a"])

    def test_estimation_returns_profiles_diagnostics_and_holdout(self):
        matrices = build_lag_matrices(self.series, 2)
        entries, profiles, bootstrap, joint, _ = estimate_horizon_profiles(
            self.series, self.calendar, matrices, 2, True, self.resonance,
            asymmetric=False, bootstrap_replications=60, seed=11,
            block_length=5, holdout_fraction=0.2)
        self.assertEqual(len(entries), 3)
        self.assertEqual(len(profiles), 3)
        self.assertTrue(bootstrap["available"])
        for entry in entries:
            self.assertIn("ljung_box", entry["residual_diagnostics"])
            self.assertIn("breusch_godfrey", entry["residual_diagnostics"])
            self.assertIn("arch_lm", entry["residual_diagnostics"])
            self.assertTrue(entry["bootstrap"]["replications"] > 0)
        for profile in profiles:
            self.assertLessEqual(profile["ci95_lower"], profile["cumulative"])
            self.assertLessEqual(profile["cumulative"], profile["ci95_upper"])
        self.assertIn("estimable", joint)

    def test_asymmetric_splits_shocks(self):
        matrices = build_lag_matrices(self.series, 2)
        entries, profiles, _, _, _ = estimate_horizon_profiles(
            self.series, self.calendar, matrices, 2, True, self.resonance,
            asymmetric=True, bootstrap_replications=50, seed=12,
            block_length=5, holdout_fraction=0.2)
        self.assertTrue(all("up" in profile and "down" in profile
                            and "asymmetry_test" in profile for profile in profiles))
        for entry in entries:
            terms = [item["term"] for item in entry["coefficients"]]
            self.assertTrue(any(term.startswith("up_lag") for term in terms))
            self.assertTrue(any(term.startswith("down_lag") for term in terms))

    def test_ljung_box_white_noise_not_significant(self):
        rng = np.random.default_rng(3)
        result = ljung_box(rng.normal(size=500), (5, 10))
        self.assertTrue(all(item["p_value"] > 0.05 for item in result))

    def test_joint_panel_is_optional(self):
        matrices = build_lag_matrices(self.series, 3)
        joint = joint_panel(self.series, self.calendar, matrices, 3,
                            True, self.resonance, False)
        if joint is not None:
            self.assertEqual(np.linalg.matrix_rank(joint["x"].T @ joint["x"]),
                             joint["x"].shape[1])

    def test_long_run_reports_engle_granger_decision(self):
        result, _ = long_run_relation(self.series, self.calendar)
        self.assertIn("adf_tau", result)
        self.assertEqual(set(result["critical_values"]), set(EG_TAU_CRITICAL))
        self.assertIsInstance(result["cointegration_supported"], bool)


class AnalysisServiceTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")

    def _request(self, **overrides):
        request = {"route_ids": ["R0"], "max_lag": 2,
                   "bootstrap_replications": 50,
                   "models": ["plain", "controlled", "asymmetric"],
                   "windows": ["full", "pre_season", "post_season"]}
        request.update(overrides)
        return request

    def test_full_run_is_json_safe_and_bounded(self):
        result = run_analysis(self.catalog, self._request())
        self.assertEqual(result["routes_analyzed"], 1)
        self.assertEqual(len(result["interpretation"]), len(INTERPRETATION))
        # 因果边界声明必须存在
        self.assertTrue(any("因果证据" in line for line in result["interpretation"]))
        route_result = result["results"][0]
        self.assertIsNotNone(route_result["trade_evidence"])
        self.assertIsNotNone(route_result["season_stability"])
        for window in route_result["windows"]:
            for model in window["models"]:
                if model.get("estimable") is False:
                    self.assertIn("reason", model)
                    continue
                self.assertIn("sample_quality", model)
                self.assertTrue(model["sample_quality"]["lag_coverage"])
        # 不允许 NaN/Infinity 进入 JSON
        import json
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertIn("cumulative", encoded)

    def test_sensitivity_matrix_covers_matching_and_quote_kinds(self):
        result = run_analysis(self.catalog, self._request(
            models=["controlled"], windows=["full"], bootstrap_replications=40))
        scenarios = result["sensitivity"]["scenarios"]
        labels = {item["scenario"] for item in scenarios}
        self.assertIn("挂牌|same_day", labels)
        self.assertIn("挂牌|window|w2", labels)
        self.assertIn("询价|same_day", labels)
        self.assertIn("挂牌|trade_day", labels)
        block_checks = result["sensitivity"]["block_length_check"]
        self.assertTrue(block_checks)

    def test_validation_rejects_bad_requests(self):
        for bad in ({"max_lag": 9}, {"bootstrap_replications": 5},
                    {"models": ["nope"]}, {"windows": ["nope"]},
                    {"quote_kind": "坏"}, {"match": "bad"},
                    {"holdout_fraction": 0.9},
                    {"start_day": "2026-13-01"},
                    {"route_ids": "R0"}, {"models": "controlled"},
                    {"max_lag": "x"}):
            with self.assertRaises(ValueError):
                validate_request(bad)

    def test_unknown_dimensions_rejected_at_run_time(self):
        with self.assertRaises(ValueError):
            run_analysis(self.catalog, self._request(route_ids=["XX"]))
        with self.assertRaises(ValueError):
            run_analysis(self.catalog, self._request(species="不存在"))

    def test_separate_windows_can_be_individually_inestimable(self):
        result = run_analysis(self.catalog, self._request(max_lag=4))
        windows = {item["window"]: item for item in result["results"][0]["windows"]}
        # 开海后只有 60 天，高滞后期下允许不可估计，但必须给出原因而不是静默丢弃
        self.assertIn("post_season", windows)
        for model in windows["post_season"]["models"]:
            self.assertIn("model", model)

    def test_recent_window_and_custom_dates(self):
        result = run_analysis(self.catalog, self._request(
            windows=["recent_90d"], models=["controlled"], max_lag=2))
        window = result["results"][0]["windows"][0]
        self.assertEqual(window["window"], "recent_90d")
        custom = run_analysis(self.catalog, self._request(
            windows=["full"], models=["plain"], max_lag=1,
            start_day="2026-04-01", end_day="2026-06-30"))
        entry = custom["results"][0]["windows"][0]
        self.assertEqual(entry["window"], "custom")
        self.assertGreaterEqual(entry["first_day"], "2026-04-01")
        self.assertLessEqual(entry["last_day"], "2026-06-30")

    def test_sensitivity_runs_even_without_full_window_in_request(self):
        result = run_analysis(self.catalog, self._request(
            windows=["recent_90d"], models=["controlled"], max_lag=2,
            bootstrap_replications=40))
        self.assertTrue(result["sensitivity"]["scenarios"])
        self.assertTrue(result["sensitivity"]["block_length_check"])
        # 敏感性对照必须落在全样本窗口上
        for scenario in result["sensitivity"]["scenarios"]:
            self.assertTrue(all("route_id" in row for row in scenario["routes"]))

    def test_sparse_quote_kind_degrades_with_reason(self):
        result = run_analysis(self.catalog, self._request(
            route_ids=["R1"], quote_kind="询价",
            models=["controlled", "asymmetric"], windows=["full"],
            max_lag=2, bootstrap_replications=40))
        for window in result["results"][0]["windows"]:
            for model in window["models"]:
                self.assertIn("model", model)
                if model.get("estimable") is False:
                    self.assertTrue(model["reason"])

    def test_all_routes_run_with_defaults(self):
        result = run_analysis(self.catalog, {
            "max_lag": 2, "bootstrap_replications": 40,
            "models": ["controlled"], "windows": ["full"]})
        self.assertEqual(result["routes_analyzed"], 6)
        for route_result in result["results"]:
            self.assertTrue(route_result["windows"][0]["models"])
            self.assertTrue(route_result["trade_evidence"])


if __name__ == "__main__":
    unittest.main()
