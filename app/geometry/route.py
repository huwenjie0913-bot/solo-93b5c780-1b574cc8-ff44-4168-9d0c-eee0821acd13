"""巡检路线总装：全量生成、增量重算、覆盖率统计。

对外主入口：
    build_route(plan)                 —— 全量生成候选点 + 排序 + A*
    update_route(plan, cache, edits)  —— 只重算受影响路段

多人分区编排（settings.workers >= 2）：
    自动巡检点与必经点按估时负载均衡分成多条路线（实际栅格距离），
    支持共同起终点或每人员独立起终点；结果同时给出：
      - routes[i]：每人员的统计、告警、颜色等元信息
      - points / segments：拍平的全量数据（带 worker 字段，兼容旧渲染）
      - stats：全队汇总（totalLengthM=总里程，etaMinutes/maxEtaMinutes=最长用时）
"""

from __future__ import annotations

import math
import time

from . import raster as ras
from .gridcache import get_grid
from .graph import (astar, diagnose, distance_field, label_components,
                    snap_point)
from .planner import generate_candidates, optimize_order, partition_balance
from . import schedule as sch

DEFAULT_PPM = 40.0          # 未校准时 1m = 40px
WALK_SPEED_MPS = 1.2        # 预计用时的默认步行速度
DWELL_S = 30.0              # 每点停留检查时间

MAX_WORKERS = 8
# 人员路线配色（与前端 canvas.js 的 WORKER_COLORS 保持一致）
WORKER_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706",
                 "#7c3aed", "#0891b2", "#db2777", "#65a30d"]


def _ppm(plan: dict) -> float:
    v = (plan.get("calibration") or {}).get("pixelsPerMeter")
    return DEFAULT_PPM if v is None else float(v)


def _workers(plan: dict) -> int:
    """人员数；缺省/非法值都按 1（单人模式，兼容旧方案）。"""
    v = (plan.get("settings") or {}).get("workers")
    try:
        return max(1, min(MAX_WORKERS, int(v)))
    except (TypeError, ValueError):
        return 1


def _max_minutes(plan: dict) -> float:
    """单人时长上限（分钟），0/缺省表示不限。"""
    v = (plan.get("settings") or {}).get("maxMinutes")
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return 0.0


