"""默认方案数据结构（前端 state 与后端共享同一 JSON 形态）。"""

from __future__ import annotations

import time


def default_plan(name: str = "未命名方案") -> dict:
    return {
        "name": name,
        "image": None,   # {filename, originalName, width, height, occupancy}
        "calibration": {"pixelsPerMeter": 40.0, "line": None,
                        "realMeters": None, "calibrated": False},
        "settings": {"spacing": 6.0, "margin": 0.3, "dwell": 30,
                     "threshold": 128},
        "walls": [],     # {id, x1,y1,x2,y2, thickness}
        "doors": [],     # {id, x1,y1,x2,y2, thickness, open, label}
        "windows": [],   # {id, x1,y1,x2,y2, thickness}
        "zones": [],     # {id, name, points:[{x,y}]}
        "start": None,   # {id, x, y, label}
        "end": None,
        "mustPass": [],
        "createdAt": int(time.time() * 1000),
    }
