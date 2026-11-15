"""批零传导分析的编排：窗口、模型族、留时段检验与敏感性比较。"""
from datetime import date

import numpy as np

from .econometrics import (
    MIN_OBSERVATIONS,
    build_lag_matrices,
    error_correction_model,
    estimate_horizon_profiles,
    lag_coverage_report,
    long_run_relation,
    season_stability,
)
from .series import (
    MATCH_MODES,
    QUOTE_KINDS,
    build_series,
    resonance_shock,
    select_routes,
    series_overview,
    trade_evidence,
)

MODEL_KINDS = ("plain", "controlled", "asymmetric")
WINDOW_PRESETS = ("full", "pre_season", "post_season", "recent_90d")

INTERPRETATION = [
    "估计量为观察资料下的批零统计关联，批发报价与零售报价不是同一口径，不能解释为零售商加价的因果证据。",
    "报价不保证成交；缺报只表示当日未观测，分析未对缺报位置做前后填充或零涨跌假设。",
    "控制模型已纳入线性趋势、星期与月份效应、节假日及同品种跨片区批发共振，共同走低本身不构成传导证据。",
    "Engle–Granger 检验不支持长期关系时，仅报告有边界的短期累计关联；仅在 10% 弱水平通过时，ECM 标注为参考性结果，不作为长期均衡的确定证据。",
    "累计关联的自助区间使用移动块重抽样以保留残差序列相关；区间覆盖零即表示该滞后期关联不具统计辨识度。",
    "季节是运营标记而非估计出的断点；换季稳定性由分段估计、交互项 Wald 检验与递归 CUSUM 共同判断。",
]


