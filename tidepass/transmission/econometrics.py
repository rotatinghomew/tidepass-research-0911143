"""批零传导的统计估计。

模型族全部基于一阶差分或误差修正形式，避免把共同趋势与季节性误判为传导：

- 对称分布滞后模型：Δretail_t = c + Σ_h β_h Δwholesale_{t-h} + 控制变量 + ε_t
- 非对称模型：批发冲击拆为上涨/下跌两部分，分别累计并检验差异
- Engle–Granger 两步法检验水平长期关系，不成立时只报告有边界的短期关联
- 误差修正模型（ECM）仅在长期关系有证据时作为补充
- 移动块自助法给出累计关联的不确定性区间，保留序列相关结构
- 留时段检验、残差序列相关/ARCH/正态性诊断、换季交互项 Wald 检验与 CUSUM

所有量均为观察口径的关联度量，不是因果处理效应。
"""
import math

import numpy as np
from scipy import stats

# Engle–Granger tau 统计量（含常数项、无趋势设定）的小样本临界值近似。
# 来源：MacKinnon (1991, 2010) 响应面在常见样本量下的取值，仅用于本地虚构资料。
EG_TAU_CRITICAL = {0.10: -3.04, 0.05: -3.34, 0.025: -3.60, 0.01: -3.90}
MIN_OBSERVATIONS = 24


def _clean_float(value):
    if value is None:
        return None
    value = float(value)
    return round(value, 6) if math.isfinite(value) else None


def ols_fit(y, x, names=None, hc3=True):
    """普通最小二乘，附带 HC3 异方差稳健协方差与完整残差诊断量。"""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    n, k = x.shape
    if n < max(k + 4, MIN_OBSERVATIONS):
        raise ValueError(f"有效样本不足：n={n}，参数 k={k}，至少需要 {max(k + 4, MIN_OBSERVATIONS)} 个观测")
    if names is None:
        names = [f"x{i}" for i in range(k)]
    xtx = x.T @ x
    if np.linalg.matrix_rank(xtx) < k:
        raise ValueError("设计矩阵列秩不足，无法识别全部参数")
    xtx_inv = np.linalg.inv(xtx)
    beta = xtx_inv @ x.T @ y
    fitted = x @ beta
    resid = y - fitted
    rss = float(resid @ resid)
    tss = float(((y - y.mean()) @ (y - y.mean())))
    sigma2 = rss / (n - k)
    classical_cov = sigma2 * xtx_inv
    if hc3:
        hat = np.einsum("ij,jk,ik->i", x, xtx_inv, x)
        weight = resid / np.maximum(1 - hat, 1e-12)
        meat = x.T @ (x * (weight ** 2)[:, None])
        cov = xtx_inv @ meat @ xtx_inv
        cov_kind = "HC3"
    else:
        cov = classical_cov
        cov_kind = "classical"
    se = np.sqrt(np.maximum(np.diag(cov), 0))
    t_values = beta / np.where(se > 0, se, np.nan)
    p_values = 2 * stats.t.sf(np.abs(t_values), df=max(n - k, 1))
    coefficients = []
    for name, estimate, std, t_value, p_value in zip(names, beta, se, t_values, p_values):
        coefficients.append({
            "term": name, "estimate": _clean_float(estimate),
            "std_error": _clean_float(std), "t": _clean_float(t_value),
            "p_value": _clean_float(p_value),
        })
    return {
        "beta": beta, "cov": cov, "resid": resid, "fitted": fitted,
        "sigma2": sigma2, "n": n, "k": k,
        "r_squared": _clean_float(1 - rss / tss) if tss > 0 else None,
        "adj_r_squared": _clean_float(1 - (1 - (1 - rss / tss)) * (n - 1) / (n - k)) if tss > 0 else None,
        "residual_std": _clean_float(math.sqrt(sigma2)),
        "cov_kind": cov_kind, "coefficients": coefficients,
    }


