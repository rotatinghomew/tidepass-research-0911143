"""可比批零序列的构建。

报价按线路单位折算为元/公斤后挂到日历网格上；缺报位置保持为空（NaN），
绝不以前后价格填充，因为缺报只是未观测，不表示当天价格没有涨跌。
"""
from collections import Counter, defaultdict

import numpy as np

QUOTE_KINDS = ("挂牌", "询价")
MATCH_MODES = ("same_day", "window", "trade_day")


def _route_index(catalog):
    return {record["id"]: record for record in catalog.rows("routes")}


def known_dimensions(catalog):
    routes = catalog.rows("routes")
    return {
        "species": sorted({record["species"] for record in routes}),
        "grade": sorted({record["grade"] for record in routes}),
        "region": sorted({record["region"] for record in routes}),
        "route_ids": sorted(record["id"] for record in routes),
    }


def select_routes(catalog, species=None, grade=None, region=None, route_ids=None):
    """按品种、规格、销售地或显式线路代号筛选线路，返回完整线路记录。"""
    dimensions = known_dimensions(catalog)
    if route_ids is not None:
        if not route_ids:
            raise ValueError("至少选择一条线路")
        unknown = sorted(set(route_ids) - set(dimensions["route_ids"]))
        if unknown:
            raise ValueError("未知线路：" + ",".join(unknown))
    selected = []
    for record in catalog.rows("routes"):
        if route_ids is not None and record["id"] not in route_ids:
            continue
        if species is not None and record["species"] != species:
            continue
        if grade is not None and record["grade"] != grade:
            continue
        if region is not None and record["region"] != region:
            continue
        selected.append(record)
    if not selected:
        raise ValueError("筛选条件没有匹配到任何线路")
    return selected


def _quote_grid(catalog, route):
    """每条线路、每一端的日历网格报价（元/公斤）及报价类型计数。"""
    calendar_days = [record["id"] for record in catalog.rows("calendar")]
    day_index = {day: position for position, day in enumerate(calendar_days)}
    unit_kg = float(route["unit_kg"])
    grid = {side: np.full(len(calendar_days), np.nan) for side in ("wholesale", "retail")}
    kinds = {side: Counter() for side in ("wholesale", "retail")}
    for record in catalog.rows("quotes"):
        if record["route_id"] != route["id"]:
            continue
        position = day_index[record["day"]]
        grid[record["side"]][position] = float(record["price"]) / unit_kg
        kinds[record["side"]][record["quote_kind"]] += 1
    return calendar_days, grid, kinds


def _trade_anchor(catalog, route):
    """两端均有真实（未取消）成交的日历位置。"""
    calendar_days = [record["id"] for record in catalog.rows("calendar")]
    day_index = {day: position for position, day in enumerate(calendar_days)}
    realized = {side: set() for side in ("wholesale", "retail")}
    for record in catalog.rows("trades"):
        if record["route_id"] != route["id"] or record["cancelled"]:
            continue
        realized[record["side"]].add(day_index[record["day"]])
    return calendar_days, realized["wholesale"] & realized["retail"]


def _apply_window(grid, window):
    """在日历网格上以 ±window 日内最近报价对齐，返回对齐网格与偏移天数。"""
    if window < 1 or window > 7:
        raise ValueError("窗口匹配半径需在一至七天之间")
    aligned = {}
    offsets = {}
    for side, values in grid.items():
        filled = values.copy()
        shift = np.full(len(values), np.nan)
        observed = np.flatnonzero(~np.isnan(values))
        for position in range(len(values)):
            if not np.isnan(values[position]):
                shift[position] = 0
                continue
            nearby = observed[np.abs(observed - position) <= window]
            if len(nearby):
                nearest = nearby[np.argmin(np.abs(nearby - position))]
                filled[position] = values[nearest]
                shift[position] = int(nearest - position)
        aligned[side] = filled
        offsets[side] = shift
    return aligned, offsets


