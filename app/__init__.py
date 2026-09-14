"""Flask 应用工厂与 HTTP 接口。"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from flask import (Flask, abort, jsonify, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

from . import db
from .geometry import build_route, update_route
from .geometry.whatif import evaluate_scenario, impact_text_lines, normalize_scenario
from .models import default_plan

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def create_app(test_config: dict | None = None) -> Flask:
    root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
    )
    app.config.from_mapping(
        MAX_CONTENT_LENGTH=MAX_IMAGE_BYTES,
        JSON_AS_ASCII=False,
    )
    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    (Path(app.instance_path) / "images").mkdir(exist_ok=True)

    app.teardown_appcontext(db.close_db)

    register_routes(app)
    return app


def register_routes(app: Flask) -> None:

    # -- 页面 ----------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/plans/<int:plan_id>/print")
    def print_page(plan_id: int):
        plan = db.get_plan(plan_id)
        if not plan:
            abort(404)
        return render_template("print.html", plan=plan)

    @app.get("/uploads/<path:filename>")
    def uploads(filename: str):
        return send_from_directory(
            Path(app.instance_path) / "images", filename)

    # -- 方案 CRUD -----------------------------------------------------------

    @app.get("/api/plans")
    def api_list():
        return jsonify({"plans": db.list_plans()})

    @app.post("/api/plans")
    def api_create():
        payload = request.get_json(force=True, silent=True) or {}
        data = payload.get("data") or default_plan()
        name = (payload.get("name") or data.get("name") or "未命名方案").strip()
        # 首次保存即带上已算好的路线（含时间轴排程与关键指标），
        # 否则重新打开/打印页会因 route_json 为空而丢失路线
        route = payload.get("route")
        if not isinstance(route, dict):
            route = None
        pid = db.create_plan(name, data, route)
        return jsonify({"id": pid, **db.get_plan(pid)}), 201

    @app.get("/api/plans/<int:pid>")
    def api_get(pid: int):
        plan = db.get_plan(pid)
        if not plan:
            abort(404)
        return jsonify(plan)

    @app.put("/api/plans/<int:pid>")
    def api_update(pid: int):
        if not db.get_plan(pid):
            abort(404)
        payload = request.get_json(force=True, silent=True) or {}
        data = payload.get("data")
        if data is None:
            abort(400, "缺少 data 字段")
        db.update_plan(pid, payload.get("name"), data, payload.get("route"))
        return jsonify(db.get_plan(pid))

    @app.delete("/api/plans/<int:pid>")
    def api_delete(pid: int):
        if not db.get_plan(pid):
            abort(404)
        db.delete_plan(pid)
        return jsonify({"ok": True})

    @app.post("/api/plans/<int:pid>/duplicate")
    def api_duplicate(pid: int):
        src = db.get_plan(pid)
        if not src:
            abort(404)
        data = dict(src["data"])
        data["name"] = src["name"] + " 副本"
        new_id = db.create_plan(data["name"], data, src.get("route"))
        return jsonify({"id": new_id, **db.get_plan(new_id)}), 201

    # -- 图片上传 ------------------------------------------------------------

    @app.post("/api/images")
    def api_upload_image():
        f = request.files.get("image")
        if not f or not f.filename:
            abort(400, "未收到图片文件")
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            abort(400, f"不支持的图片格式 {ext}，支持 {sorted(ALLOWED_EXT)}")
        fname = uuid.uuid4().hex + ext
        f.save(Path(app.instance_path) / "images" / fname)
        return jsonify({"filename": fname,
                        "url": url_for("uploads", filename=fname),
                        "originalName": secure_filename(f.filename)})

    # -- 路线计算 ------------------------------------------------------------

    @app.post("/api/route")
    def api_route():
        payload = request.get_json(force=True, silent=True)
        if not payload or "data" not in payload:
            abort(400, "缺少 data 字段")
        try:
            result = build_route(payload["data"])
        except Exception as exc:  # pragma: no cover - 防御式
            app.logger.exception("route build failed")
            return jsonify({"error": f"路线计算失败：{exc}"}), 500
        return jsonify(result)

    @app.post("/api/route/update")
    def api_route_update():
        payload = request.get_json(force=True, silent=True)
        if not payload or "data" not in payload:
            abort(400, "缺少 data 字段")
        cache = payload.get("cache") or {}
        edits = payload.get("edits") or {}
        try:
            result = update_route(payload["data"], cache, edits)
        except Exception as exc:  # pragma: no cover
            app.logger.exception("incremental route failed")
            return jsonify({"error": f"增量计算失败：{exc}"}), 500
        return jsonify(result)

    # -- 快照 / 对比 ---------------------------------------------------------

    @app.get("/api/plans/<int:pid>/snapshots")
    def api_snapshots(pid: int):
        if not db.get_plan(pid):
            abort(404)
        return jsonify({"snapshots": db.list_snapshots(pid)})

    @app.post("/api/plans/<int:pid>/snapshots")
    def api_add_snapshot(pid: int):
        plan = db.get_plan(pid)
        if not plan:
            abort(404)
        payload = request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "快照").strip()
        sid = db.add_snapshot(pid, name, plan["data"], plan.get("route") or {})
        return jsonify({"id": sid, **(db.get_snapshot(sid) or {})}), 201

    @app.get("/api/snapshots/<int:sid>")
    def api_get_snapshot(sid: int):
        snap = db.get_snapshot(sid)
        if not snap:
            abort(404)
        return jsonify(snap)

    @app.delete("/api/snapshots/<int:sid>")
    def api_delete_snapshot(sid: int):
        db.delete_snapshot(sid)
        return jsonify({"ok": True})

    @app.get("/api/plans/<int:pid>/compare")
    def api_compare(pid: int):
        """当前方案与若干快照的指标对比。"""
        cur = db.get_plan(pid)
        if not cur:
            abort(404)
        snap_ids = request.args.get("ids", "")
        ids = [int(x) for x in re.findall(r"\d+", snap_ids)][:6]
        rows = [{"name": cur["name"] + "（当前）",
                 "route": _route_summary(cur.get("route")),
                 "walls": len(cur["data"].get("walls", [])),
                 "doors": len(cur["data"].get("doors", [])),
                 "zones": len(cur["data"].get("zones", []))}]
        for sid in ids:
            snap = db.get_snapshot(sid)
            if snap and snap["planId"] == pid:
                rows.append({"name": snap["name"],
                             "route": _route_summary(snap.get("route")),
                             "walls": len(snap["data"].get("walls", [])),
                             "doors": len(snap["data"].get("doors", [])),
                             "zones": len(snap["data"].get("zones", []))})
        return jsonify({"rows": rows})

    # -- 路线变更推演 --------------------------------------------------------

    @app.post("/api/whatif/evaluate")
    def api_whatif_eval():
        """对（未保存的）方案数据直接推演。body: {data, scenario, baseline?}"""
        payload = request.get_json(force=True, silent=True) or {}
        data = payload.get("data")
        if not isinstance(data, dict):
            abort(400, "缺少 data 字段")
        try:
            result = evaluate_scenario(data, payload.get("scenario") or {})
        except Exception as exc:
            app.logger.exception("what-if evaluate failed")
            return jsonify({"error": f"推演计算失败：{exc}"}), 500
        return jsonify(result)

    @app.post("/api/plans/<int:pid>/whatif/evaluate")
    def api_plan_whatif_eval(pid: int):
        """基于已保存方案推演（可带 scenarioId 复用已存场景）。"""
        plan = db.get_plan(pid)
        if not plan:
            abort(404)
        payload = request.get_json(force=True, silent=True) or {}
        scenario = payload.get("scenario")
        if scenario is None and payload.get("scenarioId"):
            saved = db.get_scenario(int(payload["scenarioId"]))
            if saved and saved["planId"] == pid:
                scenario = saved["scenario"]
        try:
            result = evaluate_scenario(plan["data"], scenario or {})
        except Exception as exc:
            app.logger.exception("what-if evaluate failed")
            return jsonify({"error": f"推演计算失败：{exc}"}), 500
        return jsonify(result)

    @app.get("/api/plans/<int:pid>/scenarios")
    def api_scenario_list(pid: int):
        if not db.get_plan(pid):
            abort(404)
        return jsonify({"scenarios": db.list_scenarios(pid)})

    @app.post("/api/plans/<int:pid>/scenarios")
    def api_scenario_create(pid: int):
        plan = db.get_plan(pid)
        if not plan:
            abort(404)
        payload = request.get_json(force=True, silent=True) or {}
        scenario = normalize_scenario(payload.get("scenario") or {})
        name = (payload.get("name") or scenario.get("name") or "未命名推演").strip()
        reason = (payload.get("reason") or scenario.get("reason") or "").strip()
        impact = payload.get("impact")
        # 保存前重新推演一次，保证影响摘要与当前几何一致
        if payload.get("reevaluate", True):
            impact = evaluate_scenario(plan["data"], scenario)["impact"]
        sid = db.create_scenario(pid, name, reason, scenario, impact,
                                 payload.get("status") or "draft")
        return jsonify({"id": sid, **(db.get_scenario(sid) or {})}), 201

    @app.get("/api/scenarios/<int:sid>")
    def api_scenario_get(sid: int):
        scen = db.get_scenario(sid)
        if not scen:
            abort(404)
        return jsonify(scen)

    @app.put("/api/scenarios/<int:sid>")
    def api_scenario_update(sid: int):
        old = db.get_scenario(sid)
        if not old:
            abort(404)
        payload = request.get_json(force=True, silent=True) or {}
        plan = db.get_plan(old["planId"])
        scenario = normalize_scenario(
            payload.get("scenario") if payload.get("scenario") is not None
            else old["scenario"])
        name = payload.get("name")
        reason = payload.get("reason")
        impact = payload.get("impact", old.get("impact"))
        if payload.get("reevaluate"):
            impact = evaluate_scenario(plan["data"], scenario)["impact"]
        db.update_scenario(sid, name, reason, scenario, impact,
                           payload.get("status"))
        return jsonify(db.get_scenario(sid))

    @app.post("/api/scenarios/<int:sid>/publish")
    def api_scenario_publish(sid: int):
        """发布推演版本：标记 published（基线方案本身不被改动）。"""
        old = db.get_scenario(sid)
        if not old:
            abort(404)
        db.update_scenario(sid, None, None, old["scenario"], old.get("impact"),
                           "published")
        return jsonify(db.get_scenario(sid))

    @app.delete("/api/scenarios/<int:sid>")
    def api_scenario_delete(sid: int):
        db.delete_scenario(sid)
        return jsonify({"ok": True})

    @app.post("/api/scenarios/<int:sid>/diff-text")
    def api_scenario_diff_text(sid: int):
        """已保存版本的差异说明（Markdown 文本），供导出。"""
        old = db.get_scenario(sid)
        if not old:
            abort(404)
        plan = db.get_plan(old["planId"])
        ev = evaluate_scenario(plan["data"], old["scenario"])
        return jsonify({
            "markdown": "\n".join(impact_text_lines(ev)),
            "impact": ev["impact"],
        })

    # -- 错误 ----------------------------------------------------------------

    @app.errorhandler(400)
    def bad_request(err):
        return jsonify({"error": str(err.description)}), 400

    @app.errorhandler(404)
    def not_found(err):
        return jsonify({"error": "资源不存在"}), 404

    @app.errorhandler(413)
    def too_large(err):
        return jsonify({"error": "图片过大（上限 12MB）"}), 413


def _route_summary(route: dict | None) -> dict:
    if not route:
        return {}
    stats = route.get("stats") or {}
    return {
        "totalLengthM": stats.get("totalLengthM"),
        "etaMinutes": stats.get("etaMinutes"),
        "pointCount": stats.get("pointCount"),
        "coveragePercent": (route.get("coverage") or {}).get("percent"),
        "blockedCount": stats.get("blockedCount"),
        "uncoveredAreaM2": (route.get("coverage") or {}).get("uncoveredAreaM2"),
        # 时间窗排程关键指标（旧快照无 schedule → None，前端显示 —）
        "shiftStart": stats.get("shiftStart"),
        "finishClock": stats.get("finishClock"),
        "waitMinutes": stats.get("waitMinutes"),
        "lateMinutes": stats.get("lateMinutes"),
        "latePoints": stats.get("latePoints"),
        "scheduleFeasible": stats.get("scheduleFeasible"),
    }


def run() -> None:
    """开发用入口：python -m app 或 flask run。"""
    app = create_app()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
