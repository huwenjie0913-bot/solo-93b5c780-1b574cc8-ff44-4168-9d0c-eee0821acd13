"""巡检路线总装：全量生成、增量重算、覆盖率统计。

对外主入口：
    build_route(plan)                 —— 全量生成候选点 + 排序 + A*
    update_route(plan, cache, edits)  —— 只重算受影响路段
"""

from __future__ import annotations

import time

from . import raster as ras
from .gridcache import get_grid
from .graph import astar, diagnose, distance_field, label_components, snap_point
from .planner import generate_candidates, optimize_order

DEFAULT_PPM = 40.0          # 未校准时 1m = 40px
WALK_SPEED_MPS = 1.2        # 预计用时的默认步行速度
DWELL_S = 30.0              # 每点停留检查时间


def _ppm(plan: dict) -> float:
    v = (plan.get("calibration") or {}).get("pixelsPerMeter")
    return DEFAULT_PPM if v is None else float(v)


def _fingerprint(plan: dict) -> str:
    """栅格参数指纹，决定增量缓存是否仍有效。"""
    img = plan.get("image") or {}
    s = plan.get("settings") or {}
    return "|".join(str(v) for v in (
        img.get("width"), img.get("height"),
        img.get("occupancy"), s.get("margin"),
        tuple((d["id"], d.get("open", True)) for d in plan.get("doors", [])),
        tuple((w["x1"], w["y1"], w["x2"], w["y2"]) for w in plan.get("walls", [])),
        tuple((z["id"],) for z in plan.get("zones", [])),
    ))


def _dwell(plan: dict) -> float:
    v = (plan.get("settings") or {}).get("dwell")
    return DWELL_S if v is None else float(v)


def _spacing(plan: dict) -> float:
    v = (plan.get("settings") or {}).get("spacing")
    return 6.0 if v is None else float(v)


def _make_record(pid, kind, label, gx, gy, cell, moved=False, warning=None,
                 error=None) -> dict:
    return {
        "id": pid, "kind": kind, "label": label or "",
        "gx": gx, "gy": gy,
        "x": (gx + 0.5) * cell, "y": (gy + 0.5) * cell,
        "snapped": {"gx": gx, "gy": gy,
                    "x": (gx + 0.5) * cell, "y": (gy + 0.5) * cell,
                    "moved": moved},
        "warning": warning, "error": error,
    }


# ---------------------------------------------------------------------------
# 全量生成
# ---------------------------------------------------------------------------

