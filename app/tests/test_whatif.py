"""路线变更推演模块回归测试（直接 python 运行，不依赖 pytest）。

覆盖两项复核确认的缺陷修复：
1. 首页必须引用 static/js/whatif.js，否则「路线变更推演」按钮无事件；
2. 封闭通道只在生效时段与路线执行时间重叠时参与栅格化，
   错峰时段（如巡逻 08:00 已结束、封路 20:00–21:00）不得产生
   不可达点 / 封路冲突。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from app import create_app  # noqa: E402
from app.geometry import build_route  # noqa: E402
from app.geometry.whatif import (active_closures, apply_scenario,  # noqa: E402
                                 evaluate_scenario)

ROOT = Path(__file__).resolve().parent.parent.parent


def shift_plan():
    """一分为二的厂房：x=800 实墙，中间仅靠东门 (420..580) 通行。"""
    return {
        "image": None,
        "calibration": {"pixelsPerMeter": 40.0},
        "settings": {"spacing": 10.0, "margin": 0.0, "dwell": 0,
                     "shiftStart": "08:00", "workers": 1},
        "walls": [
            {"id": "w1", "x1": 800, "y1": 0, "x2": 800, "y2": 420,
             "thickness": 10},
            {"id": "w2", "x1": 800, "y1": 580, "x2": 800, "y2": 1000,
             "thickness": 10},
        ],
        "doors": [{"id": "d1", "x1": 800, "y1": 420, "x2": 800, "y2": 580,
                   "thickness": 16, "open": True, "label": "东门"}],
        "windows": [], "zones": [],
        "start": {"id": "S", "x": 200, "y": 500, "label": "大门"},
        "end": {"id": "E", "x": 1400, "y": 500, "label": "仪表间"},
        "mustPass": [],
    }


def door_closure(start, end):
    """恰好封死东门门洞的通道。"""
    return [{"id": "c1", "x1": 800, "y1": 400, "x2": 800, "y2": 600,
             "thickness": 18, "effectiveStart": start, "effectiveEnd": end,
             "label": "东门封闭"}]


# ---------------------------------------------------------------------------
# 缺陷 1：首页脚本接线
# ---------------------------------------------------------------------------

def test_index_references_whatif_js():
    app = create_app({"TESTING": True})
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'js/whatif.js' in html, "首页未引用 static/js/whatif.js"
    # 接线顺序：whatif 必须在 canvas/app 之前，供其转发交互
    pos = {f: html.index(f) for f in
           ("js/api.js", "js/state.js", "js/whatif.js",
            "js/canvas.js", "js/app.js")}
    assert pos["js/whatif.js"] < pos["js/canvas.js"] < pos["js/app.js"], \
        "whatif.js 必须在 canvas.js / app.js 之前加载"
    # 路由真实可访问，且按钮存在
    with app.test_client() as c:
        body = c.get("/").get_data(as_text=True)
    assert 'id="whatifBtn"' in body
    assert 'js/whatif.js' in body
    print("✓ 首页已接线 whatif.js（加载顺序正确，推演按钮存在）")


# ---------------------------------------------------------------------------
# 缺陷 2：错峰时段不参与重算
# ---------------------------------------------------------------------------

def test_off_shift_closure_ignored():
    plan = shift_plan()
    base = build_route(plan)
    assert base["stats"]["blockedCount"] == 0
    finish = base["stats"].get("finishClock")
    assert finish == "08:01", finish   # 巡逻 08:00 开始、08:01 即结束

    # 封路 20:00–21:00，巡逻早已结束 → 不得进入栅格
    sc = {"name": "错峰封路", "closures": door_closure("20:00", "21:00")}
    active, idle = active_closures(sc, plan, base)
    assert active == [] and [c["id"] for c in idle] == ["c1"]

    variant_plan, notes = apply_scenario(plan, sc, baseline=base)
    assert not variant_plan.get("_tempBlockers"), \
        "错峰通道不应写入临时障碍层"
    assert notes and "不重叠" in notes[0]

    ev = evaluate_scenario(plan, sc, baseline=base)
    s = ev["impact"]["summary"]
    assert ev["variant"]["stats"]["blockedCount"] == 0, "错峰不应出现受阻路段"
    assert s["droppedCount"] == 0 and s["blockedPointCount"] == 0, \
        "错峰不应产生不可达巡检点"
    blocking = [c for c in ev["impact"]["conflicts"]
                if c["type"] in ("blocked-segment", "point-unreachable",
                                 "closure-crosses-route")]
    assert not blocking, f"错峰不应产生封路类冲突: {blocking}"
    assert [c["id"] for c in ev["impact"].get("idleClosures", [])] == ["c1"]
    # 保留一条 low 级提示告知调度员该通道错峰
    assert any(c["type"] == "outside-shift" and c["severity"] == "low"
               for c in ev["impact"]["conflicts"])
    print("✓ 错峰封路（20:00–21:00，巡逻 08:01 结束）不参与重算，"
          "无不可达点/封路冲突")


def test_pre_shift_closure_still_blocks():
    """边界：封路从班次开始前延续进执行期（07:00–09:00，巡逻 08:00–08:01）。

    07:00 早于班次 08:00，但不得被解释成次日；该通道必须参与重算，
    产生断点/不可达点与封路冲突，路线不能穿过封闭通道。
    """
    from app.geometry.whatif import _window_overlaps_patrol
    plan = shift_plan()
    base = build_route(plan)
    assert base["stats"].get("finishClock") == "08:01"

    # 直接校核相交判定（旧逻辑会把 07:00 当成次日而误判 False）
    sc = {"effectiveStart": "07:00", "effectiveEnd": "09:00"}
    assert _window_overlaps_patrol(sc, base, plan) is True

    closures = door_closure("07:00", "09:00")
    scen = {"name": "班前延续封路", "closures": closures}
    active, idle = active_closures(scen, plan, base)
    assert [c["id"] for c in active] == ["c1"] and idle == [], \
        "07:00–09:00 与 08:00–08:01 相交，应参与重算"

    variant_plan, notes = apply_scenario(plan, scen, baseline=base)
    assert variant_plan.get("_tempBlockers"), "相交通道必须写入临时障碍层"
    assert not any("不重叠" in n for n in notes)

    ev = evaluate_scenario(plan, scen, baseline=base)
    s = ev["impact"]["summary"]
    assert ev["variant"]["stats"]["blockedCount"] >= 1, \
        "班前延续封路应使路线出现断点"
    assert s["droppedCount"] + s["blockedPointCount"] >= 1, \
        "应报告不可达巡检点"
    assert any(c["severity"] == "high" for c in ev["impact"]["conflicts"]), \
        "应报告高级别封路冲突"
    assert ev["impact"].get("idleClosures") == [], "不得把该通道列为错峰"
    print("✓ 班前延续封路（07:00–09:00，巡逻 08:00–08:01）参与重算，"
          "报告断点/不可达点/冲突")


def test_in_shift_closure_still_blocks():
    """对照：生效时段与巡逻重叠时必须照常封死。"""
    plan = shift_plan()
    base = build_route(plan)
    sc = {"name": "班内封路", "closures": door_closure("08:00", "09:00")}
    active, idle = active_closures(sc, plan, base)
    assert [c["id"] for c in active] == ["c1"] and idle == []
    ev = evaluate_scenario(plan, sc, baseline=base)
    assert ev["variant"]["stats"]["blockedCount"] >= 1, "班内封路应产生断点"
    assert ev["impact"]["summary"]["droppedCount"] >= 1
    assert any(c["severity"] == "high" for c in ev["impact"]["conflicts"])
    print("✓ 班内封路（08:00–09:00）照常产生断点与高级别冲突")


def test_missing_shift_or_window_is_conservative():
    """无班次时间或通道未设生效时段：无法判定时保守参与，不误放真实隔断。"""
    plan = shift_plan()
    base = build_route(plan)

    # 未设班次开始时间
    p_noshift = {**plan, "settings": {**plan["settings"], "shiftStart": None}}
    base_ns = build_route(p_noshift)
    sc = {"closures": door_closure("20:00", "21:00")}
    active, _ = active_closures(sc, p_noshift, base_ns)
    assert [c["id"] for c in active] == ["c1"]

    # 有班次但通道没填生效时段
    sc_nowin = {"closures": [{**door_closure("20:00", "21:00")[0],
                              "effectiveStart": None, "effectiveEnd": None}]}
    active2, _ = active_closures(sc_nowin, plan, base)
    assert [c["id"] for c in active2] == ["c1"]
    print("✓ 无班次/无生效时段时保守让通道参与计算")


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 个推演回归测试通过 ✅")


if __name__ == "__main__":
    run_all()