def parse_iso_day(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError("日期需为 YYYY-MM-DD 格式") from error


def validate_request(payload):
    """校验分析请求并补齐默认值。"""
    options = dict(payload or {})

    def integer(field, default, low, high, message):
        try:
            value = int(options.get(field, default))
        except (TypeError, ValueError) as error:
            raise ValueError(field + " 需为整数") from error
        if not low <= value <= high:
            raise ValueError(message)
        return value

    def number(field, default, low, high, message):
        try:
            value = float(options.get(field, default))
        except (TypeError, ValueError) as error:
            raise ValueError(field + " 需为数值") from error
        if not low <= value <= high:
            raise ValueError(message)
        return value

    max_lag = integer("max_lag", 4, 0, 7, "滞后期需在零至七天之间")
    holdout_fraction = number("holdout_fraction", 0.2, 0.1, 0.4,
                              "留时段比例需在 0.1 至 0.4 之间")
    replications = integer("bootstrap_replications", 300, 40, 2000,
                           "自助重抽次数需在四十至两千之间")
    block_length = integer("block_length", 7, 2, 21, "移动块长度需在二至二十一天之间")
    seed = integer("seed", 20260926, -10**12, 10**12, "随机种子超出允许范围")
    window = integer("window", 2, 1, 7, "窗口匹配半径需在一至七天之间")
    quote_kind = options.get("quote_kind", "挂牌")
    if quote_kind not in (*QUOTE_KINDS, "any"):
        raise ValueError("报价类型需为挂牌、询价或 any")
    match = options.get("match", "same_day")
    if match not in MATCH_MODES:
        raise ValueError("未知匹配方式：" + match)
    models = options.get("models") or list(MODEL_KINDS)
    if not isinstance(models, list) or not all(isinstance(item, str) for item in models) \
            or not set(models) <= set(MODEL_KINDS) or not models:
        raise ValueError("模型类型需取自 plain、controlled、asymmetric")
    presets = options.get("windows") or ["full"]
    if not isinstance(presets, list) or not all(isinstance(item, str) for item in presets) \
            or not set(presets) <= set(WINDOW_PRESETS) or not presets:
        raise ValueError("时间窗口需取自 full、pre_season、post_season、recent_90d")
    for field in ("species", "grade", "region"):
        if options.get(field) is not None and not isinstance(options[field], str):
            raise ValueError(field + " 需为文本")
    route_ids = options.get("route_ids")
    if route_ids is not None:
        if not isinstance(route_ids, list) or not all(isinstance(item, str) for item in route_ids):
            raise ValueError("route_ids 需为文本数组")
    return {
        "max_lag": max_lag, "holdout_fraction": holdout_fraction,
        "bootstrap_replications": replications, "block_length": block_length,
        "seed": seed, "quote_kind": quote_kind, "match": match,
        "window": window, "models": list(dict.fromkeys(models)),
        "windows": list(dict.fromkeys(presets)),
        "species": options.get("species"), "grade": options.get("grade"),
        "region": options.get("region"), "route_ids": route_ids,
        "start_day": parse_iso_day(options["start_day"]) if options.get("start_day") else None,
        "end_day": parse_iso_day(options["end_day"]) if options.get("end_day") else None,
    }


def _window_positions(series, calendar_rows, preset, start_day, end_day):
    paired = series["paired_positions"]
    if not len(paired):
        return paired, preset
    if start_day or end_day:
        kept = []
        for position in paired:
            day = calendar_rows[position]["id"]
            if start_day and day < start_day:
                continue
            if end_day and day > end_day:
                continue
            kept.append(position)
        return np.asarray(kept, dtype=int), "custom"
    if preset == "full":
        return paired, preset
    labels = {"pre_season": "开海前", "post_season": "开海后"}
    if preset in labels:
        kept = [position for position in paired
                if calendar_rows[position]["season"] == labels[preset]]
        return np.asarray(kept, dtype=int), preset
    if preset == "recent_90d":
        last_day = calendar_rows[paired[-1]]["id"]
        cutoff = (date.fromisoformat(last_day).toordinal() - 89)
        kept = [position for position in paired
                if date.fromisoformat(calendar_rows[position]["id"]).toordinal() >= cutoff]
        return np.asarray(kept, dtype=int), preset
    raise ValueError("未知时间窗口：" + preset)


def _fit_model(kind, series, calendar_rows, max_lag, resonance, positions, request):
    asymmetric = kind == "asymmetric"
    include_controls = kind in ("controlled", "asymmetric")
    matrices = build_lag_matrices(series, max_lag, positions)
    entries, profiles, bootstrap_summary, joint_result, _ = estimate_horizon_profiles(
        series, calendar_rows, matrices, max_lag, include_controls, resonance,
        asymmetric, request["bootstrap_replications"], request["seed"],
        request["block_length"], request["holdout_fraction"])
    result = {
        "model": kind,
        "observations": entries[0]["observations"],
        "cov_kind": "HC3",
        "horizon_models": entries,
        "joint_distributed_lag": joint_result,
        "sample_quality": {
            "candidate_targets": matrices["n_targets"],
            "targets_dropped_for_gap": int((~matrices["target_valid"]).sum()),
            "lag_coverage": lag_coverage_report(matrices),
        },
        "bootstrap_cumulative": bootstrap_summary,
    }
    if asymmetric:
        result["up_cumulative"] = [item["up"] for item in profiles]
        result["down_cumulative"] = [item["down"] for item in profiles]
        result["asymmetry_tests"] = [item["asymmetry_test"] for item in profiles]
    else:
        result["cumulative"] = profiles
    return result


def analyze_route(catalog, route_record, request):
    calendar_rows = catalog.rows("calendar")
    series = build_series(catalog, route_record, quote_kind=request["quote_kind"],
                          match=request["match"], window=request["window"])
    resonance = resonance_shock(catalog, route_record, series["calendar_days"])
    max_lag = request["max_lag"]

    window_results = []
    stability = None
    long_run = None
    ecm = None
    for preset in request["windows"]:
        positions, label = _window_positions(series, calendar_rows, preset,
                                             request["start_day"], request["end_day"])
        entry = {"window": label, "paired_observations": int(len(positions)),
                 "first_day": series["calendar_days"][positions[0]] if len(positions) else None,
                 "last_day": series["calendar_days"][positions[-1]] if len(positions) else None,
                 "models": [], "warnings": []}
        if len(positions) < MIN_OBSERVATIONS:
            entry["warnings"].append(f"窗口内配对观测 {len(positions)} 个，少于最低要求 {MIN_OBSERVATIONS}")
            window_results.append(entry)
            continue
        for kind in request["models"]:
            try:
                model_result = _fit_model(
                    kind, series, calendar_rows, max_lag, resonance, positions, request)
                entry["models"].append(model_result)
            except ValueError as error:
                entry["models"].append({"model": kind, "estimable": False, "reason": str(error)})
        window_results.append(entry)

    full_positions, _ = _window_positions(series, calendar_rows, "full", None, None)
    if len(full_positions) >= MIN_OBSERVATIONS:
        try:
            long_run, level_fit = long_run_relation(series, calendar_rows, full_positions)
            if long_run["cointegration_supported"]:
                ecm = error_correction_model(series, level_fit, calendar_rows, max_lag,
                                             include_controls=True, resonance=resonance,
                                             positions=full_positions)
                ecm = {"ecm_term": ecm["ecm_term"], "half_life_days": ecm["half_life_days"],
                       "r_squared": ecm["fit"]["r_squared"],
                       "residual_std": ecm["fit"]["residual_std"],
                       "strong_long_run_evidence": long_run["strong_evidence"],
                       "interpretation": (
                           "长期关系仅获弱证据，ECM 仅供参考，采购判断应以短期有边界关联为主"
                           if not long_run["strong_evidence"]
                           else "长期关系在常规显著性水平上获得支持，ECM 修正项可解读为回到长期水平的速度")}
        except ValueError as error:
            long_run = {"estimable": False, "reason": str(error)}
        try:
            matrices = build_lag_matrices(series, max_lag, full_positions)
            stability = season_stability(series, calendar_rows, matrices,
                                         include_controls=True, resonance=resonance,
                                         asymmetric=False)
        except ValueError as error:
            stability = {"estimable": False, "reason": str(error)}
    return {
        "route": {field: route_record[field] for field in
                  ("id", "species", "grade", "region", "wholesale_market",
                   "retail_market", "unit_kg")},
        "series": series_overview(catalog, series),
        "windows": window_results,
        "long_run": long_run,
        "error_correction": ecm,
        "season_stability": stability,
        "trade_evidence": trade_evidence(catalog, route_record, series["paired_days"]),
    }


def sensitivity_matrix(catalog, request):
    """结论对样本匹配、报价口径与自助块长的敏感性；使用较少重抽次数。"""
    routes = select_routes(catalog, request["species"], request["grade"],
                           request["region"], request["route_ids"])
    scenarios = []
    base_seed = request["seed"]
    alternatives = [
        {"quote_kind": "挂牌", "match": "same_day"},
        {"quote_kind": "挂牌", "match": "window", "window": 2},
        {"quote_kind": "挂牌", "match": "window", "window": 4},
        {"quote_kind": "询价", "match": "same_day"},
        {"quote_kind": "any", "match": "same_day"},
        {"quote_kind": "挂牌", "match": "trade_day"},
    ]
    block_alternatives = [5, 10]
    for scenario_index, alternative in enumerate(alternatives):
        scenario_request = dict(request)
        scenario_request.update(alternative)
        scenario_request["models"] = ["controlled"]
        scenario_request["windows"] = ["full"]
        # 敏感性对照统一在全样本上进行，避免继承自定义日期窗口
        scenario_request["start_day"] = None
        scenario_request["end_day"] = None
        scenario_request["bootstrap_replications"] = max(40, request["bootstrap_replications"] // 2)
        scenario_request["seed"] = base_seed + scenario_index + 1
        label = f"{alternative['quote_kind']}|{alternative['match']}" \
                + (f"|w{alternative['window']}" if "window" in alternative else "")
        per_route = []
        for route_record in routes:
            try:
                outcome = analyze_route(catalog, route_record, scenario_request)
                full_window = next((item for item in outcome["windows"]
                                    if item["window"] == "full"), None)
                if full_window is None:
                    per_route.append({"route_id": route_record["id"], "estimable": False,
                                      "reason": "敏感性分析需要 full 窗口"})
                    continue
                model = next((item for item in full_window["models"]
                              if item.get("model") == "controlled"), None)
                if model and model.get("cumulative"):
                    total = model["cumulative"][-1]
                    boot = None
                    if model["bootstrap_cumulative"].get("available"):
                        boot = model["bootstrap_cumulative"]["profiles"][-1]
                    per_route.append({
                        "route_id": route_record["id"],
                        "paired_observations": full_window["paired_observations"],
                        "horizon_zero_observations": model["observations"],
                        "total_cumulative": total["cumulative"],
                        "analytical_ci95_lower": total["ci95_lower"],
                        "analytical_ci95_upper": total["ci95_upper"],
                        "bootstrap_ci95_lower": boot["bootstrap_ci95_lower"] if boot else None,
                        "bootstrap_ci95_upper": boot["bootstrap_ci95_upper"] if boot else None,
                        "p_value": total["p_value"],
                        "estimable": True,
                    })
                else:
                    per_route.append({"route_id": route_record["id"], "estimable": False,
                                      "reason": (model or {}).get("reason", "样本不足")})
            except ValueError as error:
                per_route.append({"route_id": route_record["id"], "estimable": False,
                                  "reason": str(error)})
        scenarios.append({"scenario": label, "settings": alternative, "routes": per_route})

    block_check = []
    block_request = dict(request)
    block_request["models"] = ["controlled"]
    block_request["windows"] = ["full"]
    block_request["start_day"] = None
    block_request["end_day"] = None
    for block_length in block_alternatives:
        block_request["block_length"] = block_length
        block_request["seed"] = base_seed + 100 + block_length
        for route_record in routes:
            try:
                outcome = analyze_route(catalog, route_record, block_request)
                full_window = next((item for item in outcome["windows"]
                                    if item["window"] == "full"), None)
                if full_window is None:
                    block_check.append({"route_id": route_record["id"],
                                        "block_length": block_length,
                                        "estimable": False,
                                        "reason": "敏感性分析需要 full 窗口"})
                    continue
                model = next(item for item in full_window["models"]
                             if item.get("model") == "controlled")
                if model.get("estimable") is False:
                    block_check.append({"route_id": route_record["id"],
                                        "block_length": block_length,
                                        "estimable": False,
                                        "reason": model.get("reason", "样本不足")})
                    continue
                summary = model["bootstrap_cumulative"]
                profile = summary["profiles"][-1] if summary.get("available") else None
                block_check.append({
                    "route_id": route_record["id"],
                    "block_length": block_length,
                    "replications": summary.get("replications"),
                    "bootstrap_ci95": None if profile is None else {
                        "lower": profile["bootstrap_ci95_lower"],
                        "upper": profile["bootstrap_ci95_upper"]}})
            except ValueError as error:
                block_check.append({"route_id": route_record["id"],
                                    "block_length": block_length,
                                    "estimable": False, "reason": str(error)})
    return {"scenarios": scenarios, "block_length_check": block_check}


def run_analysis(catalog, payload):
    request = validate_request(payload)
    routes = select_routes(catalog, request["species"], request["grade"],
                           request["region"], request["route_ids"])
    results = [analyze_route(catalog, route_record, request) for route_record in routes]
    return {
        "source_fingerprint": catalog.fingerprint,
        "request": request,
        "routes_analyzed": len(results),
        "results": results,
        "sensitivity": sensitivity_matrix(catalog, request),
        "interpretation": INTERPRETATION,
    }
