// 路线变更推演：封闭通道 / 交接点 / 门控覆盖 / 逐段拖动，
// 基线-推演切换、影响摘要、冲突人工确认、版本保存与导出。
(function () {
  const $ = (id) => document.getElementById(id);
  let evalTimer = null;
  let evaluating = false;
  let drag = null;

  // -------------------------------------------------------------------------
  // 状态
  // -------------------------------------------------------------------------
  function defaultScenario() {
    return {
      name: "临时封路推演 " + new Date().toLocaleString("zh-CN", { hour12: false }),
      reason: "",
      closures: [],        // {id,x1,y1,x2,y2,thickness,label,effectiveStart,effectiveEnd,validFrom,validTo}
      handovers: [],       // {id,x,y,label,handoverClock,readyClock,dwellSeconds}
      doorOverrides: {},   // {doorId:false} 推演期间强制关闭
      pointMoves: [],      // {id,x,y}
      confirmations: {},   // {conflictKey:true}
    };
  }

  const W = {
    open: false,
    tool: "wselect",
    view: "variant",              // baseline | variant
    scenario: defaultScenario(),
    evaluation: null,             // 服务端推演结果
    baseline: null,               // 打开推演时的基线 route（随请求上送，避免重复全量计算）
    evalSeq: 0,
    loadedVersionId: null,
    dirty: false,
    busy: false,
  };
  State.whatif = W;

  // -------------------------------------------------------------------------
  // 打开 / 关闭
  // -------------------------------------------------------------------------
  async function open() {
    if (!State.data) { toast("请先建立方案（平面图/墙体/起点）"); return; }
    W.open = true;
    W.scenario = defaultScenario();
    W.evaluation = null;
    W.loadedVersionId = null;
    W.dirty = false;
    W.view = "variant";
    $("whatifBar").hidden = false;
    $("whatifTools").hidden = false;
    $("whatifPanel").hidden = false;
    $("toolGroup").hidden = true;            // 屏蔽常规几何工具
    syncNameFields();
    renderAll();
    CanvasView.draw();
    toast("推演模式：在图上拖出封闭通道、点击放置交接点");

    // 基线：优先用当前最新路线，否则按现有数据全量算一次
    if (State.route && !State.dirty) {
      W.baseline = State.route;
    } else {
      setBusy(true, "正在计算基线…");
      try {
        W.baseline = await Api.buildRoute(State.data);
        State.route = W.baseline;
        if (window.renderRouteIntoPanels) window.renderRouteIntoPanels(W.baseline);
      } catch (e) { toast(e.message, true); }
      setBusy(false);
    }
    scheduleEvaluate(0);
  }

  function close() {
    W.open = false;
    W.evaluation = null;
    State.whatif = null;
    $("whatifBar").hidden = true;
    $("whatifTools").hidden = true;
    $("whatifPanel").hidden = true;
    $("toolGroup").hidden = false;
    CanvasView.draw();
  }

  function resetScenario() {
    if (!confirm("清空当前推演的封闭通道/交接点/调整？")) return;
    W.scenario = defaultScenario();
    W.loadedVersionId = null;
    W.evaluation = null;
    W.dirty = false;
    syncNameFields();
    renderAll();
    CanvasView.draw();
    scheduleEvaluate(0);
  }

  // -------------------------------------------------------------------------
  // 推演计算（带去抖）
  // -------------------------------------------------------------------------
  function scheduleEvaluate(delay = 450) {
    W.dirty = true;
    CanvasView.draw();
    clearTimeout(evalTimer);
    evalTimer = setTimeout(evaluate, delay);
  }

  async function evaluate() {
    if (!W.open) return;
    const seq = ++W.evalSeq;
    setBusy(true, "推演计算中…");
    let res;
    try {
      res = await Api.evaluateWhatif(State.data, W.scenario);
      if (!res || res.error) throw new Error(res?.error || "推演失败");
    } catch (e) {
      setBusy(false);
      toast(e.message, true);
      return;
    }
    if (seq !== W.evalSeq) { setBusy(false); return; }  // 已被更新的请求取代
    W.evaluation = res;
    W.dirty = false;
    setBusy(false);
    renderAll();
    CanvasView.draw();
  }

  function setBusy(v, text) {
    W.busy = v;
    const el = $("whatifStatus");
    if (el) el.textContent = v ? (text || "计算中…") : "";
  }

  // -------------------------------------------------------------------------
  // 画布交互（由 canvas.js 转发）
  // -------------------------------------------------------------------------
  function isOpen() { return W.open; }
  function tool() { return W.tool; }

  function setTool(t) {
    W.tool = t;
    document.querySelectorAll(".wtool").forEach((b) =>
      b.classList.toggle("active", b.dataset.wtool === t));
    const hints = {
      wselect: "拖动交接点/巡检点调整；点击门切换推演期开闭；点击封闭通道选中编辑时段",
      closure: "按住拖出一段封闭通道（红色临时路障）",
      handover: "点击放置班次交接点，并在右侧设置交接时刻",
      werase: "点击删除封闭通道或交接点",
    };
    $("whatifToolHint").textContent = hints[t] || "";
    CanvasView.draw();
  }

  function hitClosure(p, tol = 10) {
    let best = null, bd = Infinity;
    for (const c of W.scenario.closures) {
      const d = distToSeg(p, c);
      if (d <= Math.max(tol, c.thickness || 14) && d < bd) { bd = d; best = c; }
    }
    return best;
  }

  function hitHandover(p, tol = 12) {
    let best = null, bd = tol;
    for (const h of W.scenario.handovers) {
      const d = Math.hypot(p.x - h.x, p.y - h.y);
      if (d < bd) { bd = d; best = h; }
    }
    return best;
  }

  function onMouseDown(p) {
    const t = W.tool;
    if (t === "closure") {
      drag = { kind: "closure", start: p, current: p };
      return;
    }
    if (t === "handover") {
      const h = {
        id: uid("h"), x: rnd(p.x), y: rnd(p.y),
        label: "交接点 " + (W.scenario.handovers.length + 1),
        handoverClock: $("wfDefaultStart").value || null,
        readyClock: null, dwellSeconds: 60,
      };
      W.scenario.handovers.push(h);
      scheduleEvaluate();
      renderAll();
      return;
    }
    if (t === "werase") {
      const c = hitClosure(p), h = hitHandover(p);
      if (c) { W.scenario.closures = W.scenario.closures.filter((x) => x !== c); scheduleEvaluate(); renderAll(); }
      else if (h) { W.scenario.handovers = W.scenario.handovers.filter((x) => x !== h); scheduleEvaluate(); renderAll(); }
      return;
    }
    // wselect
    const h = hitHandover(p);
    if (h) { drag = { kind: "handover", target: h, current: p }; return; }
    const c = hitClosure(p);
    if (c) { selectClosure(c); return; }
    const door = CanvasView.findDoor ? CanvasView.findDoor(p) : null;
    if (door) {
      const cur = W.scenario.doorOverrides[door.id];
      if (cur === undefined) W.scenario.doorOverrides[door.id] = false;  // 原开 → 强制关
      else if (cur === false) delete W.scenario.doorOverrides[door.id]; // 还原
      scheduleEvaluate();
      return;
    }
    if (W.view === "baseline") {
      const hp = CanvasView.findRoutePoint ? CanvasView.findRoutePoint(p) : null;
      if (hp) toast("当前为基线视图，切换到「推演方案」后可逐段拖动调整");
      return;
    }
    const hp = CanvasView.findRoutePoint ? CanvasView.findRoutePoint(p) : null;
    if (hp) {
      drag = { kind: "point", id: hp.id, start: p, current: p, rec: hp };
    }
  }

  function onMouseMove(p) {
    if (drag) {
      drag.current = p;
      if (drag.kind === "handover") {
        drag.target.x = rnd(p.x); drag.target.y = rnd(p.y);
      } else if (drag.kind === "point") {
        drag.rec.x = p.x; drag.rec.y = p.y; drag.rec._pending = true;
      }
      CanvasView.draw();
      return "grabbing";
    }
    if (W.tool === "wselect") {
      if (hitHandover(p) || hitClosure(p)) return "grab";
      if (CanvasView.findDoor && CanvasView.findDoor(p)) return "pointer";
      if (W.view === "variant" && CanvasView.findRoutePoint
          && CanvasView.findRoutePoint(p)) return "grab";
      return null;
    }
    if (W.tool === "werase" && (hitHandover(p) || hitClosure(p))) return "not-allowed";
    return null;
  }

  async function onMouseUp(p) {
    if (!drag) return;
    const d = drag;
    drag = null;
    if (d.kind === "closure") {
      if (Math.hypot(p.x - d.start.x, p.y - d.start.y) > 8) {
        const n = W.scenario.closures.length + 1;
        W.scenario.closures.push({
          id: uid("c"), x1: rnd(d.start.x), y1: rnd(d.start.y),
          x2: rnd(p.x), y2: rnd(p.y), thickness: 16,
          label: "封闭通道 " + n,
          effectiveStart: $("wfDefaultStart").value || null,
          effectiveEnd: $("wfDefaultEnd").value || null,
          validFrom: $("wfValidFrom").value || null,
          validTo: $("wfValidTo").value || null,
        });
        scheduleEvaluate(0);
        renderAll();
      }
    } else if (d.kind === "handover") {
      scheduleEvaluate(0);
      renderAll();
    } else if (d.kind === "point") {
      const mv = W.scenario.pointMoves.find((m) => m.id === d.id);
      const pos = { id: d.id, x: rnd(p.x), y: rnd(p.y) };
      if (mv) Object.assign(mv, pos); else W.scenario.pointMoves.push(pos);
      await evaluate();
    }
  }

  function isDragging() { return !!drag; }
  function dragInfo() { return drag; }

  function selectClosure(c) {
    W.selectedClosureId = c.id;
    renderClosureList();
    CanvasView.draw();
  }

  // -------------------------------------------------------------------------
  // 画布叠加层（由 canvas.js 在门之后、路线之前调用）
  // -------------------------------------------------------------------------
  function drawOverlays(ctx, Wpx, Hpx, helpers) {
    if (!W.open) return;
    const showC = State.ui.layers.closures !== false;
    const showH = State.ui.layers.handovers !== false;
    if (showC) {
      for (const c of W.scenario.closures) drawClosure(ctx, c, helpers, c.id === W.selectedClosureId);
      // 正在拖出的草稿
      if (drag?.kind === "closure") {
        const c = { x1: drag.start.x, y1: drag.start.y, x2: drag.current.x,
          y2: drag.current.y, thickness: 16 };
        drawClosure(ctx, c, helpers, true);
      }
    }
    if (showH) {
      for (const h of W.scenario.handovers) drawHandover(ctx, h, helpers);
    }
    drawDoorOverrides(ctx, helpers);
    if (W.view === "variant" && W.evaluation) drawDropped(ctx, helpers);
  }

  function drawClosure(ctx, c, h, selected) {
    ctx.save();
    ctx.lineCap = "round";
    ctx.strokeStyle = "rgba(220,38,38,.22)";
    ctx.lineWidth = (c.thickness || 16) + 8;
    ctx.beginPath(); ctx.moveTo(c.x1, c.y1); ctx.lineTo(c.x2, c.y2); ctx.stroke();
    ctx.strokeStyle = "#dc2626";
    ctx.lineWidth = c.thickness || 16;
    ctx.setLineDash([14, 9]);
    ctx.beginPath(); ctx.moveTo(c.x1, c.y1); ctx.lineTo(c.x2, c.y2); ctx.stroke();
    ctx.setLineDash([]);
    ctx.strokeStyle = selected ? "#7f1d1d" : "#991b1b";
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(c.x1, c.y1); ctx.lineTo(c.x2, c.y2); ctx.stroke();
    for (const [x, y] of [[c.x1, c.y1], [c.x2, c.y2]]) {
      ctx.fillStyle = "#7f1d1d";
      ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
    }
    const mx = (c.x1 + c.x2) / 2, my = (c.y1 + c.y2) / 2;
    const win = c.effectiveStart ? ` ${c.effectiveStart}–${c.effectiveEnd || "次日"}` : "";
    h.label(mx, my - (c.thickness || 16) / 2 - 9, `🚧 ${c.label || "封闭"}${win}`, "#b91c1c", true);
    ctx.restore();
  }

  function drawHandover(ctx, hh, h) {
    const s = 11;
    ctx.save();
    ctx.translate(hh.x, hh.y);
    ctx.rotate(Math.PI / 4);
    ctx.fillStyle = "#7c3aed";
    ctx.fillRect(-s * 0.7, -s * 0.7, s * 1.4, s * 1.4);
    ctx.strokeStyle = "#4c1d95"; ctx.lineWidth = 2;
    ctx.strokeRect(-s * 0.7, -s * 0.7, s * 1.4, s * 1.4);
    ctx.restore();
    ctx.fillStyle = "#fff";
    ctx.font = "bold 11px sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("交", hh.x, hh.y + 0.5);
    const tag = `${hh.label || "交接点"}${hh.handoverClock ? " ≤" + hh.handoverClock : ""}`;
    h.label(hh.x, hh.y - 22, tag, "#6d28d9", true);
    ctx.textAlign = "start";
  }

  function drawDoorOverrides(ctx, h) {
    for (const d of State.data.doors || []) {
      if (W.scenario.doorOverrides[d.id] === false) {
        h.label((d.x1 + d.x2) / 2, (d.y1 + d.y2) / 2 - 12, "推演期关闭", "#b91c1c", true);
      }
    }
  }

  function drawDropped(ctx, h) {
    // 基线里有、推演中丢失的巡检点：用灰色 ✕ 标在基线位置
    const imp = W.evaluation.impact;
    if (!imp) return;
    for (const a of imp.affectedPoints) {
      if (a.status !== "dropped") continue;
      const bp = (W.evaluation.baseline.points || []).find(
        (p) => p.id === a.id && (p.worker || 0) === (a.worker || 0)
          && p.seq === a.baseSeq);
      if (!bp) continue;
      ctx.save();
      ctx.strokeStyle = "#9ca3af"; ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.moveTo(bp.x - 7, bp.y - 7); ctx.lineTo(bp.x + 7, bp.y + 7);
      ctx.moveTo(bp.x + 7, bp.y - 7); ctx.lineTo(bp.x - 7, bp.y + 7);
      ctx.stroke();
      h.label(bp.x, bp.y - 12, "不可达", "#6b7280", true);
      ctx.restore();
    }
  }

  // -------------------------------------------------------------------------
  // 右侧面板渲染
  // -------------------------------------------------------------------------
  function syncNameFields() {
    $("wfName").value = W.scenario.name || "";
    $("wfReason").value = W.scenario.reason || "";
  }

  function renderAll() {
    renderViewSwitch();
    renderClosureList();
    renderHandoverList();
    renderImpact();
    renderConflicts();
    renderAffected();
    renderVersions();
    renderActions();
    syncNameFields();
  }

  function renderViewSwitch() {
    document.querySelectorAll(".wf-view-btn").forEach((b) => {
      const on = b.dataset.view === W.view;
      b.classList.toggle("active", on);
      b.disabled = (b.dataset.view === "variant" && !W.evaluation && W.busy);
    });
    $("whatifStatus").textContent = W.busy
      ? "推演计算中…" : W.dirty ? "参数已变更，等待重算…" : "";
  }

  function renderClosureList() {
    const box = $("wfClosures");
    if (!W.scenario.closures.length) {
      box.innerHTML = '<div class="hint">尚无封闭通道。选择左侧「🚧 封闭通道」工具在图上拖出。</div>';
      return;
    }
    box.innerHTML = "";
    W.scenario.closures.forEach((c, i) => {
      const card = document.createElement("div");
      card.className = "wf-card" + (c.id === W.selectedClosureId ? " sel" : "");
      card.innerHTML = `
        <div class="wf-card-h">
          <span>🚧 <b>${escapeHtml(c.label || ("封闭通道 " + (i + 1)))}</b></span>
          <button class="wf-del" title="删除">✕</button>
        </div>
        <div class="wf-grid">
          <span>生效</span>
          <span><input type="time" data-f="effectiveStart" step="60" value="${c.effectiveStart || ""}">
          ~ <input type="time" data-f="effectiveEnd" step="60" value="${c.effectiveEnd || ""}"></span>
          <span>起止日期</span>
          <span><input type="date" data-f="validFrom" value="${c.validFrom || ""}">
          ~ <input type="date" data-f="validTo" value="${c.validTo || ""}"></span>
        </div>`;
      card.querySelector(".wf-card-h b").addEventListener("click", () => {
        const name = prompt("封闭通道名称", c.label || "");
        if (name !== null) { c.label = name || c.label; scheduleEvaluate(300); CanvasView.draw(); renderClosureList(); }
      });
      card.querySelector(".wf-del").addEventListener("click", () => {
        W.scenario.closures = W.scenario.closures.filter((x) => x !== c);
        scheduleEvaluate(200); renderAll();
      });
      card.querySelectorAll("input").forEach((inp) => {
        inp.addEventListener("change", () => {
          const f = inp.dataset.f;
          c[f] = inp.value || null;
          scheduleEvaluate(500);
          CanvasView.draw();
        });
      });
      box.appendChild(card);
    });
  }

  function renderHandoverList() {
    const box = $("wfHandovers");
    if (!W.scenario.handovers.length) {
      box.innerHTML = '<div class="hint">尚无交接点。选择「🔁 交接点」工具在图上点击放置。</div>';
      return;
    }
    box.innerHTML = "";
    W.scenario.handovers.forEach((h) => {
      const late = h._late;
      const card = document.createElement("div");
      card.className = "wf-card" + (late ? " late" : "");
      card.innerHTML = `
        <div class="wf-card-h">
          <span>🔁 <b>${escapeHtml(h.label || h.id)}</b></span>
          <button class="wf-del" title="删除">✕</button>
        </div>
        <div class="wf-grid">
          <span>交接时刻</span><input type="time" data-f="handoverClock" step="60" value="${h.handoverClock || ""}">
          <span>最早到达</span><input type="time" data-f="readyClock" step="60" value="${h.readyClock || ""}">
          <span>停留(秒)</span><input type="number" min="0" step="10" data-f="dwellSeconds" value="${h.dwellSeconds ?? 60}">
        </div>`;
      card.querySelector("b").addEventListener("click", () => {
        const name = prompt("交接点名称", h.label || "");
        if (name !== null) { h.label = name || h.label; scheduleEvaluate(300); renderHandoverList(); CanvasView.draw(); }
      });
      card.querySelector(".wf-del").addEventListener("click", () => {
        W.scenario.handovers = W.scenario.handovers.filter((x) => x !== h);
        scheduleEvaluate(200); renderAll();
      });
      card.querySelectorAll("input").forEach((inp) => {
        inp.addEventListener("change", () => {
          const f = inp.dataset.f;
          if (f === "dwellSeconds") h[f] = inp.value === "" ? 60 : Math.max(0, parseFloat(inp.value) || 0);
          else h[f] = inp.value || null;
          scheduleEvaluate(300);
        });
      });
      box.appendChild(card);
    });
  }

  function renderImpact() {
    const box = $("wfImpact");
    const ev = W.evaluation;
    if (!ev) { box.innerHTML = '<div class="hint">设置封闭通道/交接点后自动推演…</div>'; return; }
    const s = ev.impact.summary;
    const base = ev.baseline.stats || {}, var_ = ev.variant.stats || {};
    const bc = ev.baseline.coverage || {}, vc = ev.variant.coverage || {};
    const badge = ev.feasible
      ? '<span class="wf-badge ok">✓ 推演可行（无高级别冲突）</span>'
      : '<span class="wf-badge bad">✗ 存在断点或高级别冲突，需人工确认</span>';
    const row = (label, b, v, fmt, bad) => {
      const bb = b == null ? "—" : fmt(b), vv = v == null ? "—" : fmt(v);
      let d = "";
      if (typeof b === "number" && typeof v === "number") {
        const dv = v - b;
        if (Math.abs(dv) > 1e-9) d = ` <span class="${bad && dv > 0 ? "neg" : "dim"}">(${dv > 0 ? "+" : ""}${fmt(dv)})</span>`;
      }
      return `<div class="wf-stat"><span>${label}</span><b>${bb} → ${vv}</b>${d}</div>`;
    };
    box.innerHTML = `
      <div class="wf-badgerow">${badge}</div>
      <div class="wf-kpis">
        <div><b>${s.affectedPointCount}</b><span>受影响点</span></div>
        <div><b class="${s.delayedCount ? "neg" : ""}">${s.delayedCount}</b><span>延误</span></div>
        <div><b class="${s.reroutedCount ? "warn" : ""}">${s.reroutedCount}</b><span>绕行</span></div>
        <div><b class="${s.blockedPointCount + s.droppedCount ? "neg" : ""}">${s.blockedPointCount + s.droppedCount}</b><span>不可达</span></div>
      </div>
      ${row("总里程(m)", base.totalLengthM, var_.totalLengthM, (x) => r1(x), true)}
      ${row("预计用时(分)", base.etaMinutes, var_.etaMinutes, (x) => r1(x), true)}
      <div class="wf-stat"><span>最大单点延误</span><b class="${s.maxDelayMin >= 5 ? "neg" : ""}">${r1(s.maxDelayMin)} 分</b></div>
      <div class="wf-stat"><span>绕行新增里程</span><b>${r1(s.detourM)} m</b></div>
      <div class="wf-stat"><span>重复经过</span><b>${r1(s.repeatedM)} m</b></div>
      ${row("覆盖率(%)", bc.percent, vc.percent, (x) => r1(x), false)}
      ${row("受阻路段(段)", base.blockedCount, var_.blockedCount, (x) => x, true)}
      ${base.finishClock ? row("预计交岗", base.finishClock, var_.finishClock, (x) => x, true) : ""}
    `;
  }

  function renderConflicts() {
    const box = $("wfConflicts");
    const ev = W.evaluation;
    if (!ev) { box.innerHTML = ""; return; }
    const cs = ev.impact.conflicts || [];
    if (!cs.length) {
      box.innerHTML = '<div class="hint ok-hint">无需人工确认的冲突。</div>';
      return;
    }
    const unconf = cs.filter((c) => !c.confirmed).length;
    box.innerHTML = `
      <div class="wf-conf-head ${unconf ? "" : "allok"}">
        ${unconf ? `⚠ ${unconf} 项待确认 / 共 ${cs.length} 项` : `✓ 全部 ${cs.length} 项已人工确认`}
      </div>` + cs.map((c) => `
        <label class="wf-conf ${c.severity} ${c.confirmed ? "confirmed" : ""}">
          <input type="checkbox" data-key="${escapeAttr(c.key)}" ${c.confirmed ? "checked" : ""}>
          <span class="wf-conf-tag ${c.severity}">${{ high: "高", medium: "中", low: "提示" }[c.severity]}</span>
          <span>${escapeHtml(c.message)}</span>
        </label>`).join("");
    box.querySelectorAll("input").forEach((inp) => {
      inp.addEventListener("change", () => {
        const key = inp.dataset.key;
        if (inp.checked) W.scenario.confirmations[key] = true;
        else delete W.scenario.confirmations[key];
        const c = ev.impact.conflicts.find((x) => x.key === key);
        if (c) c.confirmed = inp.checked;
        renderConflicts();
        renderImpact();
      });
    });
  }

  function renderAffected() {
    const box = $("wfAffected");
    const ev = W.evaluation;
    if (!ev) { box.innerHTML = ""; return; }
    const items = ev.impact.affectedPoints || [];
    if (!items.length) { box.innerHTML = '<div class="hint">巡检点均未受影响。</div>'; return; }
    const name = { delayed: "延误", rerouted: "绕行", blocked: "受阻", dropped: "不可达" };
    box.innerHTML = items.map((a) => {
      const det = a.status === "delayed"
        ? `+${r1(a.delayMin)} 分${a.rerouted ? " · 绕行" : ""}${a.varClock ? `（${a.baseClock || "—"}→${a.varClock}）` : ""}`
        : a.status === "rerouted" ? `绕行 +${r1(a.detourM)} m`
        : a.status === "blocked" ? "相邻路段不通"
        : "路线中丢失";
      return `<div class="wf-aff ${a.status}">
        <span class="wf-aff-tag">${name[a.status]}</span>
        <span>${a.worker ? `P${a.worker + 1} ` : ""}${escapeHtml(a.label)}${a.role === "handover" ? " 🔁" : ""}</span>
        <span class="wf-aff-d">${det}</span>
      </div>`;
    }).join("");
  }

  async function renderVersions() {
    const box = $("wfVersions");
    if (!State.planId) {
      box.innerHTML = '<div class="hint">保存方案后可把推演存为版本。</div>';
      return;
    }
    let versions = [];
    try { versions = (await Api.listScenarios(State.planId)).scenarios; } catch (e) { return; }
    if (!versions.length) { box.innerHTML = '<div class="hint">尚无已保存的推演版本。</div>'; return; }
    const stName = { draft: "草稿", confirmed: "已确认", published: "已发布" };
    box.innerHTML = versions.map((v) => `
      <div class="wf-ver ${v.id === W.loadedVersionId ? "sel" : ""}">
        <span class="wf-ver-name">${escapeHtml(v.name)}</span>
        <span class="wf-ver-st ${v.status}">${stName[v.status] || v.status}</span>
        <button data-act="load" data-id="${v.id}">载入</button>
        <button data-act="publish" data-id="${v.id}" title="标记为已发布">发布</button>
        <button data-act="del" data-id="${v.id}" class="danger">✕</button>
      </div>`).join("");
    box.querySelectorAll("button").forEach((b) => {
      b.addEventListener("click", async () => {
        const id = parseInt(b.dataset.id, 10), act = b.dataset.act;
        if (act === "load") await loadVersion(id);
        else if (act === "publish") {
          await Api.publishScenario(id); toast("推演版本已发布"); renderVersions();
        } else if (act === "del") {
          if (!confirm("删除该推演版本？")) return;
          await Api.deleteScenario(id);
          if (W.loadedVersionId === id) W.loadedVersionId = null;
          renderVersions();
        }
      });
    });
  }

  function renderActions() {
    const hasPlan = !!State.planId;
    $("wfSaveNew").disabled = !hasPlan;
    $("wfSaveUpd").hidden = !(hasPlan && W.loadedVersionId);
    $("wfConfirmVer").disabled = !(hasPlan && W.loadedVersionId);
    $("wfExportPng").disabled = !W.evaluation;
    $("wfExportMd").disabled = !W.evaluation;
  }

  // -------------------------------------------------------------------------
  // 版本保存 / 载入
  // -------------------------------------------------------------------------
  function collectPayload() {
    W.scenario.name = $("wfName").value.trim() || W.scenario.name;
    W.scenario.reason = $("wfReason").value.trim();
    return W.scenario;
  }

  async function saveNew() {
    if (!State.planId) { toast("请先保存方案"); return; }
    const sc = collectPayload();
    try {
      const created = await Api.createScenario(State.planId, {
        name: sc.name, reason: sc.reason, scenario: sc, status: "draft",
      });
      W.loadedVersionId = created.id;
      toast("推演版本已保存");
      renderVersions(); renderActions();
    } catch (e) { toast(e.message, true); }
  }

  async function saveUpdate() {
    if (!W.loadedVersionId) return;
    const sc = collectPayload();
    try {
      await Api.updateScenario(W.loadedVersionId, {
        name: sc.name, reason: sc.reason, scenario: sc,
      });
      toast("版本已更新"); renderVersions();
    } catch (e) { toast(e.message, true); }
  }

  async function markConfirmed() {
    if (!W.loadedVersionId) {
      toast("请先保存或载入一个版本"); return;
    }
    const unconf = (W.evaluation?.impact.conflicts || []).filter((c) => !c.confirmed).length;
    if (unconf && !confirm(`仍有 ${unconf} 项冲突未确认，仍要标记为「已确认」吗？`)) return;
    await Api.updateScenario(W.loadedVersionId, { status: "confirmed" });
    toast("版本已标记为已确认"); renderVersions();
  }

  async function loadVersion(id) {
    const full = await Api.getScenario(id);
    W.scenario = Object.assign(defaultScenario(), full.scenario);
    W.loadedVersionId = id;
    W.view = "variant";
    syncNameFields();
    renderAll();
    scheduleEvaluate(0);
    toast(`已载入推演版本「${full.name}」`);
  }

  // -------------------------------------------------------------------------
  // 导出
  // -------------------------------------------------------------------------
  function exportPng() {
    const ev = W.evaluation;
    if (!ev) return;
    const canvas = $("canvas");
    const Wpx = State.data.image?.width || 1600;
    const Hpx = State.data.image?.height || 1000;
    const dpr = canvas.width / Wpx || 1;
    const head = 96 * dpr;
    const out = document.createElement("canvas");
    out.width = Math.round(Wpx * dpr);
    out.height = Math.round(Hpx * dpr + head);
    const ctx = out.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, Wpx, Hpx + 96);
    // 标题条
    ctx.fillStyle = "#111827";
    ctx.fillRect(0, 0, Wpx, 96);
    ctx.fillStyle = "#f9fafb";
    ctx.font = "bold 22px sans-serif";
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    ctx.fillText(`路线推演图 — ${W.scenario.name}`, 18, 32);
    ctx.font = "14px sans-serif";
    const s = ev.impact.summary;
    const win = W.scenario.closures.map((c) =>
      c.effectiveStart ? `${c.label} ${c.effectiveStart}-${c.effectiveEnd || "次日"}` : ""
    ).filter(Boolean).join("；");
    ctx.fillText(
      `原因：${W.scenario.reason || "—"}${win ? "　生效：" + win : ""}`, 18, 56);
    ctx.fillText(
      `受影响点 ${s.affectedPointCount}（延误 ${s.delayedCount}/绕行 ${s.reroutedCount}/不可达 ${s.blockedPointCount + s.droppedCount}）`
      + `　用时 ${ev.baseline.stats.etaMinutes}→${ev.variant.stats.etaMinutes} 分`
      + `　里程 ${ev.baseline.stats.totalLengthM}→${ev.variant.stats.totalLengthM} m`
      + `　${ev.feasible ? "推演可行" : "存在需确认冲突"}`, 18, 80);
    // 当前画布（含底图/路线/叠加层）
    ctx.drawImage(canvas, 0, head, Wpx, Hpx);
    const a = document.createElement("a");
    a.download = `路线推演-${W.scenario.name}.png`.replace(/[\\/:*?"<>|]/g, "_");
    a.href = out.toDataURL("image/png");
    a.click();
    toast("路线图 PNG 已导出");
  }

  function exportMarkdown() {
    const ev = W.evaluation;
    if (!ev) return;
    const s = ev.impact.summary;
    const L = [];
    L.push(`# 路线变更推演差异说明 — ${W.scenario.name}`);
    L.push("");
    L.push(`- 变更原因：${W.scenario.reason || "—"}`);
    const wins = W.scenario.closures.map((c) =>
      `  - ${c.label}：${c.effectiveStart || "全天"}–${c.effectiveEnd || "次日"}`
      + `${c.validFrom || c.validTo ? `（${c.validFrom || "…"} ~ ${c.validTo || "…"}）` : ""}`
    );
    if (wins.length) { L.push("- 生效时段："); L.push(...wins); }
    if (W.scenario.handovers.length) {
      L.push("- 交接点：");
      for (const h of W.scenario.handovers)
        L.push(`  - ${h.label}，交接时刻 ${h.handoverClock || "未设定"}`);
    }
    L.push("");
    L.push("## 影响摘要");
    L.push("");
    L.push("| 指标 | 基线 | 推演 | 变化 |");
    L.push("|---|---|---|---|");
    const diff = (b, v, u) => {
      const d = v - b;
      return Math.abs(d) < 1e-9 ? "—" : `${d > 0 ? "+" : ""}${r1(d)}${u || ""}`;
    };
    L.push(`| 总里程 (m) | ${r1(ev.baseline.stats.totalLengthM)} | ${r1(ev.variant.stats.totalLengthM)} | ${diff(ev.baseline.stats.totalLengthM, ev.variant.stats.totalLengthM)} |`);
    L.push(`| 预计用时 (分) | ${r1(ev.baseline.stats.etaMinutes)} | ${r1(ev.variant.stats.etaMinutes)} | ${diff(ev.baseline.stats.etaMinutes, ev.variant.stats.etaMinutes)} |`);
    L.push(`| 覆盖率 (%) | ${r1(ev.baseline.coverage.percent)} | ${r1(ev.variant.coverage.percent)} | ${diff(ev.baseline.coverage.percent, ev.variant.coverage.percent)} |`);
    L.push(`| 受阻路段 (段) | ${ev.baseline.stats.blockedCount} | ${ev.variant.stats.blockedCount} | ${diff(ev.baseline.stats.blockedCount, ev.variant.stats.blockedCount)} |`);
    L.push(`| 受影响巡检点 | — | ${s.affectedPointCount} | 延误 ${s.delayedCount} / 绕行 ${s.reroutedCount} / 不可达 ${s.blockedPointCount + s.droppedCount} |`);
    L.push(`| 最大单点延误 | — | ${r1(s.maxDelayMin)} 分 | — |`);
    L.push(`| 绕行新增里程 | — | ${r1(s.detourM)} m | — |`);
    L.push(`| 重复经过 | — | ${r1(s.repeatedM)} m | — |`);
    if (ev.baseline.stats.finishClock)
      L.push(`| 预计交岗 | ${ev.baseline.stats.finishClock} | ${ev.variant.stats.finishClock} | — |`);
    L.push("");

    const aff = ev.impact.affectedPoints || [];
    if (aff.length) {
      L.push("## 受影响巡检点");
      L.push("");
      L.push("| 状态 | 点位 | 基线路序 | 推演路序 | 延误(分) | 绕行(m) | 时刻 |");
      L.push("--|---|---|---|---|---|---|");
      const nm = { delayed: "延误", rerouted: "绕行", blocked: "受阻", dropped: "不可达" };
      for (const a of aff) {
        L.push(`| ${nm[a.status]} | ${a.label}${a.role === "handover" ? " 🔁" : ""} | ${a.baseSeq ?? "—"} | ${a.varSeq ?? "—"} | ${a.delayMin ?? "—"} | ${a.detourM ?? "—"} | ${a.baseClock || "—"}→${a.varClock || "—"} |`);
      }
      L.push("");
    }
    const conf = ev.impact.conflicts || [];
    if (conf.length) {
      L.push("## 需人工确认的冲突");
      L.push("");
      const sev = { high: "高", medium: "中", low: "提示" };
      for (const c of conf)
        L.push(`- [${c.confirmed ? "x" : " "}] **[${sev[c.severity]}]** ${c.message}`);
      L.push("");
    }
    L.push(`> 生成时间：${new Date().toLocaleString("zh-CN")}`);
    const blob = new Blob([L.join("\n")], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `路线推演差异-${W.scenario.name}.md`.replace(/[\\/:*?"<>|]/g, "_");
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast("差异说明（Markdown）已导出");
  }

  // -------------------------------------------------------------------------
  // 小工具
  // -------------------------------------------------------------------------
  function rnd(v) { return Math.round(v * 10) / 10; }
  function r1(v) { return (v == null || isNaN(v)) ? "—" : (Math.round(v * 10) / 10).toFixed(1); }
  function distToSeg(p, s) {
    const dx = s.x2 - s.x1, dy = s.y2 - s.y1;
    const l2 = dx * dx + dy * dy;
    let t = l2 ? ((p.x - s.x1) * dx + (p.y - s.y1) * dy) / l2 : 0;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(p.x - (s.x1 + t * dx), p.y - (s.y1 + t * dy));
  }
  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }
  function escapeAttr(s) { return escapeHtml(s); }
  function toast(text, isErr) {
    const t = document.createElement("div");
    t.className = "toast";
    if (isErr) t.style.background = "#b91c1c";
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }

  // -------------------------------------------------------------------------
  // 事件绑定
  // -------------------------------------------------------------------------
  function bind() {
    $("whatifBtn").addEventListener("click", open);
    $("wfExit").addEventListener("click", close);
    $("wfReset").addEventListener("click", resetScenario);
    document.querySelectorAll(".wtool").forEach((b) =>
      b.addEventListener("click", () => setTool(b.dataset.wtool)));
    document.querySelectorAll(".wf-view-btn").forEach((b) =>
      b.addEventListener("click", () => {
        W.view = b.dataset.view;
        CanvasView.draw(); renderViewSwitch();
      }));
    $("wfName").addEventListener("input", (e) => { W.scenario.name = e.target.value; });
    $("wfReason").addEventListener("input", (e) => { W.scenario.reason = e.target.value; });
    $("wfSaveNew").addEventListener("click", saveNew);
    $("wfSaveUpd").addEventListener("click", saveUpdate);
    $("wfConfirmVer").addEventListener("click", markConfirmed);
    $("wfExportPng").addEventListener("click", exportPng);
    $("wfExportMd").addEventListener("click", exportMarkdown);
    setTool("wselect");
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", bind);
  else bind();

  window.Whatif = {
    isOpen, tool, setTool, onMouseDown, onMouseMove, onMouseUp,
    isDragging, dragInfo, drawOverlays, close,
  };
})();
