"""候选巡检点生成（六边形密铺）与访问顺序优化（最近邻 + 2-opt）。

多人分区：partition_balance 把待访问点按「估时负载」均衡地分给各人员，
分区时用从各起点出发的 BFS 实际栅格距离场（不可达退回欧氏距离），
点位之间用欧氏距离估算，最终路段仍由 A* 实际路径连接。
"""

from __future__ import annotations

import math

MAX_AUTO_POINTS = 260   # 自动候选点上限，保证 TSP/逐段 A* 可快速完成
WALK_SPEED_MPS = 1.2    # 与 route.py 保持一致，用于把时间负载折算成距离


def generate_candidates(blocked: bytearray, comp: list[int],
                        gw: int, gh: int, cell: float, ppm: float,
                        spacing_m: float, keep: list[tuple[int, int]]):
    """在可通行区域内按巡检间距做六边形密铺生成候选点。

    spacing_m 是「覆盖保证间距」：任意可通行格距某个候选点不超过
    spacing_m。六边形格距（相邻点距离）取 spacing_m，行距 √3/2·spacing_m，
    相邻行错开 spacing_m/2。

    keep: 必须保留的格子（起点/终点/必经点）。
    返回 (cells, spacing_used_m, adjusted)。
    """
    spacing_px = spacing_m * ppm
    spacing_cells = max(2, int(round(spacing_px / cell)))
    adjusted = False

    # 若候选过多，自适应放大间距（六边形密度 ≈ 2/(√3·s²)）
    free = sum(1 for b in blocked if not b)
    if free * 2 / (math.sqrt(3) * spacing_cells * spacing_cells) > MAX_AUTO_POINTS:
        spacing_cells = max(2, int(math.ceil(math.sqrt(
            2 * free / (math.sqrt(3) * MAX_AUTO_POINTS)))))
        adjusted = True

    row_step = max(1, int(round(math.sqrt(3) / 2 * spacing_cells)))
    cells: list[tuple[int, int]] = []
    seen = set()

    for gx, gy in keep:
        if 0 <= gx < gw and 0 <= gy < gh and not blocked[gy * gw + gx] and (gx, gy) not in seen:
            cells.append((gx, gy))
            seen.add((gx, gy))

    row = 0
    y = 0
    while y < gh:
        offset = (spacing_cells // 2) if row % 2 else 0
        x = offset
        while x < gw:
            gx, gy = int(x), int(y)
            if 0 <= gx < gw and 0 <= gy < gh:
                idx = gy * gw + gx
                if not blocked[idx] and (gx, gy) not in seen:
                    cells.append((gx, gy))
                    seen.add((gx, gy))
            x += spacing_cells
        y += row_step
        row += 1

    # 过滤掉非主分量点（以第一个 keep 点所在分量为主，否则取最大分量）
    main_comp = None
    for gx, gy in keep:
        c = comp[gy * gw + gx]
        if c >= 0:
            main_comp = c
            break
    if main_comp is None:
        sizes: dict[int, int] = {}
        for c in comp:
            if c >= 0:
                sizes[c] = sizes.get(c, 0) + 1
        main_comp = max(sizes, key=sizes.get) if sizes else -1
    cells = [c for c in cells if comp[c[1] * gw + c[0]] == main_comp]

    return cells, spacing_cells * cell / ppm, adjusted


# ---------------------------------------------------------------------------
# 多人分区：负载均衡的贪心分配
# ---------------------------------------------------------------------------

def partition_balance(recs: list[dict], k: int,
                      starts: list[dict | None],
                      dist_fields: list,
                      locked: dict[str, int],
                      gw: int, cell: float, ppm: float, dwell_s: float):
    """把待访问点（必经点 + 自动巡检点）分成 k 组，估时负载尽量均衡。

    recs:        点位记录（含 id/x/y/gx/gy）
    starts:      每人员的起点记录或 None
    dist_fields: 每个起点出发的 BFS 距离场（None 表示该人员无起点）
    locked:      {点位id: 人员序号} 人工锁定，强制归属
    返回 k 个列表（组内无序，访问顺序由 optimize_order 决定）。
    """
    groups: list[list[dict]] = [[] for _ in range(k)]
    if not recs or k <= 0:
        return groups

    dwell_cost = dwell_s * WALK_SPEED_MPS * ppm   # 每点停留折算成的像素距离

    # 实际距离查询（距离场以格为单位，乘 cell 得像素；不可达退回欧氏）
    def actual_from_start(r, w):
        f = dist_fields[w]
        if f is not None:
            d = f[r["gy"] * gw + r["gx"]]
            if d is not None:
                return d * cell
        s = starts[w]
        if s is not None:
            return math.hypot(r["x"] - s["x"], r["y"] - s["y"])
        return 0.0

    loads = [0.0] * k                       # 估时负载（像素距离当量）
    tail: list[tuple[float, float] | None] = [
        (s["x"], s["y"]) if s else None for s in starts]

    # 1) 人工锁定的点先归位，并计入负载
    for r in recs:
        w = locked.get(r["id"])
        if w is None or not (0 <= w < k):
            continue
        if tail[w] is not None:
            loads[w] += math.hypot(r["x"] - tail[w][0], r["y"] - tail[w][1])
        loads[w] += dwell_cost
        tail[w] = (r["x"], r["y"])
        groups[w].append(r)

    # 2) 未分配点：离最近起点越远越先安排（远点先占位，近点填缝）
    rest = [r for r in recs
            if not (0 <= locked.get(r["id"], -1) < k)]
    rest.sort(key=lambda r: -min(actual_from_start(r, w) for w in range(k)))

    for r in rest:
        best_w, best_load = 0, math.inf
        for w in range(k):
            extra = dwell_cost
            if tail[w] is not None:
                extra += math.hypot(r["x"] - tail[w][0], r["y"] - tail[w][1])
            cand = loads[w] + extra
            if cand < best_load:
                best_load, best_w = cand, w
        groups[best_w].append(r)
        loads[best_w] = best_load
        tail[best_w] = (r["x"], r["y"])

    return groups


# ---------------------------------------------------------------------------
# 访问顺序：最近邻构造 + 固定起终点的 2-opt
# ---------------------------------------------------------------------------

def optimize_order(points: list[tuple[float, float]],
                   fixed_start: bool = True, fixed_end: bool = True):
    """points 为像素坐标列表，返回优化后的索引顺序。

    起点（第一个）与终点（最后一个，当与起点不同点时）固定。
    用直线欧氏距离做 TSP 近似，实际路段由 A* 连接。
    """
    n = len(points)
    if n <= 2:
        return list(range(n))

    end_locked = fixed_end and n >= 3
    start_idx = 0
    end_idx = n - 1

    def d(a, b):
        return math.hypot(points[a][0] - points[b][0], points[a][1] - points[b][1])

    # 最近邻：从起点出发，终点只在最后访问
    remaining = set(range(n))
    remaining.discard(start_idx)
    if end_locked:
        remaining.discard(end_idx)
    order = [start_idx]
    while remaining:
        cur = order[-1]
        nxt = min(remaining, key=lambda j: d(cur, j))
        order.append(nxt)
        remaining.discard(nxt)
    if end_locked:
        order.append(end_idx)

    # 2-opt（内部边反转），端点固定
    left = 1
    right = n - 1 if end_locked else n
    improved = True
    passes = 0
    while improved and passes < 30:
        improved = False
        passes += 1
        for i in range(left, right - 1):
            for k in range(i + 1, right):
                a, b = order[i - 1], order[i]
                c, e = order[k], order[(k + 1) % n] if k + 1 < n else None
                delta = d(a, c) - d(a, b)
                if e is not None:
                    delta += d(e, b) - d(e, order[k])
                if delta < -1e-9:
                    order[i:k + 1] = reversed(order[i:k + 1])
                    improved = True
    return order
