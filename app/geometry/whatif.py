"""路线变更推演：临时封路 / 班次交接点叠加到方案副本后重算，
并与基线路线逐点逐段比较，标出受影响巡检点、预计延误与需人工确认的冲突。

场景（scenario）JSON 形态::

    {
      "name": "3号通道临时封闭",
      "reason": "叉车吊装作业",
      "closures": [{"id","x1","y1","x2","y2","thickness","effectiveStart",
                    "effectiveEnd","validFrom","validTo","label"}],
      "handovers": [{"id","x","y","label","handoverClock","readyClock",
                     "dwellSeconds"}],
      "doorOverrides": {"doorId": false},     # 推演期间强制关闭的门
      "pointMoves": [{"id","x","y"}],         # 逐段拖动调整（自动点/固定点）
      "confirmations": {"conflictKey": true}  # 已人工确认的冲突（前端维护）
    }

- closures 不改动方案墙体，只在栅格化最后压一层临时障碍（REASON_CLOSURE）。
- handovers 作为带「交接时刻（最晚完成）」时间窗的必经点注入 mustPass。
- 基线与推演路线都由 build_route 全量生成，再做 ID/栅格位置对齐比较。
"""

from __future__ import annotations

import copy
import time
from datetime import date

from . import raster as ras
from . import schedule as sch
from .route import build_route

# 预计延误超过该分钟数才列入影响（避免抖动）
DELAY_EPS_MIN = 0.5
# 覆盖率下降超过该百分点需人工确认
COVERAGE_DROP_WARN = 2.0
# 重复经过长度超过该米数才提示确认（路径尖角偶发共格属噪声）
REPEAT_WARN_M = 3.0
# 两条入边路径 Jaccard 相似度高于该值视为同一路由（仅时长变化）
SAME_ROUTE_SIM = 0.75

_HANDOVER_DWELL_S = 60.0


# ---------------------------------------------------------------------------
# 场景规整 / 应用
# ---------------------------------------------------------------------------

def normalize_scenario(sc: dict | None) -> dict:
    """补全场景字段，过滤非法几何。"""
    sc = sc or {}
    closures, seen_c = [], set()
    for c in sc.get("closures") or []:
        cid = c.get("id") or f"c{len(seen_c) + 1}"
        if cid in seen_c:
            cid = f"{cid}_{len(seen_c)}"
        seen_c.add(cid)
        try:
            item = {
                "id": cid,
                "x1": float(c["x1"]), "y1": float(c["y1"]),
                "x2": float(c["x2"]), "y2": float(c["y2"]),
                "thickness": max(2.0, float(c.get("thickness") or 14)),
                "label": c.get("label") or f"封闭通道 {cid}",
                "effectiveStart": c.get("effectiveStart") or None,
                "effectiveEnd": c.get("effectiveEnd") or None,
                "validFrom": c.get("validFrom") or None,
                "validTo": c.get("validTo") or None,
            }
        except (KeyError, TypeError, ValueError):
            continue
        closures.append(item)

    handovers, seen_h = [], set()
    for h in sc.get("handovers") or []:
        hid = h.get("id") or f"h{len(seen_h) + 1}"
        if hid in seen_h:
            hid = f"{hid}_{len(seen_h)}"
        seen_h.add(hid)
        try:
            item = {
                "id": hid, "x": float(h["x"]), "y": float(h["y"]),
                "label": h.get("label") or f"交接点 {hid}",
                "handoverClock": h.get("handoverClock") or None,
                "readyClock": h.get("readyClock") or None,
                "dwellSeconds": h.get("dwellSeconds"),
            }
        except (KeyError, TypeError, ValueError):
            continue
        handovers.append(item)

    overrides = {str(k): bool(v)
                 for k, v in (sc.get("doorOverrides") or {}).items()}
    moves = []
    for m in sc.get("pointMoves") or []:
        try:
            moves.append({"id": str(m["id"]),
                          "x": float(m["x"]), "y": float(m["y"])})
        except (KeyError, TypeError, ValueError):
            continue

    return {
        "name": (sc.get("name") or "未命名推演").strip() or "未命名推演",
        "reason": sc.get("reason") or "",
        "closures": closures,
        "handovers": handovers,
        "doorOverrides": overrides,
        "pointMoves": moves,
        "confirmations": {str(k): bool(v)
                          for k, v in (sc.get("confirmations") or {}).items()
                          if v},
    }


