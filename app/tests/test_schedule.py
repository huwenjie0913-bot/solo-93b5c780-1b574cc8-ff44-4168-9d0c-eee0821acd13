"""时间窗排程测试：窗口换算、等待/重排、多人汇总、不可行保留路线。

直接 python 运行，不依赖 pytest / Flask。
"""

import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

# 几何模块只依赖标准库，但导入 app.geometry 会先执行 app/__init__（需要 Flask），
# 测试环境若未装 Flask 则注入最小桩模块。
try:  # pragma: no cover - 环境相关
    import flask  # noqa: F401
except ModuleNotFoundError:
    flask = types.ModuleType("flask")
    for _n in ("Flask", "abort", "jsonify", "render_template", "request",
               "send_from_directory", "url_for", "current_app", "g"):
        setattr(flask, _n, object)
    sys.modules["flask"] = flask
    wz = types.ModuleType("werkzeug")
    wzu = types.ModuleType("werkzeug.utils")
    wzu.secure_filename = lambda x: x
    sys.modules["werkzeug"] = wz
    sys.modules["werkzeug.utils"] = wzu

from app.geometry import build_route, update_route  # noqa: E402
from app.geometry import schedule as sch  # noqa: E402


def base_plan(**settings):
    s = {"spacing": 20.0, "margin": 0.0, "dwell": 30,
         "workers": 1, "shiftStart": "22:00"}
    s.update(settings)
    return {
        "image": None,
        "calibration": {"pixelsPerMeter": 40.0},
        "settings": s,
        "walls": [], "doors": [], "windows": [], "zones": [],
        "start": {"id": "S", "x": 200, "y": 500, "label": "大门"},
        "end": {"id": "E", "x": 1400, "y": 500, "label": "交班室"},
        "mustPass": [],
    }


def test_clock_helpers():
    assert sch.parse_clock("22:00") == 22 * 60
    assert sch.parse_clock("02:05") == 125
    assert sch.parse_clock("24:00") == 1440
    assert sch.parse_clock("25:00") is None
    assert sch.parse_clock("nope") is None
    # 跨午夜：02:00 相对 22:00 班次为 +240 分钟
    assert sch.clock_offset("02:00", 22 * 60) == 240
    assert sch.clock_offset("23:30", 22 * 60) == 90
    assert sch.clock_label(240, 22 * 60) == "02:00"
    assert sch.clock_label(0, 22 * 60) == "22:00"
    print("✓ 时钟换算与跨午夜正确")


def test_wait_at_early_ready():
    """配电间 23:00 才开门，22:00 到达必须原地等待 60 分钟。"""
    plan = base_plan()
    plan["mustPass"] = [
        {"id": "M1", "x": 500, "y": 500, "label": "配电间",
         "readyClock": "23:00", "dueClock": "23:40",
         "dwellSeconds": 120, "priority": 5},
    ]
    r = build_route(plan)
    e = next(e for e in r["schedule"]["entries"] if e["id"] == "M1")
    # 起点到配电间之间可能夹着自动巡检点，到达约在 22:00~22:02
    assert e["arrivalMin"] < 2, e
    assert abs(e["waitMin"] - (60 - e["arrivalMin"])) < 0.5, e
    assert e["leaveClock"] == "23:02"
    assert r["schedule"]["summary"]["feasible"] is True
    assert abs(r["stats"]["waitMinutes"] - 60) < 1
    print(f"✓ 早到等待 {e['waitMin']:.0f} 分钟，离开 23:02，排程可行")


def test_reorder_by_due_window():
    """锅炉房最晚 22:20 完成、配电间 22:40 才开门 → 锅炉房必须先去。"""
    plan = base_plan()
    plan["mustPass"] = [
        {"id": "M1", "x": 500, "y": 500, "label": "配电间",
         "readyClock": "22:40", "dueClock": "23:40",
         "dwellSeconds": 120, "priority": 5},
        {"id": "M2", "x": 1100, "y": 500, "label": "锅炉房",
         "dueClock": "22:20", "dwellSeconds": 300, "priority": 5},
        {"id": "M3", "x": 800, "y": 800, "label": "水泵房",
         "readyClock": "22:30", "priority": 2},
    ]
    r = build_route(plan)
    order = [e["label"] for e in r["schedule"]["entries"]]
    assert order.index("锅炉房") < order.index("配电间"), order
    assert r["schedule"]["summary"]["feasible"] is True
    assert any("已按时间窗重排" in w for w in r["warnings"]), r["warnings"]
    boiler = next(e for e in r["schedule"]["entries"] if e["id"] == "M2")
    assert boiler["lateMin"] == 0
    print("✓ 按最晚完成重排：", " → ".join(order))