def _start_mode(plan: dict) -> str:
    v = (plan.get("settings") or {}).get("startMode")
    return "individual" if v == "individual" else "shared"


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
        tuple((b.get("x1"), b.get("y1"), b.get("x2"), b.get("y2"))
              for b in plan.get("_tempBlockers", []) or []),
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
    k = _workers(plan)
    individual = k >= 2 and _start_mode(plan) == "individual"

    # 班次时间窗参数校验（无效 HH:MM / 自相矛盾的窗口）
    shift = sch.shift_start_minutes(plan)
    raw_shift = (plan.get("settings") or {}).get("shiftStart")
    if raw_shift not in (None, "") and shift is None:
        warnings.append(f"班次开始时间「{raw_shift}」格式无效，已按不排程处理"
                        "（应为 HH:MM）")
        shift = None
    windows = sch.point_windows(plan, shift) if shift is not None else {}
    if shift is not None:
        for raw in plan.get("mustPass", []):
            c = sch.window_conflict(raw, shift)
            if c:
                warnings.append(c)

    # 1) 固定点：起点 / 必经点 / 终点（多人独立模式下共享起终点不参与）
    start_rec = end_rec = None
    must_recs: list[dict] = []

    def snap(raw, kind):
        s = snap_point(blocked, gw, gh, cell, float(raw["x"]), float(raw["y"]))
        name = raw.get("label") or raw.get("id")
        kind_name = {"start": "起点", "end": "终点", "must": "必经点",
                     "handover": "交接点"}.get(kind, kind)
        if s is None:
            warnings.append(f"{kind_name}「{name}」位于封闭区域内且附近无可通行格，已忽略")
            return None
        gx, gy, px, py, moved = s
        if moved:
            warnings.append(f"{kind_name}「{name}」落在障碍上，已自动挪到最近可通行位置")
        return _make_record(raw.get("id"), kind, raw.get("label"), gx, gy, cell, moved)

    if individual:
        pass   # 各自独立起终点，共享起终点不参与
    elif plan.get("start"):
        start_rec = snap(plan["start"], "start")
    else:
        warnings.append("尚未设置起点")
    for p in plan.get("mustPass", []):
        r = snap(p, "must")
        if r:
            must_recs.append(r)
            # 时间窗字段透传到吸附后的记录
            for fld in ("readyClock", "dueClock", "dwellSeconds", "priority",
                        "role"):
                if p.get(fld) is not None:
                    r[fld] = p[fld]
    if individual:
        pass
    elif plan.get("end"):
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

    # 多人独立起终点：按人员索引吸附（参与候选点保留格，避免与自动点重叠）
    worker_starts: list[dict | None] = []
    worker_ends: list[dict | None] = []
    if individual:
        ws = plan.get("workerStarts") or []
        we = plan.get("workerEnds") or []
        for i in range(k):
            raw = ws[i] if i < len(ws) else None
            worker_starts.append(snap(raw, "start") if raw else None)
        for i in range(k):
            raw = we[i] if i < len(we) else None
            worker_ends.append(snap(raw, "end") if raw else None)

    # 2) 自动候选点（保留已吸附的固定格，避免与自动点重叠）
    keep = []
    for r in (start_rec, *must_recs, *manual_recs, end_rec,
              *worker_starts, *worker_ends):
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

    # 3) 多人分区编排：均衡分组 + 每组排序连段
    if k >= 2:
        starts = worker_starts if individual else [start_rec] * k
        ends = worker_ends if individual else [end_rec] * k
        return _build_multi(plan, blocked, reason, gw, gh, cell, ppm, comp,
                            starts, ends, must_recs, auto_recs, warnings, t0,
                            shift, windows)

    # 4) 单人：序列 [start] + (must + auto) + [end]，端点固定做 2-opt
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

    # 时间窗排程：按班次时刻与点位约束重排锚点（起终点 + 必经点）
    if shift is not None:
        ordered, sw = _reorder_single(plan, ordered, blocked, gw, gh,
                                      cell, ppm, windows)
        warnings.extend(sw)

    # 必经点顺序可能被优化调整；保留原始必经次序信息由前端 label 呈现
    result = _connect_segments(plan, ordered, blocked, reason, gw, gh, cell, ppm, comp)
    result["warnings"] = warnings + result["warnings"]
    result["coverage"] = _coverage(ordered, result["segments"], blocked, comp,
                                   gw, gh, cell, ppm, spacing)
    result["points"] = ordered
    if shift is not None:
        sim = sch.simulate(ordered, result["segments"], plan, shift, windows)
        _attach_schedule(result, ordered, sim, warnings)
    result["gridInfo"] = {"gw": gw, "gh": gh, "cellPx": round(cell, 2)}
    result["cache"] = _make_cache(plan, gw, gh, cell, ordered, result["segments"])
    result["incremental"] = {"mode": "full"}
    result["elapsedMs"] = int((time.time() - t0) * 1000)
    return result


def _reorder_single(plan, ordered, blocked, gw, gh, cell, ppm, windows):
    """单人路线：带时间窗的锚点重排，返回 (可能重排后的记录, 警告列表)。"""
    warns: list[str] = []
    anchor_pos = [i for i, r in enumerate(ordered)
                  if r.get("kind") in ("start", "end", "must")]
    anchors = [ordered[i] for i in anchor_pos]
    if len(anchors) > sch.MAX_REORDER_ANCHORS:
        warns.append(
            f"必经点共 {len(anchors)} 个，超过单次时间窗重排上限 "
            f"{sch.MAX_REORDER_ANCHORS}，访问顺序保持按距离编排，仅逐站核算时刻")
        return ordered, warns

    perm, why, _, _ = sch.plan_anchor_order(
        anchors, blocked, gw, gh, cell, ppm, windows, _dwell(plan))
    if why == "reorder" and perm != list(range(len(anchors))):
        ordered = sch.apply_anchor_permutation(ordered, perm, anchors)
        reordered = [anchors[i].get("label") or anchors[i]["id"]
                     for i in perm if anchors[i].get("kind") == "must"]
        warns.append("已按时间窗重排必经点访问顺序：" + " → ".join(reordered))
    return ordered, warns