def _window_overlaps_patrol(sc: dict, route: dict, plan: dict) -> bool | None:
    """封路时段是否与本班巡逻时间相交。无班次信息返回 None（无法判定）。

    在绝对分钟轴上比较，避免把「早于班次开始」的时刻一律当成次日：
    巡逻窗为 [班次开始, 预计交岗]（可跨午夜）；封路窗按当天 HH:MM
    解析，end<=start 视为跨午夜，再整体平移到前一天/当天/次日三种
    对齐，任一与巡逻窗相交即判重叠。例如班次 08:00、巡逻 08:00–08:01，
    封路 07:00–09:00（早于班次开始但延续进执行期）应判为重叠。
    """
    start_clock, end_clock = sc.get("effectiveStart"), sc.get("effectiveEnd")
    if not start_clock or not end_clock:
        return None
    shift = sch.shift_start_minutes(plan)
    if shift is None:
        return None
    cs = sch.parse_clock(start_clock)
    ce = sch.parse_clock(end_clock)
    if cs is None or ce is None:
        return None
    if ce <= cs:
        ce += 1440          # 当天 end<=start：封路跨午夜
    finish = (route.get("stats") or {}).get("finishClock")
    if not finish:
        return None
    pat_len = sch.clock_offset(finish, shift)   # 交岗相对班次的分钟 0..1439
    p0, p1 = float(shift), float(shift) + float(pat_len)
    # 封路可能位于前一天、当天或次日（如夜班巡逻跨过午夜）
    for k in (-1, 0, 1):
        c0, c1 = cs + k * 1440, ce + k * 1440
        if c0 < p1 and p0 < c1:
            return True
    return False


def active_closures(scenario: dict, plan: dict,
                    baseline: dict | None) -> tuple[list[dict], list[dict]]:
    """按生效时段把封闭通道分成 (参与计算, 错峰不参与)。

    规则：
    - 未设置班次开始时间、通道未设生效时段、或无法取得基线交岗时刻 →
      无法判定，保守地让通道参与计算（不误放真实隔断）；
    - 否则通道仅在与「班次开始 → 基线预计交岗」区间重叠时栅格化，
      错峰通道（如巡逻 08:00 已结束、封路 20:00–21:00）不产生障碍，
      也就不会出现不可达点/封路冲突。
    """
    shift = sch.shift_start_minutes(plan)
    finish = (baseline or {}).get("stats", {}).get("finishClock")
    active, idle = [], []
    for c in scenario.get("closures", []):
        if shift is None or not c.get("effectiveStart") \
                or not c.get("effectiveEnd") or not finish:
            active.append(c)
            continue
        overlap = _window_overlaps_patrol(c, baseline, plan)
        (active if overlap is not False else idle).append(c)
    return active, idle


def apply_scenario(plan: dict, scenario: dict,
                   baseline: dict | None = None) -> tuple[dict, list[str]]:
    """把场景叠加到方案副本上，返回 (variant_plan, 处理说明)。

    封闭通道仅在生效时段与路线执行时间重叠时写入临时障碍层
    （见 :func:`active_closures`）；错峰通道保留在场景中但不参与栅格化。
    """
    v = copy.deepcopy(plan)
    notes: list[str] = []
    scenario = normalize_scenario(scenario)

    # 1) 封闭通道 → 临时障碍层（不写回 walls，便于基线对照）
    active, idle = active_closures(scenario, plan, baseline)
    if idle:
        for c in idle:
            notes.append(
                f"「{c.get('label') or c['id']}」生效时段 "
                f"{c.get('effectiveStart')}–{c.get('effectiveEnd')} "
                "与本班巡逻时间不重叠，未参与路线重算")
    v["_tempBlockers"] = [
        {"id": c["id"], "x1": c["x1"], "y1": c["y1"], "x2": c["x2"],
         "y2": c["y2"], "thickness": c["thickness"]}
        for c in active
    ]

    # 2) 门控覆盖（推演期间强制关闭/恢复）
    if scenario["doorOverrides"]:
        for d in v.get("doors", []):
            if d["id"] in scenario["doorOverrides"]:
                d["open"] = scenario["doorOverrides"][d["id"]]

    # 3) 交接点 → 带交接时刻窗口的必经点
    existing = {p.get("id") for p in v.get("mustPass", [])}
    for h in scenario["handovers"]:
        if h["id"] in existing:
            continue
        try:
            dwell = h.get("dwellSeconds")
            dwell = max(0.0, float(dwell)) if dwell not in (None, "") else None
        except (TypeError, ValueError):
            dwell = None
        v.setdefault("mustPass", []).append({
            "id": h["id"], "x": h["x"], "y": h["y"], "label": h["label"],
            "role": "handover",
            "readyClock": h.get("readyClock"),
            "dueClock": h.get("handoverClock"),
            "dwellSeconds": dwell if dwell is not None else _HANDOVER_DWELL_S,
            "priority": 5,
        })

    # 4) 逐段拖动调整（固定点改 data，自动点写入 manualPoints）
    if scenario["pointMoves"]:
        by_id = {}
        if v.get("start"):
            by_id[v["start"].get("id")] = v["start"]
        if v.get("end"):
            by_id[v["end"].get("id")] = v["end"]
        for p in v.get("mustPass", []):
            by_id[p.get("id")] = p
        v.setdefault("manualPoints", [])
        mp_by_id = {p.get("id"): p for p in v["manualPoints"]}
        for m in scenario["pointMoves"]:
            if m["id"] in by_id:
                by_id[m["id"]]["x"], by_id[m["id"]]["y"] = m["x"], m["y"]
            else:
                mp = mp_by_id.get(m["id"])
                if mp is None:
                    mp = {"id": m["id"], "x": m["x"], "y": m["y"]}
                    v["manualPoints"].append(mp)
                    mp_by_id[m["id"]] = mp
                else:
                    mp["x"], mp["y"] = m["x"], m["y"]

    return v, notes


