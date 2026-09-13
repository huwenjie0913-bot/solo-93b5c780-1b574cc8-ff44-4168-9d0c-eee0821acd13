// 主控：UI 绑定、编辑动作、路线计算（全量/增量）、保存/快照/对比
(function () {
  const $ = (id) => document.getElementById(id);
  let recomputeTimer = null;
  let computing = false;

  // -------------------------------------------------------------------------
  // 初始化
  // -------------------------------------------------------------------------
  async function init() {
    State.init();
    bindUI();
    await refreshPlanList();
    CanvasView.draw();
    toast("开始：导入平面图或直接在画布上绘制墙体");
  }

  function bindUI() {
    // 工具切换
    document.querySelectorAll(".tool").forEach((b) => {
      b.addEventListener("click", () => setTool(b.dataset.tool));
    });
    $("zoneCloseBtn").addEventListener("click", () => closeZone());
    $("zoneCancelBtn").addEventListener("click", () => {
      State.ui.drawingZone = null;
      setZoneUIVisible(false);
      CanvasView.draw();
    });

    // 图片
    $("imageInput").addEventListener("change", onImageUpload);
    $("removeImageBtn").addEventListener("click", removeImage);
    $("threshold").addEventListener("input", (e) => {
      $("thresholdVal").textContent = e.target.value;
    });
    $("threshold").addEventListener("change", onThresholdChange);
    $("showOccupancy").addEventListener("change", (e) => {
      State.ui.showOccupancy = e.target.checked;
      CanvasView.draw();
    });

    // 校准
    $("calibBtn").addEventListener("click", () => {
      setTool("select");
      State.ui.calibMode = true;
      State.ui.calib = null;
      toast("在图上拖出一段已知实际长度的直线，然后填写米数");
      bindCalibDrag();
    });
    $("calibMeters").addEventListener("change", applyCalibration);

    // 参数
    $("spacing").addEventListener("change", (e) => {
      State.data.settings.spacing = parseFloat(e.target.value) || 6;
      scheduleRecompute(true);
    });
    $("margin").addEventListener("change", (e) => {
      State.data.settings.margin = parseFloat(e.target.value) || 0;
      scheduleRecompute(true);
    });
    $("dwell").addEventListener("change", (e) => {
      State.data.settings.dwell = parseFloat(e.target.value) || 0;
      scheduleRecompute(true);
    });

    // 图层
    document.querySelectorAll(".layer").forEach((cb) => {
      cb.addEventListener("change", () => {
        State.ui.layers[cb.dataset.layer] = cb.checked;
        CanvasView.draw();
      });
    });

    // 计算
    $("computeBtn").addEventListener("click", () => recompute(true));
    $("autoRecompute").addEventListener("change", (e) => {
      if (e.target.checked && State.dirty) recompute(true);
    });

    // 方案
    $("planSelect").addEventListener("change", (e) => loadPlan(e.target.value));
    $("newPlanBtn").addEventListener("click", newPlan);
    $("dupPlanBtn").addEventListener("click", duplicatePlan);
    $("delPlanBtn").addEventListener("click", deletePlan);
    $("saveBtn").addEventListener("click", savePlan);
    $("planName").addEventListener("input", (e) => { State.data.name = e.target.value; });

    // 快照 / 对比 / 打印
    $("snapBtn").addEventListener("click", addSnapshot);
    $("compareBtn").addEventListener("click", openCompare);
    $("compareCloseBtn").addEventListener("click", () => $("compareMask").hidden = true);
    $("printBtn").addEventListener("click", () => {
      if (State.planId) window.open(`/plans/${State.planId}/print`, "_blank");
      else toast("请先保存方案再打印");
    });
  }

  // -------------------------------------------------------------------------
  // 工具
  // -------------------------------------------------------------------------
  function setTool(t) {
    State.ui.tool = t;
    document.querySelectorAll(".tool").forEach((b) =>
      b.classList.toggle("active", b.dataset.tool === t));
    const hints = {
      select: "拖动点位移动；点击门切换开闭；点击红色 ! 查看不通原因",
      wall: "按住拖出一段墙",
      door: "在门洞位置拖出门段（绿=开，点击可关闭）",
      window: "拖出窗户（不可通行）",
      zone: "逐点点击绘制禁入区，双击或点「闭合」完成",
      start: "点击设置起点", end: "点击设置终点", must: "点击添加必经点",
      erase: "点击要删除的墙/窗/门/点",
    };
    $("toolHint").textContent = hints[t] || "";
    if (t !== "zone") { State.ui.drawingZone = null; setZoneUIVisible(false); }
    CanvasView.draw();
  }
  window.setTool = setTool;

  function setZoneUIVisible(v) {
    $("zoneActions").hidden = !v;
  }

  // -------------------------------------------------------------------------
  // 几何编辑
  // -------------------------------------------------------------------------
  window.App = {
    addGeometry(kind, a, b) {
      const seg = {
        id: uid(kind[0]), x1: round(a.x), y1: round(a.y),
        x2: round(b.x), y2: round(b.y), thickness: kind === "door" ? 14 : 8,
      };
      if (kind === "wall") State.data.walls.push(seg);
      else if (kind === "window") State.data.windows.push(seg);
      else if (kind === "door") State.data.doors.push({ ...seg, open: true });
      scheduleRecompute(true);
    },

    async toggleDoor(door) {
      door.open = !door.open;
      // 门窗状态改变 → 全量重算（可通行性变化）
      CanvasView.draw();
      const res = await callRoute({ type: "door", id: door.id, open: door.open });
      applyRoute(res);
    },

    placeFixedPoint(kind, p) {
      const id = uid(kind[0].toUpperCase());
      const rec = { id, x: round(p.x), y: round(p.y),
        label: kind === "start" ? "起点" : kind === "end" ? "终点" : "必经点" };
      if (kind === "start") State.data.start = rec;
      else if (kind === "end") State.data.end = rec;
      else State.data.mustPass.push(rec);
      scheduleRecompute(true);
    },

    eraseAt(p, hits) {
      const { door, wall, win, point } = hits;
      if (door) {
        State.data.doors = State.data.doors.filter((d) => d !== door);
      } else if (wall) {
        State.data.walls = State.data.walls.filter((w) => w !== wall);
      } else if (win) {
        State.data.windows = State.data.windows.filter((w) => w !== win);
      } else if (point && point.kind === "must") {
        // 只能删除用户必经点；自动点删除需要在 data 里维护排除列表
        const raw = State.data.mustPass.find((m) => m.id === point.id);
        if (raw) State.data.mustPass = State.data.mustPass.filter((m) => m !== raw);
        else { toast("自动巡检点由间距生成，可调整间距或把点拖走"); return; }
      } else if (point && (point.kind === "start" || point.kind === "end")) {
        State.data[point.kind] = null;
      } else {
        return;
      }
      scheduleRecompute(true);
    },

    async movePoint(id, p) {
      // 起/终/必经点：改 data；自动点：记录到 manualPoints（拖动后位置持久化）
      const rawFixed =
        (State.data.start?.id === id && State.data.start) ||
        (State.data.end?.id === id && State.data.end) ||
        State.data.mustPass.find((m) => m.id === id);
      if (rawFixed) {
        rawFixed.x = round(p.x); rawFixed.y = round(p.y);
      } else {
        State.data.manualPoints = State.data.manualPoints || [];
        let mp = State.data.manualPoints.find((m) => m.id === id);
        if (!mp) {
          mp = { id, x: round(p.x), y: round(p.y) };
          State.data.manualPoints.push(mp);
        } else {
          mp.x = round(p.x); mp.y = round(p.y);
        }
      }
      const res = await callRoute({ type: "movePoint", id, x: round(p.x), y: round(p.y) }, true);
      if (res) applyRoute(res);
    },

    setZoneUIVisible,
    closeZone: () => closeZone(),
  };

  function closeZone() {
    const pts = State.ui.drawingZone;
    if (!pts || pts.length < 3) { toast("至少需要 3 个点"); return; }
    const name = prompt("禁入区名称（如：储罐区）", "禁入区");
    if (name === null) return;
    State.data.zones.push({
      id: uid("z"), name: name || "禁入区",
      points: pts.map((p) => ({ x: round(p.x), y: round(p.y) })),
    });
    State.ui.drawingZone = null;
    setZoneUIVisible(false);
    scheduleRecompute(true);
  }

  // -------------------------------------------------------------------------
  // 图片与阈值
  // -------------------------------------------------------------------------
  async function onImageUpload(e) {
    const file = e.target.files[0];
    if (!file) return;
    toast("上传图片中…");
    let meta;
    try {
      meta = await Api.uploadImage(file);
    } catch (err) { toast(err.message, true); return; }

    const img = await loadImage(meta.url);
    State.planImage = img;
    const threshold = parseInt($("threshold").value, 10);
    State.data.image = Occupancy.buildImagePayload(
      img, meta, threshold);
    // 图片坐标系变了，旧几何全部失效
    State.data.start = null; State.data.end = null;
    State.data.mustPass = []; State.data.manualPoints = [];
    State.data.calibration = { pixelsPerMeter: 40, line: null, realMeters: null, calibrated: false };
    updateCalibInfo();
    $("imageInfo").hidden = false;
    $("imageInfoText").textContent =
      `${meta.originalName} ${img.naturalWidth}×${img.naturalHeight}`;
    CanvasView.draw();
    scheduleRecompute(true);
    toast("平面图已导入，可拖校准线设定比例尺");
  }

  function removeImage() {
    if (!confirm("移除平面图？墙体/门窗等标记会保留。")) return;
    State.data.image = null;
    State.planImage = null;
    $("imageInfo").hidden = true;
    CanvasView.draw();
    scheduleRecompute(true);
  }

  function onThresholdChange() {
    const t = parseInt($("threshold").value, 10);
    State.ui.threshold = t;
    State.data.settings.threshold = t;
    if (State.planImage && State.data.image) {
      const payload = Occupancy.buildImagePayload(
        State.planImage,
        { filename: State.data.image.filename, url: State.data.image.url,
          originalName: State.data.image.originalName },
        t);
      State.data.image.gridWidth = payload.gridWidth;
      State.data.image.gridHeight = payload.gridHeight;
      State.data.image.occupancy = payload.occupancy;
      State.data.image.threshold = t;
      scheduleRecompute(true);
    }
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = url;
    });
  }

  // -------------------------------------------------------------------------
  // 比例尺校准
  // -------------------------------------------------------------------------
  let calibDrag = null;
  function bindCalibDrag() {
    const canvas = $("canvas");
    function down(e) {
      if (!State.ui.calibMode) return;
      calibDrag = CanvasView.getPos(e);
      State.ui.calib = { x1: calibDrag.x, y1: calibDrag.y, x2: calibDrag.x, y2: calibDrag.y };
      canvas.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up, { once: true });
      CanvasView.draw();
    }
    function move(e) {
      if (!calibDrag) return;
      const p = CanvasView.getPos(e);
      State.ui.calib = { x1: calibDrag.x, y1: calibDrag.y, x2: p.x, y2: p.y };
      CanvasView.draw();
    }
    function up() {
      calibDrag = null;
      canvas.removeEventListener("mousemove", move);
    }
    canvas.addEventListener("mousedown", down, { once: true });
  }

  function applyCalibration() {
    const c = State.ui.calib || State.data.calibration?.line;
    const meters = parseFloat($("calibMeters").value);
    if (!c || !meters || meters <= 0) { toast("请先拖校准线并输入正确的实际长度", true); return; }
    const px = Math.hypot(c.x2 - c.x1, c.y2 - c.y1);
    const ppm = px / meters;
    State.data.calibration = {
      pixelsPerMeter: +ppm.toFixed(3), line: {
        x1: round(c.x1), y1: round(c.y1), x2: round(c.x2), y2: round(c.y2),
      }, realMeters: meters, calibrated: true,
    };
    State.ui.calibMode = false;
    updateCalibInfo();
    CanvasView.draw();
    scheduleRecompute(true);
    toast(`比例尺已校准：1m = ${ppm.toFixed(1)}px`);
  }

  function updateCalibInfo() {
    const c = State.data.calibration;
    $("calibInfo").textContent = c.calibrated
      ? `已校准：1m = ${c.pixelsPerMeter}px（基准段 ${c.realMeters}m）`
      : `未校准，默认 1m = ${c.pixelsPerMeter}px`;
  }

  // -------------------------------------------------------------------------
  // 路线计算
  // -------------------------------------------------------------------------
  function scheduleRecompute(geomChanged) {
    if (geomChanged) State.markDirty();
    CanvasView.draw();
    if (!$("autoRecompute").checked) return;
    clearTimeout(recomputeTimer);
    recomputeTimer = setTimeout(() => recompute(false), 350);
  }

  async function recompute(manual) {
    if (computing) return;
    computing = true;
    $("computeBtn").disabled = true;
    $("computeBtn").textContent = "计算中…";
    try {
      const res = await callRoute(null);
      if (res) {
        applyRoute(res);
        State.dirty = false;
        if (manual) toast("路线已重新生成");
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      computing = false;
      $("computeBtn").disabled = false;
      $("computeBtn").textContent = "生成 / 重算巡检路线";
    }
  }

  // edits=null → 全量；movePoint → 增量
  async function callRoute(edits = null, incremental = false) {
    try {
      if (incremental && edits?.type === "movePoint" && State.route?.cache) {
        const res = await Api.updateRoute(State.data, State.route.cache, edits);
        if (!res.error) return res;
        // 增量失败则回退全量
      }
      if (edits?.type === "door") {
        return await Api.updateRoute(State.data, State.route?.cache || {}, edits);
      }
      return await Api.buildRoute(State.data);
    } catch (err) {
      toast(err.message, true);
      return null;
    }
  }

  function applyRoute(res) {
    State.route = res;
    // 同步参数面板（吸附后的起点等在 res.points 中渲染）
    renderStats(res);
    renderMessages(res);
    renderPointList(res);
    CanvasView.draw();
  }

  function renderStats(r) {
    const s = r.stats, c = r.coverage;
    $("statLength").textContent = s.totalLengthM != null ? s.totalLengthM + " m" : "—";
    $("statEta").textContent = s.etaMinutes != null ? s.etaMinutes + " 分钟" : "—";
    $("statPoints").textContent = s.pointCount ?? "—";
    $("statCoverage").textContent = c.percent != null ? c.percent + "%" : "—";
    const cov = $("statCoverage");
    cov.className = c.percent >= 95 ? "cov good" : c.percent >= 85 ? "cov warn" : "cov bad";
    $("statUncovered").textContent = c.uncoveredAreaM2 != null ? c.uncoveredAreaM2 + " m²" : "—";
    $("statBlocked").textContent = s.blockedCount
      ? `${s.blockedCount} 段` : "0";
    $("statBlocked").style.color = s.blockedCount ? "var(--danger)" : "";
    const inc = r.incremental;
    $("incInfo").textContent = inc?.mode === "partial"
      ? `增量更新：仅重算 ${inc.recalculatedSegments.map((i) => i + 1).join("、") || "无"} 段，${r.elapsedMs}ms`
      : inc?.mode === "grid-rebuilt"
        ? `门窗状态改变，栅格重建（${r.elapsedMs}ms）`
        : `全量计算 ${r.elapsedMs}ms，栅格 ${r.gridInfo?.gw}×${r.gridInfo?.gh}`;
  }

  function renderMessages(r) {
    const ul = $("messages");
    ul.innerHTML = "";
    const add = (text, cls) => {
      const li = document.createElement("li");
      li.className = cls;
      li.textContent = text;
      ul.appendChild(li);
    };
    for (const w of r.warnings || []) add(w, "warn");
    for (const s of r.segments || []) {
      if (s.blocked) {
        add(`路段 ${s.fromSeq}→${s.toSeq} 不通：${s.reason.summary}`, "err");
      }
    }
    if (!ul.children.length) add("路线计算完成", "ok");
  }

  function renderPointList(r) {
    const ol = $("pointList");
    ol.innerHTML = "";
    for (const p of r.points) {
      const li = document.createElement("li");
      const blocked = (r.segments || []).some(
        (s) => s.blocked && (s.from === p.id || s.to === p.id));
      if (blocked) li.className = "blocked";
      const tag = { start: "起点", end: "终点", must: "必经", auto: "巡检点" }[p.kind];
      li.innerHTML = `${p.seq}. ${p.label || p.id}<span class="tag">${tag}${blocked ? " · 相邻路段不通" : ""}</span>`;
      li.addEventListener("click", () => CanvasView.focusPoint(p.id));
      ol.appendChild(li);
    }
  }

  // -------------------------------------------------------------------------
  // 方案持久化
  // -------------------------------------------------------------------------
  async function refreshPlanList(selectId) {
    const { plans } = await Api.listPlans();
    const sel = $("planSelect");
    sel.innerHTML = '<option value="">— 本地新方案 —</option>';
    for (const p of plans) {
      const o = document.createElement("option");
      o.value = p.id; o.textContent = p.name;
      sel.appendChild(o);
    }
    if (selectId) sel.value = selectId;
  }

  async function loadPlan(id) {
    if (!id) { State.init(); syncForms(); CanvasView.draw(); renderEmpty(); return; }
    const plan = await Api.getPlan(id);
    State.planId = plan.id;
    State.data = plan.data;
    State.route = plan.route;
    $("planName").value = plan.name;
    if (plan.data.image?.url) {
      State.planImage = await loadImage(plan.data.image.url).catch(() => null);
      $("imageInfo").hidden = false;
      $("imageInfoText").textContent =
        `${plan.data.image.originalName || ""} ${plan.data.image.width}×${plan.data.image.height}`;
    } else {
      State.planImage = null;
      $("imageInfo").hidden = true;
    }
    syncForms();
    updateCalibInfo();
    if (State.route) { renderStats(State.route); renderMessages(State.route); renderPointList(State.route); }
    else renderEmpty();
    CanvasView.draw();
  }

  function syncForms() {
    const d = State.data;
    $("spacing").value = d.settings.spacing;
    $("margin").value = d.settings.margin;
    $("dwell").value = d.settings.dwell;
    $("threshold").value = d.settings.threshold || 128;
    $("thresholdVal").textContent = d.settings.threshold || 128;
  }

  function renderEmpty() {
    for (const id of ["statLength", "statEta", "statPoints", "statCoverage", "statUncovered"])
      $(id).textContent = "—";
    $("statBlocked").textContent = "0";
    $("messages").innerHTML = "";
    $("pointList").innerHTML = "";
    $("incInfo").textContent = "";
  }

  async function newPlan() {
    State.planId = null;
    State.init();
    $("planSelect").value = "";
    $("planName").value = State.data.name;
    syncForms(); renderEmpty(); CanvasView.draw();
  }

  async function savePlan() {
    const name = $("planName").value.trim() || "未命名方案";
    State.data.name = name;
    // 保存前确保路线是最新的
    if (State.dirty || !State.route) await recompute(false);
    if (State.planId) {
      await Api.updatePlan(State.planId, { name, data: State.data, route: State.route });
      toast("已保存");
    } else {
      const created = await Api.createPlan({ name, data: State.data, route: State.route });
      State.planId = created.id;
      await refreshPlanList(created.id);
      toast("已新建并保存");
    }
  }

  async function duplicatePlan() {
    if (!State.planId) { toast("请先保存当前方案"); return; }
    const d = await Api.duplicatePlan(State.planId);
    await refreshPlanList(d.id);
    loadPlan(d.id);
  }

  async function deletePlan() {
    if (!State.planId) return;
    if (!confirm("确认删除当前方案及其快照？")) return;
    await Api.deletePlan(State.planId);
    await newPlan();
    await refreshPlanList();
    toast("已删除");
  }

  // -------------------------------------------------------------------------
  // 快照 / 对比
  // -------------------------------------------------------------------------
  async function addSnapshot() {
    if (!State.planId) { toast("请先保存方案再另存快照"); return; }
    if (State.dirty || !State.route) await recompute(true);
    const name = prompt("快照名称", new Date().toLocaleString("zh-CN"));
    if (!name) return;
    await Api.addSnapshot(State.planId, name);
    toast("快照已保存");
  }

  async function openCompare() {
    if (!State.planId) { toast("请先保存方案"); return; }
    const { snapshots } = await Api.listSnapshots(State.planId);
    const body = $("compareBody");
    if (!snapshots.length) {
      body.innerHTML = '<p class="hint">还没有快照。编辑方案后点击「📸 另存快照」，再回到这里对比。</p>';
    } else {
      const chosen = new Set(snapshots.slice(0, 2).map((s) => s.id));
      body.innerHTML = `
        <p class="hint">选择要与当前方案对比的快照（最多 6 个）：</p>
        <div class="snapshot-list">${snapshots.map((s) =>
          `<span class="snap-chip ${chosen.has(s.id) ? "sel" : ""}" data-id="${s.id}">
            ${s.name} <button title="删除" data-del="${s.id}">✕</button></span>`).join("")}
        </div>
        <div id="compareTable"></div>`;
      body.querySelectorAll(".snap-chip").forEach((chip) => {
        chip.addEventListener("click", async (e) => {
          if (e.target.dataset.del) {
            await Api.deleteSnapshot(e.target.dataset.del);
            openCompare();
            return;
          }
          chip.classList.toggle("sel");
          await renderCompareTable();
        });
      });
      await renderCompareTable();
    }
    $("compareMask").hidden = false;

    async function renderCompareTable() {
      const ids = [...body.querySelectorAll(".snap-chip.sel")].map((c) => c.dataset.id);
      const { rows } = await Api.compare(State.planId, ids);
      const cols = [
        ["方案", "name"], ["总里程(m)", "totalLengthM"], ["用时(分)", "etaMinutes"],
        ["点位数", "pointCount"], ["覆盖率(%)", "coveragePercent"],
        ["未覆盖(m²)", "uncoveredAreaM2"], ["受阻段", "blockedCount"],
        ["墙", "walls"], ["门", "doors"], ["禁入区", "zones"],
      ];
      $("compareTable").innerHTML = `<table class="compare"><thead><tr>${
        cols.map((c) => `<th>${c[0]}</th>`).join("")}</tr></thead><tbody>${
        rows.map((r) => "<tr>" + cols.map(([, k]) => {
          const v = k === "name" ? r.name : (r.route?.[k] ?? r[k] ?? "—");
          return `<td>${v}</td>`;
        }).join("")).join("</tr>")}</tr></tbody></table>`;
    }
  }

  // -------------------------------------------------------------------------
  function toast(text, isErr = false) {
    const t = document.createElement("div");
    t.className = "toast";
    if (isErr) t.style.background = "#b91c1c";
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }

  function round(v) { return Math.round(v * 10) / 10; }

  init();
})();
