"""时间窗排程：按班次开始时间与点位约束重排必经点、仿真时刻、统计超窗。

核心概念
--------
* 班次开始时间 ``settings.shiftStart``（"HH:MM"）；缺省即不排程，路线保持
  原有「只按距离编排」的行为。
* 必经点 ``mustPass[i]`` 可携带：
    - ``readyClock``/``dueClock``：最早到达 / 最晚完成（"HH:MM"，可跨午夜）；
    - ``dwellSeconds``：本站停留秒数（缺省取全局 settings.dwell）；
    - ``priority``：1..5，数字越大越优先保证不逾期。
* 排程器只重排「锚点」（起终点 + 必经点），自动巡检点填充在相邻锚点之间，
  不参与顺序优化，仅产生步行/停留耗时。
* 锚点重排用多源 BFS 距离场（8 邻接，与 A* 同尺度）估算站间耗时；
  最终时刻用逐段 A* 的实际路段长度仿真，保证与画布路径一致。
* 无可行顺序时不报错：保留几何最优的可执行路线，逐站列出冲突与原因
  （逾期分钟、等待、不可达估算）。
"""

from __future__ import annotations

import math
import re

from .graph import distance_field

MAX_REORDER_ANCHORS = 24      # 参与时间窗重排的锚点上限（含起终点）
DEFAULT_DWELL_S = 30.0
WALK_SPEED_MPS = 1.2           # 与 route.py 保持一致
BLOCKED_PENALTY_S = 100_000.0  # 重排评分中不可达段的大惩罚，仅用于分胜负