# ---------------------------------------------------------------------------
# 路线对齐工具
# ---------------------------------------------------------------------------

def _chains(route: dict) -> dict[int, list[dict]]:
    """按人员把点位按 seq 排序，返回 {worker: [point,...]}。"""
    chains: dict[int, list] = {}
    for p in route.get("points", []):
        chains.setdefault(p.get("worker") or 0, []).append(p)
    for w in chains:
        chains[w].sort(key=lambda p: p.get("seq", 0))
    return chains


def _arrival_index(route: dict, plan: dict) -> dict[tuple[str, int], dict]:
    """建立 (点id, 人员) → 到达信息（分钟/钟面/入边）。

    有班次排程时用 schedule entries；无排程时沿路线累计步行+停留分钟。
    """
    shift = sch.shift_start_minutes(plan)
    idx: dict[tuple[str, int], dict] = {}
    if shift is not None:
        for key, e in _schedule_entries(route).items():
            idx[key] = {
                "arrivalMin": e["arrivalMin"], "clock": e["arrivalClock"],
                "lateMin": e.get("lateMin", 0.0),
                "waitMin": e.get("waitMin", 0.0),
                "estimated": e.get("estimated", False),
            }
        return idx

    speed = (route.get("stats") or {}).get("walkSpeedMps") or 1.2
    for w, pts in _chains(route).items():
        segs = [s for s in route.get("segments", [])
                if (s.get("worker") or 0) == w]
        seg_by_to = {s["to"]: s for s in segs}
        t = 0.0
        for i, p in enumerate(pts):
            if i > 0:
                s = seg_by_to.get(p["id"])
                if s is not None and not s.get("blocked"):
                    t += s.get("lengthM", 0.0) / speed / 60.0
                else:
                    t = None  # 受阻之后时刻不可信
            if t is None:
                idx[(p["id"], w)] = {"arrivalMin": None, "clock": None,
                                     "lateMin": 0.0, "estimated": True}
                continue
            idx[(p["id"], w)] = {
                "arrivalMin": round(t, 2),
                "clock": sch.clock_label(t, shift or 0),
                "lateMin": 0.0, "estimated": False,
            }
            # 停留计入下一站出发
            if p.get("kind") not in ("start", "end"):
                t += ((route.get("stats") or {}).get("dwellSeconds", 0)
                      or 0) / 60.0
    return idx


def _point_cells(route: dict) -> dict[tuple[int, int], str]:
    """栅格位置 → 点id（自动点按位置对齐用）。"""
    return {(p["gx"], p["gy"]): p["id"] for p in route.get("points", [])}


def _path_sim(a_cells: set, b_cells: set) -> float:
    if not a_cells or not b_cells:
        return 0.0
    return len(a_cells & b_cells) / len(a_cells | b_cells)


# ---------------------------------------------------------------------------
# 差异比较
# ---------------------------------------------------------------------------

