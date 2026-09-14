// 全局状态：方案数据、计算结果、界面状态
const State = {
  planId: null,
  data: null,          // 与后端共享的方案 JSON
  route: null,         // 后端返回的路线结果
  planImage: null,     // HTMLImageElement（平面图）
  dirty: false,        // 方案有未计算的几何改动
  suppressAuto: false, // 拖动过程中临时抑制自动重算
  ui: {
    tool: "select",
    layers: { walls: true, zones: true, points: true, route: true, uncovered: true, grid: false },
    showOccupancy: false,
    threshold: 128,
    drawingZone: null,      // 正在绘制的禁入区点集
    calib: null,            // {x1,y1,x2,y2} 校准线
    calibMode: false,
    hoverPoint: null,
    selectedPoint: null,
    pan: { x: 0, y: 0 },
  },
};

function uid(prefix) {
  return prefix + "_" + Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-3);
}

function defaultData() {
  return {
    name: "未命名方案",
    image: null,
    calibration: { pixelsPerMeter: 40, line: null, realMeters: null, calibrated: false },
    settings: {
      spacing: 6, margin: 0.3, dwell: 30, threshold: 128,
      workers: 1, maxMinutes: 0, startMode: "shared",
      shiftStart: null,   // "HH:MM"；null=不排程（旧方案行为）
    },
    walls: [], doors: [], windows: [], zones: [],
    start: null, end: null, mustPass: [],
    workerStarts: [], workerEnds: [], assignments: {},
    createdAt: Date.now(),
  };
}

// 哪些几何元素参与栅格化（编辑后需要全量重算）
const GEOM_KEYS = ["walls", "doors", "windows", "zones", "image", "calibration", "settings"];

// 班次时钟 "HH:MM"（可跨午夜）→ 班次相对分钟；非法返回 null
function clockToOffset(clock, shiftClock) {
  const m = /^(\d{1,2}):(\d{2})$/.exec((clock || "").trim());
  const s = /^(\d{1,2}):(\d{2})$/.exec((shiftClock || "").trim());
  if (!m || !s) return null;
  const t = +m[1] * 60 + +m[2], base = +s[1] * 60 + +s[2];
  if (+m[1] > 24 || +m[2] > 59) return null;
  return (t - base + 1440) % 1440;
}

State.init = function () {
  this.data = defaultData();
  this.route = null;
  this.planImage = null;
  this.dirty = false;
};

State.markDirty = function () {
  this.dirty = true;
};

// 找到某点在路线序列中的记录
State.pointRecord = function (id) {
  return (this.route?.points || []).find((p) => p.id === id) || null;
};
