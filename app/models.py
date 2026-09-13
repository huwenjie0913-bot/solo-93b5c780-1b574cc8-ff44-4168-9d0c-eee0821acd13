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
                     "threshold": 128,
                     # 多人分区编排：workers=1 即单人模式（旧方案默认）
                     "workers": 1,          # 人员数
                     "maxMinutes": 0,       # 单人时长上限（分钟，0=不限）
                     "startMode": "shared"},  # shared=共同起终点 / individual=各自独立
        "walls": [],     # {id, x1,y1,x2,y2, thickness}
        "doors": [],     # {id, x1,y1,x2,y2, thickness, open, label}
        "windows": [],   # {id, x1,y1,x2,y2, thickness}
        "zones": [],     # {id, name, points:[{x,y}]}
        "start": None,   # {id, x, y, label}
        "end": None,
        "mustPass": [],
        # 多人模式：独立起终点（按人员索引）与人工锁定分配 {点位id: 人员序号}
        "workerStarts": [],
        "workerEnds": [],
        "assignments": {},
        "createdAt": int(time.time() * 1000),
    }