def compare_routes(base: dict, variant: dict, plan: dict,
                   variant_plan: dict, scenario: dict,
                   active: list[dict] | None = None) -> dict:
    """基线 vs 推演：受影响点位、延误、绕行、重复经过、冲突。

    active 为本轮真正参与计算的封闭通道（错峰通道不进入切过判定/高亮）。
    """
    t0 = time.time()
    if active is None:
        active, _ = active_closures(scenario, plan, base)
    base_arr = _arrival_index(base, plan)
    var_arr = _arrival_index(variant, variant_plan)
    base_cells = _point_cells(base)
    var_cells = _point_cells(variant)

    base_chains = _chains(base)
    var_chains = _chains(variant)

    # 入边表（按人员、到达点）
    def incoming(chains, route):
        out: dict[tuple[str, int], dict] = {}
        for s in route.get("segments", []):
            out[(s["to"], s.get("worker") or 0)] = s
        return out

    base_in = incoming(base_chains, base)
    var_in = incoming(var_chains, variant)

    # 栅格宽（路径 cell 转线性索引）
    gw = (variant.get("gridInfo") or base.get("gridInfo") or {}).get("gw") or 0

    def cellset(seg):
        return {(x, y) for x, y in (seg.get("pathCells") or [])}

    # ---- 逐点比较 ----------------------------------------------------------
    affected_points: list[dict] = []
    dropped_keys: set[tuple[str, int]] = set()
    matched_var: set[tuple[str, int]] = set()

    for w, pts in base_chains.items():
        var_pts = var_chains.get(w, [])
        var_by_id = {(p["id"], w): p for p in var_pts}
        var_by_cell: dict[tuple[int, int], dict] = {}
        for q in var_pts:
            var_by_cell.setdefault((q["gx"], q["gy"]), q)
        used_var: set[tuple] = set()
        for p in pts:
            bkey = (p["id"], w)
            vp = None
            if p.get("kind") != "auto":
                # 固定点/必经点/手动点 ID 稳定，按 ID 对齐
                vp = var_by_id.get(bkey)
            else:
                # 自动点 ID 按生成顺序分配，封路后会重排 → 按栅格位置对齐
                cand = var_by_cell.get((p["gx"], p["gy"]))
                if cand and cand.get("kind") == "auto" \
                        and (cand["id"], w) not in used_var:
                    vp = cand
            if vp is None:
                dropped_keys.add(bkey)
                affected_points.append({
                    "id": p["id"], "worker": w,
                    "label": p.get("label") or p["id"],
                    "kind": p.get("kind"), "role": p.get("role"),
                    "status": "dropped",
                    "baseSeq": p.get("seq"), "varSeq": None,
                    "delayMin": None, "baseClock": None, "varClock": None,
                    "detourM": None, "rerouted": False,
                })
                continue
            vkey = (vp["id"], w)
            used_var.add(vkey)
            matched_var.add(vkey)
            vseg = var_in.get(vkey)
            blocked = bool(vseg and vseg.get("blocked"))
            ba, va = base_arr.get(bkey), var_arr.get(vkey)
            delay = None
            if ba and va and ba.get("arrivalMin") is not None \
                    and va.get("arrivalMin") is not None:
                delay = round(va["arrivalMin"] - ba["arrivalMin"], 1)

            detour = None
            sim = None
            bseg = base_in.get(bkey)
            if bseg and vseg and not bseg.get("blocked") and not vseg.get("blocked"):
                bc, vc_ = cellset(bseg), cellset(vseg)
                sim = round(_path_sim(bc, vc_), 2)
                detour = round(max(0.0, vseg.get("lengthM", 0)
                                   - bseg.get("lengthM", 0)), 1)
            rerouted = (detour or 0) > 0.1 and (sim is None or sim < SAME_ROUTE_SIM)

            status = "ok"
            if blocked:
                status = "blocked"
            elif delay is not None and delay > DELAY_EPS_MIN:
                status = "delayed"        # 延误优先，绕行情况通过 rerouted 标记保留
            elif rerouted:
                status = "rerouted"

            if status != "ok":
                affected_points.append({
                    "id": vp["id"], "worker": w,
                    "label": vp.get("label") or p.get("label") or vp["id"],
                    "kind": vp.get("kind"), "role": vp.get("role"),
                    "status": status,
                    "baseSeq": p.get("seq"), "varSeq": vp.get("seq"),
                    "delayMin": delay,
                    "baseClock": ba.get("clock") if ba else None,
                    "varClock": va.get("clock") if va else None,
                    "detourM": detour, "pathSimilarity": sim,
                    "rerouted": rerouted,
                })

    # 推演新增固定点（主要是交接点；自动点按栅格对齐，不视为新增）
    base_fixed_ids = {(p["id"], p.get("worker") or 0)
                      for pts in base_chains.values() for p in pts
                      if p.get("kind") in ("start", "end", "must")}
    new_points = []
    for p in variant.get("points", []):
        key = (p["id"], p.get("worker") or 0)
        if key in matched_var or key in base_fixed_ids:
            continue
        if p.get("kind") in ("start", "end", "must"):
            new_points.append({
                "id": p["id"], "worker": key[1],
                "label": p.get("label") or p["id"],
                "kind": p.get("kind"), "role": p.get("role"),
                "varSeq": p.get("seq"),
                "varClock": (var_arr.get(key) or {}).get("clock"),
            })

    # ---- 绕行段 / 封路切过基线路径（仅参与计算的通道） --------------------
    active_scenario = dict(scenario)
    active_scenario["closures"] = active
    closure_cells = _closure_cells(plan, active_scenario)
    base_blocked_by_closure = _segments_intersecting(base, closure_cells)
    var_segments_diff = _segment_diffs(base_in, var_in, base_chains,
                                       var_chains, cellset)

    # ---- 重复经过（同一格被同一路线多次经过） ------------------------------
    repeated = _repeated_length_m(variant, plan)

    # ---- 汇总指标 ----------------------------------------------------------
    bs, vs_ = base.get("stats") or {}, variant.get("stats") or {}
    bc_, vc2 = base.get("coverage") or {}, variant.get("coverage") or {}
    eta_delta = _num_delta(vs_.get("etaMinutes"), bs.get("etaMinutes"))
    length_delta = _num_delta(vs_.get("totalLengthM"), bs.get("totalLengthM"))
    cov_delta = _num_delta(vc2.get("percent"), bc_.get("percent"))

    delayed = [a for a in affected_points if a["status"] == "delayed"]
    blocked_pts = [a for a in affected_points if a["status"] == "blocked"]
    dropped = [a for a in affected_points if a["status"] == "dropped"]
    rerouted = [a for a in affected_points if a["status"] == "rerouted"]
    max_delay = round(max((a["delayMin"] or 0) for a in delayed), 1) \
        if delayed else 0.0

    summary = {
        "affectedPointCount": len(affected_points),
        "delayedCount": len(delayed),
        "blockedPointCount": len(blocked_pts),
        "droppedCount": len(dropped),
        "reroutedCount": len(rerouted),
        "newPointCount": len(new_points),
        "maxDelayMin": max_delay,
        "totalDelayMin": round(sum(a["delayMin"] or 0 for a in delayed), 1),
        "etaDeltaMin": eta_delta,
        "lengthDeltaM": length_delta,
        "detourM": round(sum(max(0.0, d.get("extraM") or 0.0)
                             for d in var_segments_diff["rerouted"]), 1),
        "repeatedM": repeated["totalM"],
        "repeatedSegments": repeated["segments"],
        "blockedSegmentDelta": int(vs_.get("blockedCount", 0)
                                   - bs.get("blockedCount", 0)),
        "coverageDeltaPct": cov_delta,
        "baseBlockedSegmentCount": len(base_blocked_by_closure),
    }

    conflicts = _build_conflicts(
        scenario, variant, variant_plan, affected_points, new_points,
        base_blocked_by_closure, closure_cells, summary, base, bs, vs_, bc_,
        vc2)

    return {
        "affectedPoints": affected_points,
        "newPoints": new_points,
        "segmentDiffs": var_segments_diff["rerouted"],
        "repeatedSegments": repeated["segments"],
        "closureCells": sorted(closure_cells)[:4000],
        "closureTruncated": len(closure_cells) > 4000,
        "summary": summary,
        "conflicts": conflicts,
        "elapsedMs": int((time.time() - t0) * 1000),
    }


