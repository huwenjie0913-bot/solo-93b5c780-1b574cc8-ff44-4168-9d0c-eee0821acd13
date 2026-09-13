"""端到端 HTTP 接口测试（需先启动服务器：PYTHONPATH=... python3 -m app）"""

import json
import urllib.request

B = "http://127.0.0.1:5000"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(B + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


PLAN_DATA = {
    "name": "E2E车间",
    "calibration": {"pixelsPerMeter": 40},
    "settings": {"spacing": 8, "margin": 0.2, "dwell": 20, "threshold": 128},
    "walls": [
        {"id": "w1", "x1": 800, "y1": 0, "x2": 800, "y2": 690, "thickness": 10},
        {"id": "w2", "x1": 800, "y1": 810, "x2": 800, "y2": 1000, "thickness": 10},
    ],
    "doors": [{"id": "d1", "x1": 800, "y1": 690, "x2": 800, "y2": 810,
               "thickness": 16, "open": True, "label": "东门"}],
    "windows": [], "zones": [],
    "start": {"id": "S", "x": 200, "y": 500, "label": "大门"},
    "end": {"id": "E", "x": 1400, "y": 500, "label": "仪表间"},
    "mustPass": [],
}


def main():
    # 1. 创建
    pid = call("POST", "/api/plans", {"name": "E2E车间", "data": PLAN_DATA})["id"]

    # 2. 全量
    r1 = call("POST", "/api/route", {"data": PLAN_DATA})
    assert r1["stats"]["blockedCount"] == 0, "开门时应全通"
    assert r1["stats"]["totalLengthM"] > 100
    print(f"✓ 全量: {r1['stats']['totalLengthM']}m, {r1['stats']['pointCount']}点, "
          f"覆盖{r1['coverage']['percent']}%, {r1['elapsedMs']}ms")

    # 3. 保存路线
    call("PUT", f"/api/plans/{pid}", {"name": "E2E车间", "data": PLAN_DATA, "route": r1})

    # 4. 增量拖动一个自动点
    auto = next(p for p in r1["points"] if p["kind"] == "auto")
    edits = {"type": "movePoint", "id": auto["id"],
             "x": auto["x"] + 50, "y": auto["y"] + 40}
    r2 = call("POST", "/api/route/update",
              {"data": PLAN_DATA, "cache": r1["cache"], "edits": edits})
    assert r2["incremental"]["mode"] == "partial", r2.get("incremental")
    assert len(r2["incremental"]["recalculatedSegments"]) <= 2
    print(f"✓ 增量拖动: 重算段 {r2['incremental']['recalculatedSegments']}, "
          f"{r2['elapsedMs']}ms")

    # 5. 关门 → 诊断
    closed = json.loads(json.dumps(PLAN_DATA))
    closed["doors"][0]["open"] = False
    r3 = call("POST", "/api/route/update",
              {"data": closed, "cache": r1["cache"],
               "edits": {"type": "door", "id": "d1", "open": False}})
    assert r3["stats"]["blockedCount"] >= 1
    seg = next(s for s in r3["segments"] if s["blocked"])
    assert "不连通" in seg["reason"]["summary"]
    assert any("关闭" in d for d in seg["reason"]["details"])
    print(f"✓ 关门诊断: {seg['reason']['summary']}")
    print(f"  建议: {seg['reason']['details'][0]}")

    # 6. 快照 + 对比
    call("POST", f"/api/plans/{pid}/snapshots", {"name": "开门基线"})
    snaps = call("GET", f"/api/plans/{pid}/snapshots")["snapshots"]
    cmp_ = call("GET", f"/api/plans/{pid}/compare?ids={snaps[0]['id']}")
    assert len(cmp_["rows"]) == 2
    print(f"✓ 快照对比: {[r['name'] for r in cmp_['rows']]}")

    # 7. 打印页存在
    with urllib.request.urlopen(f"{B}/plans/{pid}/print") as resp:
        html = resp.read().decode()
    assert "行走次序表" in html and "巡检路线图" in html
    print("✓ 打印页可访问且含路线图/次序表")

    # 8. 清理
    call("DELETE", f"/api/plans/{pid}")
    print("\n端到端接口测试全部通过 ✅")


if __name__ == "__main__":
    main()
