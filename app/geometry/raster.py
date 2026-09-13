"""栅格化：把可通行空间转成栅格。

约定
----
* 所有几何坐标均为「图像像素」坐标（浮点，原点在左上角）。
* 栅格按顺序逐行编码，index = y * gw + x。
* 每一层障碍物用 bytearray 标记：1 = 占据。最终 blocked 层 = 墙|窗|禁入|膨胀
  之后再把「开启的门洞」从墙层上扣除。
"""

from __future__ import annotations

import math
from typing import Iterable

# 阻塞原因标签（写入 reason 数组，未阻塞为 ""）
REASON_IMAGE = "img"      # 平面图灰度像素本身（立柱/设备等）
REASON_WALL = "wall"
REASON_WINDOW = "window"
REASON_ZONE = "zone"
REASON_DOOR_CLOSED = "closed-door"

DEFAULT_MAX_CELLS = 300_000   # gw * gh 上限，保证 A* / BFS 可在数秒内完成
MIN_CELL = 4.0                # 每格最小像素尺寸（图像坐标），墙线至少占一格


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def grid_dimensions(width: float, height: float, max_cells: int = DEFAULT_MAX_CELLS):
    """根据画布尺寸选择格网大小，返回 (gw, gh, cell)。"""
    width = max(1.0, float(width))
    height = max(1.0, float(height))
    # 目标 ~0.3m 一格，但先按像素估算；cell 不小于 MIN_CELL 像素。
    target = max(6.0, min(width, height) / 320.0)
    cell = max(MIN_CELL, target)
    gw = max(8, int(round(width / cell)))
    gh = max(8, int(round(height / cell)))
    # 超限则整体放大格子
    if gw * gh > max_cells:
        scale = math.sqrt(gw * gh / max_cells)
        cell *= scale
        gw = max(8, int(round(width / cell)))
        gh = max(8, int(round(height / cell)))
    return gw, gh, cell


def unpack_bits(packed: str | None, n: int) -> bytearray:
    """解包前端送来的位图（每个像素 1 个有效位，行优先，每行字节对齐）。

    前端按 gw*gh 的格网渲染，故 packed 的位序与栅格 index 一一对应。
    """
    out = bytearray(n)
    if not packed:
        return out
    raw = __import__("base64").b64decode(packed)
    bit = 0
    for byte in raw:
        for k in range(8):
            if bit >= n:
                return out
            if (byte >> (7 - k)) & 1:
                out[bit] = 1
            bit += 1
    return out


def _points_on_segment(x1, y1, x2, y2, half_thickness):
    """生成线段（带厚度）覆盖到的栅格中心候选点（图像坐标）。

    用沿线段步进的方式采样，步长取半格以下，保证斜线不断裂。
    """
    length = math.hypot(x2 - x1, y2 - y1)
    steps = max(1, int(math.ceil(length / max(1.0, half_thickness))))
    for i in range(steps + 1):
        t = i / steps
        yield x1 + (x2 - x1) * t, y1 + (y2 - y1) * t


def rasterize_line(layer: bytearray, reason: list, gw: int, gh: int,
                   x1, y1, x2, y2, thickness: float, cell: float,
                   tag: str) -> set[int]:
    """把一条带厚度的线段画进栅格层，返回被影响的格子集合。"""
    r = max(thickness / 2.0, cell * 0.6)
    rad = math.ceil(r / cell) + 1
    length = math.hypot(x2 - x1, y2 - y1)
    steps = max(1, int(math.ceil(length / (cell * 0.5))))
    touched: set[int] = set()
    for i in range(steps + 1):
        t = i / steps
        px = x1 + (x2 - x1) * t
        py = y1 + (y2 - y1) * t
        cx, cy = px / cell, py / cell
        for dy in range(-rad, rad + 1):
            gy = int(cy) + dy
            if gy < 0 or gy >= gh:
                continue
            for dx in range(-rad, rad + 1):
                gx = int(cx) + dx
                if gx < 0 or gx >= gw:
                    continue
                if dx * dx + dy * dy > (r / cell + 0.5) ** 2:
                    continue
                idx = gy * gw + gx
                touched.add(idx)
    for idx in touched:
        layer[idx] = 1
        reason[idx] = tag
    return touched


