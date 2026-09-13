// 从灰度平面图提取占用栅格（黑/深色像素 = 实体），并按栅格行优先打包成位串。
// 后端 raster.unpack_bits 要求逐位、每行按字节对齐，与这里编码保持一致。

const OCC_MAX_COLS = 320;   // 占用栅格最大列数
const OCC_MAX_ROWS = 240;

/**
 * 计算栅格尺寸：长边对齐 OCC_MAX_COLS。
 */
function gridSize(naturalW, naturalH) {
  const ratio = Math.min(OCC_MAX_COLS / naturalW, OCC_MAX_ROWS / naturalH, 1);
  return {
    w: Math.max(8, Math.round(naturalW * ratio)),
    h: Math.max(8, Math.round(naturalH * ratio)),
  };
}

/**
 * 提取占用位图。
 * @returns {gridW, gridH, bits: Uint8Array(0/1)}
 */
function extract(imgEl, threshold) {
  const { w, h } = gridSize(imgEl.naturalWidth, imgEl.naturalHeight);
  const off = document.createElement("canvas");
  off.width = w; off.height = h;
  const ctx = off.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(imgEl, 0, 0, w, h);
  const { data } = ctx.getImageData(0, 0, w, h);
  const bits = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    const r = data[i * 4], g = data[i * 4 + 1], b = data[i * 4 + 2];
    const gray = 0.299 * r + 0.587 * g + 0.114 * b;
    // 透明像素视为可通行，不透明且深于阈值视为实体
    const a = data[i * 4 + 3];
    bits[i] = a > 128 && gray < threshold ? 1 : 0;
  }
  return { gridW: w, gridH: h, bits };
}

/**
 * 打包为 base64：每像素 1 bit，行优先，每行末尾补 0 到字节边界。
 */
function encode(bits, w, h) {
  const rowBytes = Math.ceil(w / 8);
  const out = new Uint8Array(rowBytes * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (bits[y * w + x]) {
        const bitInRow = x;
        out[y * rowBytes + (bitInRow >> 3)] |= 0x80 >> (bitInRow & 7);
      }
    }
  }
  // Uint8Array → binary string → base64
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < out.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, out.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

/** 重建占用数据（用于预览覆盖层），返回与栅格等大的 Uint8Array。 */
function decode(b64, w, h) {
  const bin = atob(b64 || "");
  const rowBytes = Math.ceil(w / 8);
  const bits = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const byte = bin.charCodeAt(y * rowBytes + (x >> 3)) || 0;
      bits[y * w + x] = (byte >> (7 - (x & 7))) & 1;
    }
  }
  return bits;
}

/** 完整流程：HTMLImageElement + 阈值 → 写入 data.image 的占用字段。 */
function buildImagePayload(imgEl, fileMeta, threshold) {
  const { gridW, gridH, bits } = extract(imgEl, threshold);
  return {
    filename: fileMeta.filename,
    url: fileMeta.url,
    originalName: fileMeta.originalName,
    width: imgEl.naturalWidth,
    height: imgEl.naturalHeight,
    gridWidth: gridW,
    gridHeight: gridH,
    threshold,
    occupancy: encode(bits, gridW, gridH),
  };
}

window.Occupancy = { gridSize, extract, encode, decode, buildImagePayload };
