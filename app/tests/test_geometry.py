"""geometry 包单元/集成测试，直接 python 运行，不依赖 pytest。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.geometry import build_route, update_route  # noqa: E402
from app.geometry.raster import build_grid  # noqa: E402
from app.geometry.graph import label_components  # noqa: E402


def base_plan():
    return {
        "image": None,
        "calibration": {"pixelsPerMeter": 40.0},
        "settings": {"spacing": 10.0, "margin": 0.0, "dwell": 0},
        "walls": [], "doors": [], "windows": [], "zones": [],
        "start": {"id": "S", "x": 200, "y": 200, "label": "起点"},
        "end": {"id": "E", "x": 1400, "y": 800, "label": "终点"},
        "mustPass": [],
    }


def test_open_room_connects():
    plan = base_plan()
    plan["settings"]["spacing"] = 3.0   # 密集布点
    r = build_route(plan)
    assert not r["blockedSegments"], "空房间不应有受阻路段"
    assert r["stats"]["totalLengthM"] > 30
    assert r["stats"]["pointCount"] >= 2
    assert r["coverage"]["percent"] > 95, "密集布点覆盖率应接近 100%"
    print("✓ 空旷房间连通，里程 %.1fm，点 %d，覆盖 %.0f%%，用时 %dms"
          % (r["stats"]["totalLengthM"], r["stats"]["pointCount"],
             r["coverage"]["percent"], r["elapsedMs"]))

    # 稀疏布点时应能标出盲区（产品功能本身）
    plan2 = base_plan()
    plan2["settings"]["spacing"] = 12.0
    r2 = build_route(plan2)
    assert r2["coverage"]["uncoveredAreaM2"] > 0
    assert r2["coverage"]["percent"] < 100
    print("✓ 稀疏布点标出盲区 %.0f m²，覆盖率 %.0f%%"
          % (r2["coverage"]["uncoveredAreaM2"], r2["coverage"]["percent"]))


def test_wall_with_open_door():
    plan = base_plan()
    # 竖墙从顶到底，在中间留门洞 (690..810)
    plan["walls"] = [
        {"id": "w1", "x1": 800, "y1": 0, "x2": 800, "y2": 690, "thickness": 10},
        {"id": "w2", "x1": 800, "y1": 810, "x2": 800, "y2": 1000, "thickness": 10},
    ]
    plan["doors"] = [{"id": "d1", "x1": 800, "y1": 690, "x2": 800, "y2": 810,
                      "thickness": 16, "open": True, "label": "车间门"}]
    r = build_route(plan)
    assert not r["blockedSegments"], f"开门时应能穿过门洞: {[s.get('reason') for s in r['segments'] if s.get('blocked')]}"
    # 路径应经过门洞 x≈800
    crossed = any(abs(p["x"] - 800) < 40
                  for s in r["segments"] for p in s.get("path", []))
    assert crossed, "路线应穿过门洞"
    print("✓ 开门洞时路线穿门而过，里程 %.1fm" % r["stats"]["totalLengthM"])

    # 关门：左右两侧不应连通
    plan["doors"][0]["open"] = False
    r2 = build_route(plan)
    assert r2["blockedSegments"], "关门后应出现受阻路段"
    seg = next(s for s in r2["segments"] if s["blocked"])
    assert "门" in seg["reason"]["summary"] or seg["reason"]["details"], "应给出关门原因"
    assert any("关闭" in d for d in seg["reason"]["details"]), \
        f"诊断应提示关闭的门: {seg['reason']}"
    print("✓ 关门后诊断：%s" % seg["reason"]["summary"])


def test_incremental_move_point():
    plan = base_plan()
    r = build_route(plan)
    cache = r["cache"]
    auto = next(p for p in r["points"] if p["kind"] == "auto")
    # 拖动一个自动点
    nx, ny = auto["x"] + 60, auto["y"] + 60
    plan_state = plan  # 空房间，几何不变
    r2 = update_route(plan_state, cache,
                      {"type": "movePoint", "id": auto["id"], "x": nx, "y": ny})
    assert r2["incremental"]["mode"] == "partial"
    affected = r2["incremental"]["recalculatedSegments"]
    assert 1 <= len(affected) <= 2, f"只应重算相邻1-2段，实际 {affected}"
    assert not r2["blockedSegments"]
    print("✓ 拖动点位只重算路段 %s，用时 %dms（全量 %dms）"
          % (affected, r2["elapsedMs"], r["elapsedMs"]))

    # 门状态改变 → 栅格重建
    plan["walls"] = [
        {"id": "w1", "x1": 800, "y1": 0, "x2": 800, "y2": 690, "thickness": 10},
        {"id": "w2", "x1": 800, "y1": 810, "x2": 800, "y2": 1000, "thickness": 10},
    ]
    plan["doors"] = [{"id": "d1", "x1": 800, "y1": 690, "x2": 800, "y2": 810,
                      "thickness": 16, "open": True}]
    r3 = build_route(plan)
    plan["doors"][0]["open"] = False
    r4 = update_route(plan, r3["cache"], {"type": "door", "id": "d1", "open": False})
    assert r4["incremental"]["mode"] == "grid-rebuilt"
    print("✓ 门状态切换触发栅格重建")


def test_zone_blocks():
    plan = base_plan()
    plan["start"] = {"id": "S", "x": 200, "y": 500}
    plan["end"] = {"id": "E", "x": 1400, "y": 500}
    plan["zones"] = [{"id": "z1", "name": "储罐区", "points": [
        {"x": 700, "y": 100}, {"x": 900, "y": 100},
        {"x": 900, "y": 900}, {"x": 700, "y": 900}]}]
    r = build_route(plan)
    assert not r["blockedSegments"]
    # 路线必须绕行，不能穿过 y 中线直走最短（>1200px≈30m 的直线距离应被绕远）
    assert r["stats"]["totalLengthM"] > 33, f"应绕行禁入区，实际 {r['stats']['totalLengthM']}m"
    print("✓ 禁入区迫使路线绕行，里程 %.1fm" % r["stats"]["totalLengthM"])


def test_grid_split_components():
    plan = base_plan()
    plan["start"] = {"id": "S", "x": 200, "y": 500}
    plan["end"] = {"id": "E", "x": 1500, "y": 500}
    # 整面封闭墙，无门
    plan["walls"] = [{"id": "w1", "x1": 800, "y1": 0, "x2": 800, "y2": 1000,
                      "thickness": 12}]
    r = build_route(plan)
    assert r["blockedSegments"]
    seg = next(s for s in r["segments"] if s["blocked"])
    assert seg["reason"]["startComponent"] != seg["reason"]["targetComponent"]
    assert "不连通" in seg["reason"]["summary"]
    print("✓ 完全隔断时分量诊断正确：%s" % seg["reason"]["summary"])


def test_snapping_warning():
    plan = base_plan()
    plan["start"] = {"id": "S", "x": 800, "y": 500}  # 落在墙上
    plan["walls"] = [{"id": "w1", "x1": 800, "y1": 0, "x2": 800, "y2": 1000,
                      "thickness": 20}]
    plan["end"] = {"id": "E", "x": 200, "y": 500}
    r = build_route(plan)
    assert any("挪到最近" in w for w in r["warnings"]), r["warnings"]
    print("✓ 点落在墙上时自动吸附并提示")


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 个测试通过 ✅")


if __name__ == "__main__":
    run_all()