def _point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """射线法判断点是否在多边形内。"""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def rasterize_polygon(layer: bytearray, reason: list, gw: int, gh: int,
                      points: list, cell: float, tag: str) -> set[int]:
    """栅格化凸/凹多边形（填充）。"""
    poly = [(float(p["x"]), float(p["y"])) for p in points]
    if len(poly) < 3:
        return set()
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    x0 = max(0, int(min(xs) / cell) - 1)
    x1 = min(gw - 1, int(max(xs) / cell) + 1)
    y0 = max(0, int(min(ys) / cell) - 1)
    y1 = min(gh - 1, int(max(ys) / cell) + 1)
    touched: set[int] = set()
    for gy in range(y0, y1 + 1):
        for gx in range(x0, x1 + 1):
            px = (gx + 0.5) * cell
            py = (gy + 0.5) * cell
            if _point_in_polygon(px, py, poly):
                idx = gy * gw + gx
                layer[idx] = 1
                reason[idx] = tag
                touched.add(idx)
    return touched


def dilate(layer: bytearray, radius_cells: int, gw: int, gh: int) -> bytearray:
    """方形膨胀（安全余量），两次一维滑动窗口，复杂度 O(n)。"""
    if radius_cells <= 0:
        return bytearray(layer)
    r = radius_cells
    n = gw * gh

    # 水平：horiz[y,x] = 行 y 内列区间 [x-r, x+r] 是否有障碍
    horiz = bytearray(n)
    for gy in range(gh):
        base = gy * gw
        cnt = sum(layer[base:base + r + 1])
        for gx in range(gw):
            if cnt:
                horiz[base + gx] = 1
            drop = gx - r
            if drop >= 0 and layer[base + drop]:
                cnt -= 1
            add = gx + r + 1
            if add < gw and layer[base + add]:
                cnt += 1

    # 垂直：对 horiz 做列方向滑动窗口
    out = bytearray(n)
    for gx in range(gw):
        cnt = sum(horiz[yy * gw + gx] for yy in range(min(r + 1, gh)))
        for gy in range(gh):
            if cnt:
                out[gy * gw + gx] = 1
            drop = gy - r
            if drop >= 0 and horiz[drop * gw + gx]:
                cnt -= 1
            add = gy + r + 1
            if add < gh and horiz[add * gw + gx]:
                cnt += 1
    return out


# ---------------------------------------------------------------------------
# 门洞
# ---------------------------------------------------------------------------

def _rect_contains(px: float, py: float, door: dict, cell: float) -> bool:
    """点（图像坐标）是否落在门洞矩形内。

    门洞以墙线为中线，沿 (x1,y1)-(x2,y2) 方向取全长、法向取厚度。
    """
    x1, y1 = float(door["x1"]), float(door["y1"])
    x2, y2 = float(door["x2"]), float(door["y2"])
    vx, vy = x2 - x1, y2 - y1
    length = math.hypot(vx, vy)
    if length < 1e-6:
        return False
    ux, uy = vx / length, vy / length
    nx, ny = -uy, ux
    wx, wy = px - x1, py - y1
    along = wx * ux + wy * uy
    across = wx * nx + wy * ny
    thickness = float(door.get("thickness") or 14.0)
    return -thickness * 0.5 <= across <= thickness * 0.5 and 0.0 <= along <= length


def carve_open_doors(wall_layer: bytearray, reason: list, doors: Iterable[dict],
                     gw: int, gh: int, cell: float) -> set[int]:
    """把开启门洞覆盖的墙格清空，返回被挖开的格子集合。"""
    opened = [d for d in doors if d.get("open", True)]
    carved: set[int] = set()
    if not opened:
        return carved
    for gy in range(gh):
        py = (gy + 0.5) * cell
        for gx in range(gw):
            idx = gy * gw + gx
            if not wall_layer[idx]:
                continue
            px = (gx + 0.5) * cell
            for d in opened:
                if _rect_contains(px, py, d, cell):
                    wall_layer[idx] = 0
                    reason[idx] = ""
                    carved.add(idx)
                    break
    return carved


# ---------------------------------------------------------------------------
# 总装
# ---------------------------------------------------------------------------