def _calendar_controls(calendar_rows, positions, resonance):
    """趋势、星期、节假日、月份固定效应与地区共振控制。"""
    weekday_dummies = np.zeros((len(positions), 6))
    holiday = np.zeros(len(positions))
    trend = np.zeros(len(positions))
    months = sorted({calendar_rows[position]["id"][:7] for position in positions})
    month_dummies = np.zeros((len(positions), max(len(months) - 1, 0)))
    for row, position in enumerate(positions):
        record = calendar_rows[position]
        weekday = int(record["weekday"])
        if weekday < 6:
            weekday_dummies[row, weekday] = 1
        holiday[row] = 1.0 if record["holiday"] else 0.0
        trend[row] = position
        month_index = months.index(record["id"][:7])
        if month_index > 0:
            month_dummies[row, month_index - 1] = 1
    columns = [trend, weekday_dummies, holiday[:, None]]
    names = ["trend"] + [f"weekday_{d}" for d in range(6)] + ["holiday"]
    if month_dummies.shape[1]:
        columns.append(month_dummies)
        names.extend(f"month_{month}" for month in months[1:])
    if resonance is not None:
        values = resonance[positions].astype(float)
        missing = np.isnan(values)
        # 共振缺测只表示其他片区当日未报价，不能丢样本；以零代入并加缺测指示
        values = np.where(missing, 0.0, values)
        columns.extend([values[:, None], missing.astype(float)[:, None]])
        names.extend(["regional_resonance", "regional_resonance_missing"])
    controls = np.column_stack(columns)
    if controls.shape[0] == 0:
        return controls, names
    # 常数列作为基准先行保留：样本内缺失的星期/月份类别会使哑变量之和等于 1，
    # 与常数项共线，逐列正交检查可自动剔除这类冗余列
    constant = np.ones((controls.shape[0], 1))
    basis = constant
    retained_names = ["const_basis"]
    retained_indices = []
    for index in range(controls.shape[1]):
        candidate = controls[:, index]
        if np.std(candidate, ddof=0) <= 1e-12:
            continue
        projection = basis @ np.linalg.lstsq(basis, candidate, rcond=None)[0]
        residual = candidate - projection
        if np.std(residual, ddof=0) / np.std(candidate, ddof=0) <= 1e-10:
            continue
        basis = np.column_stack([basis, candidate])
        retained_indices.append(index)
    return controls[:, retained_indices], [names[index] for index in retained_indices]


def build_lag_matrices(series, max_lag, positions=None):
    """构造目标零售差分与各阶批发滞后矩阵；缺报缺口保留为 NaN，不做填充。"""
    paired = series["paired_positions"] if positions is None else np.asarray(positions, dtype=int)
    wholesale = series["wholesale"]
    retail = series["retail"]
    dw = np.diff(wholesale[paired])
    dr = np.diff(retail[paired])
    if len(paired) <= max_lag + 2:
        raise ValueError("配对观测不足，无法在该滞后期下估计")

    n_targets = len(paired) - 1
    lag_matrix = np.full((n_targets, max_lag + 1), np.nan)
    lag_valid = np.zeros((n_targets, max_lag + 1), dtype=bool)
    target_valid = np.zeros(n_targets, dtype=bool)
    target_positions = np.full(n_targets, -1, dtype=int)
    for end in range(1, len(paired)):
        row = end - 1
        target_positions[row] = paired[end]
        if paired[end] - paired[end - 1] != 1:
            continue
        target_valid[row] = True
        for horizon in range(max_lag + 1):
            source = end - 1 - horizon
            if source < 0 or paired[source + 1] - paired[source] != 1:
                continue
            lag_matrix[row, horizon] = dw[source]
            lag_valid[row, horizon] = True
    return {
        "target_diff": dr, "lag_matrix": lag_matrix, "lag_valid": lag_valid,
        "target_valid": target_valid, "target_positions": target_positions,
        "n_targets": n_targets,
    }


def horizon_panel(series, calendar_rows, matrices, horizon, include_controls,
                  resonance, asymmetric):
    """单个滞后期的回归设计：仅使用该滞后项与目标差分均来自相邻日历日的观测。"""
    rows = matrices["target_valid"] & matrices["lag_valid"][:, horizon]
    positions = matrices["target_positions"][rows]
    y = matrices["target_diff"][rows]
    shock = matrices["lag_matrix"][rows, horizon]
    if len(y) < MIN_OBSERVATIONS:
        raise ValueError(f"滞后 {horizon} 期有效样本不足：n={len(y)}，至少需要 {MIN_OBSERVATIONS} 个观测")
    if asymmetric:
        x_core = np.column_stack([np.maximum(shock, 0), np.minimum(shock, 0)])
        core_names = [f"up_lag{horizon}", f"down_lag{horizon}"]
    else:
        x_core = shock[:, None]
        core_names = [f"wholesale_lag{horizon}"]
    design = [np.ones(len(y)), x_core]
    names = ["const", *core_names]
    if include_controls:
        controls, control_names = _calendar_controls(calendar_rows, positions, resonance)
        design.append(controls)
        names.extend(control_names)
    return {
        "y": y, "x": np.column_stack(design), "names": names,
        "horizon": horizon, "asymmetric": asymmetric, "positions": positions,
        "days": [series["calendar_days"][position] for position in positions],
        "observations": int(len(y)),
    }