def build_series(catalog, route, quote_kind="挂牌", match="same_day", window=2):
    """构建单条线路的批零可比序列。

    quote_kind: 仅使用挂牌或询价报价；"any" 允许两类同时进入样本，
    但同一线路同一天同一端不会同时存在两类报价，混合口径会在结果中标注。
    match:
      - same_day: 仅保留两端同日都有报价的日历点；
      - window: 允许 ±window 日内最近报价对齐，记录实际偏移；
      - trade_day: 在 same_day 基础上进一步限定两端当天均有未取消成交。
    """
    if quote_kind not in (*QUOTE_KINDS, "any"):
        raise ValueError("报价类型需为挂牌、询价或 any")
    if match not in MATCH_MODES:
        raise ValueError("未知匹配方式：" + match)

    calendar_days, raw_grid, kinds = _quote_grid(catalog, route)
    if quote_kind != "any":
        grid = {}
        for side, values in raw_grid.items():
            kept = values.copy()
            # 重新按类型过滤：原始网格构建时未区分类型，需要回到记录表
            kept[:] = np.nan
            grid[side] = kept
        unit_kg = float(route["unit_kg"])
        day_index = {day: position for position, day in enumerate(calendar_days)}
        for record in catalog.rows("quotes"):
            if record["route_id"] != route["id"] or record["quote_kind"] != quote_kind:
                continue
            grid[record["side"]][day_index[record["day"]]] = float(record["price"]) / unit_kg
        used_kinds = {side: {quote_kind: kinds[side].get(quote_kind, 0)} for side in kinds}
    else:
        grid = raw_grid
        used_kinds = {side: dict(counter) for side, counter in kinds.items()}

    offsets = {side: np.zeros(len(calendar_days)) for side in ("wholesale", "retail")}
    if match == "window":
        grid, offsets = _apply_window(grid, window)

    anchor = None
    if match == "trade_day":
        _, anchor_set = _trade_anchor(catalog, route)
        anchor = anchor_set

    paired = []
    for position, day in enumerate(calendar_days):
        if np.isnan(grid["wholesale"][position]) or np.isnan(grid["retail"][position]):
            continue
        if anchor is not None and position not in anchor:
            continue
        paired.append(position)

    wholesale = grid["wholesale"]
    retail = grid["retail"]
    missing = {side: int(np.isnan(grid[side]).sum()) for side in grid}
    adjacency_gaps = []
    for previous, current in zip(paired, paired[1:]):
        adjacency_gaps.append(current - previous)
    return {
        "route_id": route["id"],
        "species": route["species"],
        "grade": route["grade"],
        "region": route["region"],
        "unit_kg": float(route["unit_kg"]),
        "calendar_days": calendar_days,
        "wholesale": wholesale,
        "retail": retail,
        "paired_positions": np.asarray(paired, dtype=int),
        "paired_days": [calendar_days[position] for position in paired],
        "quote_kind": quote_kind,
        "quote_kind_counts": used_kinds,
        "mixed_quote_kinds": quote_kind == "any",
        "match": match,
        "window": window if match == "window" else None,
        "align_offsets": offsets,
        "missing_cells": missing,
        "calendar_size": len(calendar_days),
        "paired_count": len(paired),
        "adjacent_paired_share": (float(np.mean(np.asarray(adjacency_gaps) == 1))
                                  if adjacency_gaps else None),
    }


def resonance_shock(catalog, target_route, calendar_days):
    """同品种跨片区的批发端平均一阶差分，作为地区共振控制变量。

    缺报位置不参与当日平均；若当天没有任何其他片区观测，则该位置为空。
    """
    day_index = {day: position for position, day in enumerate(calendar_days)}
    others = [record for record in catalog.rows("routes")
              if record["species"] == target_route["species"]
              and record["grade"] == target_route["grade"]
              and record["id"] != target_route["id"]]
    if not others:
        return np.full(len(calendar_days), np.nan)
    shocks = []
    for route in others:
        _, grid, _ = _quote_grid(catalog, route)
        values = grid["wholesale"]
        diff = np.full(len(values), np.nan)
        observed = np.flatnonzero(~np.isnan(values))
        for previous, current in zip(observed, observed[1:]):
            if current == previous + 1:
                diff[current] = values[current] - values[previous]
        shocks.append(diff)
    stacked = np.vstack(shocks)
    valid = ~np.isnan(stacked)
    counts = valid.sum(axis=0)
    sums = np.nansum(np.where(valid, stacked, 0.0), axis=0)
    mean = np.divide(sums, counts, out=np.full(stacked.shape[1], np.nan),
                     where=counts > 0)
    return mean