def build_grid(plan: dict) -> dict:
    """根据方案构建完整栅格。

    返回 dict：
      gw, gh, cell, blocked(bytearray), reason(list[str]),
      image_layer / wall_layer / zone_layer（膨胀后，供增量更新复用）,
      door_cells: {door_id: set(idx)} 每个开启门洞挖开的格子
    """
    img = plan.get("image") or {}
    width = float(img.get("width") or 1600)
    height = float(img.get("height") or 1000)

    # 前端上传平面图时按固定低分辨率（gridWidth 列）做阈值分割并打包位图，
    # 栅格直接沿用该分辨率，保证前后端逐格对齐；无图时自动估算。
    occ_gw, occ_gh = img.get("gridWidth"), img.get("gridHeight")
    if occ_gw and occ_gh and img.get("occupancy"):
        gw, gh = int(occ_gw), int(occ_gh)
        cell = width / gw
    else:
        gw, gh, cell = grid_dimensions(width, height)
    n = gw * gh

    settings = plan.get("settings") or {}
    margin = settings.get("margin")
    margin = 0.3 if margin is None else float(margin)
    cal = plan.get("calibration") or {}
    ppm = cal.get("pixelsPerMeter")
    ppm = 40.0 if ppm is None else float(ppm)
    radius_cells = max(0, int(round(margin * ppm / cell)))

    image_layer = unpack_bits(img.get("occupancy"), n)
    wall_layer = bytearray(n)
    window_layer = bytearray(n)
    zone_layer = bytearray(n)

    for w in plan.get("walls", []):
        rasterize_line(wall_layer, ["" for _ in range(n)], gw, gh,
                       float(w["x1"]), float(w["y1"]),
                       float(w["x2"]), float(w["y2"]),
                       float(w.get("thickness") or 8.0), cell, REASON_WALL)
    for win in plan.get("windows", []):
        rasterize_line(window_layer, ["" for _ in range(n)], gw, gh,
                       float(win["x1"]), float(win["y1"]),
                       float(win["x2"]), float(win["y2"]),
                       float(win.get("thickness") or 8.0), cell, REASON_WINDOW)
    for z in plan.get("zones", []):
        rasterize_polygon(zone_layer, ["" for _ in range(n)], gw, gh,
                          z["points"], cell, REASON_ZONE)

    # 分层膨胀后合并，再挖门洞——避免膨胀把开口重新封死
    image_layer = dilate(image_layer, radius_cells, gw, gh)
    wall_layer = dilate(wall_layer, radius_cells, gw, gh)
    window_layer = dilate(window_layer, radius_cells, gw, gh)
    zone_layer = dilate(zone_layer, radius_cells, gw, gh)

    # 合并并标注阻塞原因（优先级：禁入区 > 窗户 > 墙 > 图像实体）
    blocked = bytearray(n)
    final_reason = [""] * n
    for layer, tag in ((image_layer, REASON_IMAGE), (wall_layer, REASON_WALL),
                       (window_layer, REASON_WINDOW), (zone_layer, REASON_ZONE)):
        for i, v in enumerate(layer):
            if v:
                blocked[i] = 1
                final_reason[i] = tag

    # 开启的门洞：一次性扫描，挖空门洞覆盖的障碍格
    opened = [d for d in plan.get("doors", []) if d.get("open", True)]
    door_cells: dict[str, set[int]] = {d["id"]: set() for d in opened}
    for gy in range(gh):
        py = (gy + 0.5) * cell
        for gx in range(gw):
            idx = gy * gw + gx
            if not blocked[idx]:
                continue
            px = (gx + 0.5) * cell
            for d in opened:
                if _rect_contains(px, py, d, cell):
                    door_cells[d["id"]].add(idx)
                    blocked[idx] = 0
                    final_reason[idx] = ""
                    break

    # 关闭的门额外压一层（门板本身）
    for d in plan.get("doors", []):
        if d.get("open", True):
            continue
        touched = rasterize_line(bytearray(n), [""] * n, gw, gh,
                                 float(d["x1"]), float(d["y1"]),
                                 float(d["x2"]), float(d["y2"]),
                                 float(d.get("thickness") or 10.0) + 2, cell,
                                 REASON_DOOR_CLOSED)
        for idx in touched:
            blocked[idx] = 1
            final_reason[idx] = REASON_DOOR_CLOSED

    # 外边框一圈视为障碍，避免路径贴着图边走
    for gx in range(gw):
        for gy in (0, gh - 1):
            idx = gy * gw + gx
            if not blocked[idx]:
                blocked[idx] = 1
                final_reason[idx] = REASON_WALL
    for gy in range(gh):
        for gx in (0, gw - 1):
            idx = gy * gw + gx
            if not blocked[idx]:
                blocked[idx] = 1
                final_reason[idx] = REASON_WALL

    return {
        "gw": gw, "gh": gh, "cell": cell,
        "blocked": blocked, "reason": final_reason,
        "image_layer": image_layer, "wall_layer": wall_layer,
        "window_layer": window_layer,
        "zone_layer": zone_layer, "door_cells": door_cells,
    }