def joint_panel(series, calendar_rows, matrices, max_lag, include_controls,
                resonance, asymmetric):
    """完整联合分布滞后面板（完整行删除）；秩不足或样本不足时返回 None。"""
    rows = matrices["target_valid"] & matrices["lag_valid"].all(axis=1)
    positions = matrices["target_positions"][rows]
    y = matrices["target_diff"][rows]
    x_core = matrices["lag_matrix"][rows]
    if len(y) < MIN_OBSERVATIONS:
        return None
    if asymmetric:
        x_core = np.column_stack([np.maximum(x_core, 0), np.minimum(x_core, 0)])
        core_names = [f"up_lag{h}" for h in range(max_lag + 1)] \
            + [f"down_lag{h}" for h in range(max_lag + 1)]
    else:
        core_names = [f"wholesale_lag{h}" for h in range(max_lag + 1)]
    design = [np.ones(len(y)), x_core]
    names = ["const", *core_names]
    if include_controls:
        controls, control_names = _calendar_controls(calendar_rows, positions, resonance)
        design.append(controls)
        names.extend(control_names)
    x = np.column_stack(design)
    if np.linalg.matrix_rank(x.T @ x) < x.shape[1] or x.shape[0] < x.shape[1] + 4:
        return None
    return {"y": y, "x": x, "names": names, "max_lag": max_lag,
            "asymmetric": asymmetric, "core_count": x_core.shape[1],
            "positions": positions,
            "days": [series["calendar_days"][position] for position in positions]}


def lag_coverage_report(matrices):
    report = []
    for horizon in range(matrices["lag_matrix"].shape[1]):
        available = int(matrices["lag_valid"][:, horizon].sum())
        report.append({
            "horizon": horizon, "available": available,
            "coverage": round(available / matrices["n_targets"], 4)
            if matrices["n_targets"] else None})
    return report


def estimate_horizon_profiles(series, calendar_rows, matrices, max_lag, include_controls,
                              resonance, asymmetric, bootstrap_replications, seed,
                              block_length, holdout_fraction):
    """逐滞后期分别估计分布滞后关联。

    每个滞后期在该期实际可用的相邻日样本上估计，避免完整行删除造成的样本塌缩；
    累计关联为各期系数之和，解析标准误按独立估计的保守上界（方差和）计算，
    移动块自助法在每期面板上分别重抽样并对累计和取分位数。
    """
    horizon_fits = []
    for horizon in range(max_lag + 1):
        panel = horizon_panel(series, calendar_rows, matrices, horizon,
                              include_controls, resonance, asymmetric)
        fit = ols_fit(panel["y"], panel["x"], panel["names"])
        entry = {
            "horizon": horizon, "observations": panel["observations"],
            "coefficients": fit["coefficients"], "r_squared": fit["r_squared"],
            "residual_std": fit["residual_std"], "cov_kind": fit["cov_kind"],
            "residual_diagnostics": residual_diagnostics(panel, fit),
        }
        try:
            entry["holdout"] = holdout_check(panel, holdout_fraction)
        except ValueError as error:
            entry["holdout"] = {"available": False, "reason": str(error)}
        bootstrap = moving_block_bootstrap(
            panel, replications=bootstrap_replications, seed=seed + horizon + 1,
            block_length=block_length)
        # 原始抽样矩阵只供累计区间组装，不进入接口响应
        entry["bootstrap"] = {key: value for key, value in bootstrap.items()
                              if key != "draws"}
        horizon_fits.append({"panel": panel, "fit": fit, "entry": entry,
                             "bootstrap": bootstrap})

    profiles = []
    for cutoff in range(max_lag + 1):
        if asymmetric:
            profiles.append(_asymmetric_cumulative(horizon_fits[:cutoff + 1], cutoff))
        else:
            profiles.append(_symmetric_cumulative(horizon_fits[:cutoff + 1], cutoff))
    bootstrap_summary = cumulative_bootstrap_intervals(horizon_fits, asymmetric)
    entries = [item["entry"] for item in horizon_fits]
    joint = joint_panel(series, calendar_rows, matrices, max_lag, include_controls,
                        resonance, asymmetric)
    joint_result = None
    if joint is not None:
        try:
            joint_fit = ols_fit(joint["y"], joint["x"], joint["names"])
            joint_result = {
                "estimable": True, "observations": joint_fit["n"],
                "r_squared": joint_fit["r_squared"],
                "residual_std": joint_fit["residual_std"],
                "coefficients": joint_fit["coefficients"],
                "residual_diagnostics": residual_diagnostics(joint, joint_fit),
            }
        except ValueError as error:
            joint_result = {"estimable": False, "reason": str(error)}
    else:
        joint_result = {"estimable": False,
                        "reason": "完整行删除后样本不足或设计矩阵列秩不足，逐滞后期结果仍可使用"}
    return entries, profiles, bootstrap_summary, joint_result, horizon_fits