def _attach_schedule(result, records, sim, warnings):
    """把仿真时刻合并到点位记录，并在结果上挂 schedule 汇总。"""
    by_id = {e["id"]: e for e in sim["entries"]}
    for r in records:
        e = by_id.get(r["id"])
        if e:
            r["schedule"] = {k: e[k] for k in (
                "arrivalClock", "leaveClock", "arrivalMin", "leaveMin",
                "waitMin", "walkMin", "dwellSeconds", "lateMin",
                "readyClock", "dueClock", "priority", "windowed",
                "estimated", "blockedIn") if k in e}
    result["schedule"] = sim
    result["stats"].update({
        "shiftStart": sim["summary"]["shiftStart"],
        "finishClock": sim["summary"]["finishClock"],
        "waitMinutes": sim["summary"]["waitMinutes"],
        "waitPoints": sim["summary"]["waitPoints"],
        "lateMinutes": sim["summary"]["lateMinutes"],
        "latePoints": sim["summary"]["latePoints"],
        "scheduleFeasible": sim["summary"]["feasible"],
    })
    # 无效窗口的提示已在 build_route 入口统一生成，这里只补逾期/不可达
    for c in sim["conflicts"]:
        if c["type"] == "late":
            warnings.append("排程逾期：" + c["message"])
        elif c["type"] == "unreachable":
            warnings.append("排程不可达：" + c["message"])


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


# ---------------------------------------------------------------------------
# 多人分区编排：均衡分组 → 组内排序 → 逐段 A* → 再均衡 → 汇总
# ---------------------------------------------------------------------------

def _build_multi(plan, blocked, reason, gw, gh, cell, ppm, comp,
                 starts, ends, must_recs, auto_recs, warnings, t0,
                 shift=None, windows=None):
    """把自动巡检点与必经点分成负载均衡的 k 条路线并汇总结果。"""
    k = len(starts)
    dwell = _dwell(plan)
    visit = must_recs + auto_recs

    locked, stale = _locked_map(plan, k, {r["id"] for r in visit})
    if stale:
        warnings.append(f"{stale} 条人工分配已失效（点位或人员不存在），已按自动分配处理")

    # 从各起点出发的实际栅格距离场，用于分区时的负载估计
    dist_fields = [
        distance_field([s["gy"] * gw + s["gx"]], blocked, gw, gh) if s else None
        for s in starts
    ]
    groups = partition_balance(visit, k, starts, dist_fields, locked,
                               gw, cell, ppm, dwell)

    routes = []
    for i in range(k):
        recs = _order_route(groups[i], starts[i], ends[i]) if groups[i] else []
        routes.append({
            "start": starts[i], "end": ends[i], "group": groups[i],
            "records": recs,
            "conn": _connect_segments(plan, recs, blocked, reason,
                                      gw, gh, cell, ppm, comp),
        })

    _rebalance(plan, routes, locked, blocked, reason, gw, gh, cell, ppm, comp)

    # 时间窗排程：各组在几何均衡完成后按窗口重排锚点并重连路段
    if shift is not None:
        for i, rt in enumerate(routes):
            records = rt["records"]
            anchor_pos = [j for j, r in enumerate(records)
                          if r.get("kind") in ("start", "end", "must")]
            anchors = [records[j] for j in anchor_pos]
            if len(anchors) > sch.MAX_REORDER_ANCHORS:
                warnings.append(
                    f"人员 {i + 1} 必经点超过 {sch.MAX_REORDER_ANCHORS} 个，"
                    "顺序保持按距离编排，仅逐站核算时刻")
                continue
            perm, why, _, _ = sch.plan_anchor_order(
                anchors, blocked, gw, gh, cell, ppm, windows, dwell)
            if why == "reorder" and perm != list(range(len(anchors))):
                new_records = sch.apply_anchor_permutation(records, perm, anchors)
                rt["records"] = new_records
                rt["group"] = [r for r in new_records
                               if r.get("kind") not in ("start", "end")]
                rt["conn"] = _connect_segments(plan, new_records, blocked, reason,
                                               gw, gh, cell, ppm, comp)
                names = " → ".join(
                    anchors[j].get("label") or anchors[j]["id"]
                    for j in perm if anchors[j].get("kind") == "must")
                warnings.append(f"人员 {i + 1} 已按时间窗重排：{names}")

    return _assemble_multi(plan, routes, locked, blocked, comp, gw, gh, cell,
                           ppm, _spacing(plan), warnings, t0, {"mode": "full"},
                           shift, windows)


