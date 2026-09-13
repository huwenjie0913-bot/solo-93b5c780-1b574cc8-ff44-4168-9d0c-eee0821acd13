// 极简 fetch 封装
const Api = (() => {
  async function req(url, opts = {}) {
    const res = await fetch(url, {
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
    return data;
  }
  return {
    listPlans: () => req("/api/plans"),
    getPlan: (id) => req(`/api/plans/${id}`),
    createPlan: (payload) => req("/api/plans", { method: "POST", body: JSON.stringify(payload) }),
    updatePlan: (id, payload) => req(`/api/plans/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
    deletePlan: (id) => req(`/api/plans/${id}`, { method: "DELETE" }),
    duplicatePlan: (id) => req(`/api/plans/${id}/duplicate`, { method: "POST" }),

    uploadImage: (file) => {
      const fd = new FormData();
      fd.append("image", file);
      return fetch("/api/images", { method: "POST", body: fd })
        .then(async (r) => {
          const d = await r.json();
          if (!r.ok) throw new Error(d.error || "上传失败");
          return d;
        });
    },

    buildRoute: (data) => req("/api/route", { method: "POST", body: JSON.stringify({ data }) }),
    updateRoute: (data, cache, edits) =>
      req("/api/route/update", {
        method: "POST",
        body: JSON.stringify({ data, cache, edits }),
      }),

    listSnapshots: (pid) => req(`/api/plans/${pid}/snapshots`),
    addSnapshot: (pid, name) =>
      req(`/api/plans/${pid}/snapshots`, { method: "POST", body: JSON.stringify({ name }) }),
    deleteSnapshot: (sid) => req(`/api/snapshots/${sid}`, { method: "DELETE" }),
    compare: (pid, ids) => req(`/api/plans/${pid}/compare?ids=${ids.join(",")}`),
  };
})();