def series_overview(catalog, series):
    """供接口返回的序列资料剖面（不含完整数组）。"""
    positions = series["paired_positions"]
    wholesale = series["wholesale"][positions] if len(positions) else np.array([])
    retail = series["retail"][positions] if len(positions) else np.array([])
    offset_days = {side: [int(series["align_offsets"][side][position]) for position in positions]
                   for side in ("wholesale", "retail")}
    return {
        "route_id": series["route_id"],
        "species": series["species"],
        "grade": series["grade"],
        "region": series["region"],
        "unit_kg": series["unit_kg"],
        "price_unit": "元/公斤",
        "quote_kind": series["quote_kind"],
        "mixed_quote_kinds": series["mixed_quote_kinds"],
        "match": series["match"],
        "window": series["window"],
        "calendar_days": series["calendar_size"],
        "missing_cells": series["missing_cells"],
        "paired_count": series["paired_count"],
        "adjacent_paired_share": series["adjacent_paired_share"],
        "paired_first_day": series["paired_days"][0] if series["paired_days"] else None,
        "paired_last_day": series["paired_days"][-1] if series["paired_days"] else None,
        "wholesale_median": float(np.median(wholesale)) if len(wholesale) else None,
        "retail_median": float(np.median(retail)) if len(retail) else None,
        "align_offset_nonzero": {side: int(sum(offset != 0 for offset in values))
                                 for side, values in offset_days.items()},
        "quote_kind_counts": series["quote_kind_counts"],
    }


def trade_evidence(catalog, route, days=None):
    """汇总线路上的成交证据；days 给定时只统计该窗口内的日期。"""
    allowed = set(days) if days is not None else None
    unit_kg = float(route["unit_kg"])
    buckets = defaultdict(lambda: {"records": 0, "cancelled": 0, "quantity_kg": 0.0,
                                   "amount": 0.0, "trade_prices": [], "quote_pairs": []})
    quote_lookup = {}
    for record in catalog.rows("quotes"):
        if record["route_id"] == route["id"]:
            quote_lookup[(record["day"], record["side"])] = float(record["price"]) / unit_kg
    for record in catalog.rows("trades"):
        if record["route_id"] != route["id"]:
            continue
        if allowed is not None and record["day"] not in allowed:
            continue
        bucket = buckets[record["side"]]
        bucket["records"] += 1
        if record["cancelled"]:
            bucket["cancelled"] += 1
            continue
        price = float(record["unit_price_kg"])
        quantity = float(record["quantity_kg"])
        bucket["quantity_kg"] += quantity
        bucket["amount"] += quantity * price
        bucket["trade_prices"].append(price)
        quote = quote_lookup.get((record["day"], record["side"]))
        if quote is not None:
            bucket["quote_pairs"].append(price - quote)

    output = {}
    for side, bucket in buckets.items():
        prices = np.asarray(bucket["trade_prices"])
        gaps = np.asarray(bucket["quote_pairs"])
        realized = bucket["records"] - bucket["cancelled"]
        output[side] = {
            "records": bucket["records"],
            "realized_records": realized,
            "cancelled_records": bucket["cancelled"],
            "realized_quantity_kg": round(bucket["quantity_kg"], 3),
            "volume_weighted_price_kg": (round(bucket["amount"] / bucket["quantity_kg"], 4)
                                         if bucket["quantity_kg"] else None),
            "trade_price_median": round(float(np.median(prices)), 4) if len(prices) else None,
            "quote_trade_pairs": len(gaps),
            "quote_trade_gap_mean": round(float(np.mean(gaps)), 4) if len(gaps) else None,
            "quote_trade_gap_sd": round(float(np.std(gaps, ddof=1)), 4) if len(gaps) > 1 else None,
        }
    return output