def _symmetric_cumulative(horizon_fits, cutoff):
    estimates, variances = [], []
    for item in horizon_fits:
        index = item["panel"]["names"].index(f"wholesale_lag{item['panel']['horizon']}")
        estimates.append(item["fit"]["beta"][index])
        variances.append(item["fit"]["cov"][index, index])
    value = float(np.sum(estimates))
    # 不同滞后期在重叠样本上估计，真实协方差未知；零协方差假设给出方差和的保守表达，
    # 正式不确定性以移动块自助区间为准
    std = float(np.sqrt(np.sum(variances)))
    dof = min(item["fit"]["n"] - item["fit"]["k"] for item in horizon_fits)
    return _cumulative_entry(cutoff, value, std, dof, "cumulative")


def _asymmetric_cumulative(horizon_fits, cutoff):
    output = {}
    for direction, prefix in (("up", "up_lag"), ("down", "down_lag")):
        estimates, variances = [], []
        for item in horizon_fits:
            horizon = item["panel"]["horizon"]
            index = item["panel"]["names"].index(f"{prefix}{horizon}")
            estimates.append(item["fit"]["beta"][index])
            variances.append(item["fit"]["cov"][index, index])
        value = float(np.sum(estimates))
        std = float(np.sqrt(np.sum(variances)))
        dof = min(item["fit"]["n"] - item["fit"]["k"] for item in horizon_fits)
        output[direction] = _cumulative_entry(cutoff, value, std, dof, direction)
    # 涨跌非对称检验：最后一期两个方向系数之差
    last = horizon_fits[-1]
    up_index = last["panel"]["names"].index(f"up_lag{last['panel']['horizon']}")
    down_index = last["panel"]["names"].index(f"down_lag{last['panel']['horizon']}")
    difference = float(last["fit"]["beta"][up_index] - last["fit"]["beta"][down_index])
    variance = float(last["fit"]["cov"][up_index, up_index]
                     + last["fit"]["cov"][down_index, down_index]
                     - 2 * last["fit"]["cov"][up_index, down_index])
    std_diff = math.sqrt(max(variance, 0))
    t_value = difference / std_diff if std_diff > 0 else float("nan")
    p_value = 2 * stats.t.sf(abs(t_value), last["fit"]["n"] - last["fit"]["k"]) \
        if std_diff > 0 else float("nan")
    output["asymmetry_test"] = {
        "horizon": cutoff, "up_minus_down": _clean_float(difference),
        "std_error": _clean_float(std_diff), "t": _clean_float(t_value),
        "p_value": _clean_float(p_value)}
    return output


def _cumulative_entry(horizon, value, std, dof, key):
    t_value = value / std if std > 0 else float("nan")
    p_value = 2 * stats.t.sf(abs(t_value), dof) if std > 0 else float("nan")
    return {
        "horizon": horizon, key: _clean_float(value),
        "std_error_conservative": _clean_float(std),
        "ci95_lower": _clean_float(value - stats.t.ppf(0.975, dof) * std),
        "ci95_upper": _clean_float(value + stats.t.ppf(0.975, dof) * std),
        "t": _clean_float(t_value), "p_value": _clean_float(p_value),
        "std_error_note": "解析标准误假设各滞后期估计不相关，作为保守参照；优先使用移动块自助区间",
    }


