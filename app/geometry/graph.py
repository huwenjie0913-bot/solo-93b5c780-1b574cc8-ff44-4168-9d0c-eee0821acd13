"""栅格图分析：连通分量、吸附、BFS 距离场、A* 寻路。"""

from __future__ import annotations

import heapq
import math
from collections import deque
from typing import Optional

from .raster import REASON_DOOR_CLOSED, REASON_WINDOW, REASON_ZONE

# 8 邻接：(dx, dy, 代价)
_NEIGH = [
    (1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
    (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)),
    (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2)),
]

ASTAR_NODE_CAP = 120_000   # 单次寻路最大扩展节点数，超时即判失败


def label_components(blocked: bytearray, gw: int, gh: int) -> list[int]:
    """4 邻接连通分量标注，障碍格标记 -1。"""
    comp = [-1] * (gw * gh)
    cid = 0
    for start in range(len(blocked)):
        if blocked[start] or comp[start] != -1:
            continue
        comp[start] = cid
        q = deque([start])
        while q:
            idx = q.popleft()
            x, y = idx % gw, idx // gw
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < gw and 0 <= ny < gh:
                    nidx = ny * gw + nx
                    if not blocked[nidx] and comp[nidx] == -1:
                        comp[nidx] = cid
                        q.append(nidx)
        cid += 1
    return comp


def nearest_free(blocked: bytearray, gw: int, gh: int, gx: int, gy: int,
                 max_radius: int = 200) -> Optional[tuple[int, int]]:
    """BFS 找最近的可通行格（螺旋向外扩展）。"""
    gx = max(0, min(gw - 1, gx))
    gy = max(0, min(gh - 1, gy))
    if not blocked[gy * gw + gx]:
        return gx, gy
    visited = bytearray(gw * gh)
    q = deque([(gx, gy, 0)])
    visited[gy * gw + gx] = 1
    while q:
        x, y, r = q.popleft()
        if r >= max_radius:
            return None
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < gw and 0 <= ny < gh:
                nidx = ny * gw + nx
                if visited[nidx]:
                    continue
                visited[nidx] = 1
                if not blocked[nidx]:
                    return nx, ny
                q.append((nx, ny, r + 1))
    return None


def snap_point(blocked, gw, gh, cell, x: float, y: float):
    """把像素坐标吸附到最近可通行格，返回 (gx, gy, px, py, moved)。"""
    hit = nearest_free(blocked, gw, gh, int(x / cell), int(y / cell))
    if hit is None:
        return None
    gx, gy = hit
    moved = (gx != int(x / cell) or gy != int(y / cell))
    return gx, gy, (gx + 0.5) * cell, (gy + 0.5) * cell, moved


def distance_field(sources: list[int], blocked: bytearray, gw: int, gh: int):
    """多源 BFS，返回每个可通行格到最近源点的栅格距离（None 表示不可达/障碍）。"""
    dist: list[Optional[float]] = [None] * (gw * gh)
    q = deque()
    for s in sources:
        if 0 <= s < len(blocked) and not blocked[s] and dist[s] is None:
            dist[s] = 0.0
            q.append(s)
    while q:
        idx = q.popleft()
        x, y = idx % gw, idx // gw
        for dx, dy, cost in _NEIGH:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < gw and 0 <= ny < gh):
                continue
            nidx = ny * gw + nx
            if blocked[nidx] or dist[nidx] is not None:
                continue
            dist[nidx] = dist[idx] + cost
            q.append(nidx)
    return dist