def _locked_map(plan, k, valid_ids=None):
    """读取人工锁定分配 {点位id: 人员序号}，过滤越界/失效项，返回 (映射, 失效数)。"""
    locked: dict[str, int] = {}
    stale = 0
    for pid, w in (plan.get("assignments") or {}).items():
        try:
            w = int(w)
        except (TypeError, ValueError):
            stale += 1
            continue
        if 0 <= w < k and (valid_ids is None or pid in valid_ids):
            locked[pid] = w
        else:
            stale += 1
    return locked, stale


def _order_route(group, start_rec, end_rec):
    """单条路线排序：[start] + group + [end]，端点固定 2-opt，返回带 seq 的记录。"""
    seq = ([start_rec] if start_rec else []) + list(group) + \
          ([end_rec] if end_rec else [])
    if not seq:
        return []
    xy = [(r["x"], r["y"]) for r in seq]
    perm = optimize_order(xy, fixed_start=bool(start_rec),
                          fixed_end=bool(end_rec))
    out = []
    for new_i, old_i in enumerate(perm):
        r = dict(seq[old_i])
        r["seq"] = new_i + 1
        out.append(r)
    return out


def _remove_delta(seq, j):
    """从序列移除第 j 个点节省的欧氏距离（像素）。"""
    rec = seq[j]
    prev = seq[j - 1] if j > 0 else None
    nxt = seq[j + 1] if j + 1 < len(seq) else None
    d = 0.0
    if prev:
        d += math.hypot(rec["x"] - prev["x"], rec["y"] - prev["y"])
    if nxt:
        d += math.hypot(rec["x"] - nxt["x"], rec["y"] - nxt["y"])
    if prev and nxt:
        d -= math.hypot(prev["x"] - nxt["x"], prev["y"] - nxt["y"])
    return max(0.0, d)


def _insert_delta(seq, rec):
    """把点插入序列的最小额外欧氏距离（像素）。"""
    if not seq:
        return 0.0
    best = math.inf
    for j in range(len(seq) + 1):
        prev = seq[j - 1] if j > 0 else None
        nxt = seq[j] if j < len(seq) else None
        cost = 0.0
        if prev:
            cost += math.hypot(rec["x"] - prev["x"], rec["y"] - prev["y"])
        if nxt:
            cost += math.hypot(rec["x"] - nxt["x"], rec["y"] - nxt["y"])
        if prev and nxt:
            cost -= math.hypot(prev["x"] - nxt["x"], prev["y"] - nxt["y"])
        if cost < best:
            best = cost
    return max(0.0, best)