def moving_block_bootstrap(panel, replications, seed, block_length):
    """对单个滞后面板做移动块重抽样，返回该期系数的自助抽样矩阵与分位区间。"""
    rng = np.random.default_rng(seed)
    y, x = panel["y"], panel["x"]
    n = len(y)
    block_length = max(2, min(block_length, max(n // 3, 2)))
    block_count = int(math.ceil(n / block_length))
    starts = rng.integers(0, n - block_length + 1, size=(replications, block_count))
    coefficient_indices = [index for index, name in enumerate(panel["names"])
                           if "lag" in name]
    coefficient_names = [panel["names"][index] for index in coefficient_indices]
    draws = []
    for start_row in starts:
        index = np.concatenate([np.arange(start, start + block_length)
                                for start in start_row])[:n]
        # 重抽样本可能缺少个别类别，先在重抽设计矩阵内部剔除常数列再最小二乘估计，
        # 避免少量类别缺失误伤整个自助方案
        boot_x = x[index]
        varying = [0] + [column for column in range(1, boot_x.shape[1])
                         if np.std(boot_x[:, column], ddof=0) > 1e-12]
        boot_x = boot_x[:, varying]
        if np.linalg.matrix_rank(boot_x.T @ boot_x) < boot_x.shape[1]:
            continue
        beta, *_ = np.linalg.lstsq(boot_x, y[index], rcond=None)
        full_beta = np.full(x.shape[1], np.nan)
        full_beta[np.asarray(varying)] = beta
        draws.append(full_beta[coefficient_indices])
    if not draws:
        return {"replications": 0, "block_length": block_length,
                "horizon": panel["horizon"], "coefficient_names": coefficient_names,
                "draws": None, "intervals": None}
    sample = np.asarray(draws)
    intervals = []
    for column, name in enumerate(coefficient_names):
        lower, upper = np.percentile(sample[:, column], [2.5, 97.5])
        intervals.append({"term": name, "ci95_lower": _clean_float(lower),
                          "ci95_upper": _clean_float(upper),
                          "mean": _clean_float(float(np.mean(sample[:, column])))})
    return {"replications": len(draws), "block_length": block_length,
            "horizon": panel["horizon"], "coefficient_names": coefficient_names,
            "draws": sample, "intervals": intervals}


def cumulative_bootstrap_intervals(horizon_fits, asymmetric):
    """把各滞后期的自助抽样按相同重抽序号求和，得到累计关联的自助区间。

    各期面板的可用重抽次数可能不同（控制类别在重抽样本中缺失时会被跳过），
    按最小公共次数对齐，保证累计和使用同一批重抽序号。
    """
    draw_counts = {item["bootstrap"]["replications"] for item in horizon_fits}
    if not draw_counts or 0 in draw_counts:
        return {"available": False, "reason": "部分滞后期自助重抽失败"}
    common = min(draw_counts)
    profiles = []
    for cutoff in range(len(horizon_fits)):
        if asymmetric:
            entry = {"horizon": cutoff}
            for direction in ("up", "down"):
                sums = np.zeros(common)
                for item in horizon_fits[:cutoff + 1]:
                    bootstrap = item["bootstrap"]
                    column = bootstrap["coefficient_names"].index(
                        f"{direction}_lag{item['panel']['horizon']}")
                    sums += bootstrap["draws"][:common, column]
                entry[f"{direction}_bootstrap_ci95_lower"] = _clean_float(
                    float(np.percentile(sums, 2.5)))
                entry[f"{direction}_bootstrap_ci95_upper"] = _clean_float(
                    float(np.percentile(sums, 97.5)))
            profiles.append(entry)
        else:
            sums = np.zeros(common)
            for item in horizon_fits[:cutoff + 1]:
                bootstrap = item["bootstrap"]
                column = bootstrap["coefficient_names"].index(
                    f"wholesale_lag{item['panel']['horizon']}")
                sums += bootstrap["draws"][:common, column]
            profiles.append({
                "horizon": cutoff,
                "bootstrap_ci95_lower": _clean_float(float(np.percentile(sums, 2.5))),
                "bootstrap_ci95_upper": _clean_float(float(np.percentile(sums, 97.5)))})
    return {"available": True,
            "block_length": horizon_fits[0]["bootstrap"]["block_length"],
            "replications": common,
            "note": "各滞后期重抽次数不一致时按最小公共次数对齐",
            "profiles": profiles}


def ljung_box(resid, lags):
    n = len(resid)
    output = []
    for lag in lags:
        if lag >= n - 2:
            continue
        acf = np.array([np.corrcoef(resid[:n - h], resid[h:])[0, 1] for h in range(1, lag + 1)])
        statistic = n * (n + 2) * float(np.sum(acf ** 2 / (n - np.arange(1, lag + 1))))
        output.append({"lag": lag, "statistic": _clean_float(statistic),
                       "p_value": _clean_float(stats.chi2.sf(statistic, lag))})
    return output


def breusch_godfrey(panel, fit, lag):
    resid = fit["resid"]
    n = len(resid)
    x = panel["x"]
    lagged = np.column_stack([resid[lag - h:n - h] for h in range(1, lag + 1)])
    design = np.column_stack([x[lag:], lagged])
    try:
        auxiliary = ols_fit(resid[lag:], design, hc3=False)
    except ValueError:
        return None
    effective_n = n - lag
    statistic = effective_n * auxiliary["r_squared"]
    return {"lag": lag, "statistic": _clean_float(float(statistic)),
            "p_value": _clean_float(stats.chi2.sf(statistic, lag))}


def arch_lm(resid, lag):
    n = len(resid)
    squared = resid ** 2
    rows, targets = [], []
    for index in range(lag, n):
        rows.append(squared[index - lag:index][::-1])
        targets.append(squared[index])
    x = np.column_stack([np.ones(len(rows)), np.asarray(rows)])
    try:
        fit = ols_fit(np.asarray(targets), x, hc3=False)
    except ValueError:
        return None
    statistic = len(targets) * fit["r_squared"]
    return {"lag": lag, "statistic": _clean_float(statistic),
            "p_value": _clean_float(stats.chi2.sf(statistic, lag))}


def residual_diagnostics(panel, fit):
    resid = fit["resid"]
    jb_stat, jb_p = stats.jarque_bera(resid)
    return {
        "ljung_box": ljung_box(resid, (3, 7, 14)),
        "breusch_godfrey": [result for result in (breusch_godfrey(panel, fit, lag) for lag in (3, 7))
                            if result is not None],
        "arch_lm": [result for result in (arch_lm(resid, lag) for lag in (3, 7)) if result is not None],
        "jarque_bera": {"statistic": _clean_float(float(jb_stat)), "p_value": _clean_float(float(jb_p))},
        "residual_mean": _clean_float(float(np.mean(resid))),
        "residual_acf1": _clean_float(float(np.corrcoef(resid[:-1], resid[1:])[0, 1]))
        if len(resid) > 2 else None,
    }


def adf_on_residuals(resid, lag):
    """Engle–Granger 第二步：对协整回归残差做 ADF-tau 检验。"""
    differenced = np.diff(resid)
    rows, targets, levels = [], [], []
    for index in range(lag + 1, len(resid)):
        rows.append(differenced[index - lag - 1:index - 1][::-1])
        targets.append(differenced[index - 1])
        levels.append(resid[index - 1])
    x = np.column_stack([np.ones(len(rows)), np.asarray(levels), np.asarray(rows)])
    fit = ols_fit(np.asarray(targets), x, hc3=False)
    level_index = 1
    estimate = fit["beta"][level_index]
    std = np.sqrt(fit["cov"][level_index, level_index])
    tau = estimate / std if std > 0 else float("nan")
    significance = None
    for alpha in sorted(EG_TAU_CRITICAL):
        if tau <= EG_TAU_CRITICAL[alpha]:
            significance = alpha
    if significance is None:
        strength = "不支持长期关系"
    elif significance <= 0.05:
        strength = "常规水平（5% 或更强）支持长期关系"
    else:
        strength = "仅在 10% 弱水平上支持长期关系，证据有限需保守使用"
    return {
        "adf_tau": _clean_float(float(tau)),
        "adf_lag": lag,
        "critical_values": EG_TAU_CRITICAL,
        "reject_at": significance,
        "cointegration_supported": significance is not None,
        "strong_evidence": significance is not None and significance <= 0.05,
        "evidence_strength": strength,
        "note": "tau 为协整残差 ADF 统计量；临界值为 MacKinnon 响应面近似，"
                "10% 水平仅作弱证据，样本偏小时结论需保守",
    }


def long_run_relation(series, calendar_rows, positions=None):
    """水平协整回归（含趋势与月份固定效应）加 Engle–Granger 检验。"""
    paired = series["paired_positions"] if positions is None else np.asarray(positions, dtype=int)
    wholesale = series["wholesale"][paired]
    retail = series["retail"][paired]
    n = len(paired)
    months = sorted({calendar_rows[position]["id"][:7] for position in paired})
    trend = (paired - paired[0]).astype(float)
    month_matrix = np.zeros((n, max(len(months) - 1, 0)))
    for row, position in enumerate(paired):
        month_index = months.index(calendar_rows[position]["id"][:7])
        if month_index > 0 and month_index - 1 < month_matrix.shape[1]:
            month_matrix[row, month_index - 1] = 1
    x = np.column_stack([np.ones(n), wholesale, trend, month_matrix])
    names = ["const", "wholesale_level", "trend",
             *[f"month_{month}" for month in months[1:]]]
    fit = ols_fit(retail, x, names)
    adf = adf_on_residuals(fit["resid"], lag=1)
    level_index = names.index("wholesale_level")
    adf["long_run_slope"] = fit["coefficients"][level_index]["estimate"]
    adf["long_run_slope_se"] = fit["coefficients"][level_index]["std_error"]
    adf["level_r_squared"] = fit["r_squared"]
    return adf, fit


def error_correction_model(series, level_fit, calendar_rows, max_lag, include_controls,
                           resonance=None, positions=None):
    """长期关系有证据时估计 ECM；否则不估计，避免把伪回归包装成长期结论。"""
    paired = series["paired_positions"] if positions is None else np.asarray(positions, dtype=int)
    ec = level_fit["resid"]
    wholesale = series["wholesale"]
    retail = series["retail"]
    dw = np.diff(wholesale[paired])
    dr = np.diff(retail[paired])
    rows_y, rows_x, used = [], [], []
    for end in range(1, len(paired)):
        if paired[end] - paired[end - 1] != 1:
            continue
        lag_sources = [end - 1 - horizon for horizon in range(max_lag + 1)]
        if min(lag_sources) < 0 or any(paired[source + 1] - paired[source] != 1
                                       for source in lag_sources):
            continue
        lagged = np.asarray([dw[source] for source in lag_sources])
        rows_y.append(dr[end - 1])
        rows_x.append(np.concatenate([[ec[end - 1]], lagged[::-1]]))
        used.append(paired[end])
    y = np.asarray(rows_y)
    x_core = np.asarray(rows_x)
    names = ["const", "ecm_lag", *[f"wholesale_lag{h}" for h in range(max_lag + 1)]]
    design = [np.ones(len(y)), x_core]
    if include_controls:
        controls, control_names = _calendar_controls(calendar_rows,
                                                     np.asarray(used, dtype=int), resonance)
        design.append(controls)
        names.extend(control_names)
    fit = ols_fit(y, np.column_stack(design), names)
    ecm_term = next(item for item in fit["coefficients"] if item["term"] == "ecm_lag")
    return {"fit": fit, "ecm_term": ecm_term,
            "half_life_days": _clean_float(math.log(0.5) / ecm_term["estimate"])
            if ecm_term["estimate"] and ecm_term["estimate"] < 0 else None}


def holdout_check(panel, holdout_fraction):
    """按时间顺序留后段：训练集重估，检验后段样本外拟合。"""
    n = len(panel["y"])
    cut = n - max(8, int(round(n * holdout_fraction)))
    if cut < MIN_OBSERVATIONS:
        raise ValueError("样本不足以留出检验时段")
    train_fit = ols_fit(panel["y"][:cut], panel["x"][:cut], panel["names"])
    test_y = panel["y"][cut:]
    test_pred = panel["x"][cut:] @ train_fit["beta"]
    rmse = float(np.sqrt(np.mean((test_y - test_pred) ** 2)))
    baseline = float(np.sqrt(np.mean((test_y - panel["y"][:cut].mean()) ** 2)))
    tss = float(((test_y - test_y.mean()) @ (test_y - test_y.mean())))
    rss = float((test_y - test_pred) @ (test_y - test_pred))
    return {
        "train_observations": cut, "holdout_observations": n - cut,
        "holdout_first_day": panel["days"][cut], "holdout_last_day": panel["days"][-1],
        "out_of_sample_rmse": _clean_float(rmse),
        "flat_benchmark_rmse": _clean_float(baseline),
        "rmse_ratio_vs_flat": _clean_float(rmse / baseline) if baseline else None,
        "out_of_sample_r_squared": _clean_float(1 - rss / tss) if tss > 0 else None,
    }


def _season_segment_panel(series, calendar_rows, matrices, include_controls,
                          resonance, asymmetric, season_label):
    """按季节标签重建零阶滞后面板，控制变量在该季节的日历上重新生成。"""
    rows = matrices["target_valid"] & matrices["lag_valid"][:, 0]
    positions = matrices["target_positions"][rows]
    season_mask = np.array([calendar_rows[position]["season"] == season_label
                            for position in positions])
    positions = positions[season_mask]
    row_index = np.where(rows)[0][season_mask]
    y = matrices["target_diff"][row_index]
    shock = matrices["lag_matrix"][row_index, 0]
    if asymmetric:
        x_core = np.column_stack([np.maximum(shock, 0), np.minimum(shock, 0)])
        core_names = ["up_lag0", "down_lag0"]
    else:
        x_core = shock[:, None]
        core_names = ["wholesale_lag0"]
    design = [np.ones(len(y)), x_core]
    names = ["const", *core_names]
    if include_controls:
        controls, control_names = _calendar_controls(calendar_rows, positions, resonance)
        if controls.shape[1]:
            design.append(controls)
            names.extend(control_names)
    x = np.column_stack(design)
    if np.linalg.matrix_rank(x.T @ x) < x.shape[1]:
        raise ValueError("季节内设计矩阵列秩不足")
    return {"y": y, "x": x, "names": names, "positions": positions}


def season_stability(series, calendar_rows, matrices, include_controls, resonance, asymmetric):
    """换季前后参数稳定性：分段估计 + 季节交互 Wald 检验 + 递归 CUSUM。

    以零阶（当日）批发冲击面板为载体，该面板样本最大、季节两组均可估计；
    更高阶滞后的分段结果由逐滞后期模型在分季节窗口中单独给出。
    """
    panel = horizon_panel(series, calendar_rows, matrices, 0, include_controls,
                          resonance, asymmetric)
    seasons = np.array([calendar_rows[position]["season"] for position in panel["positions"]])
    labels = sorted(set(seasons))
    segment_fits = {}
    for label in labels:
        try:
            segment_panel = _season_segment_panel(series, calendar_rows, matrices,
                                                  include_controls, resonance,
                                                  asymmetric, label)
            if len(segment_panel["y"]) < MIN_OBSERVATIONS:
                segment_fits[label] = {"observations": int(len(segment_panel["y"])),
                                       "estimable": False,
                                       "reason": f"季节内观测少于最低要求 {MIN_OBSERVATIONS}"}
                continue
            fit = ols_fit(segment_panel["y"], segment_panel["x"], segment_panel["names"])
            segment_fits[label] = {
                "observations": fit["n"], "estimable": True,
                "r_squared": fit["r_squared"], "residual_std": fit["residual_std"],
                "core_coefficients": [item for item in fit["coefficients"]
                                      if "lag" in item["term"]],
            }
        except ValueError as error:
            count = int((seasons == label).sum())
            segment_fits[label] = {"observations": count, "estimable": False,
                                   "reason": str(error)}

    core_indices = [index for index, name in enumerate(panel["names"]) if "lag" in name]
    interaction_names = [panel["names"][index] for index in core_indices]
    season_dummy = (seasons == labels[-1]).astype(float)
    interaction = panel["x"][:, core_indices] * season_dummy[:, None]
    full_x = np.column_stack([panel["x"], interaction])
    full_names = [*panel["names"], *[f"season_x_{name}" for name in interaction_names]]
    wald = None
    try:
        full_fit = ols_fit(panel["y"], full_x, full_names)
        added = np.arange(panel["x"].shape[1], full_x.shape[1])
        beta = full_fit["beta"][added]
        cov = full_fit["cov"][np.ix_(added, added)]
        statistic = float(beta @ np.linalg.pinv(cov) @ beta)
        wald = {"statistic": _clean_float(statistic),
                "p_value": _clean_float(stats.chi2.sf(statistic, len(added))),
                "degrees_of_freedom": len(added),
                "interaction_terms": [full_names[index] for index in added]}
    except ValueError:
        wald = None

    # 递归 CUSUM（Brown–Durbin–Evans），按时间顺序检测系数漂移。
    # 在不含类别固定效应的核心面板上计算，避免递归早期窗口因缺少月份类别而秩不足
    cusum = None
    try:
        core_panel = horizon_panel(series, calendar_rows, matrices, 0,
                                   include_controls=False, resonance=None,
                                   asymmetric=asymmetric)
        n, k = core_panel["x"].shape
        start = max(k + 8, n // 3)
        base_fit = ols_fit(core_panel["y"][:start], core_panel["x"][:start],
                           core_panel["names"], hc3=False)
        sigma = math.sqrt(base_fit["sigma2"])
        path = []
        recursive_sigma = []
        cumulative = 0.0
        for end in range(start, n):
            partial = ols_fit(core_panel["y"][:end + 1], core_panel["x"][:end + 1],
                              core_panel["names"], hc3=False)
            recursive_sigma.append(partial["residual_std"])
            prediction_error = core_panel["y"][end] - core_panel["x"][end] @ partial["beta"]
            cumulative += prediction_error / sigma
            path.append(cumulative)
        boundary = 0.948 * math.sqrt(n - start)
        cusum = {"path_max_abs": _clean_float(float(np.max(np.abs(path)))),
                 "boundary_5pct": _clean_float(boundary),
                 "exceeds_boundary": bool(np.max(np.abs(path)) > boundary),
                 "recursive_sigma_start": _clean_float(sigma),
                 "recursive_sigma_end": recursive_sigma[-1] if recursive_sigma else None}
    except ValueError:
        cusum = None

    return {"segments": segment_fits, "wald": wald, "cusum": cusum,
            "season_labels": labels, "carrier_horizon": 0}