def astar(blocked: bytearray, gw: int, gh: int,
          sx: int, sy: int, tx: int, ty: int):
    """A* 寻路。返回 (path, cost)；path 为格子坐标列表，失败返回 (None, None)。

    采用 8 邻接，对角移动要求两侧正交格至少一侧可通行（防穿墙尖角）。
    """
    start = sy * gw + sx
    target = ty * gw + tx
    if blocked[start] or blocked[target]:
        return None, None

    def h(idx):
        x, y = idx % gw, idx // gw
        ax, ay = abs(x - tx), abs(y - ty)
        return (ax + ay) + (math.sqrt(2) - 2) * min(ax, ay)

    g_score = {start: 0.0}
    came = {}
    open_heap = [(h(start), 0.0, start)]
    counter = 0
    expanded = 0

    while open_heap:
        _, _, cur = heapq.heappop(open_heap)
        if cur == target:
            path = []
            node = cur
            while node != start:
                path.append(node)
                node = came[node]
            path.append(start)
            path.reverse()
            return [(idx % gw, idx // gw) for idx in path], g_score[cur]
        if expanded > ASTAR_NODE_CAP:
            return None, None
        expanded += 1
        x, y = cur % gw, cur // gw
        g_cur = g_score[cur]
        for dx, dy, cost in _NEIGH:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < gw and 0 <= ny < gh):
                continue
            nidx = ny * gw + nx
            if blocked[nidx]:
                continue
            if dx != 0 and dy != 0:
                # 两侧都堵则不允许斜穿
                if blocked[y * gw + nx] and blocked[ny * gw + x]:
                    continue
            ng = g_cur + cost
            if ng < g_score.get(nidx, math.inf):
                g_score[nidx] = ng
                came[nidx] = cur
                counter += 1
                heapq.heappush(open_heap, (ng + h(nidx), counter, nidx))
    return None, None


# ---------------------------------------------------------------------------
# 不可达原因诊断
# ---------------------------------------------------------------------------

def diagnose(blocked: bytearray, reason: list[str], doors: list[dict],
             comp: list[int], gw: int, gh: int,
             sg: tuple[int, int], tg: tuple[int, int], ppm: float,
             cell: float = 1.0) -> dict:
    """诊断两点为何不通，给出人能看懂的原因与建议。"""
    sx, sy = sg
    tx, ty = tg
    sc = comp[sy * gw + sx]
    tc = comp[ty * gw + tx]

    details = []

    # 1) 找出离终点最近的「关闭的门」，提示打开可能恢复连通
    closed_doors = [d for d in doors if not d.get("open", True)]
    if closed_doors:
        tx_px, ty_px = (tx + 0.5) * cell, (ty + 0.5) * cell
        best = min(closed_doors,
                   key=lambda d: _point_segment_dist(tx_px, ty_px, d))
        mx = (float(best["x1"]) + float(best["x2"])) / 2
        my = (float(best["y1"]) + float(best["y2"])) / 2
        details.append(
            f"隔断上的门「{best.get('label') or best['id']}」处于关闭状态"
            f"（位置约 {mx:.0f},{my:.0f}），打开该门可能恢复连通"
        )

    # 2) 终点周围一圈是什么挡住了
    ring_tags = []
    for r in (1, 2, 3):
        ring_tags = _ring_block_reasons(blocked, reason, gw, gh, tx, ty, r)
        if ring_tags:
            break
    if ring_tags:
        label_map = {
            "wall": "实墙",
            "window": "封闭窗户",
            "zone": "禁入区",
            "img": "平面图上的实体（立柱/设备）",
            "closed-door": "关闭的门",
        }
        tags = sorted({label_map.get(t, t) for t in ring_tags})
        details.append("目标点四周被 " + "、".join(tags) + " 包围")

    # 3) 连通分量大小
    comp_sizes: dict[int, int] = {}
    for c in comp:
        if c >= 0:
            comp_sizes[c] = comp_sizes.get(c, 0) + 1
    s_size = comp_sizes.get(sc, 0)
    t_size = comp_sizes.get(tc, 0)

    if sc == tc:
        summary = "两点位于同一区域，但寻路中断（可能超出计算上限），请缩小安全余量后重试"
    else:
        isolated = "（封闭小空间）" if t_size < 20 else ""
        summary = (
            f"起点所在可通行区域与终点区域不连通：起点区域约 {s_size} 格，"
            f"终点区域约 {t_size} 格{isolated}"
        )

    return {"summary": summary, "details": details,
            "startComponent": sc, "targetComponent": tc,
            "startAreaCells": s_size, "targetAreaCells": t_size}


def _ring_block_reasons(blocked, reason, gw, gh, cx, cy, r):
    tags = []
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if max(abs(dx), abs(dy)) != r:
                continue
            x, y = cx + dx, cy + dy
            if 0 <= x < gw and 0 <= y < gh:
                idx = y * gw + x
                if blocked[idx] and reason[idx]:
                    tags.append(reason[idx])
    return tags


def _point_segment_dist(px, py, door):
    """像素点到门段的距离。"""
    x1, y1 = float(door["x1"]), float(door["y1"])
    x2, y2 = float(door["x2"]), float(door["y2"])
    vx, vy = x2 - x1, y2 - y1
    l2 = vx * vx + vy * vy
    if l2 < 1e-9:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / l2))
    return math.hypot(px - (x1 + t * vx), py - (y1 + t * vy))