def build_route(plan: dict) -> dict:
    t0 = time.time()
    grid = get_grid(plan)
    gw, gh, cell = grid["gw"], grid["gh"], grid["cell"]
    blocked, reason = grid["blocked"], grid["reason"]
    ppm = _ppm(plan)
    comp = grid.get("comp") or label_components(blocked, gw, gh)

    warnings: list[str] = []

    # 1) 固定点：起点 / 必经点 / 终点
    start_rec = end_rec = None
    must_recs: list[dict] = []

    def snap(raw, kind):
        s = snap_point(blocked, gw, gh, cell, float(raw["x"]), float(raw["y"]))
        name = raw.get("label") or raw.get("id")
        kind_name = {"start": "起点", "end": "终点", "must": "必经点"}[kind]
        if s is None:
            warnings.append(f"{kind_name}「{name}」位于封闭区域内且附近无可通行格，已忽略")
            return None
        gx, gy, px, py, moved = s
        if moved:
            warnings.append(f"{kind_name}「{name}」落在障碍上，已自动挪到最近可通行位置")
        return _make_record(raw.get("id"), kind, raw.get("label"), gx, gy, cell, moved)

    if plan.get("start"):
        start_rec = snap(plan["start"], "start")
    else:
        warnings.append("尚未设置起点")
    for p in plan.get("mustPass", []):
        r = snap(p, "must")
        if r:
            must_recs.append(r)
    if plan.get("end"):
        end_rec = snap(plan["end"], "end")
    else:
        warnings.append("尚未设置终点（路线将以起点收束）")

    # 用户拖动过的自动点：位置在 manualPoints 中持久化，重算时保留
    manual_recs: list[dict] = []
    for mp in plan.get("manualPoints", []):
        r = snap(mp, "must")
        if r:
            r["id"] = mp.get("id") or r["id"]
            r["kind"] = "auto"
            manual_recs.append(r)

    # 2) 自动候选点（保留已吸附的固定格，避免与自动点重叠）
    keep = []
    for r in (start_rec, *must_recs, *manual_recs, end_rec):
        if r:
            keep.append((r["gx"], r["gy"]))
    spacing = _spacing(plan)
    auto_cells, spacing_used, adjusted = generate_candidates(
        blocked, comp, gw, gh, cell, ppm, spacing, keep)
    if adjusted:
        warnings.append(f"巡检区域较大，间距已自动放大到 {spacing_used:.1f} m 以控制点位数量")

    keep_set = set(keep)
    auto_recs = list(manual_recs)
    for i, (gx, gy) in enumerate(c for c in auto_cells if c not in keep_set):
        auto_recs.append(_make_record(f"A{i+1:03d}", "auto", "", gx, gy, cell))

    # 3) 排序：序列 [start] + (must + auto) + [end]，端点固定做 2-opt
    middle = must_recs + auto_recs
    seq = ([start_rec] if start_rec else []) + middle + ([end_rec] if end_rec else [])
    if not seq:
        return _empty_result(plan, gw, gh, cell, blocked, reason, comp, warnings, t0)
    xy = [(r["x"], r["y"]) for r in seq]
    perm = optimize_order(xy, fixed_start=bool(start_rec), fixed_end=bool(end_rec))
    ordered = []
    for new_i, old_i in enumerate(perm):
        r = dict(seq[old_i])
        r["seq"] = new_i + 1
        ordered.append(r)

    # 必经点顺序可能被优化调整；保留原始必经次序信息由前端 label 呈现
    result = _connect_segments(plan, ordered, blocked, reason, gw, gh, cell, ppm, comp)
    result["warnings"] = warnings + result["warnings"]
    result["coverage"] = _coverage(ordered, result["segments"], blocked, comp,
                                   gw, gh, cell, ppm, spacing)
    result["points"] = ordered
    result["gridInfo"] = {"gw": gw, "gh": gh, "cellPx": round(cell, 2)}
    result["cache"] = _make_cache(plan, gw, gh, cell, ordered, result["segments"])
    result["incremental"] = {"mode": "full"}
    result["elapsedMs"] = int((time.time() - t0) * 1000)
    return result


def _empty_result(plan, gw, gh, cell, blocked, reason, comp, warnings, t0):
    return {
        "points": [], "segments": [], "blockedSegments": [],
        "warnings": warnings or ["没有可布置的点位"],
        "coverage": {"percent": 0.0, "uncoveredAreaM2": 0.0,
                     "uncoveredRuns": [], "radiusCells": 0},
        "stats": {"totalLengthM": 0, "etaMinutes": 0, "pointCount": 0,
                  "walkSpeedMps": WALK_SPEED_MPS, "dwellSeconds": _dwell(plan),
                  "blockedCount": 0},
        "gridInfo": {"gw": gw, "gh": gh, "cellPx": round(cell, 2)},
        "cache": _make_cache(plan, gw, gh, cell, [], []),
        "incremental": {"mode": "full"},
        "elapsedMs": int((time.time() - t0) * 1000),
    }


def _make_cache(plan, gw, gh, cell, records, segments) -> dict:
    return {
        "fingerprint": _fingerprint(plan),
        "gw": gw, "gh": gh, "cell": cell,
        "segments": [
            {"from": s["from"], "to": s["to"],
             "pathCells": s.get("pathCells"), "cost": s.get("cost")}
            for s in segments
        ],
        "points": [{"id": r["id"], "gx": r["gx"], "gy": r["gy"],
                    "kind": r["kind"], "seq": r["seq"]} for r in records],
    }


# ---------------------------------------------------------------------------
# 逐段 A* 连接
# ---------------------------------------------------------------------------