def _num_delta(a, b) -> float | None:
    try:
        return round(float(a) - float(b), 1)
    except (TypeError, ValueError):
        return None


def _closure_cells(plan: dict, scenario: dict) -> set[tuple[int, int]]:
    """封闭通道覆盖的栅格（用方案栅格参数离线栅格化，供画布高亮与切过判定）。"""
    img = plan.get("image") or {}
    width = float(img.get("width") or 1600)
    height = float(img.get("height") or 1000)
    occ_gw, occ_gh = img.get("gridWidth"), img.get("gridHeight")
    if occ_gw and occ_gh and img.get("occupancy"):
        gw, gh = int(occ_gw), int(occ_gh)
        cell = width / gw
    else:
        gw, gh, cell = ras.grid_dimensions(width, height)
    n = gw * gh
    layer = bytearray(n)
    reason = [""] * n
    for c in scenario["closures"]:
        ras.rasterize_closure(layer, reason, gw, gh, cell, c)
    return {(i % gw, i // gw) for i, v in enumerate(layer) if v}


def _segments_intersecting(route: dict, cells: set[tuple[int, int]]) -> list[dict]:
    """基线中路径经过封闭格的路段。"""
    if not cells:
        return []
    hits = []
    for s in route.get("segments", []):
        if s.get("blocked"):
            continue
        sc = {(x, y) for x, y in s.get("pathCells") or []}
        if sc & cells:
            hits.append({"seq": s.get("seq"), "worker": s.get("worker") or 0,
                         "from": s["from"], "to": s["to"],
                         "crossCells": len(sc & cells)})
    return hits


def _segment_diffs(base_in, var_in, base_chains, var_chains, cellset) -> dict:
    """逐入边对比：路径明显变化即 rerouted。"""
    rerouted = []
    for key, vseg in var_in.items():
        bseg = base_in.get(key)
        if not bseg:
            continue
        if vseg.get("blocked") or bseg.get("blocked"):
            continue
        bc, vc_ = cellset(bseg), cellset(vseg)
        sim = _path_sim(bc, vc_)
        if sim < SAME_ROUTE_SIM:
            rerouted.append({
                "pointId": key[0], "worker": key[1],
                "from": vseg.get("from"), "to": vseg.get("to"),
                "baseLengthM": bseg.get("lengthM"),
                "varLengthM": vseg.get("lengthM"),
                "extraM": round(vseg.get("lengthM", 0)
                                - bseg.get("lengthM", 0), 1),
                "similarity": round(sim, 2),
            })
    return {"rerouted": rerouted}


def _repeated_length_m(variant: dict, plan: dict) -> dict:
    """同一人员路线中被两条以上路段共用的栅格长度（重复经过）。"""
    ppm = ((plan.get("calibration") or {}).get("pixelsPerMeter")) or 40.0
    grid = variant.get("gridInfo") or {}
    # 像素画布尺寸无栅格时退化为默认值；cellPx 已在 gridInfo 中给出
    cell_m = (grid.get("cellPx") or 10.0) / float(ppm)
    total_m = 0.0
    segs_out = []
    for w in {s.get("worker") or 0 for s in variant.get("segments", [])}:
        use: dict[tuple[int, int], int] = {}
        wsegs = [s for s in variant.get("segments", [])
                 if (s.get("worker") or 0) == w and not s.get("blocked")]
        for s in wsegs:
            for cell in {(x, y) for x, y in s.get("pathCells") or []}:
                use[cell] = use.get(cell, 0) + 1
        repeat = {c for c, n in use.items() if n > 1}
        if not repeat:
            continue
        m = len(repeat) * cell_m
        total_m += m
        segs_out.append({"worker": w, "cells": len(repeat),
                         "lengthM": round(m, 1)})
    return {"totalM": round(total_m, 1), "segments": segs_out}


# ---------------------------------------------------------------------------
# 冲突（需人工确认）
# ---------------------------------------------------------------------------

def _schedule_entries(route: dict) -> dict[tuple[str, int], dict]:
    """单人/多人模式统一的排程条目索引 {(id, worker): entry}。"""
    out: dict[tuple[str, int], dict] = {}
    if route.get("mode") == "multi":
        for rt in route.get("routes", []):
            sim = rt.get("schedule") or {}
            w = rt.get("worker") or 0
            for e in sim.get("entries", []):
                out[(e["id"], e.get("worker", w) or 0)] = e
    else:
        for e in route.get("schedule", {}).get("entries", []):
            out[(e["id"], e.get("worker") or 0)] = e
    return out


def _build_conflicts(scenario, variant, variant_plan, affected, new_points,
                     base_hits, closure_cells, summary, base, bs, vs_,
                     bcov, vcov) -> list[dict]:
    conflicts: list[dict] = []

    def add(severity, ctype, message, key_suffix=None, **extra):
        ident = key_suffix if key_suffix is not None else \
            (extra.get("pointId") or extra.get("closureId") or "")
        key = f"{ctype}:{ident}"
        conflicts.append({"key": key, "severity": severity, "type": ctype,
                          "message": message, "confirmed": bool(
                              scenario.get("confirmations", {}).get(key)),
                          **extra})

    # 0) 设置了交接点但未排班 → 无法校核交接时刻
    if scenario.get("handovers") and sch.shift_start_minutes(variant_plan) is None:
        add("medium", "no-shift",
            "已设置交接点，但方案未设置班次开始时间，无法校核交接时刻，"
            "请先在「班次与排程」中设置班次开始时间", key_suffix="plan")

    # 1) 推演后出现不可达路段
    for s in variant.get("segments", []):
        if s.get("blocked"):
            add("high", "blocked-segment",
                f"人员 {s.get('worker', 0) + 1} 路段 {s.get('fromSeq')}→"
                f"{s.get('toSeq')} 在推演条件下无法连通："
                f"{(s.get('reason') or {}).get('summary', '')}",
                key_suffix=f"{s.get('worker', 0)}-{s.get('seq')}",
                pointId=s.get("to"), worker=s.get("worker") or 0)

    # 2) 巡检点无法再到达（丢失）
    for a in affected:
        if a["status"] == "dropped":
            add("high", "point-unreachable",
                f"巡检点「{a['label']}」在推演条件下无法到达，将从路线中丢失",
                pointId=a["id"], worker=a["worker"])

    # 3) 交接点：未在交接时刻前完成
    var_entries = _schedule_entries(variant)
    for np_ in new_points:
        if np_.get("role") != "handover":
            continue
        ent = var_entries.get((np_["id"], np_["worker"]))
        hv = next((h for h in scenario["handovers"] if h["id"] == np_["id"]),
                  None)
        if ent and hv and hv.get("handoverClock") and ent.get("lateMin", 0) > 0:
            add("high", "handover-late",
                f"交接点「{np_['label']}」预计 {ent['arrivalClock']} 到达，"
                f"晚于交接时刻 {hv['handoverClock']} 约 "
                f"{round(ent['lateMin'])} 分钟",
                pointId=np_["id"], worker=np_["worker"])

    # 4) 封路切过基线路线（必然产生绕行/断点）
    for hit in base_hits:
        add("medium", "closure-crosses-route",
            f"封闭通道切过基线 {hit['from']}→{hit['to']} 段"
            f"（人员 {hit['worker'] + 1}），已改走替代路线，请现场确认绕行可行",
            key_suffix=f"{hit['worker']}-{hit['seq']}",
            segmentSeq=hit["seq"], worker=hit["worker"])

    # 5) 排程新增逾期（基线不逾期 → 推演逾期）
    base_entries = _schedule_entries(base)
    for key, e in var_entries.items():
        if e.get("lateMin", 0) > 0 \
                and (base_entries.get(key) or {}).get("lateMin", 0) <= 0:
            add("high", "new-late",
                f"「{e.get('label') or key[0]}」因推演调整新增逾期约 "
                f"{round(e['lateMin'])} 分钟（预计 {e['arrivalClock']}）",
                pointId=key[0], worker=key[1])

    # 6) 预计延误较大
    if summary["maxDelayMin"] >= 5:
        add("medium", "delay",
            f"最大单点预计延误 {summary['maxDelayMin']:.0f} 分钟，"
            f"全队预计用时变化 {(summary.get('etaDeltaMin') or 0):+.0f} 分钟",
            key_suffix="max")

    # 7) 重复经过
    rep_m = summary.get("repeatedM") or 0
    if rep_m > REPEAT_WARN_M:
        add("low", "repeated-route",
            f"推演路线存在重复经过路段，合计约 {rep_m:.0f} m，"
            "请确认是否需要进一步调整分区", key_suffix="plan")

    # 8) 覆盖率下降
    if summary.get("coverageDeltaPct") is not None \
            and summary["coverageDeltaPct"] <= -COVERAGE_DROP_WARN:
        add("medium", "coverage-drop",
            f"覆盖率下降 {abs(summary['coverageDeltaPct']):.1f} 个百分点"
            f"（{bcov.get('percent')}% → {vcov.get('percent')}%）",
            key_suffix="plan")

    # 9) 封路时段与本班巡逻时间不重叠：该通道未参与重算，仅作提示
    shift = sch.shift_start_minutes(variant_plan)
    active_ids = {c["id"] for c in active_closures(scenario, variant_plan, base)[0]}
    for c in scenario["closures"]:
        if c["id"] in active_ids:
            continue
        if shift is None or not c.get("effectiveStart"):
            continue
        add("low", "outside-shift",
            f"「{c['label']}」生效时段 {c['effectiveStart']}–"
            f"{c['effectiveEnd']} 与本班巡逻时间不重叠，未参与路线重算",
            closureId=c["id"])

    # 去重（同 key 保留严重度最高的一条）
    sev_rank = {"high": 3, "medium": 2, "low": 1}
    best: dict[str, dict] = {}
    for c in conflicts:
        old = best.get(c["key"])
        if old is None or sev_rank[c["severity"]] > sev_rank[old["severity"]]:
            best[c["key"]] = c
    out = list(best.values())
    out.sort(key=lambda c: -sev_rank[c["severity"]])
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def evaluate_scenario(plan: dict, scenario: dict,
                      baseline: dict | None = None) -> dict:
    """基线全量 → 叠加场景全量 → 差异分析，返回推演结果包。

    返回：{scenario, baseline(route), variant(route), impact,
           feasible, confirmedAll}
    """
    sc = normalize_scenario(scenario)
    t0 = time.time()
    base = baseline if baseline is not None else build_route(plan)
    active, idle = active_closures(sc, plan, base)
    variant_plan, notes = apply_scenario(plan, sc, baseline=base)
    variant = build_route(variant_plan)

    impact = compare_routes(base, variant, plan, variant_plan, sc,
                            active=active)
    impact["idleClosures"] = [
        {"id": c["id"], "label": c.get("label") or c["id"],
         "effectiveStart": c.get("effectiveStart"),
         "effectiveEnd": c.get("effectiveEnd")}
        for c in idle
    ]

    # 给推演点位/路段打影响标签，供画布直接上色
    _tag_variant(variant, impact, variant_plan, sc)

    unconfirmed = [c for c in impact["conflicts"] if not c["confirmed"]]
    return {
        "scenario": sc,
        "notes": notes,
        "baseline": _route_brief(base),
        "variant": variant,
        "impact": impact,
        "feasible": variant.get("stats", {}).get("blockedCount", 0) == 0
                    and not any(c["severity"] == "high"
                                for c in impact["conflicts"]),
        "confirmedAll": len(unconfirmed) == 0,
        "elapsedMs": int((time.time() - t0) * 1000),
    }


def _route_brief(route: dict) -> dict:
    """基线只回传渲染/对比必需的精简结构（完整路径仍保留以便画布切换）。"""
    return route


def _tag_variant(variant: dict, impact: dict, variant_plan: dict,
                 scenario: dict) -> None:
    """把 impact 状态写回 variant.points / segments。"""
    pt_status = {(a["id"], a.get("worker") or 0): a
                 for a in impact["affectedPoints"]}
    for p in variant.get("points", []):
        a = pt_status.get((p["id"], p.get("worker") or 0))
        if a:
            p["impact"] = a["status"]
            p["delayMin"] = a.get("delayMin")
            p["detourM"] = a.get("detourM")
        if p.get("role") == "handover":
            p["impact"] = p.get("impact") or "handover"
    for s in variant.get("segments", []):
        if s.get("blocked"):
            s["impact"] = "blocked"
        else:
            d = next((d for d in impact["segmentDiffs"]
                      if d["to"] == s["to"]
                      and (d.get("worker") or 0) == (s.get("worker") or 0)),
                     None)
            if d:
                s["impact"] = "rerouted"
                s["extraM"] = d.get("extraM")


# ---------------------------------------------------------------------------
# 影响摘要的人类可读文本（保存版本/差异说明用）
# ---------------------------------------------------------------------------

def impact_text_lines(ev: dict) -> list[str]:
    s = ev["impact"]["summary"]
    sc = ev["scenario"]
    lines = [f"推演方案：{sc['name']}"]
    if sc.get("reason"):
        lines.append(f"变更原因：{sc['reason']}")
    win = _scenario_window_text(sc)
    if win:
        lines.append(f"生效时段：{win}")
    lines.append(
        f"受影响巡检点 {s['affectedPointCount']} 个"
        f"（延误 {s['delayedCount']}、绕行 {s['reroutedCount']}、"
        f"不可达 {s['blockedPointCount'] + s['droppedCount']}）；"
        f"预计延误最多 {s['maxDelayMin']:.0f} 分钟，"
        f"全队用时变化 {_sig(s['etaDeltaMin'])} 分钟，"
        f"绕行增加 {s['detourM']:.0f} m，"
        f"重复经过约 {(s.get('repeatedM') or 0):.0f} m，"
        f"受阻路段变化 {_sig(s['blockedSegmentDelta'])} 段，"
        f"覆盖率变化 {_sig(s['coverageDeltaPct'])} 个百分点。"
    )
    if ev["impact"]["conflicts"]:
        lines.append("需人工确认：")
        for c in ev["impact"]["conflicts"]:
            mark = "✓" if c["confirmed"] else "✗"
            lines.append(f"  [{mark}][{c['severity']}] {c['message']}")
    return lines


def _sig(v) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}" if v else "0"


def _scenario_window_text(sc: dict) -> str:
    parts = []
    for c in sc.get("closures", []):
        if c.get("effectiveStart"):
            rng = f"{c['effectiveStart']}–{c.get('effectiveEnd') or '次日'}"
            if c.get("validFrom") or c.get("validTo"):
                rng += f"（{c.get('validFrom') or '…'}~{c.get('validTo') or '…'}）"
            parts.append(f"{c['label']} {rng}")
    return "；".join(parts)


def today_iso() -> str:
    return date.today().isoformat()