def _rebalance(plan, routes, locked_ids, blocked, reason, gw, gh, cell, ppm, comp):
    """把最忙人员的未锁定点挪给最闲人员，直到最长/最短路线用时足够接近。"""
    k = len(routes)
    if k <= 1:
        return
    dwell = _dwell(plan)
    for _ in range(24):
        times = [r["conn"]["stats"]["etaMinutes"] for r in routes]
        hi = max(range(k), key=lambda i: times[i])
        lo = min(range(k), key=lambda i: times[i])
        mean = sum(times) / k
        if times[hi] - times[lo] <= max(1.0, 0.05 * mean):
            break
        movable = [r for r in routes[hi]["group"] if r["id"] not in locked_ids]
        if not movable:
            break
        base = max((times[i] for i in range(k) if i not in (hi, lo)), default=0.0)
        seq_hi = routes[hi]["records"]
        seq_lo = routes[lo]["records"]
        best = None
        for rec in movable:
            j = next((idx for idx, r in enumerate(seq_hi)
                      if r["id"] == rec["id"]), None)
            if j is None:
                continue
            save = _remove_delta(seq_hi, j)
            add = _insert_delta(seq_lo, rec)
            t_hi = times[hi] - (save / ppm / WALK_SPEED_MPS + dwell) / 60.0
            t_lo = times[lo] + (add / ppm / WALK_SPEED_MPS + dwell) / 60.0
            new_max = max(t_hi, t_lo, base)
            if best is None or new_max < best[0]:
                best = (new_max, rec)
        if best is None or best[0] >= times[hi] - 0.05:
            break
        rec = best[1]
        routes[hi]["group"] = [r for r in routes[hi]["group"]
                               if r["id"] != rec["id"]]
        routes[lo]["group"] = list(routes[lo]["group"]) + [rec]
        for i in (hi, lo):
            routes[i]["records"] = _order_route(routes[i]["group"],
                                                routes[i]["start"],
                                                routes[i]["end"])
            routes[i]["conn"] = _connect_segments(plan, routes[i]["records"],
                                                  blocked, reason, gw, gh,
                                                  cell, ppm, comp)


def _assemble_multi(plan, routes, locked, blocked, comp, gw, gh, cell, ppm,
                    spacing, warnings, t0, incremental,
                    shift=None, windows=None):
    """汇总各人员路线：routes 元信息 + 拍平 points/segments + 全队统计。"""
    k = len(routes)
    max_min = _max_minutes(plan)
    all_points: list[dict] = []
    all_segments: list[dict] = []
    routes_meta: list[dict] = []
    blocked_count = 0
    overtime_count = 0
    sim_results: list[dict] = []

    for i, rt in enumerate(routes):
        conn = rt["conn"]
        st = conn["stats"]

        # 时间窗排程：逐人到达/离开/等待/超窗仿真
        sim = None
        sched_by_id: dict[str, dict] = {}
        if shift is not None:
            sim = sch.simulate(rt["records"], conn["segments"], plan, shift,
                               windows, worker=i)
            sim_results.append(sim)
            sched_by_id = {e["id"]: e for e in sim["entries"]}
            for c in sim["conflicts"]:
                if c["type"] == "late":
                    warnings.append(f"人员 {i + 1} 排程逾期：" + c["message"])
                elif c["type"] == "unreachable":
                    warnings.append(f"人员 {i + 1} 排程不可达：" + c["message"])

        for r in rt["records"]:
            p = dict(r)
            p["worker"] = i
            if r["id"] in locked:
                p["locked"] = True
            e = sched_by_id.get(r["id"])
            if e:
                p["schedule"] = {kk: e[kk] for kk in (
                    "arrivalClock", "leaveClock", "arrivalMin", "leaveMin",
                    "waitMin", "walkMin", "dwellSeconds", "lateMin",
                    "readyClock", "dueClock", "priority", "windowed",
                    "estimated", "blockedIn") if kk in e}
            all_points.append(p)
        for s in conn["segments"]:
            seg = dict(s)
            seg["worker"] = i
            all_segments.append(seg)
        blocked_count += st["blockedCount"]

        overtime = bool(max_min > 0 and st["etaMinutes"] > max_min)
        if overtime:
            overtime_count += 1
        rt_warnings = []
        if overtime:
            rt_warnings.append(
                f"人员 {i + 1} 预计用时 {st['etaMinutes']:.0f} 分钟，"
                f"超出单人上限 {max_min:.0f} 分钟")
        if st["blockedCount"]:
            rt_warnings.append(f"人员 {i + 1} 有 {st['blockedCount']} 段路线不可达")
        if not rt["records"]:
            rt_warnings.append(f"人员 {i + 1} 未分配到点位")
        warnings.extend(rt_warnings)
        meta_stats = {**st, "overtime": overtime}
        if sim is not None:
            meta_stats.update({
                "shiftStart": sim["summary"]["shiftStart"],
                "finishClock": sim["summary"]["finishClock"],
                "waitMinutes": sim["summary"]["waitMinutes"],
                "waitPoints": sim["summary"]["waitPoints"],
                "lateMinutes": sim["summary"]["lateMinutes"],
                "latePoints": sim["summary"]["latePoints"],
                "scheduleFeasible": sim["summary"]["feasible"],
            })
        routes_meta.append({
            "worker": i,
            "label": f"人员 {i + 1}",
            "color": WORKER_COLORS[i % len(WORKER_COLORS)],
            "stats": meta_stats,
            "warnings": rt_warnings,
            "blockedSegments": conn["blockedSegments"],
            "schedule": sim,
        })

    total_m = sum(rt["conn"]["stats"]["totalLengthM"] for rt in routes)
    makespan = max((rt["conn"]["stats"]["etaMinutes"] for rt in routes),
                   default=0.0)
    stats = {
        "totalLengthM": round(total_m, 1),   # 全队总里程
        "etaMinutes": round(makespan, 1),    # 全队最长用时
        "maxEtaMinutes": round(makespan, 1),
        "pointCount": len({p["id"] for p in all_points}),
        "workers": k,
        "walkSpeedMps": WALK_SPEED_MPS,
        "dwellSeconds": _dwell(plan),
        "blockedCount": blocked_count,
        "overtimeCount": overtime_count,
        "maxMinutes": max_min or None,
    }

    team_schedule = None
    if shift is not None and sim_results:
        team_schedule = sch.aggregate_simulations(sim_results)
        stats.update({
            "shiftStart": team_schedule["summary"]["shiftStart"],
            "finishClock": team_schedule["summary"]["finishClock"],
            "waitMinutes": team_schedule["summary"]["waitMinutes"],
            "waitPoints": team_schedule["summary"]["waitPoints"],
            "lateMinutes": team_schedule["summary"]["lateMinutes"],
            "latePoints": team_schedule["summary"]["latePoints"],
            "scheduleFeasible": team_schedule["summary"]["feasible"],
        })

    return {
        "mode": "multi",
        "workers": k,
        "routes": routes_meta,
        "points": all_points,
        "segments": all_segments,
        "blockedSegments": [s["seq"] for s in all_segments if s.get("blocked")],
        "warnings": warnings,
        "schedule": team_schedule,
        "coverage": _coverage(all_points, all_segments, blocked, comp,
                              gw, gh, cell, ppm, spacing),
        "stats": stats,
        "gridInfo": {"gw": gw, "gh": gh, "cellPx": round(cell, 2)},
        "cache": _make_cache_multi(plan, gw, gh, cell, routes),
        "incremental": incremental,
        "elapsedMs": int((time.time() - t0) * 1000),
    }