def _connect_segments(plan, records, blocked, reason, gw, gh, cell, ppm, comp) -> dict:
    segments = []
    blocked_segments = []
    total_cost = 0.0
    doors = plan.get("doors", [])

    for i in range(len(records) - 1):
        a, b = records[i], records[i + 1]
        seg = {"seq": i + 1, "from": a["id"], "to": b["id"],
               "fromSeq": a["seq"], "toSeq": b["seq"]}
        path, cost = astar(blocked, gw, gh, a["gx"], a["gy"], b["gx"], b["gy"])
        if path is None:
            diag = diagnose(blocked, reason, doors, comp, gw, gh,
                            (a["gx"], a["gy"]), (b["gx"], b["gy"]), ppm, cell)
            seg.update(blocked=True, reason=diag,
                       fallbackPath=[{"x": a["x"], "y": a["y"]},
                                     {"x": b["x"], "y": b["y"]}])
            blocked_segments.append(seg["seq"])
        else:
            seg.update(blocked=False,
                       pathCells=[[x, y] for x, y in path],
                       path=[{"x": (x + 0.5) * cell, "y": (y + 0.5) * cell}
                             for x, y in path],
                       cost=cost, lengthM=cost * cell / ppm)
            total_cost += cost
        segments.append(seg)

    total_m = total_cost * cell / ppm
    n_visitable = len(records)
    eta_s = total_m / WALK_SPEED_MPS + _dwell(plan) * n_visitable
    return {
        "segments": segments,
        "blockedSegments": blocked_segments,
        "stats": {
            "totalLengthM": round(total_m, 1),
            "etaMinutes": round(eta_s / 60.0, 1),
            "pointCount": len(records),
            "walkSpeedMps": WALK_SPEED_MPS,
            "dwellSeconds": _dwell(plan),
            "blockedCount": len(blocked_segments),
        },
        "warnings": [],
    }


def _finalize_stats(plan, records, segments, cell, ppm) -> dict:
    total_cost = sum(s.get("cost", 0) for s in segments if not s.get("blocked"))
    total_m = total_cost * cell / ppm
    eta_s = total_m / WALK_SPEED_MPS + _dwell(plan) * len(records)
    return {
        "totalLengthM": round(total_m, 1),
        "etaMinutes": round(eta_s / 60.0, 1),
        "pointCount": len(records),
        "walkSpeedMps": WALK_SPEED_MPS,
        "dwellSeconds": _dwell(plan),
        "blockedCount": sum(1 for s in segments if s.get("blocked")),
    }


# ---------------------------------------------------------------------------
# 覆盖率：路线经过格做多源 BFS，间距半径外即盲区
# ---------------------------------------------------------------------------

