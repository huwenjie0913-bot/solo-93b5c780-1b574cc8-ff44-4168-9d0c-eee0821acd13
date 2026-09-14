// Canvas 2D 渲染 + 鼠标交互（绘制、拖动、门窗切换）
(function () {
  const canvas = document.getElementById("canvas");
  const ctx = canvas.getContext("2d");
  const HUD = document.getElementById("hud");

  // 逻辑画布（图像像素坐标）尺寸
  let W = 1600, H = 1000;

  const COLORS = {
    wall: "#374151",
    window: "#0ea5e9",
    zone: "rgba(220,38,38,.12)",
    zoneEdge: "#dc2626",
    route: "#2563eb",
    blocked: "#dc2626",
    uncovered: "rgba(245,158,11,.32)",
    start: "#16a34a",
    end: "#dc2626",
    must: "#f59e0b",
    auto: "#2563eb",
    grid: "rgba(55,65,81,.12)",
    occ: "rgba(17,24,39,.35)",
    calib: "#7c3aed",
    draft: "#9ca3af",
    wait: "#0891b2",   // 等待点（青色）
    late: "#dc2626",   // 逾期点（红色）
  };
  // 与后端 WORKER_COLORS 保持一致
  const WORKER_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706",
    "#7c3aed", "#0891b2", "#db2777", "#65a30d"];

  function schedById() {
    const m = new Map();
    for (const e of State.route?.schedule?.entries || []) m.set(e.id + "#" + (e.worker ?? ""), e);
    return m;
  }

  function schedOf(p) {
    return (State.route?.schedule?.entries || []).find(
      (e) => e.id === p.id && (e.worker ?? null) === (p.worker ?? null));
  }

  let drag = null; // {kind:'new'|'move'|'pan', ...}

  // -------------------------------------------------------------------------
  function setSize() {
    const img = State.data.image;
    W = img ? img.width : 1600;
    H = img ? img.height : 1000;
    const dpr = window.devicePixelRatio || 1;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function getPos(e) {
    const r = canvas.getBoundingClientRect();
    return {
      x: (e.clientX - r.left) * (W / r.width),
      y: (e.clientY - r.top) * (H / r.height),
    };
  }

  // -------------------------------------------------------------------------
  // 绘制
  // -------------------------------------------------------------------------
  function draw() {
    setSize();
    ctx.clearRect(0, 0, W, H);

    // 底图
    if (State.planImage && State.planImage.complete) {
      ctx.drawImage(State.planImage, 0, 0, W, H);
    } else {
      // 无图时的浅底纹
      ctx.fillStyle = "#fbfdff";
      ctx.fillRect(0, 0, W, H);
      ctx.strokeStyle = "#e5e7eb";
      ctx.lineWidth = 1;
      for (let x = 0; x <= W; x += 100) line(x, 0, x, H, "#f1f5f9");
      for (let y = 0; y <= H; y += 100) line(0, y, W, y, "#f1f5f9");
    }

    // 图像实体覆盖层
    if (State.ui.showOccupancy && State.data.image?.occupancy) {
      drawOccupancy();
    }

    // 栅格
    if (State.ui.layers.grid && State.route?.gridInfo) drawGrid();

    // 未覆盖区域（路线之下）
    if (State.ui.layers.uncovered && State.route) drawUncovered();

    // 禁入区
    if (State.ui.layers.zones) drawZones();

    // 墙、窗
    if (State.ui.layers.walls) {
      for (const w of State.data.windows) drawSegment(w, COLORS.window, 5, "▭");
      for (const w of State.data.walls) drawSegment(w, COLORS.wall, 4);
    }

    // 门
    drawDoors();

    // 路线
    if (State.ui.layers.route && State.route) drawRoute();

    // 点位
    if (State.ui.layers.points && State.route) drawPoints();

    // 校准线
    drawCalib();

    // 正在绘制的禁入区草稿
    if (State.ui.drawingZone) drawZoneDraft();

    // 正在拖出的线段草稿
    if (drag?.kind === "new" && drag.start) {
      const p = drag.current;
      ctx.setLineDash([6, 5]);
      line(drag.start.x, drag.start.y, p.x, p.y, COLORS.draft, 2);
      ctx.setLineDash([]);
    }
  }

  function line(x1, y1, x2, y2, color, width = 2) {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }

  function drawSegment(s, color, width, mark) {
    line(s.x1, s.y1, s.x2, s.y2, color, s.thickness || width);
    if (mark) {
      // 窗户画三道线
      const mx = (s.x1 + s.x2) / 2, my = (s.y1 + s.y2) / 2;
      const ang = Math.atan2(s.y2 - s.y1, s.x2 - s.x1) + Math.PI / 2;
      for (const k of [-1, 1]) {
        line(mx + Math.cos(ang) * 3 * k, my + Math.sin(ang) * 3 * k,
          s.x1, s.y1, color, 1);
      }
    }
  }

  function drawDoors() {
    for (const d of State.data.doors) {
      const open = d.open !== false;
      const col = open ? "#16a34a" : "#dc2626";
      // 门洞处墙断开 → 用门色画门板
      line(d.x1, d.y1, d.x2, d.y2, open ? "#15803d" : "#b91c1c", 3);
      // 门弧 + 门轴（门绕 (x1,y1) 转动）
      const len = Math.hypot(d.x2 - d.x1, d.y2 - d.y1);
      const ang0 = Math.atan2(d.y2 - d.y1, d.x2 - d.x1);
      ctx.fillStyle = col;
      ctx.beginPath(); ctx.arc(d.x1, d.y1, 4, 0, Math.PI * 2); ctx.fill();
      if (open) {
        ctx.strokeStyle = col; ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.arc(d.x1, d.y1, len, ang0 - Math.PI / 2, ang0);
        ctx.stroke();
      } else {
        line(d.x1, d.y1, d.x2, d.y2, col, 5);
      }
    }
  }

  function drawZones() {
    for (const z of State.data.zones) {
      const pts = z.points;
      if (pts.length < 2) continue;
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      ctx.closePath();
      ctx.fillStyle = COLORS.zone;
      ctx.fill();
      ctx.strokeStyle = COLORS.zoneEdge;
      ctx.lineWidth = 2;
      ctx.setLineDash([8, 5]);
      ctx.stroke();
      ctx.setLineDash([]);
      if (z.name) {
        const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
        const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
        label(cx, cy, "⛔ " + z.name, COLORS.zoneEdge);
      }
    }
  }

  function drawZoneDraft() {
    const pts = State.ui.drawingZone;
    if (!pts.length) return;
    ctx.strokeStyle = COLORS.zoneEdge; ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
    if (drag?.current) ctx.lineTo(drag.current.x, drag.current.y);
    ctx.stroke();
    ctx.setLineDash([]);
    for (const p of pts) dot(p.x, p.y, 4, COLORS.zoneEdge);
  }

  function drawCalib() {
    const c = State.ui.calibMode ? State.ui.calib : State.data.calibration?.line;
    if (!c) return;
    ctx.strokeStyle = COLORS.calib; ctx.lineWidth = 3;
    ctx.setLineDash([10, 6]);
    ctx.beginPath(); ctx.moveTo(c.x1, c.y1); ctx.lineTo(c.x2, c.y2); ctx.stroke();
    ctx.setLineDash([]);
    dot(c.x1, c.y1, 5, COLORS.calib); dot(c.x2, c.y2, 5, COLORS.calib);
    label((c.x1 + c.x2) / 2, (c.y1 + c.y2) / 2 - 10,
      `校准段 ${Math.hypot(c.x2 - c.x1, c.y2 - c.y1).toFixed(0)}px`, COLORS.calib);
  }

  function drawOccupancy() {
    const img = State.data.image;
    const bits = Occupancy.decode(img.occupancy, img.gridWidth, img.gridHeight);
    const cw = W / img.gridWidth, ch = H / img.gridHeight;
    ctx.fillStyle = COLORS.occ;
    for (let y = 0; y < img.gridHeight; y++) {
      for (let x = 0; x < img.gridWidth; x++) {
        if (bits[y * img.gridWidth + x]) ctx.fillRect(x * cw, y * ch, cw + 0.5, ch + 0.5);
      }
    }
  }

  function drawGrid() {
    const { gw, gh, cellPx } = State.route.gridInfo;
    const cell = W / gw;
    ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 0.5;
    ctx.beginPath();
    for (let x = 0; x <= gw; x++) { ctx.moveTo(x * cell, 0); ctx.lineTo(x * cell, H); }
    for (let y = 0; y <= gh; y++) { ctx.moveTo(0, y * cell); ctx.lineTo(W, y * cell); }
    ctx.stroke();
  }

  function drawUncovered() {
    const cov = State.route.coverage;
    if (!cov?.uncoveredRuns?.length) return;
    const { gw } = State.route.gridInfo;
    const cell = W / gw;
    ctx.fillStyle = COLORS.uncovered;
    for (const [start, len] of cov.uncoveredRuns) {
      for (let i = 0; i < len; i++) {
        const idx = start + i;
        const x = idx % gw, y = (idx / gw) | 0;
        ctx.fillRect(x * cell, y * cell, cell + 0.5, cell + 0.5);
      }
    }
  }

  function drawRoute() {
    const segs = State.route.segments || [];
    for (const s of segs) {
      if (s.blocked) {
        // 受阻：红虚线 + 中点诊断标记
        const f = s.fallbackPath;
        ctx.setLineDash([9, 7]);
        line(f[0].x, f[0].y, f[1].x, f[1].y, COLORS.blocked, 2.5);
        ctx.setLineDash([]);
        const mx = (f[0].x + f[1].x) / 2, my = (f[0].y + f[1].y) / 2;
        ctx.fillStyle = COLORS.blocked;
        ctx.beginPath(); ctx.arc(mx, my, 9, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = "#fff"; ctx.font = "bold 12px sans-serif";
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText("!", mx, my + 0.5);
      } else {
        const pts = s.path;
        ctx.strokeStyle = COLORS.route; ctx.lineWidth = 3.5;
        ctx.lineJoin = "round"; ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].y);
        for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
        ctx.stroke();
        drawArrowHeads(pts);
      }
    }
  }

  function drawArrowHeads(pts) {
    // 每隔若干路径点画一个方向箭头
    const step = Math.max(8, Math.floor(pts.length / Math.max(2, pts.length / 14)));
    ctx.fillStyle = COLORS.route;
    for (let i = step; i < pts.length - 2; i += step) {
      const a = pts[i - 3] || pts[i - 1], b = pts[i];
      const ang = Math.atan2(b.y - a.y, b.x - a.x);
      const x = b.x, y = b.y, s = 7;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x - s * Math.cos(ang - 0.42), y - s * Math.sin(ang - 0.42));
      ctx.lineTo(x - s * Math.cos(ang + 0.42), y - s * Math.sin(ang + 0.42));
      ctx.closePath();
      ctx.fill();
    }
  }

  function drawPoints() {
    const sel = State.ui.selectedPoint;
    for (const p of State.route.points) {
      const isSel = sel === p.id;
      let col = COLORS.auto, r = 6, fill = "#fff";
      if (p.kind === "start") { col = COLORS.start; fill = COLORS.start; r = 9; }
      if (p.kind === "end") { col = COLORS.end; fill = COLORS.end; r = 9; }
      if (p.kind === "must") { col = COLORS.must; fill = COLORS.must; r = 8; }
      ctx.beginPath();
      ctx.arc(p.x, p.y, r + (isSel ? 3 : 0), 0, Math.PI * 2);
      ctx.fillStyle = fill; ctx.fill();
      ctx.lineWidth = 2.5; ctx.strokeStyle = col; ctx.stroke();
      label(p.x, p.y - r - 7, String(p.seq), col, true);
    }
  }

  function dot(x, y, r, color) {
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
  }

  function label(x, y, text, color, bold = false) {
    ctx.font = `${bold ? "bold " : ""}12px sans-serif`;
    const w = ctx.measureText(text).width;
    ctx.fillStyle = "rgba(255,255,255,.9)";
    ctx.fillRect(x - w / 2 - 3, y - 9, w + 6, 14);
    ctx.fillStyle = color; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(text, x, y - 1.5);
    ctx.textAlign = "start";
  }

  // -------------------------------------------------------------------------
  // 命中测试
  // -------------------------------------------------------------------------
  function hitPoint(p, tol = 12) {
    if (!State.route) return null;
    let best = null, bd = tol;
    for (const q of State.route.points) {
      const d = Math.hypot(p.x - q.x, p.y - q.y);
      if (d < bd) { bd = d; best = q; }
    }
    return best;
  }

  function hitDoor(p, tol = 9) {
    for (const d of State.data.doors) {
      if (distToSeg(p, d) <= Math.max(tol, (d.thickness || 10))) return d;
    }
    return null;
  }

  function hitSegment(p, arr, tol = 8) {
    for (const s of arr) {
      if (distToSeg(p, s) <= Math.max(tol, s.thickness || 6)) return s;
    }
    return null;
  }

  function distToSeg(p, s) {
    const dx = s.x2 - s.x1, dy = s.y2 - s.y1;
    const l2 = dx * dx + dy * dy;
    let t = l2 ? ((p.x - s.x1) * dx + (p.y - s.y1) * dy) / l2 : 0;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(p.x - (s.x1 + t * dx), p.y - (s.y1 + t * dy));
  }

  function hitBlockedMarker(p) {
    for (const s of State.route?.segments || []) {
      if (!s.blocked) continue;
      const f = s.fallbackPath;
      const mx = (f[0].x + f[1].x) / 2, my = (f[0].y + f[1].y) / 2;
      if (Math.hypot(p.x - mx, p.y - my) < 16) return s;
    }
    return null;
  }

  // -------------------------------------------------------------------------
  // 鼠标交互
  // -------------------------------------------------------------------------
  canvas.addEventListener("mousedown", (e) => {
    const p = getPos(e);
    const tool = State.ui.tool;
    HUD.style.display = "none";

    // 受阻标记 → 显示诊断
    const bm = hitBlockedMarker(p);
    if (bm && tool === "select") { showDiagnosis(bm); return; }

    // 门：点击切换开闭
    const door = hitDoor(p);
    if (door && tool === "select") {
      App.toggleDoor(door);
      return;
    }

    // 选择/拖动点位
    if (tool === "select") {
      const hp = hitPoint(p);
      if (hp) {
        State.ui.selectedPoint = hp.id;
        drag = { kind: "move", id: hp.id, start: p, current: p };
        canvas.style.cursor = "grabbing";
      } else {
        const seg = hitSegment(p, State.data.walls) || hitSegment(p, State.data.windows);
        if (seg) State.ui.selectedPoint = null;
        // 点击禁入区也不做拖动
      }
      draw();
      return;
    }

    if (tool === "zone") {
      if (!State.ui.drawingZone) State.ui.drawingZone = [];
      State.ui.drawingZone.push(p);
      drag = { kind: "zone", current: p };
      App.setZoneUIVisible(true);
      draw();
      return;
    }

    if (tool === "start" || tool === "end" || tool === "must") {
      App.placeFixedPoint(tool, p);
      return;
    }

    if (tool === "erase") {
      App.eraseAt(p, { door, wall: hitSegment(p, State.data.walls),
        win: hitSegment(p, State.data.windows), point: hitPoint(p) });
      return;
    }

    if (tool === "wall" || tool === "door" || tool === "window") {
      drag = { kind: "new", geom: tool, start: p, current: p };
      return;
    }
  });

  canvas.addEventListener("mousemove", (e) => {
    const p = getPos(e);
    if (drag?.kind === "move") {
      drag.current = p;
      // 实时跟随（吸附由后端返回；拖动中先用原始位置）
      const rec = State.route.points.find((q) => q.id === drag.id);
      if (rec) { rec.x = p.x; rec.y = p.y; rec._pending = true; }
      draw();
      return;
    }
    if (drag?.kind === "new" || drag?.kind === "zone") {
      drag.current = p; draw(); return;
    }
    // hover 光标
    const tool = State.ui.tool;
    let cur = "crosshair";
    if (tool === "select") {
      if (hitPoint(p)) cur = "grab";
      else if (hitDoor(p)) cur = "pointer";
      else if (hitBlockedMarker(p)) cur = "help";
    }
    canvas.style.cursor = cur;
  });

  window.addEventListener("mouseup", async (e) => {
    if (!drag) return;
    if (drag.kind === "new") {
      const p = getPos(e);
      if (Math.hypot(p.x - drag.start.x, p.y - drag.start.y) > 6) {
        App.addGeometry(drag.geom, drag.start, p);
      }
    } else if (drag.kind === "move") {
      const p = drag.current;
      await App.movePoint(drag.id, p);
    }
    drag = null;
    canvas.style.cursor = "";
    draw();
  });

  canvas.addEventListener("dblclick", (e) => {
    if (State.ui.tool === "zone" && State.ui.drawingZone?.length >= 3) {
      App.closeZone();
    }
  });

  function showDiagnosis(seg) {
    const r = seg.reason;
    HUD.innerHTML =
      `<b>路段 ${seg.fromSeq} → ${seg.toSeq} 无法连通</b><br>${r.summary}` +
      (r.details?.length ? "<br>" + r.details.map((d) => "• " + d).join("<br>") : "") +
      `<br><span style="color:#6b7280">提示：在选择工具下点击门可切换开闭</span>`;
    HUD.style.display = "block";
  }

  // 暴露给 app.js
  window.CanvasView = {
    draw, setSize, getPos,
    closeZoneDraft: () => { State.ui.drawingZone = null; draw(); },
    focusPoint(id) {
      State.ui.selectedPoint = id;
      draw();
    },
    showDiagnosisAtPoint(p) {
      const s = hitBlockedMarker(p);
      if (s) showDiagnosis(s);
    },
  };
})();