def _make_cache_multi(plan, gw, gh, cell, routes):
    return {
        "mode": "multi",
        "fingerprint": _fingerprint(plan),
        "gw": gw, "gh": gh, "cell": cell,
        "workers": len(routes),
        "routes": [
            {"points": [{"id": r["id"], "gx": r["gx"], "gy": r["gy"],
                         "kind": r["kind"], "seq": r["seq"],
                         "label": r.get("label") or ""}
                        for r in rt["records"]],
             "segments": [{"from": s["from"], "to": s["to"],
                           "fromSeq": s.get("fromSeq"), "toSeq": s.get("toSeq"),
                           "pathCells": s.get("pathCells"),
                           "cost": s.get("cost")}
                          for s in rt["conn"]["segments"]]}
            for rt in routes
        ],
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

    result = {
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

    # 拖动点位后同样重算时间窗排程时刻（顺序不变，仅时刻随路段变化）
    inc_shift = sch.shift_start_minutes(plan)
    if inc_shift is not None:
        windows = sch.point_windows(plan, inc_shift)
        for raw in plan.get("mustPass", []):
            c = sch.window_conflict(raw, inc_shift)
            if c:
                warnings.append(c)
        sim = sch.simulate(records, merged, plan, inc_shift, windows)
        _attach_schedule(result, records, sim, warnings)
    return result