def _coverage(records, segments, blocked, comp, gw, gh, cell, ppm, spacing_m):
    sources: set[int] = set()
    for seg in segments:
        for x, y in seg.get("pathCells") or []:
            sources.add(y * gw + x)
    if not sources:
        for r in records:
            sources.add(r["gy"] * gw + r["gx"])

    radius_cells = spacing_m * ppm / cell
    dist = distance_field(list(sources), blocked, gw, gh)

    main_comp = comp[records[0]["gy"] * gw + records[0]["gx"]] if records else None

    uncovered_runs: list[list[int]] = []
    uncovered_cells = 0
    reachable = 0
    run_start: int | None = None

    def flush(idx):
        nonlocal run_start
        if run_start is not None:
            uncovered_runs.append([run_start, idx - run_start])
            run_start = None

    for idx, dv in enumerate(dist):
        if main_comp is not None and comp[idx] != main_comp:
            flush(idx)
            continue
        if dv is None:
            flush(idx)
            continue
        reachable += 1
        if dv > radius_cells:
            uncovered_cells += 1
            if run_start is None:
                run_start = idx
        else:
            flush(idx)
    flush(len(dist))

    cell_area = (cell / ppm) ** 2
    pct = 100.0 * (1 - uncovered_cells / reachable) if reachable else 0.0
    truncated = len(uncovered_runs) > 20000
    return {
        "percent": round(pct, 1),
        "uncoveredAreaM2": round(uncovered_cells * cell_area, 1),
        "uncoveredRuns": uncovered_runs[:20000],
        "radiusCells": round(radius_cells, 2),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# 增量更新
# ---------------------------------------------------------------------------

def update_route(plan: dict, cache: dict, edits: dict) -> dict:
    """拖动点位 / 切换门窗时只重算受影响路段。

    edits: {"type": "movePoint", "id", "x", "y"} 或 {"type": "door", ...}
    """
    t0 = time.time()
    etype = edits.get("type")

    # 门窗开闭改变可通行性，必须重建栅格（全部路段都可能受影响）
    if etype == "door":
        full = build_route(plan)
        full["incremental"] = {"mode": "grid-rebuilt", "reason": "door-state"}
        return full

    if etype != "movePoint" or not cache or not cache.get("points"):
        full = build_route(plan)
        full["incremental"] = {"mode": "full"}
        return full

    # 图纸/障碍参数变化则缓存失效
    if cache.get("fingerprint") != _fingerprint(plan):
        full = build_route(plan)
        full["incremental"] = {"mode": "full", "reason": "obstacle-changed"}
        return full

    gw, gh, cell = cache["gw"], cache["gh"], cache["cell"]
    grid = get_grid(plan)
    blocked, reason = grid["blocked"], grid["reason"]
    ppm = _ppm(plan)
    comp = grid.get("comp") or label_components(blocked, gw, gh)

    pid = edits["id"]
    s = snap_point(blocked, gw, gh, cell, float(edits["x"]), float(edits["y"]))
    if s is None:
        return {"error": f"点 {pid} 附近没有可通行区域，已忽略本次移动",
                "incremental": {"mode": "noop"}}
    gx, gy, _, _, moved = s

    records = []
    for p in sorted(cache["points"], key=lambda x: x["seq"]):
        gg_x, gg_y = (gx, gy) if p["id"] == pid else (p["gx"], p["gy"])
        rec = _make_record(p["id"], p["kind"], "", gg_x, gg_y, cell)
        rec["seq"] = p["seq"]
        records.append(rec)
    idx = next((i for i, r in enumerate(records) if r["id"] == pid), None)
    affected = set()
    if idx is not None:
        if idx > 0:
            affected.add(idx - 1)
        if idx < len(records) - 1:
            affected.add(idx)

    fresh = _connect_segments(plan, records, blocked, reason, gw, gh, cell, ppm, comp)

    # 未受影响的路段直接沿用缓存（含 pathCells / cost）
    old_by_pair = {(s["from"], s["to"]): s for s in cache["segments"]}
    merged = []
    for i, seg in enumerate(fresh["segments"]):
        if i not in affected:
            old = old_by_pair.get((seg["from"], seg["to"]))
            if old and old.get("pathCells") is not None:
                seg["blocked"] = False
                seg.pop("reason", None)
                seg["pathCells"] = old["pathCells"]
                seg["cost"] = old["cost"]
                seg["path"] = [{"x": (x + 0.5) * cell, "y": (y + 0.5) * cell}
                               for x, y in old["pathCells"]]
                seg["lengthM"] = old["cost"] * cell / ppm
        merged.append(seg)

    blocked_segments = [s["seq"] for s in merged if s.get("blocked")]
    spacing = _spacing(plan)
    warnings = []
    if moved:
        warnings.append("点已自动吸附到最近可通行位置")

    return {
        "points": records,
        "segments": merged,
        "blockedSegments": blocked_segments,
        "warnings": warnings,
        "stats": _finalize_stats(plan, records, merged, cell, ppm),
        "coverage": _coverage(records, merged, blocked, comp, gw, gh, cell, ppm, spacing),
        "gridInfo": {"gw": gw, "gh": gh, "cellPx": round(cell, 2)},
        "cache": _make_cache(plan, gw, gh, cell, records, merged),
        "incremental": {"mode": "partial", "recalculatedSegments": sorted(affected)},
        "elapsedMs": int((time.time() - t0) * 1000),
    }