def test_infeasible_keeps_executable_route():
    """不可能赶上的窗口：保留可执行路线，明确冲突点与原因。"""
    plan = base_plan()
    plan["mustPass"] = [
        {"id": "M2", "x": 1200, "y": 700, "label": "锅炉房",
         "dueClock": "22:02", "dwellSeconds": 300, "priority": 5},
    ]
    r = build_route(plan)
    s = r["schedule"]
    assert s["summary"]["feasible"] is False
    assert s["summary"]["latePoints"] == 1
    conflict = next(c for c in s["conflicts"] if c["type"] == "late")
    assert conflict["pointId"] == "M2"
    assert "锅炉房" in conflict["message"] and "晚" in conflict["message"]
    # 路线本身仍然完整可执行
    assert not r["blockedSegments"]
    assert all(e["arrivalClock"] for e in s["entries"])
    print("✓ 不可行时保留路线并指出冲突：", conflict["message"])


def test_multi_worker_per_person_schedule():
    """多人模式分别给出每人的到达/离开/超窗与全队汇总。"""
    plan = base_plan(workers=2, spacing=18.0)
    plan["mustPass"] = [
        {"id": "M1", "x": 500, "y": 300, "label": "配电间",
         "readyClock": "22:30", "dueClock": "22:45",
         "dwellSeconds": 120, "priority": 5},
        {"id": "M2", "x": 1200, "y": 700, "label": "锅炉房",
         "dueClock": "23:20", "dwellSeconds": 120, "priority": 4},
    ]
    r = build_route(plan)
    assert r["mode"] == "multi"
    assert r["schedule"] is not None
    for rt in r["routes"]:
        assert rt["schedule"]["summary"]["shiftStart"] == "22:00"
    # 每个必经点恰好归属一名人员
    owners = {}
    for p in r["points"]:
        if p["id"] in ("M1", "M2"):
            owners.setdefault(p["id"], set()).add(p["worker"])
    assert all(len(v) == 1 for v in owners.values()), owners
    assert r["stats"]["shiftStart"] == "22:00"
    assert "finishClock" in r["stats"]
    print(f"✓ 多人排程：全队 {r['stats']['shiftStart']}→"
          f"{r['stats']['finishClock']}，等待 {r['stats']['waitMinutes']} 分")


def test_invalid_shift_and_window():
    """非法班次时间退回不排程；自相矛盾的窗口给出提示。"""
    plan = base_plan()
    plan["settings"]["shiftStart"] = "99:99"
    plan["mustPass"] = [
        {"id": "M1", "x": 500, "y": 500, "label": "配电间",
         "readyClock": "23:00", "dueClock": "22:30"},
    ]
    r = build_route(plan)
    assert "schedule" not in r or r.get("schedule") is None
    assert any("班次开始时间" in w for w in r["warnings"]), r["warnings"]

    plan["settings"]["shiftStart"] = "22:00"
    r = build_route(plan)
    assert any("时间窗无效" in w for w in r["warnings"]), r["warnings"]
    print("✓ 非法班次/矛盾窗口均有明确提示")


def test_no_shift_keeps_legacy_behavior():
    """旧方案（无 shiftStart）结果不带 schedule，行为与以前一致。"""
    plan = base_plan()
    plan["settings"].pop("shiftStart")
    plan["mustPass"] = [
        {"id": "M1", "x": 500, "y": 500, "label": "配电间",
         "readyClock": "23:00", "dueClock": "23:40"},
    ]
    r = build_route(plan)
    assert "schedule" not in r
    assert "finishClock" not in r["stats"]
    assert not any("排程" in w for w in r["warnings"])
    print("✓ 无班次开始时间时沿用旧行为（不排程）")


def test_incremental_move_keeps_schedule():
    """拖动点位的增量结果仍带排程时刻。"""
    plan = base_plan()
    plan["mustPass"] = [
        {"id": "M1", "x": 500, "y": 500, "label": "配电间",
         "readyClock": "23:00", "dueClock": "23:40",
         "dwellSeconds": 120, "priority": 5},
    ]
    r1 = build_route(plan)
    auto = next(p for p in r1["points"] if p["kind"] == "auto")
    r2 = update_route(plan, r1["cache"],
                      {"type": "movePoint", "id": auto["id"],
                       "x": auto["x"] + 40, "y": auto["y"] + 40})
    assert r2["incremental"]["mode"] == "partial"
    assert r2["schedule"]["summary"]["shiftStart"] == "22:00"
    e = next(e for e in r2["schedule"]["entries"] if e["id"] == "M1")
    assert e["readyClock"] == "23:00" and e["dueClock"] == "23:40"
    print("✓ 增量拖动后排程时刻同步重算")


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 个排程测试通过 ✅")


if __name__ == "__main__":
    run_all()
