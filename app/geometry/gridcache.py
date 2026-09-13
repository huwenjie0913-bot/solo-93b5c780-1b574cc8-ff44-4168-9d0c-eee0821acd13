"""栅格缓存：同一份几何（图/墙/门/窗/区/余量）在多次请求间复用。"""

from __future__ import annotations

import threading

from .graph import label_components
from .raster import build_grid

_lock = threading.Lock()
_cache: dict[str, dict] = {}
_MAX = 8


def geometry_fingerprint(plan: dict) -> str:
    img = plan.get("image") or {}
    s = plan.get("settings") or {}
    key = (
        img.get("width"), img.get("height"), img.get("occupancy"),
        img.get("gridWidth"), img.get("gridHeight"),
        s.get("margin"),
        (plan.get("calibration") or {}).get("pixelsPerMeter"),
        tuple(sorted((d["id"], d.get("open", True), d["x1"], d["y1"], d["x2"], d["y2"],
                      d.get("thickness")) for d in plan.get("doors", []))),
        tuple(sorted((w["x1"], w["y1"], w["x2"], w["y2"], w.get("thickness"))
                     for w in plan.get("walls", []))),
        tuple(sorted((w["x1"], w["y1"], w["x2"], w["y2"], w.get("thickness"))
                     for w in plan.get("windows", []))),
        tuple(sorted(tuple((p["x"], p["y"]) for p in z["points"])
                     for z in plan.get("zones", []))),
    )
    return repr(key)


def get_grid(plan: dict) -> dict:
    fp = geometry_fingerprint(plan)
    with _lock:
        g = _cache.get(fp)
        if g is not None:
            return g
    g = build_grid(plan)
    g["fingerprint"] = fp
    g["comp"] = label_components(g["blocked"], g["gw"], g["gh"])
    with _lock:
        _cache[fp] = g
        if len(_cache) > _MAX:
            # 淘汰最早的一个
            oldest = next(iter(_cache))
            _cache.pop(oldest, None)
    return g