_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def parse_clock(s) -> int | None:
    """"HH:MM" → 当天分钟数（0..1439）；非法返回 None。"""
    if not isinstance(s, str):
        return None
    m = _CLOCK_RE.match(s.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59 or (h == 24 and mi):
        return None
    return h * 60 + mi


def clock_offset(clock: str | None, shift: int) -> int | None:
    """把墙上时钟换算成「班次开始后的分钟数」。

    时刻早于班次开始则视为次日（跨午夜，如班次 22:00、窗口 02:00 → +240）。
    """
    t = parse_clock(clock)
    if t is None:
        return None
    off = t - shift
    if off < 0:
        off += 1440
    return off


def clock_label(offset_min: float, shift: int) -> str:
    """班次相对分钟 → "HH:MM" 墙上时钟（支持跨午夜，分钟四舍五入）。"""
    total = shift + int(round(offset_min))
    total %= 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def shift_start_minutes(plan: dict) -> int | None:
    return parse_clock((plan.get("settings") or {}).get("shiftStart"))


def _global_dwell(plan: dict) -> float:
    v = (plan.get("settings") or {}).get("dwell")
    try:
        return DEFAULT_DWELL_S if v is None else float(v)
    except (TypeError, ValueError):
        return DEFAULT_DWELL_S


def _dwell_for(rec: dict, default_s: float) -> float:
    if rec.get("kind") in ("start", "end"):
        return 0.0
    v = rec.get("dwellSeconds")
    if v is None:
        v = (rec.get("window") or {}).get("dwellSeconds")
    try:
        return max(0.0, float(v)) if v is not None else default_s
    except (TypeError, ValueError):
        return default_s


def _dwell_anchor(rec: dict, windows: dict[str, dict], default_s: float) -> float:
    """锚点仿真取停留：先记录字段，再查窗口表中的点级 dwellSeconds。"""
    if rec.get("kind") in ("start", "end"):
        return 0.0
    v = rec.get("dwellSeconds")
    if v is None:
        w = windows.get(rec.get("id"))
        if w:
            v = w.get("dwellSeconds")
    try:
        return max(0.0, float(v)) if v is not None else default_s
    except (TypeError, ValueError):
        return default_s


# ---------------------------------------------------------------------------
# 窗口数据
# ---------------------------------------------------------------------------

def point_windows(plan: dict, shift: int) -> dict[str, dict]:
    """从 mustPass 解析每点的时间窗（班次相对分钟）。

    返回 {pointId: {ready, due, priority, dwellSeconds, readyClock, dueClock}}。
    """
    out: dict[str, dict] = {}
    for p in plan.get("mustPass", []):
        pid = p.get("id")
        if pid is None:
            continue
        ready = clock_offset(p.get("readyClock"), shift)
        due = clock_offset(p.get("dueClock"), shift)
        try:
            prio = max(1, min(5, int(p.get("priority", 3))))
        except (TypeError, ValueError):
            prio = 3
        dwell = p.get("dwellSeconds")
        try:
            dwell = max(0.0, float(dwell)) if dwell not in (None, "") else None
        except (TypeError, ValueError):
            dwell = None
        out[pid] = {"ready": ready, "due": due, "priority": prio,
                    "dwellSeconds": dwell,
                    "readyClock": p.get("readyClock"),
                    "dueClock": p.get("dueClock")}
    return out


def window_conflict(raw: dict, shift: int) -> str | None:
    """时间窗自身是否无效（最晚完成早于最早到达，且未跨午夜）。"""
    r = clock_offset(raw.get("readyClock"), shift)
    d = clock_offset(raw.get("dueClock"), shift)
    if r is not None and d is not None and d < r:
        name = raw.get("label") or raw.get("id")
        return (f"必经点「{name}」时间窗无效：最晚完成 {raw['dueClock']} "
                f"早于最早到达 {raw['readyClock']}")
    return None


# ---------------------------------------------------------------------------
# 锚点站间耗时（栅格距离场）
# ---------------------------------------------------------------------------

def anchor_travel_matrix(anchors: list[dict], blocked, gw: int, gh,
                         cell: float, ppm: float, speed: float = WALK_SPEED_MPS):
    """计算锚点两两站间步行秒数矩阵（8 邻接 BFS 距离场，不可达退回直线）。

    返回 (matrix, estimated)：matrix[i][j] 秒；estimated[i][j] 为 True
    表示该方向不可达、使用直线距离估算。
    """
    fields: list[list[float | None] | None] = []
    for a in anchors:
        if len(anchors) > MAX_REORDER_ANCHORS:
            fields.append(None)
        else:
            fields.append(distance_field(
                [a["gy"] * gw + a["gx"]], blocked, gw, gh))
    n = len(anchors)
    matrix = [[0.0] * n for _ in range(n)]
    estimated = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            d_cells = None
            if fields[i] is not None:
                d_cells = fields[i][anchors[j]["gy"] * gw + anchors[j]["gx"]]
            if d_cells is None:
                d_cells = math.hypot(anchors[i]["x"] - anchors[j]["x"],
                                     anchors[i]["y"] - anchors[j]["y"]) / cell
                estimated[i][j] = True
            matrix[i][j] = d_cells * cell / ppm / speed
    return matrix, estimated


# ---------------------------------------------------------------------------
# 锚点重排：带时间窗的贪心插入（最小化加权逾期分钟）
# ---------------------------------------------------------------------------

def _anchor_simulate(order_idx, anchors, travel, estimated, windows,
                     default_dwell):
    """按锚点顺序前向仿真（等待计入），返回 (加权逾期分, 不可达段数, 步行秒)。

    「最晚完成」按离开本站的时刻判定；等待只发生在服务开始前。
    """
    t = 0.0
    weighted_late = 0.0
    n_est = 0
    walk = 0.0
    for pos, ai in enumerate(order_idx):
        rec = anchors[ai]
        if pos > 0:
            pa = order_idx[pos - 1]
            leg = travel[pa][ai] / 60.0     # travel 矩阵单位为秒
            walk += leg
            if estimated[pa][ai]:
                n_est += 1
                weighted_late += BLOCKED_PENALTY_S / 60.0
            t += leg
        w = windows.get(rec["id"])
        if w and w["ready"] is not None and t < w["ready"]:
            t = w["ready"]                    # 原地等待开门/窗口
        dwell = _dwell_anchor(rec, windows, default_dwell) / 60.0
        leave = t + dwell
        if w and w["due"] is not None and leave > w["due"]:
            weighted_late += (leave - w["due"]) * w["priority"]
        t = leave
    return weighted_late, n_est, walk


def plan_anchor_order(anchors: list[dict], blocked, gw: int, gh, cell: float,
                      ppm: float, windows: dict[str, dict],
                      default_dwell: float, speed: float = WALK_SPEED_MPS):
    """决定锚点访问顺序。

    返回 (perm, reason, travel, estimated)：
      perm    —— anchors 的索引排列；
      reason  —— "fixed"（无窗口约束/锚点过多，沿用几何顺序）或
                 "reorder"（按时间窗重排）；
    """
    n = len(anchors)
    identity = list(range(n))
    if n <= 2:
        return identity, "fixed", None, None

    # 注意：routes 记录里手动移动过的自动点 kind 也可能是 "must"，
    # 但它不在 windows 中——必须按 id 判定真正的必经点锚点。
    windowed = [i for i, a in enumerate(anchors)
                if a["id"] in windows
                and (windows[a["id"]]["ready"] is not None
                     or windows[a["id"]]["due"] is not None)]
    if not windowed:
        return identity, "fixed", None, None

    if n > MAX_REORDER_ANCHORS:
        return identity, "fixed", None, None

    travel, estimated = anchor_travel_matrix(
        anchors, blocked, gw, gh, cell, ppm, speed)

    start_idx = next((i for i, a in enumerate(anchors)
                      if a.get("kind") == "start"), None)
    end_idx = next((i for i, a in enumerate(anchors)
                    if a.get("kind") == "end"), None)

    # 有窗口的必须满足者：按最晚完成升序，其次优先级降序、最早到达升序
    windowed.sort(key=lambda i: (
        windows[anchors[i]["id"]]["due"]
        if windows[anchors[i]["id"]]["due"] is not None else 1e9,
        -windows[anchors[i]["id"]]["priority"],
        windows[anchors[i]["id"]]["ready"]
        if windows[anchors[i]["id"]]["ready"] is not None else -1e9))
    fixed_set = {i for i in windowed}
    if start_idx is not None:
        fixed_set.add(start_idx)
    if end_idx is not None:
        fixed_set.add(end_idx)

    # 其余无窗口必经点：保持几何次序，按几何序逐个插入（不影响窗口点的相对关系）
    free_others = [i for i in range(n) if i not in fixed_set]

    order = []
    if start_idx is not None:
        order.append(start_idx)
    order.extend(windowed)
    if end_idx is not None and (not order or order[-1] != end_idx):
        order.append(end_idx)

    def score(o):
        weighted, n_est, walk = _anchor_simulate(
            o, anchors, travel, estimated, windows, default_dwell)
        # 先比加权逾期（不可达已计入大惩罚），再比纯步行；n_est 作次级保险
        return (weighted, walk, n_est)

    best_score = score(order)
    for ai in free_others:
        best_o, best_s = None, None
        lo = 1 if (order and order[0] == start_idx) else 0
        hi = len(order) - 1 if (order and order[-1] == end_idx) else len(order)
        for pos in range(lo, hi + 1):
            cand = order[:pos] + [ai] + order[pos:]
            s = score(cand)
            if best_s is None or s < best_s:
                best_s, best_o = s, cand
        order = best_o

    if order == identity:
        return identity, "fixed", travel, estimated
    # 与原几何顺序对比：重排确实更优（加权逾期更小，其次步行更短）才采用
    if score(identity) <= best_score:
        return identity, "fixed", travel, estimated
    return order, "reorder", travel, estimated


# ---------------------------------------------------------------------------
# 完整记录重排：自动点随相邻锚点归桶
# ---------------------------------------------------------------------------

def apply_anchor_permutation(records: list[dict], perm: list[int],
                             anchors: list[dict]) -> list[dict]:
    """按锚点新顺序重建完整记录序列，自动巡检点跟随其后一个锚点移动。

    正向扫描时桶结构恒为 [锚点自身, 其后的自动点…]；首锚点之前的记录
    （无起点时的散点）放入 pre 保持在队首。
    """
    anchor_ids = {a["id"] for a in anchors}
    buckets: dict[str, list[dict]] = {a["id"]: [] for a in anchors}
    pre: list[dict] = []
    cur: str | None = None
    for r in records:
        if r["id"] in anchor_ids:
            cur = r["id"]
        (buckets[cur] if cur is not None else pre).append(r)

    out = list(pre)
    for ai in perm:
        out.extend(dict(r) for r in buckets[anchors[ai]["id"]])
    for i, r in enumerate(out):
        r["seq"] = i + 1
    return out


# ---------------------------------------------------------------------------
# 精确时刻仿真：逐段 A* 实际路径
# ---------------------------------------------------------------------------

def simulate(records: list[dict], segments: list[dict], plan: dict,
             shift: int, windows: dict[str, dict] | None = None,
             worker: int | None = None,
             speed: float = WALK_SPEED_MPS) -> dict:
    """按访问顺序仿真每站的到达/离开/等待/超窗。

    不可达路段按直线距离估算耗时并标记 estimated、计入 conflicts。
    返回 {"entries", "summary", "conflicts"}。
    """
    if windows is None:
        windows = point_windows(plan, shift)
    default_dwell = _global_dwell(plan)
    ppm_val = (plan.get("calibration") or {}).get("pixelsPerMeter")
    ppm = float(ppm_val) if ppm_val not in (None, 0) else 40.0
    # 入边表：to=本站 id 的路段（simulate 关心「到达本站走了多久」）
    seg_by_to: dict[str, dict] = {}
    for s in segments:
        if s.get("to") is not None:
            seg_by_to[s["to"]] = s
    labels = {p.get("id"): p.get("label")
              for p in plan.get("mustPass", []) if p.get("id") is not None}

    entries: list[dict] = []
    conflicts: list[dict] = []

    t = 0.0          # 班次相对分钟
    total_wait = 0.0
    total_walk_s = 0.0
    total_est_s = 0.0
    late_points = 0
    ready_pending = set()

    for idx, rec in enumerate(records):
        seg = seg_by_to.get(rec["id"])
        # 正常情况下第 idx 站的入边即 segments[idx-1]；对齐失败再按 to 查
        if idx > 0 and seg is None and idx - 1 < len(segments):
            seg = segments[idx - 1]
        walk_min = 0.0
        est = False
        blocked = False
        if idx > 0:
            prev = records[idx - 1]
            if seg is not None and not seg.get("blocked"):
                walk_min = (seg.get("lengthM", 0.0)) / speed / 60.0
            else:
                if seg is not None and seg.get("blocked"):
                    blocked = True
                est = True
                m = math.hypot(rec["x"] - prev["x"], rec["y"] - prev["y"]) / ppm
                walk_min = m / speed / 60.0

        t += walk_min
        arrival = t
        total_walk_s += walk_min * 60.0
        if est:
            total_est_s += walk_min * 60.0

        w = windows.get(rec["id"], {}) if rec.get("kind") == "must" else {}
        ready = w.get("ready")
        due = w.get("due")
        prio = w.get("priority", 3)

        wait = 0.0
        if ready is not None and arrival < ready:
            wait = ready - arrival
            t = ready
        start_service = t
        dwell_s = _dwell_for(rec, default_dwell)
        leave = start_service + dwell_s / 60.0
        t = leave
        total_wait += wait

        late_due = 0.0
        if due is not None and arrival > due:
            late_due = arrival - due
        late_finish = 0.0
        if due is not None and leave > due:
            late_finish = leave - due
        # 临界（四舍五入到同一分钟）时仍计为一个逾期点
        if late_finish > 1e-9:
            late_points += 1

        name = rec.get("label") or labels.get(rec["id"]) or rec["id"]
        if blocked:
            reason = (seg.get("reason") or {}).get("summary") \
                if seg is not None else None
            conflicts.append({
                "pointId": rec["id"], "label": name, "type": "unreachable",
                "minutes": round(walk_min, 1),
                "message": f"「{name}」前一路段不可达"
                           + (f"：{reason}" if reason else "")
                           + "，时刻按直线距离估算，请先处理受阻路段",
                "worker": worker,
            })
        if ready is not None and due is not None and due < ready:
            conflicts.append({
                "pointId": rec["id"], "label": name, "type": "invalid-window",
                "minutes": 0,
                "message": f"「{name}」最晚完成早于最早到达，时间窗无法满足",
                "worker": worker,
            })
        if late_finish > 1e-9:
            shown = max(late_finish, 1 if late_finish > 0 else 0)
            msg = (f"「{name}」完成 {clock_label(leave, shift)} 晚于最晚完成 "
                   f"{clock_label(due, shift)}，逾期约 {shown:.0f} 分钟")
            if late_due > 1e-6 and wait > 1e-6:
                msg += "（等待后到达仍已晚）"
            elif late_due > 1e-6:
                msg += "（到达时已晚）"
            conflicts.append({
                "pointId": rec["id"], "label": name, "type": "late",
                "minutes": round(late_finish, 1),
                "message": msg, "worker": worker,
            })
        if wait > 1e-6 and ready is not None:
            ready_pending.add(rec["id"])

        e = {
            "seq": rec.get("seq", idx + 1),
            "id": rec["id"],
            "label": name,
            "kind": rec.get("kind"),
            "worker": worker,
            "arrivalMin": round(arrival, 2),
            "leaveMin": round(leave, 2),
            "arrivalClock": clock_label(arrival, shift),
            "leaveClock": clock_label(leave, shift),
            "walkMin": round(walk_min, 2),
            "waitMin": round(wait, 2),
            "dwellSeconds": round(dwell_s, 1),
            "readyClock": w.get("readyClock"),
            "dueClock": w.get("dueClock"),
            "priority": prio if w else None,
            "lateMin": round(late_finish, 2),
            "lateArrivalMin": round(late_due, 2),
            "estimated": bool(est),
            "blockedIn": bool(blocked),
            "windowed": bool(w),
            "x": rec.get("x"), "y": rec.get("y"),
        }
        if worker is not None:
            e["worker"] = worker
        entries.append(e)

    finish_min = t
    summary = {
        "shiftStart": clock_label(0, shift),
        "finishClock": clock_label(finish_min, shift),
        "finishMin": round(finish_min, 1),
        "walkMinutes": round(total_walk_s / 60.0, 1),
        "waitMinutes": round(total_wait, 1),
        "waitPoints": len(ready_pending),
        "lateMinutes": round(sum(e["lateMin"] for e in entries), 1),
        "latePoints": late_points,
        "windowedPoints": len(windows),
        "estimatedMinutes": round(total_est_s / 60.0, 1),
        "feasible": late_points == 0
                    and not any(c["type"] in ("unreachable", "invalid-window")
                                for c in conflicts),
        "conflictCount": len(conflicts),
    }
    return {"entries": entries, "summary": summary, "conflicts": conflicts}


def simulate_score(result: dict) -> tuple:
    """排程结果选优评分：加权逾期+受阻惩罚优先，其次纯步行时间。"""
    s = result["summary"]
    weighted = s["lateMinutes"]
    if s.get("estimatedMinutes"):
        weighted += BLOCKED_PENALTY_S / 60.0
    return (weighted, s["walkMinutes"])


def aggregate_simulations(results: list[dict]) -> dict:
    """多人模式：合并各人员仿真结果为全队汇总。"""
    entries: list[dict] = []
    conflicts: list[dict] = []
    finish_min = 0.0
    walk = wait = late = est = 0.0
    wait_pts = late_pts = win_pts = 0
    feasible = True
    for r in results:
        entries.extend(r["entries"])
        conflicts.extend(r["conflicts"])
        s = r["summary"]
        finish_min = max(finish_min, s["finishMin"])
        walk += s["walkMinutes"]
        wait += s["waitMinutes"]
        wait_pts += s["waitPoints"]
        late += s["lateMinutes"]
        late_pts += s["latePoints"]
        win_pts += s["windowedPoints"]
        est += s["estimatedMinutes"]
        feasible = feasible and s["feasible"]
    shift = parse_clock(results[0]["summary"]["shiftStart"]) or 0 if results else 0
    return {
        "entries": entries,
        "conflicts": conflicts,
        "summary": {
            "shiftStart": results[0]["summary"]["shiftStart"] if results else None,
            "finishClock": clock_label(finish_min, shift),
            "finishMin": round(finish_min, 1),
            "walkMinutes": round(walk, 1),
            "waitMinutes": round(wait, 1),
            "waitPoints": wait_pts,
            "lateMinutes": round(late, 1),
            "latePoints": late_pts,
            "windowedPoints": win_pts,
            "estimatedMinutes": round(est, 1),
            "feasible": feasible,
            "conflictCount": len(conflicts),
        },
    }
