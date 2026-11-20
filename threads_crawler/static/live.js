// Tab LIVE: kết nối TikTok, giả lập sự kiện để thử, xem trước sân khấu.
(() => {
  const STATUS = { idle: "Chưa kết nối", connecting: "Đang kết nối…", live: "Đang nhận tương tác" };

  async function post(path, body) {
    const r = await fetch("/api/live/" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    if (!r.ok) {
      const d = (await r.json().catch(() => ({}))).detail;
      throw new Error(typeof d === "string" ? d : "Yêu cầu không hợp lệ");
    }
    return r.json();
  }
  const guard = (fn) => async (...a) => {
    try { await fn(...a); } catch (e) { toast(e.message); }
  };

  const fields = { music_on: "#liveMusicOn", music_group: "#liveMusicGroup", music_volume: "#liveMusicVol", sfx_on: "#liveSfxOn", sfx_volume: "#liveSfxVol", host_on: "#liveHostOn" };
  const readField = (el) => (el.type === "checkbox" ? el.checked : el.type === "range" ? Number(el.value) : el.value);

  function renderSettings(st) {
    for (const [k, sel] of Object.entries(fields)) {
      const el = $(sel);
      if (el.type === "checkbox") el.checked = st[k];
      else if (document.activeElement !== el) el.value = st[k];
    }
  }
  Object.values(fields).forEach((sel) => {
    $(sel).addEventListener("change", guard(() => post("settings", Object.fromEntries(Object.entries(fields).map(([k, s]) => [k, readField($(s))])))));
  });

  async function loadMedia() {
    const m = await (await fetch("/api/live/media")).json();
    const group = $("#liveMusicGroup"), current = lastSettings?.music_group ?? group.value;
    group.innerHTML = `<option value="">Mọi nhóm</option>` + m.music_groups.map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join("");
    group.value = current;
    const clips = Object.keys(m.host);
    $("#liveHostInfo").textContent = m.host.idle
      ? `Đang dùng video: ${clips.join(", ")}. Video luôn có nhãn "Nhân vật AI". Thêm hoặc đổi file thì bấm Refresh trong OBS.`
      : "Chưa có video. Chép vào materials/live_host/ file idle.webm (lặp liên tục), và nếu muốn thêm gift.webm, levelup.webm (phát khi có quà, lên cấp).";
  }
  let lastSettings = null;

  let previewOn = false;
  $("#livePreviewAudio").onclick = () => {
    previewOn = !previewOn;
    $("#liveStage").contentWindow.setStageAudio?.(previewOn);
    $("#livePreviewAudio").textContent = previewOn ? "🔊 Tắt nghe thử" : "🔇 Nghe thử";
  };

  function render(s) {
    lastSettings = s.settings;
    renderSettings(s.settings);
    $("#liveStatus").textContent = (STATUS[s.status] ?? s.status) + (s.status !== "idle" && s.username ? ` (@${s.username})` : "");
    $("#liveStatus").dataset.state = s.status;
    $("#liveError").textContent = s.error ? "Lỗi kết nối: " + s.error : "";
    $("#liveConnect").disabled = s.status !== "idle";
    $("#liveDisconnect").disabled = s.status === "idle";
    $("#liveReset").disabled = s.status !== "idle";
    $$("#liveSim button[data-sim]").forEach((b) => (b.disabled = s.status !== "idle"));
    $("#liveExitTest").classList.toggle("hidden", !s.test);
    $("#liveWorldInfo").textContent = `${s.test ? "Bản thử" : "Thế giới thật"}: cấp ${s.level}, ${s.diamonds} 💎, ${s.buildings.length} công trình, ${s.villagers.length} cư dân`;
  }

  new EventSource("/api/live/stream").onmessage = (m) => render(JSON.parse(m.data).state);

  guard(loadMedia)();
  $$('.tabs button[data-tab="live"]').forEach((b) => b.addEventListener("click", guard(loadMedia)));

  $("#liveStageUrl").textContent = location.origin + "/stage.html";
  $("#liveConnect").onclick = guard(async () => {
    const username = $("#liveUser").value.trim();
    if (!username) throw new Error("Nhập tên kênh TikTok trước đã");
    store.set("liveUser", username);
    await post("connect", { username });
  });
  $("#liveUser").value = store.get("liveUser", "");
  $("#liveDisconnect").onclick = guard(() => post("disconnect"));
  $("#liveExitTest").onclick = guard(() => post("sandbox/exit"));
  $("#liveReset").onclick = guard(async () => {
    if (confirm("Xóa toàn bộ thị trấn, cấp độ và bảng nhà tài trợ?")) await post("reset");
  });

  const SIM = {
    like: { type: "like", count: 50 },
    gift: { type: "gift", name: "Hoa hồng", diamonds: 5 },
    biggift: { type: "gift", name: "Quà lớn", diamonds: 200 },
    follow: { type: "follow" },
    share: { type: "share" },
    rain: { type: "comment", text: "mưa" },
    snow: { type: "comment", text: "tuyết" },
    sun: { type: "comment", text: "nắng" },
    night: { type: "comment", text: "đêm" },
    jump: { type: "comment", text: "nhảy" },
    eat: { type: "comment", text: "ăn" },
  };
  let simN = 0;
  $("#liveSim").addEventListener("click", guard(async (e) => {
    const key = e.target.dataset?.sim;
    if (key) await post("simulate", { user: `Khách thử ${(simN++ % 6) + 1}`, ...SIM[key] });
  }));
})();
