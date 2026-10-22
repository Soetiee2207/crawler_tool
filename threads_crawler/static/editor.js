// Trình dựng video: dự án nhiều bài, trình phát xem trước (canvas) và timeline kéo thả.
//
// Mốc thời gian tính giống hệt threads_crawler/video.py (class Timeline):
// thẻ nối tiếp nhau (giọng đọc / tốc độ + giữ thêm, nghỉ `gap` giữa hai thẻ); meme, sfx và chữ gắn vào
// một thẻ (anchor = id thẻ + offset) nên đổi thứ tự thẻ thì chúng đi theo; nhạc tính theo giây.
"use strict";

const Editor = (() => {
  const W = 1080, H = 1920;
  const LEAD_IN = 0.35, TAIL = 0.9, SILENT = 2.5;
  const TEXT_TOP = 150, CARD_TOP = 300, SAFE_BOTTOM = 300;
  const CHARS_PER_SEC = 16; // ước tính khi chưa có giọng đọc thật
  // Watermark nảy khắp khung hình (khớp WATERMARK_TEXT / WATERMARK_SPEED trong video.py).
  const WATERMARK = { text: "@chillvideos666", vx: 150, vy: 110 };
  const POST_COLORS = ["#2563eb", "#db2777", "#16a34a", "#ca8a04", "#9333ea", "#0891b2", "#dc2626", "#65a30d"];
  const POSITIONS = ["top-left", "top-center", "top-right", "middle-left", "middle-center", "middle-right", "bottom-left", "bottom-center", "bottom-right"];

  let P = null; // dự án đang làm (lưu localStorage)
  let tl = null; // mốc thời gian đã tính
  let sel = null; // {type, id} mục đang chọn
  let history = [];
  const postCache = {};
  let pps = store.get("tlZoom", 32); // pixel / giây trên timeline
  let t = 0, playing = false, t0 = 0, wall0 = 0;
  let renderJobId = null;

  const defaults = () => Object.assign(
    { voice: "Hải Đăng", precision: "fp32", speed: 1.1, gap: 0.35, theme: "dark", musicCat: "funny", musicVolume: 0.15 },
    store.get("editorDefaults", {}),
  );
  const saveDefaults = () => store.set("editorDefaults", {
    ...defaults(), voice: P.voice, precision: P.precision, speed: P.speed, gap: P.gap, theme: P.theme,
    musicVolume: P.music[0]?.volume ?? defaults().musicVolume,
    musicCat: P.music.length ? musicInfo(P.music[0].path)?.category ?? defaults().musicCat : "",
  });

  const uid = () => Math.random().toString(16).slice(2, 10);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const round1 = (v) => Math.round(v * 10) / 10;
  const round2 = (v) => Math.round(v * 100) / 100;
  const fmtT = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
  const memeInfo = (p) => state.materials.memes.find((m) => m.path === p);
  const bgInfo = (p) => state.materials.backgrounds.find((m) => m.path === p);
  const musicInfo = (p) => state.materials.music.find((m) => m.path === p);
  const sfxInfo = (p) => (state.materials.sfx || []).find((m) => m.path === p);
  // tên file sfx: <tên hiển thị>__<slug myinstants>
  const sfxName = (p) => p.split("/").pop().replace(/\.\w+$/, "").split("__")[0].replace(/[-_]+/g, " ");
  const anchored = () => [...P.memes, ...P.sfx, ...P.texts]; // các mục gắn vào thẻ
  const fileUrl = (kind, p) => `/api/materials/file/${kind}/${encodeURIComponent(p)}`;
  const thumbUrl = (kind, p) => `/api/materials/thumb/${kind}/${encodeURIComponent(p)}`;
  const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

  // ───────────────────────── Mốc thời gian ─────────────────────────

  const segVoice = (s) => s.voice || P.voice; // thẻ có thể chọn giọng riêng
  const voiceLabel = (id) => (state.voices.find((v) => v.id === id)?.label || id).replace(/^⭐ /, "").split(" — ")[0];
  const audioKey = (s) => `${segVoice(s)}|${P.precision}|${s.read_text.trim()}`;
  const hasAudio = (s) => s.audio && s.audio.key === audioKey(s);

  function voiceDur(s) {
    if (!s.read_text.trim()) return null;
    return hasAudio(s) ? s.audio.duration : Math.max(1, s.read_text.trim().length / CHARS_PER_SEC);
  }

  function computeTimeline() {
    const seg = {};
    let tt = LEAD_IN;
    for (const s of P.segments) {
      const vd = voiceDur(s);
      const voice = vd ? vd / P.speed : SILENT;
      const len = voice + (s.hold || 0);
      seg[s.id] = { start: tt, end: tt + len, voice: vd ? voice : 0, est: vd != null && !hasAudio(s) };
      tt += len + P.gap;
    }
    const segEnd = P.segments.length ? tt - P.gap : LEAD_IN;
    const at = (a) => Math.max(0, (a?.seg && seg[a.seg] ? seg[a.seg].start : 0) + (a?.offset || 0));
    const ends = [segEnd + TAIL];
    const memes = {};
    for (const m of P.memes) {
      const full = memeInfo(m.path)?.duration || 3;
      const st = at(m.anchor);
      memes[m.id] = { start: st, end: st + Math.min(m.duration || full, full), full };
      ends.push(memes[m.id].end);
    }
    const sfx = {};
    for (const x of P.sfx) {
      const full = sfxInfo(x.path)?.duration || 1;
      const st = at(x.anchor);
      sfx[x.id] = { start: st, end: st + Math.min(x.duration || full, full), full };
      ends.push(sfx[x.id].end);
    }
    for (const x of P.texts) if (x.duration) ends.push(at(x.anchor) + x.duration);
    const total = Math.max(...ends);
    const texts = {};
    for (const x of P.texts) {
      const st = at(x.anchor);
      texts[x.id] = { start: st, end: x.duration ? Math.min(total, st + x.duration) : total };
    }
    const music = {};
    for (const m of P.music) music[m.id] = { start: Math.min(m.start, total), end: Math.min(m.end ?? total, total) };
    const shows = {}; // thẻ hiện từ lúc bắt đầu đến khi thẻ sau bắt đầu
    P.segments.forEach((s, i) => {
      const next = P.segments[i + 1];
      shows[s.id] = { start: seg[s.id].start, end: next ? seg[next.id].start : total };
    });
    return { seg, segEnd, total, memes, sfx, texts, music, shows, at };
  }

  function anchorAt(time, timeline = tl) {
    let best = null;
    for (const s of P.segments) if (timeline.seg[s.id].start <= time + 1e-6) best = s;
    return best
      ? { seg: best.id, offset: round1(time - timeline.seg[best.id].start) }
      : { seg: null, offset: round1(Math.max(0, time)) };
  }

  // ───────────────────────── Dự án ─────────────────────────

  function newProject() {
    const d = defaults();
    return {
      id: Date.now().toString(36) + uid(), name: "", post_codes: [], posts: {}, segments: [], texts: [], memes: [], sfx: [], music: [], background: null,
      voice: d.voice, precision: d.precision, speed: d.speed, gap: d.gap, theme: d.theme,
    };
  }

  async function fetchPost(code) {
    if (!postCache[code]) postCache[code] = await api(`/api/posts/${code}`);
    return postCache[code];
  }

  async function buildSegments(items) {
    if (!items.length) return [];
    return api("/api/script", {
      method: "POST",
      body: { items, max_lines: +$("#maxLines").value, with_image: $("#withImage").checked },
    });
  }

  const authorReplies = (p) => p.replies.filter((r) => r.username === p.username).map((r) => r.code);

  function pickBackground(keepFile = false) {
    const list = state.materials.backgrounds;
    if (!list.length) return;
    const file = keepFile && P.background ? bgInfo(P.background.path) || pick(list) : pick(list);
    const need = (tl?.total || 60) + 1;
    P.background = { path: file.path, start: round1(Math.random() * Math.max(0, file.duration - need)) };
  }

  function defaultMusic() {
    const d = defaults();
    if (!d.musicCat) return;
    const list = state.materials.music.filter((m) => m.category === d.musicCat);
    const file = list.length ? pick(list) : state.materials.music[0];
    if (file) P.music = [{ id: uid(), path: file.path, start: 0, end: null, volume: d.musicVolume }];
  }

  async function createFromPosts(codes) {
    if (!codes.length) return;
    const hadProject = !!P;
    await saveNow(); // dự án đang mở vẫn nằm trong danh sách dự án dở
    try {
      const posts = await Promise.all(codes.map(fetchPost));
      const proj = newProject();
      for (const p of posts) {
        proj.post_codes.push(p.code);
        proj.posts[p.code] = { replies: authorReplies(p) };
      }
      proj.segments = await buildSegments(proj.post_codes.map((c) => ({ code: c, reply_codes: proj.posts[c].replies })));
      P = proj;
      store.set("projectId", P.id);
      history = [];
      sel = null;
      tl = computeTimeline();
      pickBackground();
      defaultMusic();
      t = 0;
      changed({ posts: true });
      showTab("create");
      fitZoom();
      if (hadProject) toast("Dự án trước đã được lưu trong 📁 Dự án");
    } catch (e) { toast(e.message, true); }
  }

  async function addPosts(codes) {
    if (!P) return createFromPosts(codes);
    const fresh = codes.filter((c) => !P.post_codes.includes(c));
    if (!fresh.length) return showTab("create");
    try {
      const posts = await Promise.all(fresh.map(fetchPost));
      const segs = await buildSegments(posts.map((p) => ({ code: p.code, reply_codes: authorReplies(p) })));
      mutate(() => {
        for (const p of posts) {
          P.post_codes.push(p.code);
          P.posts[p.code] = { replies: authorReplies(p) };
        }
        P.segments.push(...segs);
      }, { posts: true });
      showTab("create");
      toast(`Đã thêm ${posts.length} bài vào cuối timeline`);
    } catch (e) { toast(e.message, true); }
  }

  /** Remove segments matching pred; meme/chữ đang gắn vào chúng được gắn lại theo thời điểm cũ. */
  function removeSegments(pred) {
    const before = computeTimeline();
    const abs = new Map(anchored().map((it) => [it.id, before.at(it.anchor)]));
    const removed = new Set(P.segments.filter(pred).map((s) => s.id));
    P.segments = P.segments.filter((s) => !removed.has(s.id));
    const after = computeTimeline();
    for (const it of anchored()) {
      if (removed.has(it.anchor.seg)) it.anchor = anchorAt(abs.get(it.id), after);
    }
  }

  // ───────────────────────── Lưu & mở dự án ─────────────────────────
  // Dự án lưu ở server (data/threads.db), tự động sau mỗi thay đổi; trình duyệt chỉ nhớ id dự án đang mở.

  let dirty = false;
  function saveSoon() {
    dirty = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 600);
    $("#saveStatus").textContent = "● Chưa lưu";
  }

  async function saveNow() {
    clearTimeout(saveTimer);
    if (!P || !dirty) return;
    dirty = false;
    const proj = P;
    try {
      const r = await api(`/api/projects/${proj.id}`, { method: "POST", body: proj });
      if (!r.saved && P === proj) return closeProject("Dự án này đã xuất video nên không còn trong danh sách dở");
      if (P === proj) $("#saveStatus").textContent = `✓ Đã lưu ${new Date().toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" })}`;
    } catch (e) {
      dirty = true;
      $("#saveStatus").textContent = "⚠ Lưu lỗi — sẽ thử lại";
      saveTimer = setTimeout(saveNow, 5000);
    }
  }

  /** Đóng trang / chuyển tab trình duyệt: gửi nốt bản chưa lưu (fetch thường có thể bị hủy). */
  function flushOnLeave() {
    if (P && dirty) {
      navigator.sendBeacon(`/api/projects/${P.id}`, JSON.stringify(P));
      dirty = false;
    }
  }

  async function openProject(id) {
    await saveNow();
    try {
      const proj = await api(`/api/projects/${id}`);
      proj.id = id;
      proj.sfx ||= []; // dự án lưu trước khi có hàng SFX
      pause();
      P = proj;
      store.set("projectId", id);
      history = [];
      sel = null;
      t = 0;
      dirty = false;
      $("#saveStatus").textContent = "";
      onShow();
      fitZoom();
    } catch (e) {
      toast(e.message, true);
      loadProjects();
    }
  }

  async function closeProject(msg) {
    await saveNow();
    pause();
    P = null;
    sel = null;
    store.set("projectId", null);
    showEditor();
    if (msg) toast(msg);
  }

  const fmtWhen = (iso) => {
    const d = new Date(iso);
    const mins = Math.round((Date.now() - d) / 60000);
    const rel = mins < 1 ? "vừa xong" : mins < 60 ? `${mins} phút trước` : mins < 1440 ? `${Math.round(mins / 60)} giờ trước` : `${Math.round(mins / 1440)} ngày trước`;
    return `${d.toLocaleString("vi-VN", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} · ${rel}`;
  };

  async function loadProjects() {
    let list = [];
    try { list = await api("/api/projects"); } catch (e) { return toast(e.message, true); }
    $("#projectList").innerHTML = list.length ? list.map((p) => `
      <div class="project-card" data-project="${esc(p.id)}">
        <b>${esc(p.name || (p.usernames.length ? "@" + p.usernames.join(", @") : "Dự án chưa đặt tên"))}</b>
        <p class="snippet">${esc(p.preview || "")}</p>
        <span class="muted small">${p.posts} bài · ${p.segments} thẻ · ${p.memes} meme${p.sfx ? ` · ${p.sfx} sfx` : ""}${p.publish_at ? ` · 🕖 đăng ${esc(p.publish_at.replace("T", " "))}` : ""}</span>
        <span class="when">💾 Lưu lần cuối: ${esc(fmtWhen(p.updated_at))}</span>
        <span class="muted small">Tạo lúc ${esc(new Date(p.created_at).toLocaleString("vi-VN"))}</span>
        <div class="actions">
          <button class="primary sm" data-pact="open">✏️ Chỉnh tiếp</button>
          <button class="ghost danger sm" data-pact="delete">🗑 Xóa</button>
        </div>
      </div>`).join("")
      : '<p class="muted">Chưa có dự án nào đang dở. Vào Kho bài, tick chọn bài rồi bấm "Tạo video".</p>';
  }

  async function onProjectListClick(e) {
    const card = e.target.closest("[data-project]");
    if (!card) return;
    const id = card.dataset.project;
    if (e.target.closest("[data-pact=delete]")) {
      if (!confirm("Xóa dự án này? Không khôi phục được.")) return;
      try {
        await api(`/api/projects/${id}`, { method: "DELETE" });
        toast("Đã xóa dự án");
      } catch (err) { toast(err.message, true); }
      return loadProjects();
    }
    openProject(id);
  }

  // ───────────────────────── Thay đổi & hoàn tác ─────────────────────────

  let saveTimer, ttsTimer;
  function snapshot() {
    history.push(JSON.stringify(P));
    if (history.length > 80) history.shift();
  }

  function mutate(fn, opts = {}) {
    snapshot();
    fn();
    changed(opts);
  }

  function undo() {
    if (!history.length) return toast("Không còn gì để hoàn tác");
    P = JSON.parse(history.pop());
    if (sel && !findItem(sel.type, sel.id)) sel = null;
    changed({ posts: true });
  }

  function changed(opts = {}) {
    if (!P) return;
    tl = computeTimeline();
    t = Math.min(t, tl.total);
    saveSoon();
    renderTimeline();
    if (!opts.keepInspector) renderInspector();
    if (opts.posts) renderPosts();
    if (!opts.noTts) {
      clearTimeout(ttsTimer);
      ttsTimer = setTimeout(runTts, 900);
    }
    updateTtsStatus();
    syncMedia();
    draw();
  }

  // ───────────────────────── Giọng đọc (tạo dần ở nền) ─────────────────────────

  let ttsBusy = false, ttsCurrent = null, modelWarm = false;
  async function runTts() {
    if (ttsBusy || !P) return;
    ttsBusy = true;
    try {
      for (;;) {
        const s = P?.segments.find((x) => x.read_text.trim() && !hasAudio(x));
        if (!s) break;
        const key = audioKey(s);
        ttsCurrent = s.id;
        updateTtsStatus();
        renderTimeline();
        const r = await api("/api/tts", { method: "POST", body: { text: s.read_text.trim(), voice: segVoice(s), precision: P.precision } });
        modelWarm = true;
        const seg = P?.segments.find((x) => x.id === s.id);
        if (seg && audioKey(seg) === key) seg.audio = { key, url: r.url, duration: r.duration };
        changed({ keepInspector: true, noTts: true });
      }
    } catch (e) {
      toast("Lỗi tạo giọng đọc: " + e.message, true);
    } finally {
      ttsBusy = false;
      ttsCurrent = null;
      updateTtsStatus();
      renderTimeline();
    }
  }

  function updateTtsStatus() {
    if (!P) return;
    const withText = P.segments.filter((s) => s.read_text.trim());
    const ready = withText.filter(hasAudio).length;
    let msg = ready === withText.length
      ? `🎙 Giọng đọc sẵn sàng · ⏱ ${fmtDur(tl.total)}`
      : `🎙 Đang tạo giọng đọc ${ready}/${withText.length}${modelWarm ? "" : " (lần đầu nạp model ~1 phút)"} · ⏱ ~${fmtDur(tl.total)}`;
    if (renderJobId) msg = renderMsg || msg;
    $("#ttsStatus").textContent = msg;
  }

  // ───────────────────────── Trình phát xem trước ─────────────────────────

  const canvas = () => $("#stage");
  const bgVideo = document.createElement("video");
  bgVideo.muted = true;
  bgVideo.playsInline = true;
  bgVideo.preload = "auto";
  bgVideo.addEventListener("seeked", () => draw());
  bgVideo.addEventListener("loadeddata", () => draw());
  const media = new Map(); // id -> HTMLMediaElement (giọng đọc / meme / nhạc)
  const cardImgs = new Map(); // key -> {img, ready}
  const textImgs = new Map();
  const off = document.createElement("canvas");
  const octx = off.getContext("2d", { willReadFrequently: true });

  function mediaEl(id, kind, src) {
    let el = media.get(id);
    if (el && el.dataset.src !== src) {
      el.pause();
      el = null;
    }
    if (!el) {
      el = document.createElement(kind);
      el.preload = "auto";
      el.src = src;
      el.dataset.src = src;
      if (kind === "video") {
        el.playsInline = true;
        el.addEventListener("seeked", () => draw());
        el.addEventListener("loadeddata", () => draw());
      }
      media.set(id, el);
    }
    return el;
  }

  function cardKey(s) {
    return JSON.stringify([P.theme, s.kind, s.username, s.display_text, s.published_at, s.likes, s.replies, s.reposts, s.image_path, s.page_label, s.verified, s.avatar_path]);
  }

  function cardImg(s) {
    const key = cardKey(s);
    let c = cardImgs.get(key);
    if (!c) {
      c = { img: new Image(), ready: false };
      cardImgs.set(key, c);
      api("/api/card", { method: "POST", body: { segment: s, theme: P.theme } }).then((blob) => {
        c.img.onload = () => { c.ready = true; draw(); };
        c.img.src = URL.createObjectURL(blob);
      }).catch(() => cardImgs.delete(key));
    }
    return c.ready ? c.img : null;
  }

  /** Khung thẻ trên khung hình (khớp card_xy trong video.py): ảnh gốc × scale, tâm ở giữa dải thẻ + (dx, dy). */
  function cardRect(s, img) {
    const k = s.scale ?? 1;
    const w = img.naturalWidth * k, h = img.naturalHeight * k;
    const band = H - SAFE_BOTTOM - CARD_TOP;
    return { x: (W - w) / 2 + (s.dx || 0), y: CARD_TOP + (band - h) / 2 + (s.dy || 0), w, h };
  }

  function textImg(text) {
    let c = textImgs.get(text);
    if (!c) {
      c = { img: new Image(), ready: false };
      c.img.onload = () => { c.ready = true; draw(); };
      c.img.src = "/api/text-image?text=" + encodeURIComponent(text);
      textImgs.set(text, c);
    }
    return c.ready ? c.img : null;
  }

  function bgTime(time) {
    const info = P.background && bgInfo(P.background.path);
    if (!info) return 0;
    const start = Math.min(P.background.start, Math.max(0, info.duration - tl.total - 0.5));
    return (start + time) % info.duration;
  }

  function memeBox(m) {
    const info = memeInfo(m.path) || { width: 1, height: 1 };
    const w = Math.floor((W * m.size) / 2) * 2;
    const h = Math.floor((w * info.height) / info.width / 2) * 2;
    const [row, col] = m.position.split("-");
    const x = { left: 40, center: (W - w) / 2, right: W - w - 40 }[col];
    const y = { top: 200, middle: (H - h) / 2, bottom: H - h - SAFE_BOTTOM + 40 }[row];
    return { x, y, w, h };
  }

  function keyOut(data, key, despillGreen) {
    const kr = parseInt(key.slice(2, 4), 16), kg = parseInt(key.slice(4, 6), 16), kb = parseInt(key.slice(6, 8), 16);
    const T = 115, SOFT = 45;
    for (let i = 0; i < data.length; i += 4) {
      const r = data[i], g = data[i + 1], b = data[i + 2];
      const d = Math.sqrt((r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2);
      if (d < T) data[i + 3] = 0;
      else if (d < T + SOFT) data[i + 3] = (data[i + 3] * (d - T)) / SOFT;
      if (despillGreen) data[i + 1] = Math.min(g, Math.max(r, b));
      else data[i + 2] = Math.min(b, Math.max(r, g));
    }
  }

  function draw() {
    const cv = canvas();
    if (!cv || !P || !tl) return;
    const ctx = cv.getContext("2d");
    const k = cv.width / W;
    ctx.setTransform(k, 0, 0, k, 0, 0);
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, W, H);

    if (bgVideo.readyState >= 2 && bgVideo.videoWidth) {
      const vw = bgVideo.videoWidth, vh = bgVideo.videoHeight;
      const sc = Math.max(W / vw, H / vh);
      const sw = W / sc, sh = H / sc;
      ctx.drawImage(bgVideo, (vw - sw) / 2, (vh - sh) / 2, sw, sh, 0, 0, W, H);
    }

    const cur = P.segments.find((s) => t >= tl.shows[s.id].start && t < tl.shows[s.id].end);
    stageCard = null;
    const img = cur && cardImg(cur);
    if (img) {
      const r = cardRect(cur, img);
      ctx.drawImage(img, r.x, r.y, r.w, r.h);
      stageCard = { id: cur.id, ...r };
    }

    for (const m of P.memes) {
      const span = tl.memes[m.id];
      if (t < span.start || t >= span.end) continue;
      const v = media.get(m.id);
      if (!v || v.readyState < 2) continue;
      const { x, y, w, h } = memeBox(m);
      const ow = Math.max(1, Math.round(w * k)), oh = Math.max(1, Math.round(h * k));
      off.width = ow;
      off.height = oh;
      octx.drawImage(v, 0, 0, ow, oh);
      const info = memeInfo(m.path);
      const key = m.chroma === false ? null : info?.key || (m.chroma ? "0x00ff00" : null);
      if (key) {
        const d = octx.getImageData(0, 0, ow, oh);
        keyOut(d.data, key, parseInt(key.slice(4, 6), 16) >= parseInt(key.slice(6, 8), 16));
        octx.putImageData(d, 0, 0);
      }
      ctx.drawImage(off, x, y, w, h);
      if (sel?.type === "meme" && sel.id === m.id) {
        ctx.strokeStyle = "#25f4ee";
        ctx.lineWidth = 4;
        ctx.setLineDash([16, 10]);
        ctx.strokeRect(x, y, w, h);
        ctx.setLineDash([]);
      }
    }

    for (const x of P.texts) {
      const span = tl.texts[x.id];
      if (!x.text.trim() || t < span.start || t >= span.end) continue;
      const img = textImg(x.text);
      if (!img) continue;
      const y = { top: TEXT_TOP, middle: (H - img.naturalHeight) / 2, bottom: H - SAFE_BOTTOM - img.naturalHeight }[x.position];
      ctx.drawImage(img, (W - img.naturalWidth) / 2, y);
    }
    drawWatermark(ctx);
    if (stageCard && sel?.type === "seg" && sel.id === stageCard.id) drawCardFrame(ctx, stageCard);
    $("#timeLabel").textContent = `${fmtT(t)} / ${fmtT(tl.total)}`;
  }

  const HANDLE = 34; // cạnh ô vuông kéo đổi cỡ ở góc thẻ (px khung 1080)

  function drawCardFrame(ctx, r) {
    ctx.strokeStyle = "#25f4ee";
    ctx.lineWidth = 5;
    ctx.setLineDash([18, 12]);
    ctx.strokeRect(r.x, r.y, r.w, r.h);
    ctx.setLineDash([]);
    ctx.fillStyle = "#25f4ee";
    for (const [x, y] of corners(r)) ctx.fillRect(x - HANDLE / 2, y - HANDLE / 2, HANDLE, HANDLE);
  }

  const corners = (r) => [[r.x, r.y], [r.x + r.w, r.y], [r.x, r.y + r.h], [r.x + r.w, r.y + r.h]];

  // ── Kéo thẻ trên khung hình: kéo thân = dời, kéo góc / lăn chuột = phóng to, thu nhỏ ──
  let stageCard = null; // thẻ đang hiện: {id, x, y, w, h}
  let stageDrag = null;
  const SNAP = 14;

  function stagePoint(e) {
    const r = canvas().getBoundingClientRect();
    return { x: ((e.clientX - r.left) / r.width) * W, y: ((e.clientY - r.top) / r.height) * H };
  }

  function stageHit(p) {
    const r = stageCard;
    if (!r) return null;
    if (sel?.type === "seg" && sel.id === r.id && corners(r).some(([x, y]) => Math.abs(p.x - x) < HANDLE && Math.abs(p.y - y) < HANDLE)) return "resize";
    return p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h ? "move" : null;
  }

  function onStageDown(e) {
    if (e.button !== 0 || !P) return;
    const p = stagePoint(e);
    const mode = stageHit(p);
    if (!mode) return;
    if (playing) pause();
    const seg = findItem("seg", stageCard.id);
    if (!(sel?.type === "seg" && sel.id === seg.id)) select("seg", seg.id);
    snapshot();
    const cx = stageCard.x + stageCard.w / 2, cy = stageCard.y + stageCard.h / 2;
    stageDrag = { seg, mode, p0: p, dx: seg.dx || 0, dy: seg.dy || 0, scale: seg.scale ?? 1, cx, cy,
      dist: Math.max(20, Math.hypot(p.x - cx, p.y - cy)) };
    canvas().setPointerCapture(e.pointerId);
  }

  function onStageMove(e) {
    const p = stagePoint(e);
    if (!stageDrag) {
      const mode = stageHit(p);
      canvas().style.cursor = mode === "resize" ? "nwse-resize" : mode ? "move" : "";
      return;
    }
    const d = stageDrag;
    if (d.mode === "move") {
      let dx = d.dx + p.x - d.p0.x, dy = d.dy + p.y - d.p0.y;
      if (Math.abs(dx) < SNAP) dx = 0; // hít về giữa
      if (Math.abs(dy) < SNAP) dy = 0;
      d.seg.dx = Math.round(dx);
      d.seg.dy = Math.round(dy);
    } else {
      d.seg.scale = round2(clamp(d.scale * Math.hypot(p.x - d.cx, p.y - d.cy) / d.dist, 0.2, 5));
    }
    d.moved = true;
    draw();
    updateCardFields(d.seg);
  }

  function onStageUp() {
    const d = stageDrag;
    stageDrag = null;
    if (!d) return;
    if (d.moved) changed({ noTts: true });
    else history.pop(); // chỉ bấm chọn, không đổi gì
  }

  let wheelTimer = null;
  function onStageWheel(e) {
    if (!P || !stageCard || stageHit(stagePoint(e)) === null) return;
    e.preventDefault();
    const seg = findItem("seg", stageCard.id);
    if (!wheelTimer) {
      snapshot();
      if (!(sel?.type === "seg" && sel.id === seg.id)) select("seg", seg.id);
    }
    seg.scale = round2(clamp((seg.scale ?? 1) * (e.deltaY < 0 ? 1.06 : 1 / 1.06), 0.2, 5));
    draw();
    updateCardFields(seg);
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => { wheelTimer = null; changed({ noTts: true }); }, 350);
  }

  /** Cập nhật ô cỡ/vị trí trong bảng thuộc tính khi kéo trên khung hình (không vẽ lại cả bảng). */
  function updateCardFields(seg) {
    const insp = $("#inspector");
    const sc = $("[data-f=scale]", insp);
    if (!sc) return;
    sc.value = seg.scale;
    sc.closest("label").querySelector("b").textContent = `${Math.round(seg.scale * 100)}%`;
    $("[data-f=dx]", insp).value = seg.dx || 0;
    $("[data-f=dy]", insp).value = seg.dy || 0;
  }

  function drawWatermark(ctx) {
    ctx.font = 'bold 46px "Noto Sans", sans-serif';
    ctx.textBaseline = "top";
    const w = ctx.measureText(WATERMARK.text).width + 4, h = 62;
    const bounce = (v, span) => Math.abs(((t * v) % (2 * span)) - span);
    const x = bounce(WATERMARK.vx, W - w), y = bounce(WATERMARK.vy, H - h);
    ctx.lineWidth = 4;
    ctx.strokeStyle = "rgba(0,0,0,0.18)";
    ctx.fillStyle = "rgba(255,255,255,0.35)";
    ctx.strokeText(WATERMARK.text, x + 2, y + 2);
    ctx.fillText(WATERMARK.text, x + 2, y + 2);
  }

  function syncEl(el, active, pos, rate = 1) {
    if (!active) {
      if (!el.paused) el.pause();
      return;
    }
    if (playing) {
      el.playbackRate = rate;
      if (el.paused) {
        el.currentTime = pos;
        el.play().catch(() => {});
      } else if (Math.abs(el.currentTime - pos) > 0.3) el.currentTime = pos;
    } else {
      if (!el.paused) el.pause();
      if (Math.abs(el.currentTime - pos) > 0.04) el.currentTime = pos;
    }
  }

  function syncMedia() {
    if (!P || !tl) return;
    if (P.background) {
      const src = fileUrl("backgrounds", P.background.path);
      if (bgVideo.dataset.src !== src) {
        bgVideo.src = src;
        bgVideo.dataset.src = src;
      }
      syncEl(bgVideo, true, bgTime(t));
    }
    const live = new Set();
    for (const s of P.segments) {
      if (!hasAudio(s)) continue;
      live.add(s.id);
      const span = tl.seg[s.id];
      const el = mediaEl(s.id, "audio", s.audio.url);
      syncEl(el, playing && t >= span.start && t < span.start + span.voice, (t - span.start) * P.speed, P.speed);
    }
    for (const m of P.memes) {
      live.add(m.id);
      const span = tl.memes[m.id];
      const el = mediaEl(m.id, "video", fileUrl("memes", m.path));
      el.volume = Math.min(1, m.volume);
      el.muted = !playing;
      syncEl(el, t >= span.start && t < span.end, t - span.start);
    }
    for (const x of P.sfx) {
      live.add(x.id);
      const span = tl.sfx[x.id];
      const el = mediaEl(x.id, "audio", fileUrl("sfx", x.path));
      el.volume = clamp(x.volume, 0, 1);
      syncEl(el, playing && t >= span.start && t < span.end, t - span.start);
    }
    for (const mu of P.music) {
      live.add(mu.id);
      const span = tl.music[mu.id];
      const el = mediaEl(mu.id, "audio", fileUrl("music", mu.path));
      el.volume = clamp(mu.volume * 2, 0, 1); // nhạc nền trong video được trộn nhỏ; nghe thử to hơn chút
      const dur = musicInfo(mu.path)?.duration || 1e9;
      syncEl(el, playing && t >= span.start && t < span.end, (t - span.start) % dur);
    }
    for (const [id, el] of media) {
      if (!live.has(id)) {
        el.pause();
        media.delete(id);
      }
    }
  }

  function tick(now) {
    if (!playing) return;
    t = t0 + (now - wall0) / 1000;
    if (t >= tl.total) {
      t = tl.total;
      pause();
    }
    syncMedia();
    draw();
    updatePlayhead(true);
    if (playing) requestAnimationFrame(tick);
  }

  function play() {
    if (!P || !tl) return;
    if (t >= tl.total - 0.05) t = 0;
    playing = true;
    t0 = t;
    wall0 = performance.now();
    $("#playBtn").textContent = "⏸";
    syncMedia();
    requestAnimationFrame(tick);
  }

  function pause() {
    if (!playing) return;
    playing = false;
    $("#playBtn").textContent = "▶";
    bgVideo.pause();
    for (const el of media.values()) el.pause();
    syncMedia();
  }

  function seek(time) {
    if (!tl) return;
    t = clamp(time, 0, tl.total);
    if (playing) {
      t0 = t;
      wall0 = performance.now();
      for (const el of media.values()) el.pause();
    }
    syncMedia();
    draw();
    updatePlayhead();
  }

  // ───────────────────────── Timeline ─────────────────────────

  const content = () => $("#tlContent");
  const scroller = () => $("#tlScroll");

  function tickStep() {
    for (const s of [1, 2, 5, 10, 15, 30, 60]) if (s * pps >= 56) return s;
    return 120;
  }

  let renderPending = false;
  function renderTimeline() {
    if (!P || !tl || !$("#tab-create").classList.contains("active")) return;
    if (drag && drag.type !== "playhead") {
      renderPending = true; // đang kéo: vẽ lại sẽ làm mất mục đang kéo, để sau khi thả
      return;
    }
    renderPending = false;
    const width = Math.max(scroller().clientWidth, (tl.total + 6) * pps);
    content().style.width = width + "px";

    const step = tickStep();
    const minor = step >= 5 ? step / 5 : step / 2;
    let ruler = "";
    for (let s = 0; s * pps < width; s += minor) {
      const major = Math.abs(s / step - Math.round(s / step)) < 1e-6;
      ruler += major
        ? `<div class="tl-tick" style="left:${s * pps}px">${fmtDur(s)}</div>`
        : `<div class="tl-tick minor" style="left:${s * pps}px"></div>`;
    }
    ruler += `<div class="tl-end" style="left:${tl.total * pps}px;right:0"></div>`;
    $("#tlRuler").innerHTML = ruler;

    const box = (start, end) => `left:${start * pps}px;width:${Math.max(6, (end - start) * pps)}px`;
    const isSel = (type, id) => (sel?.type === type && sel.id === id ? "sel" : "");
    const endMark = `<div class="tl-end" style="left:${tl.total * pps}px;right:0"></div>`;

    row("text").innerHTML = P.texts.map((x) => {
      const s = tl.texts[x.id];
      return `<div class="tl-item text ${isSel("text", x.id)}" data-type="text" data-id="${x.id}" style="${box(s.start, s.end)}" title="${esc(x.text)}">
        <span class="lbl">✏️ ${esc(x.text || "(trống)")}</span><span class="sub">${x.duration ? x.duration.toFixed(1) + "s" : "đến hết"}</span>
        <div class="handle" title="Kéo để đổi độ dài"></div></div>`;
    }).join("") + endMark;

    row("seg").innerHTML = P.segments.map((s, i) => {
      const span = tl.seg[s.id];
      const color = POST_COLORS[Math.max(0, P.post_codes.indexOf(s.post_code)) % POST_COLORS.length];
      const icon = !s.read_text.trim() ? "🔇" : ttsCurrent === s.id ? "⏳" : hasAudio(s) ? "🔊" : "≈";
      const holdW = (s.hold || 0) * pps;
      return `<div class="tl-item seg ${isSel("seg", s.id)} ${span.est ? "est" : ""}" data-type="seg" data-id="${s.id}"
          style="${box(span.start, span.end)};background:${color}" title="${esc(s.username)}: ${esc(s.display_text.slice(0, 200))}">
        ${holdW ? `<div class="hold" style="width:${holdW}px" title="Giữ thêm ${s.hold}s"></div>` : ""}
        <span class="lbl">${i + 1}. ${icon} @${esc(s.username)}${s.page_label ? ` (${s.page_label})` : ""}${(s.scale ?? 1) !== 1 ? ` 🔍${Math.round(s.scale * 100)}%` : ""}${s.voice ? ` 🗣 ${esc(voiceLabel(s.voice))}` : ""}</span>
        <span class="sub">${esc(s.display_text.replace(/\s+/g, " ").slice(0, 80))}</span>
        <div class="handle" title="Kéo để giữ thẻ lâu hơn"></div></div>`;
    }).join("") + endMark + '<div class="tl-insert hidden" id="tlInsert"></div>';

    row("meme").innerHTML = P.memes.map((m) => {
      const s = tl.memes[m.id];
      return `<div class="tl-item meme ${isSel("meme", m.id)}" data-type="meme" data-id="${m.id}" style="${box(s.start, s.end)}" title="${esc(m.path)}">
        <img src="${thumbUrl("memes", m.path)}" alt=""><span class="lbl">${esc(m.path.split("/").pop().replace(/\.\w+$/, ""))}</span>
        <div class="handle" title="Kéo để cắt ngắn"></div></div>`;
    }).join("") + endMark;

    row("sfx").innerHTML = P.sfx.map((x) => {
      const s = tl.sfx[x.id];
      return `<div class="tl-item sfx ${isSel("sfx", x.id)}" data-type="sfx" data-id="${x.id}" style="${box(s.start, s.end)}" title="${esc(sfxName(x.path))}">
        <span class="lbl">🔊 ${esc(sfxName(x.path))}</span><span class="sub">${Math.round(x.volume * 100)}%</span>
        <div class="handle" title="Kéo để cắt ngắn"></div></div>`;
    }).join("") + endMark;

    row("music").innerHTML = P.music.map((mu) => {
      const s = tl.music[mu.id];
      return `<div class="tl-item music ${isSel("music", mu.id)}" data-type="music" data-id="${mu.id}" style="${box(s.start, s.end)}">
        <span class="lbl">🎵 ${esc(mu.path.split("/").pop().replace(/\.\w+$/, ""))}</span><span class="sub">${Math.round(mu.volume * 100)}% · ${mu.end == null ? "đến hết" : (s.end - s.start).toFixed(1) + "s"}</span>
        <div class="handle" title="Kéo để đổi độ dài"></div></div>`;
    }).join("") + endMark;

    row("bg").innerHTML = P.background
      ? `<div class="tl-item bg ${isSel("bg", "bg")}" data-type="bg" data-id="bg" style="${box(0, tl.total)}">
          <span class="lbl">🎞 ${esc(P.background.path.replace(/\.\w+$/, "").slice(0, 60))}</span>
          <span class="sub">lấy từ ${fmtDur(bgTime(0))} — bấm để đổi</span></div>` + endMark
      : endMark;
    updatePlayhead();
  }

  const row = (name) => $(`.tl-row[data-row="${name}"]`);

  function updatePlayhead(follow = false) {
    const x = t * pps;
    $("#tlPlayhead").style.left = x + "px";
    if (follow) {
      const sc = scroller();
      if (x > sc.scrollLeft + sc.clientWidth - 60 || x < sc.scrollLeft) sc.scrollLeft = x - 120;
    }
  }

  function fitZoom() {
    if (!tl) return;
    pps = clamp((scroller().clientWidth - 40) / (tl.total + 1), 8, 140);
    $("#zoom").value = pps;
    store.set("tlZoom", pps);
    renderTimeline();
  }

  const timeAtClientX = (x) => (x - content().getBoundingClientRect().left) / pps;

  let drag = null;
  function onPointerDown(e) {
    if (e.button !== 0 || !P) return;
    const item = e.target.closest(".tl-item");
    if (!item) {
      seek(timeAtClientX(e.clientX));
      drag = { type: "playhead" };
      content().setPointerCapture(e.pointerId);
      return;
    }
    const { type, id } = item.dataset;
    select(type, id);
    if (type === "bg") return;
    drag = {
      type, id, el: item, mode: e.target.closest(".handle") ? "resize" : "move",
      x0: e.clientX, left: item.offsetLeft, width: item.offsetWidth, moved: false,
    };
    item.setPointerCapture(e.pointerId);
  }

  function onPointerMove(e) {
    if (!drag) return;
    if (drag.type === "playhead") return seek(timeAtClientX(e.clientX));
    const dx = e.clientX - drag.x0;
    if (!drag.moved && Math.abs(dx) < 4) return;
    drag.moved = true;
    drag.el.classList.add("dragging");
    if (drag.mode === "move") {
      const left = Math.max(0, drag.left + dx);
      drag.el.style.left = left + "px";
      if (drag.type === "seg") showInsert(timeAtClientX(e.clientX)); // chèn tại vị trí con trỏ
    } else {
      drag.el.style.width = Math.max(8, drag.width + dx) + "px";
    }
  }

  function onPointerUp(e) {
    const d = drag;
    drag = null;
    $("#tlInsert")?.classList.add("hidden");
    if (!d || d.type === "playhead" || !d.moved) {
      if (renderPending) renderTimeline();
      return;
    }
    applyDrag(d, (e.clientX - d.x0) / pps, timeAtClientX(e.clientX));
  }

  function insertIndex(center, id) {
    const others = P.segments.filter((s) => s.id !== id);
    return { others, idx: others.filter((s) => (tl.seg[s.id].start + tl.seg[s.id].end) / 2 < center).length };
  }

  function showInsert(center) {
    const { others, idx } = insertIndex(center, drag.id);
    const x = idx === 0
      ? (others.length ? tl.seg[others[0].id].start : LEAD_IN)
      : tl.seg[others[idx - 1].id].end + P.gap / 2;
    const el = $("#tlInsert");
    el.style.left = x * pps - 1 + "px";
    el.classList.remove("hidden");
  }

  function applyDrag(d, dt, pointerTime) {
    const it = findItem(d.type, d.id);
    if (!it) return renderTimeline();
    mutate(() => {
      if (d.type === "seg") {
        if (d.mode === "move") {
          const { others, idx } = insertIndex(pointerTime, d.id);
          others.splice(idx, 0, it);
          P.segments = others;
        } else {
          it.hold = round1(Math.max(0, (it.hold || 0) + dt));
        }
      } else if (d.type === "meme") {
        const s = tl.memes[it.id];
        if (d.mode === "move") {
          it.anchor = anchorAt(Math.max(0, s.start + dt));
          delete it.holdSeg; // đã kéo đi chỗ khác: không còn gắn với khoảng chờ của thẻ
        } else {
          it.duration = round1(clamp(s.end - s.start + dt, 0.3, s.full));
          adjustHold(it, s.end - s.start, it.duration);
        }
      } else if (d.type === "sfx") {
        const s = tl.sfx[it.id];
        if (d.mode === "move") it.anchor = anchorAt(Math.max(0, s.start + dt));
        else it.duration = round1(clamp(s.end - s.start + dt, 0.1, s.full));
      } else if (d.type === "text") {
        const s = tl.texts[it.id];
        if (d.mode === "move") it.anchor = anchorAt(Math.max(0, s.start + dt));
        else it.duration = round1(Math.max(0.5, s.end - s.start + dt));
      } else if (d.type === "music") {
        const s = tl.music[it.id];
        if (d.mode === "move") {
          it.start = round1(Math.max(0, it.start + dt));
          if (it.end != null) it.end = round1(it.start + (s.end - s.start));
        } else {
          it.end = round1(Math.max(it.start + 0.5, s.end + dt));
        }
      }
    });
  }

  function findItem(type, id) {
    if (!P) return null;
    if (type === "bg") return P.background;
    const list = { seg: P.segments, meme: P.memes, sfx: P.sfx, text: P.texts, music: P.music }[type];
    return list?.find((x) => x.id === id) || null;
  }

  function select(type, id) {
    sel = type ? { type, id } : null;
    // chọn thẻ đang không hiện → nhảy tới thẻ đó để chỉnh trực tiếp trên khung hình
    const show = type === "seg" && tl?.shows[id];
    if (show && !playing && (t < show.start || t >= show.end)) seek(show.start + 0.05);
    $$("#tlContent .tl-item").forEach((el) => el.classList.toggle("sel", el.dataset.type === type && el.dataset.id === id));
    renderInspector();
    draw();
  }

  function deleteSelected() {
    if (!sel || sel.type === "bg") return;
    const { type, id } = sel;
    mutate(() => {
      if (type === "seg") {
        const seg = findItem("seg", id);
        removeSegments((s) => s.id === id);
        // bỏ tick bình luận nếu không còn thẻ nào của nó
        const ps = seg && P.posts[seg.post_code];
        if (ps && seg.kind === "reply" && !P.segments.some((s) => s.code === seg.code)) {
          ps.replies = ps.replies.filter((c) => c !== seg.code);
        }
      }
      if (type === "meme") {
        const m = findItem("meme", id);
        if (m) adjustHold(m, memeLen(m), 0);
        P.memes = P.memes.filter((x) => x.id !== id);
      }
      if (type === "sfx") P.sfx = P.sfx.filter((x) => x.id !== id);
      if (type === "text") P.texts = P.texts.filter((m) => m.id !== id);
      if (type === "music") P.music = P.music.filter((m) => m.id !== id);
      sel = null;
    }, { posts: type === "seg" });
  }

  /** Nhân bản thẻ: bản sao nằm ngay sau thẻ gốc (vd để phóng to nhấn mạnh một câu). */
  function duplicateSeg(id) {
    const seg = findItem("seg", id);
    if (!seg) return;
    mutate(() => {
      const copy = { ...structuredClone(seg), id: uid(), hold: 0 };
      P.segments.splice(P.segments.indexOf(seg) + 1, 0, copy);
      sel = { type: "seg", id: copy.id };
    });
    toast(`Đã nhân bản thành thẻ ${P.segments.findIndex((s) => s.id === sel.id) + 1}`);
  }

  function addMeme(path, time) {
    mutate(() => {
      const m = { id: uid(), path, anchor: anchorAt(time), duration: null, position: "bottom-right", size: 0.4, volume: 1, chroma: null };
      P.memes.push(m);
      sel = { type: "meme", id: m.id };
    });
  }

  /** Thẻ đang chọn → meme chèn ngay sau phần hiện của thẻ, thẻ được giữ thêm đúng bằng độ dài meme. */
  function insertMeme(path) {
    const seg = sel?.type === "seg" && findItem("seg", sel.id);
    if (!seg) return addMeme(path, t);
    const len = Math.min(memeInfo(path)?.duration || 3, 60);
    mutate(() => {
      const span = tl.seg[seg.id];
      P.memes.push({ id: uid(), path, anchor: { seg: seg.id, offset: span.end - span.start }, duration: null,
        position: "bottom-right", size: 0.4, volume: 1, chroma: null, holdSeg: seg.id });
      seg.hold = round1((seg.hold || 0) + len);
    });
    toast(`Đã chèn meme sau thẻ ${P.segments.indexOf(seg) + 1} (+${len.toFixed(1)}s)`);
  }

  /** Chèn sfx: thẻ đang chọn → đúng lúc thẻ hiện; meme đang chọn → cùng lúc meme; không thì tại đầu phát. */
  function insertSfx(path, time = null) {
    const seg = time == null && sel?.type === "seg" && findItem("seg", sel.id);
    const meme = time == null && sel?.type === "meme" && findItem("meme", sel.id);
    const x = { id: uid(), path, anchor: null, duration: null, volume: 1 };
    x.anchor = seg ? { seg: seg.id, offset: 0 } : meme ? { ...meme.anchor } : anchorAt(time ?? t);
    mutate(() => {
      P.sfx.push(x);
      if (!seg) sel = { type: "sfx", id: x.id }; // đang chọn thẻ thì giữ nguyên để chèn tiếp
    });
    toast(seg ? `🔊 Đã chèn "${sfxName(path)}" vào đầu thẻ ${P.segments.indexOf(seg) + 1}`
      : `🔊 Đã chèn "${sfxName(path)}" tại ${fmtT(tl.sfx[x.id].start)}`);
  }

  /** Meme chèn sau thẻ đổi độ dài (hoặc bị xóa) → thời gian giữ của thẻ đổi theo. */
  function adjustHold(m, oldLen, newLen) {
    const seg = m.holdSeg && findItem("seg", m.holdSeg);
    if (seg) seg.hold = round1(Math.max(0, (seg.hold || 0) + newLen - oldLen));
  }

  function memeLen(m) {
    const full = memeInfo(m.path)?.duration || 3;
    return Math.min(m.duration || full, full);
  }

  function addText() {
    mutate(() => {
      const x = { id: uid(), text: "", anchor: anchorAt(t), duration: 3, position: "top" };
      P.texts.push(x);
      sel = { type: "text", id: x.id };
    });
    $("#inspector textarea[data-f=text]")?.focus();
  }

  function addMusic() {
    const list = state.materials.music;
    if (!list.length) return toast("Chưa có nhạc trong materials/background_musics", true);
    const cat = defaults().musicCat;
    const file = pick(list.filter((m) => m.category === cat).length ? list.filter((m) => m.category === cat) : list);
    mutate(() => {
      const mu = { id: uid(), path: file.path, start: round1(t), end: null, volume: defaults().musicVolume };
      P.music.push(mu);
      sel = { type: "music", id: mu.id };
    });
  }

  // ───────────────────────── Bảng bài viết (trái) ─────────────────────────

  let openPost = null;
  async function renderPosts() {
    if (!P) return;
    const missing = P.post_codes.filter((c) => !postCache[c]);
    if (missing.length) {
      await Promise.all(missing.map((c) => fetchPost(c).catch(() => null)));
    }
    $("#edPosts").innerHTML = P.post_codes.map((code, i) => {
      const p = postCache[code];
      if (!p) return "";
      const chosen = P.posts[code]?.replies || [];
      const open = openPost === code;
      const nSeg = P.segments.filter((s) => s.post_code === code).length;
      const sorted = [...p.replies].sort((a, b) => (b.username === p.username) - (a.username === p.username) || b.like_count - a.like_count);
      return `<div class="ed-post" data-post="${esc(code)}">
        <div class="ed-post-head" data-act="toggle">
          <span class="dot" style="background:${POST_COLORS[i % POST_COLORS.length]}"></span>
          <div class="who"><b>${i + 1}. @${esc(p.username)}</b><span class="muted small">❤️ ${fmt(p.like_count)} · ${nSeg} thẻ · ${chosen.length}/${p.replies.length} bình luận</span></div>
          <button class="ghost" data-act="up" title="Đưa lên" ${i === 0 ? "disabled" : ""}>↑</button>
          <button class="ghost" data-act="down" title="Đưa xuống" ${i === P.post_codes.length - 1 ? "disabled" : ""}>↓</button>
          <button class="ghost danger" data-act="remove" title="Bỏ bài khỏi video">✕</button>
        </div>
        ${open ? `<div class="ed-post-body">
          <p class="snippet">${esc(p.text)}</p>
          <div class="row gap" style="margin-bottom:6px">
            <button class="ghost sm" data-act="top3">+ Top 3 like</button>
            <button class="ghost sm" data-act="none">Bỏ hết</button>
          </div>
          <div class="replies">${sorted.length ? sorted.map((r) => replyHtml(r, p, chosen.indexOf(r.code) + 1)).join("") : '<p class="muted small">Chưa có bình luận đã lưu.</p>'}</div>
        </div>` : ""}
      </div>`;
    }).join("");
  }

  async function onPostsClick(e) {
    const postEl = e.target.closest("[data-post]");
    if (!postEl) return;
    const code = postEl.dataset.post;
    const act = e.target.closest("[data-act]")?.dataset.act;
    const reply = e.target.closest("[data-reply]")?.dataset.reply;
    if (reply) return toggleReply(code, reply);
    if (act === "toggle") {
      openPost = openPost === code ? null : code;
      return renderPosts();
    }
    if (act === "remove") {
      if (!confirm("Bỏ bài này (và các thẻ của nó) khỏi video?")) return;
      return mutate(() => {
        removeSegments((s) => s.post_code === code);
        P.post_codes = P.post_codes.filter((c) => c !== code);
        delete P.posts[code];
      }, { posts: true });
    }
    if (act === "up" || act === "down") {
      const i = P.post_codes.indexOf(code);
      const j = act === "up" ? i - 1 : i + 1;
      return mutate(() => {
        [P.post_codes[i], P.post_codes[j]] = [P.post_codes[j], P.post_codes[i]];
        // nhóm lại thẻ theo thứ tự bài mới, giữ thứ tự thẻ trong từng bài
        P.segments = P.post_codes.flatMap((c) => P.segments.filter((s) => s.post_code === c));
      }, { posts: true });
    }
    if (act === "top3") {
      const p = postCache[code];
      const chosen = P.posts[code].replies;
      const add = p.replies.filter((r) => r.username !== p.username && !chosen.includes(r.code))
        .sort((a, b) => b.like_count - a.like_count).slice(0, 3);
      for (const r of add) await toggleReply(code, r.code);
    }
    if (act === "none") {
      const chosen = [...P.posts[code].replies];
      mutate(() => {
        removeSegments((s) => s.post_code === code && s.kind === "reply" && chosen.includes(s.code));
        P.posts[code].replies = [];
      }, { posts: true });
    }
  }

  async function toggleReply(postCode, replyCode) {
    const chosen = P.posts[postCode].replies;
    if (chosen.includes(replyCode)) {
      return mutate(() => {
        P.posts[postCode].replies = chosen.filter((c) => c !== replyCode);
        removeSegments((s) => s.post_code === postCode && s.code === replyCode);
      }, { posts: true });
    }
    try {
      const segs = (await buildSegments([{ code: postCode, reply_codes: [replyCode] }])).filter((s) => s.code === replyCode);
      mutate(() => {
        P.posts[postCode].replies.push(replyCode);
        let idx = -1;
        P.segments.forEach((s, i) => { if (s.post_code === postCode) idx = i; });
        P.segments.splice(idx + 1, 0, ...segs);
      }, { posts: true });
    } catch (e) { toast(e.message, true); }
  }

  async function rebuildAll() {
    if (!confirm("Dựng lại tất cả thẻ từ bài viết? Chỉnh sửa chữ/lời đọc trên thẻ sẽ mất.")) return;
    try {
      const segs = await buildSegments(P.post_codes.map((c) => ({ code: c, reply_codes: P.posts[c].replies })));
      mutate(() => {
        const abs = new Map(anchored().map((it) => [it.id, tl.at(it.anchor)]));
        P.segments = segs;
        const after = computeTimeline();
        for (const it of anchored()) it.anchor = anchorAt(abs.get(it.id), after);
      }, { posts: true });
    } catch (e) { toast(e.message, true); }
  }

  // ───────────────────────── Bảng thuộc tính (phải) ─────────────────────────

  function positionGrid(current, attr = "data-pos") {
    return `<div class="pos-grid">${POSITIONS.map((p) => `<button ${attr}="${p}" class="${p === current ? "on" : ""}" title="${p}"></button>`).join("")}</div>`;
  }

  function renderInspector() {
    updateMemeHint();
    const el = $("#inspector");
    if (!P) return (el.innerHTML = "");
    const it = sel && findItem(sel.type, sel.id);
    if (!it) sel = null;
    const head = (title, del = true) => `<div class="insp-title"><h2>${title}</h2>
      ${sel ? `<div class="row gap">${del ? '<button class="ghost danger sm" data-cmd="delete">🗑 Xóa</button>' : ""}<button class="ghost sm" data-cmd="deselect">✕</button></div>` : ""}</div>`;
    let html = "";

    if (!sel) {
      html = head("Cài đặt video", false) + `
        <p class="hint">Bấm vào một mục trên timeline để chỉnh nó.</p>
        <label class="field"><span>📅 Ngày giờ đăng TikTok</span>
          <input type="datetime-local" data-f="publish_at" value="${esc(P.publish_at || "")}"></label>
        <div class="row gap wrap">${[["today19", "Tối nay 19:00"], ["tomorrow12", "Mai 12:00"], ["tomorrow19", "Mai 19:00"], ["clear", "Chưa hẹn"]]
          .map(([k, l]) => `<button class="ghost sm" data-when="${k}">${l}</button>`).join("")}</div>
        <p class="hint">Video sẽ được lưu vào <code>data/videos/${esc(P.publish_at ? P.publish_at.slice(0, 10) : "chưa_hẹn_giờ")}/</code>${P.publish_at ? `, tên file bắt đầu bằng <code>${esc(P.publish_at.slice(11, 13))}h${esc(P.publish_at.slice(14, 16))}</code>` : ""}.</p>
        <label class="field"><span>Giọng đọc</span>
          <select data-f="voice">${state.voices.map((v) => `<option value="${esc(v.id)}" ${v.id === P.voice ? "selected" : ""}>${esc(v.label)}</option>`).join("")}</select></label>
        <button class="ghost sm" data-cmd="sample">▶ Nghe thử giọng này</button>
        <div class="two">
          <label class="field"><span>Tốc độ đọc <b>${P.speed.toFixed(2)}x</b></span><input type="range" data-f="speed" min="0.8" max="1.6" step="0.05" value="${P.speed}"></label>
          <label class="field"><span>Chất lượng</span><select data-f="precision">
            <option value="fp32" ${P.precision === "fp32" ? "selected" : ""}>Chuẩn</option>
            <option value="int8" ${P.precision === "int8" ? "selected" : ""}>Nhanh (int8)</option></select></label>
        </div>
        <label class="field"><span>Nghỉ giữa các thẻ <b>${P.gap.toFixed(2)}s</b></span><input type="range" data-f="gap" min="0" max="2" step="0.05" value="${P.gap}"></label>
        <label class="field"><span>Kiểu thẻ</span></label>
        <div class="seg-toggle">${["dark", "light"].map((v) => `<button data-theme="${v}" class="${P.theme === v ? "active" : ""}">${v === "light" ? "Sáng" : "Tối (như Threads)"}</button>`).join("")}</div>
        <h3>Phím tắt</h3>
        <p class="hint">Space: phát/dừng · Delete: xóa mục đang chọn · Ctrl+D: nhân bản thẻ · Ctrl+Z: hoàn tác · ←/→: tua 1 giây</p>`;
    } else if (sel.type === "seg") {
      const i = P.segments.indexOf(it);
      const span = tl.seg[it.id];
      html = head(`Thẻ ${i + 1} · @${esc(it.username)}`) + `
        <p class="hint">${it.kind === "post" ? "Bài viết" : it.reply_to ? "Bình luận → @" + esc(it.reply_to) : "Tác giả viết tiếp"} · ${fmtT(span.start)} → ${fmtT(span.end)}
          ${hasAudio(it) ? "· 🔊 " + it.audio.duration.toFixed(1) + "s" : it.read_text.trim() ? "· ≈ đang chờ giọng đọc" : "· 🔇 không đọc"}</p>
        <label class="field"><span>Chữ trên thẻ</span><textarea rows="6" data-f="display_text">${esc(it.display_text)}</textarea></label>
        <label class="field"><span>Lời đọc <em>(để trống = thẻ im lặng ${SILENT}s)</em></span><textarea rows="4" data-f="read_text">${esc(it.read_text)}</textarea></label>
        <label class="field"><span>Giọng đọc thẻ này</span>
          <select data-f="segVoice"><option value="">Giọng chung của video (${esc(voiceLabel(P.voice))})</option>
            ${state.voices.map((v) => `<option value="${esc(v.id)}" ${v.id === it.voice ? "selected" : ""}>${esc(v.label)}</option>`).join("")}</select></label>
        <label class="field"><span>Giữ thẻ thêm (giây) sau khi đọc xong</span><input type="number" data-f="hold" min="0" step="0.5" value="${it.hold || 0}"></label>
        <label class="field"><span>🔍 Cỡ thẻ <b>${Math.round((it.scale ?? 1) * 100)}%</b></span>
          <input type="range" data-f="scale" min="0.2" max="4" step="0.05" value="${it.scale ?? 1}"></label>
        <div class="row gap wrap">${[0.7, 1, 1.5, 2, 3].map((z) => `<button class="ghost sm ${Math.abs((it.scale ?? 1) - z) < 1e-3 ? "on" : ""}" data-scale="${z}">${Math.round(z * 100)}%</button>`).join("")}</div>
        <div class="two">
          <label class="field"><span>Dời ngang (px)</span><input type="number" data-f="dx" step="10" value="${it.dx || 0}"></label>
          <label class="field"><span>Dời dọc (px)</span><input type="number" data-f="dy" step="10" value="${it.dy || 0}"></label>
        </div>
        <button class="ghost sm" data-cmd="centerCard">⌖ Về giữa, cỡ 100%</button>
        <p class="hint">Trên khung hình: kéo thẻ để dời (gần giữa sẽ tự hít vào), kéo ô vuông ở góc hoặc lăn chuột trên thẻ để phóng to / thu nhỏ. Thẻ được phép tràn ra ngoài khung. Mẹo nhấn mạnh: nhân bản thẻ, sửa chữ chỉ còn câu cần nhấn rồi phóng to bản sao.</p>
        <div class="row gap wrap">
          <button class="ghost sm" data-cmd="duplicate" title="Ctrl+D">⧉ Nhân bản thẻ</button>
          <button class="ghost sm" data-cmd="playFrom">▶ Phát từ thẻ này</button>
        </div>`;
    } else if (sel.type === "meme") {
      const span = tl.memes[it.id];
      html = head("Meme") + `
        <div class="row gap"><img src="${thumbUrl("memes", it.path)}" class="checker" style="width:64px;height:64px;object-fit:contain;border-radius:8px">
          <span class="small muted">${esc(it.path)}<br>dài ${span.full.toFixed(1)}s</span></div>
        <div class="two">
          <label class="field"><span>Bắt đầu (giây)</span><input type="number" data-f="start" min="0" step="0.1" value="${span.start.toFixed(1)}"></label>
          <label class="field"><span>Độ dài (trống = cả meme)</span><input type="number" data-f="duration" min="0.3" max="${span.full}" step="0.1" value="${it.duration ?? ""}"></label>
        </div>
        <p class="hint">Gắn với ${it.anchor.seg ? "thẻ " + (P.segments.findIndex((s) => s.id === it.anchor.seg) + 1) + " (đổi thứ tự thẻ thì meme đi theo)" : "đầu video"}</p>
        <label class="field"><span>Vị trí</span></label>${positionGrid(it.position)}
        <label class="field"><span>Kích thước <b>${Math.round(it.size * 100)}%</b></span><input type="range" data-f="size" min="0.15" max="1" step="0.05" value="${it.size}"></label>
        <label class="field"><span>Âm thanh meme <b>${Math.round(it.volume * 100)}%</b></span><input type="range" data-f="volume" min="0" max="1.5" step="0.05" value="${it.volume}"></label>
        <label class="field"><span>Tách nền xanh</span><select data-f="chroma">
          <option value="auto" ${it.chroma === null ? "selected" : ""}>Tự động</option>
          <option value="on" ${it.chroma === true ? "selected" : ""}>Bật</option>
          <option value="off" ${it.chroma === false ? "selected" : ""}>Tắt</option></select></label>`;
    } else if (sel.type === "sfx") {
      const span = tl.sfx[it.id];
      const segI = P.segments.findIndex((s) => s.id === it.anchor.seg);
      html = head("Hiệu ứng âm thanh") + `
        <div class="row gap"><button class="ghost sm" data-cmd="sfxPreview">▶ Nghe thử</button>
          <span class="small muted">🔊 ${esc(sfxName(it.path))}<br>${esc(it.path.split("/")[0].replaceAll("_", " "))} · dài ${span.full.toFixed(1)}s</span></div>
        <div class="two">
          <label class="field"><span>Bắt đầu (giây)</span><input type="number" data-f="start" min="0" step="0.1" value="${span.start.toFixed(1)}"></label>
          <label class="field"><span>Độ dài (trống = cả âm)</span><input type="number" data-f="duration" min="0.1" max="${span.full}" step="0.1" value="${it.duration ?? ""}"></label>
        </div>
        <p class="hint">Gắn với ${segI >= 0 ? `thẻ ${segI + 1} (đổi thứ tự thẻ thì sfx đi theo)` : "đầu video"}</p>
        ${segI >= 0 ? `<div class="row gap wrap">${[["head", "⏮ Đầu thẻ"], ["voice", "🗣 Hết lời đọc"], ["end", "⏭ Cuối thẻ"]]
          .map(([k, l]) => `<button class="ghost sm" data-sfxat="${k}">${l}</button>`).join("")}</div>` : ""}
        <label class="field"><span>Âm lượng <b>${Math.round(it.volume * 100)}%</b></span><input type="range" data-f="volume" min="0" max="2" step="0.05" value="${it.volume}"></label>`;
    } else if (sel.type === "text") {
      const span = tl.texts[it.id];
      html = head("Chữ trên video") + `
        <label class="field"><span>Nội dung</span><textarea rows="3" data-f="text" placeholder="vd: Đi làm ở Đà Nẵng có gì vui? 🌊">${esc(it.text)}</textarea></label>
        <div class="two">
          <label class="field"><span>Bắt đầu (giây)</span><input type="number" data-f="start" min="0" step="0.1" value="${span.start.toFixed(1)}"></label>
          <label class="field"><span>Độ dài (trống = đến hết)</span><input type="number" data-f="duration" min="0.5" step="0.5" value="${it.duration ?? ""}"></label>
        </div>
        <label class="field"><span>Vị trí</span></label>
        <div class="seg-toggle">${[["top", "Trên"], ["middle", "Giữa"], ["bottom", "Dưới"]].map(([v, l]) => `<button data-tpos="${v}" class="${it.position === v ? "active" : ""}">${l}</button>`).join("")}</div>`;
    } else if (sel.type === "music") {
      const cats = [...new Set(state.materials.music.map((m) => m.category))];
      html = head("Nhạc nền") + `
        <label class="field"><span>Bài nhạc</span><select data-f="path">${cats.map((c) => `<optgroup label="${esc(c || "chung")}">${state.materials.music.filter((m) => m.category === c).map((m) =>
          `<option value="${esc(m.path)}" ${m.path === it.path ? "selected" : ""}>${esc(m.name)} (${fmtDur(m.duration)})</option>`).join("")}</optgroup>`).join("")}</select></label>
        <div class="two">
          <label class="field"><span>Bắt đầu (giây)</span><input type="number" data-f="mstart" min="0" step="0.5" value="${it.start}"></label>
          <label class="field"><span>Kết thúc (trống = hết video)</span><input type="number" data-f="end" min="0" step="0.5" value="${it.end ?? ""}"></label>
        </div>
        <label class="field"><span>Âm lượng <b>${Math.round(it.volume * 100)}%</b></span><input type="range" data-f="volume" min="0" max="0.6" step="0.01" value="${it.volume}"></label>
        <p class="hint">Nhạc ngắn hơn đoạn sẽ tự lặp lại. Thêm nhiều đoạn nhạc bằng nút "+ Nhạc".</p>`;
    } else if (sel.type === "bg") {
      html = head("Video nền", false) + `
        <p class="hint">Chỉ lấy một đoạn dài bằng video, bắt đầu từ giây bạn chọn.</p>
        <div class="two">
          <label class="field"><span>Lấy từ giây</span><input type="number" data-f="bgstart" min="0" step="1" value="${P.background.start}"></label>
          <label class="field"><span>&nbsp;</span><button class="ghost" data-cmd="rerollBg">🎲 Đoạn khác</button></label>
        </div>
        <div class="thumb-grid">${state.materials.backgrounds.map((b) => `<div class="thumb ${b.path === P.background.path ? "on" : ""}" data-bg="${esc(b.path)}" title="${esc(b.name)} (${fmtDur(b.duration)})">
          <img src="${thumbUrl("backgrounds", b.path)}" loading="lazy"><span class="cap">${esc(b.name)}</span></div>`).join("")}</div>`;
    }
    el.innerHTML = `<div class="insp">${html}</div>`;
  }

  function onInspectorInput(e) {
    const f = e.target.dataset.f;
    if (!f || !P) return;
    const raw = e.target.value;
    const num = raw === "" ? null : parseFloat(raw);
    const it = sel && findItem(sel.type, sel.id);
    const live = e.target.tagName === "TEXTAREA" || e.target.type === "number" || e.target.type === "range" || e.target.tagName === "INPUT";
    const apply = () => {
      if (!sel) {
        if (f === "publish_at") P.publish_at = raw || null;
        else if (f === "voice" || f === "precision") P[f] = raw;
        else P[f] = num ?? P[f];
        saveDefaults();
      } else if (sel.type === "seg") {
        if (f === "hold") it.hold = Math.max(0, num || 0);
        else if (f === "scale") it.scale = clamp(num || 1, 0.2, 5);
        else if (f === "dx" || f === "dy") it[f] = clamp(Math.round(num || 0), -3000, 3000);
        else if (f === "segVoice") it.voice = raw || null;
        else it[f] = raw;
      } else if (sel.type === "meme") {
        if (f === "start") it.anchor = anchorAt(Math.max(0, num || 0));
        else if (f === "duration") {
          const old = memeLen(it);
          it.duration = num && num > 0 ? num : null;
          adjustHold(it, old, memeLen(it));
        } else if (f === "chroma") it.chroma = { auto: null, on: true, off: false }[raw];
        else it[f] = num;
      } else if (sel.type === "sfx") {
        if (f === "start") it.anchor = anchorAt(Math.max(0, num || 0));
        else if (f === "duration") it.duration = num && num > 0 ? num : null;
        else it[f] = num;
      } else if (sel.type === "text") {
        if (f === "start") it.anchor = anchorAt(Math.max(0, num || 0));
        else if (f === "duration") it.duration = num && num > 0 ? num : null;
        else it[f] = raw;
      } else if (sel.type === "music") {
        if (f === "path") it.path = raw;
        else if (f === "mstart") it.start = Math.max(0, num || 0);
        else if (f === "end") it.end = num && num > it.start ? num : null;
        else it[f] = num;
        saveDefaults();
      } else if (sel.type === "bg" && f === "bgstart") {
        P.background.start = Math.max(0, num || 0);
      }
    };
    apply();
    // Ô nhập liệu: không vẽ lại bảng (giữ con trỏ); nhãn <b> cạnh thanh trượt cập nhật tại chỗ.
    const b = e.target.closest("label")?.querySelector("b");
    if (b && e.target.type === "range") {
      b.textContent = f === "speed" ? `${(+raw).toFixed(2)}x` : f === "gap" ? `${(+raw).toFixed(2)}s` : `${Math.round(raw * 100)}%`;
    }
    changed({ keepInspector: live && e.type === "input" });
  }

  function onInspectorClick(e) {
    const t_ = e.target;
    const cmd = t_.closest("[data-cmd]")?.dataset.cmd;
    const it = sel && findItem(sel.type, sel.id);
    if (cmd === "delete") return deleteSelected();
    if (cmd === "deselect") return select(null);
    if (cmd === "sample") return playSample(t_.closest("button"));
    if (cmd === "playFrom") {
      seek(tl.seg[it.id].start);
      return play();
    }
    if (cmd === "rerollBg") return mutate(() => pickBackground(true));
    if (cmd === "duplicate") return duplicateSeg(it.id);
    if (cmd === "sfxPreview") return previewSfx(it.path);
    const at = t_.closest("[data-sfxat]")?.dataset.sfxat;
    if (at && it) {
      const span = tl.seg[it.anchor.seg];
      return mutate(() => (it.anchor.offset = round1({ head: 0, voice: span.voice, end: span.end - span.start }[at])));
    }
    if (cmd === "centerCard") return mutate(() => Object.assign(it, { scale: 1, dx: 0, dy: 0 }), { noTts: true });
    const scale = t_.closest("[data-scale]")?.dataset.scale;
    if (scale && it) return mutate(() => (it.scale = +scale));
    const when = t_.closest("[data-when]")?.dataset.when;
    if (when) return mutate(() => (P.publish_at = whenPreset(when)));
    const pos = t_.closest("[data-pos]")?.dataset.pos;
    if (pos && it) return mutate(() => (it.position = pos));
    const tpos = t_.closest("[data-tpos]")?.dataset.tpos;
    if (tpos && it) return mutate(() => (it.position = tpos));
    const theme = t_.closest("[data-theme]")?.dataset.theme;
    if (theme) return mutate(() => { P.theme = theme; saveDefaults(); });
    const bg = t_.closest("[data-bg]")?.dataset.bg;
    if (bg) return mutate(() => { P.background = { path: bg, start: 0 }; pickBackground(true); });
  }

  // Giờ đăng dạng "YYYY-MM-DDTHH:MM" theo giờ máy (đúng định dạng của input datetime-local).
  function whenPreset(key) {
    if (key === "clear") return null;
    const d = new Date();
    if (key.startsWith("tomorrow")) d.setDate(d.getDate() + 1);
    d.setHours(+key.replace(/\D/g, ""), 0, 0, 0);
    return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }

  async function playSample(btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Đang tạo...";
    try {
      const text = P.segments.find((s) => s.read_text.trim())?.read_text.slice(0, 160) || "Xin chào các bạn, đây là giọng đọc thử.";
      const r = await api("/api/tts", { method: "POST", body: { text, voice: P.voice, precision: P.precision } });
      const a = new Audio(r.url);
      a.playbackRate = P.speed;
      a.play();
    } catch (e) { toast(e.message, true); }
    finally {
      btn.disabled = false;
      btn.textContent = "▶ Nghe thử giọng này";
    }
  }

  // ───────────────────────── Xuất video ─────────────────────────

  let renderMsg = "";
  async function startRender() {
    if (!P?.segments.length) return toast("Timeline chưa có thẻ nào", true);
    if (!P.background) return toast("Chưa có video nền trong materials/background_videos", true);
    try {
      const job = await api("/api/render", { method: "POST", body: P });
      renderJobId = job.id;
      renderMsg = "🎬 Đang chờ xuất video...";
      updateTtsStatus();
      toast("Đã bắt đầu xuất video. Xuất xong dự án sẽ tự đóng và xóa khỏi danh sách dở.");
      pollJobs();
    } catch (e) { toast(e.message, true); }
  }

  function onJobs(jobs) {
    const j = renderJobId && jobs.find((x) => x.id === renderJobId);
    if (!j) return;
    if (j.status === "done" || j.status === "error") {
      renderJobId = null;
      renderMsg = "";
      if (j.status === "done" && j.result?.project_id && j.result.project_id === P?.id) {
        dirty = false; // dự án đã bị xóa ở server, không lưu lại
        closeProject();
      }
    } else {
      renderMsg = `🎬 Xuất video ${Math.round(j.progress * 100)}% — ${j.message}`;
    }
    updateTtsStatus();
  }

  // ───────────────────────── Khởi tạo ─────────────────────────

  function renderMemeLib() {
    const all = state.materials.memes;
    const cats = [...new Set(all.map((m) => m.category))].sort((a, b) => a.localeCompare(b, "vi"));
    const label = (c) => (c || "chung").replaceAll("_", " ");
    const cur = store.get("memeCat", "");
    $("#memeCat").innerHTML = `<option value="">😹 Tất cả meme (${all.length})</option>` +
      cats.map((c) => `<option value="${esc(c)}" ${c === cur ? "selected" : ""}>${esc(label(c))} (${all.filter((m) => m.category === c).length})</option>`).join("");
    const q = $("#memeSearch").value.trim().toLowerCase();
    const memes = all.filter((m) => (!cur || m.category === cur) && (!q || memeName(m).toLowerCase().includes(q)));
    $("#memeLib").innerHTML = memes.length ? memes.map((m) => `
      <div class="meme-item" draggable="true" data-meme="${esc(m.path)}" title="Bấm để xem trước · kéo vào hàng Meme">
        <div class="thumb checker"><img src="${thumbUrl("memes", m.path)}" loading="lazy" alt=""></div>
        <div class="meta"><b>${esc(memeName(m))}</b><span>${esc(label(m.category))} · ${m.duration.toFixed(1)}s</span></div></div>`).join("")
      : '<span class="muted small">Không có meme nào</span>';
  }

  // Tên file vlipsy: slug-<id 8 ký tự>; tên cũ: Chu-Hoa-Green-Screen → bỏ phần thừa cho dễ đọc.
  const memeName = (m) => (/^[a-z0-9-]+-[A-Za-z0-9]{8}$/.test(m.name) ? m.name.slice(0, -9) : m.name).replace(/[-_]+/g, " ").replace(/\s*green screen\s*/i, " ").trim();

  function updateMemeHint() {
    const seg = P && sel?.type === "seg" && findItem("seg", sel.id);
    const n = seg && P.segments.indexOf(seg) + 1;
    $("#memeHint").textContent = libTab() === "sfx"
      ? seg ? `Bấm ➕ để chèn sfx vào đầu thẻ ${n}`
        : sel?.type === "meme" ? "Bấm ➕ để chèn sfx cùng lúc với meme đang chọn"
          : "Bấm sfx để nghe thử · ➕ chèn tại đầu phát · chọn thẻ trên timeline để chèn vào đầu thẻ đó · hoặc kéo vào hàng SFX"
      : seg ? `Bấm meme để chèn sau thẻ ${n} — thẻ sẽ chờ thêm bằng độ dài meme`
        : "Bấm meme để xem trước · chọn một thẻ trên timeline để chèn meme ngay sau thẻ đó";
  }

  // ── Thư viện SFX ──
  const libTab = () => store.get("libTab", "memes");

  function showLibTab(tab) {
    store.set("libTab", tab);
    $$("#libTabs button").forEach((b) => b.classList.toggle("active", b.dataset.lib === tab));
    $$(".lib-ctl").forEach((el) => el.classList.toggle("hidden", el.dataset.pane !== tab));
    $("#memeLib").classList.toggle("hidden", tab !== "memes");
    $("#sfxLib").classList.toggle("hidden", tab !== "sfx");
    if (tab !== "sfx") stopSfxPreview();
    updateMemeHint();
  }

  function renderSfxLib() {
    const all = state.materials.sfx || [];
    const cats = [...new Set(all.map((m) => m.category))].sort((a, b) => a.localeCompare(b, "vi"));
    const label = (c) => (c || "chung").replaceAll("_", " ");
    const cur = store.get("sfxCat", "");
    $("#sfxCat").innerHTML = `<option value="">🔊 Tất cả sfx (${all.length})</option>` +
      cats.map((c) => `<option value="${esc(c)}" ${c === cur ? "selected" : ""}>${esc(label(c))} (${all.filter((m) => m.category === c).length})</option>`).join("");
    const q = $("#sfxSearch").value.trim().toLowerCase();
    const list = all.filter((m) => (!cur || m.category === cur) && (!q || sfxName(m.path).toLowerCase().includes(q)));
    $("#sfxLib").innerHTML = list.length ? list.map((m) => `
      <div class="sfx-item ${sfxPlaying === m.path ? "playing" : ""}" draggable="true" data-sfx="${esc(m.path)}" title="Bấm để nghe thử · kéo vào hàng SFX">
        <span class="play">${sfxPlaying === m.path ? "⏸" : "▶"}</span>
        <div class="meta"><b>${esc(sfxName(m.path))}</b><span>${esc(label(m.category))} · ${m.duration.toFixed(1)}s</span></div>
        <button class="ghost sm add" data-sact="add" title="Chèn vào video">➕</button></div>`).join("")
      : '<span class="muted small">Chưa có sfx. Chạy <code>uv run threads-crawler sfx</code> để tải về materials/sfx/</span>';
  }

  const sfxAudio = new Audio();
  let sfxPlaying = null;
  sfxAudio.addEventListener("ended", () => { sfxPlaying = null; renderSfxLib(); });

  function previewSfx(path) {
    if (playing) pause();
    if (sfxPlaying === path) return stopSfxPreview();
    sfxAudio.src = fileUrl("sfx", path);
    sfxAudio.currentTime = 0;
    sfxAudio.play().catch(() => {});
    sfxPlaying = path;
    renderSfxLib();
  }

  function stopSfxPreview() {
    sfxAudio.pause();
    if (sfxPlaying) {
      sfxPlaying = null;
      renderSfxLib();
    }
  }

  let previewPath = null;
  function openMemePreview(path) {
    const m = memeInfo(path);
    if (!m) return;
    if (playing) pause();
    previewPath = path;
    const seg = P && sel?.type === "seg" && findItem("seg", sel.id);
    $("#memeModalTitle").textContent = memeName(m);
    $("#memeModalInfo").textContent = `${m.category.replaceAll("_", " ") || "chung"} · ${m.duration.toFixed(1)}s`;
    $("#memeInsert").textContent = !P ? "Chưa có video đang làm" : seg
      ? `➕ Chèn sau thẻ ${P.segments.indexOf(seg) + 1} (+${m.duration.toFixed(1)}s)`
      : `➕ Thêm tại ${fmtT(t)}`;
    $("#memeInsert").disabled = !P;
    const v = $("#memePreview");
    v.src = fileUrl("memes", path);
    v.play().catch(() => {});
    $("#memeModal").classList.remove("hidden");
  }

  function closeMemePreview() {
    const v = $("#memePreview");
    v.pause();
    v.removeAttribute("src");
    v.load();
    $("#memeModal").classList.add("hidden");
    previewPath = null;
  }

  function showEditor() {
    if (!P) loadProjects();
    $("#createEmpty").classList.toggle("hidden", !!P);
    $("#editor").classList.toggle("hidden", !P);
    if (P) $("#pName").value = P.name || "";
  }

  async function init() {
    // Bản cũ lưu một dự án trong localStorage → chuyển lên server thành dự án dở.
    const legacy = store.get("project", null);
    if (legacy?.segments) {
      legacy.id ||= Date.now().toString(36) + uid();
      try {
        await api(`/api/projects/${legacy.id}`, { method: "POST", body: legacy });
        store.set("projectId", legacy.id);
        localStorage.removeItem("project");
      } catch {}
    } else {
      localStorage.removeItem("project");
    }
    const id = store.get("projectId", null);
    if (id) {
      try {
        P = await api(`/api/projects/${id}`);
        P.id = id;
        P.sfx ||= [];
      } catch {
        P = null;
        store.set("projectId", null);
      }
    }
    $("#zoom").value = pps;
    renderMemeLib();

    const c = content();
    c.addEventListener("pointerdown", onPointerDown);
    c.addEventListener("pointermove", onPointerMove);
    c.addEventListener("pointerup", onPointerUp);
    c.addEventListener("pointercancel", onPointerUp);
    const dropRow = (e) => (e.dataTransfer.types.includes("text/meme") ? "meme" : e.dataTransfer.types.includes("text/sfx") ? "sfx" : null);
    c.addEventListener("dragover", (e) => {
      const r = dropRow(e);
      if (!r) return;
      e.preventDefault();
      row(r).classList.add("drop");
    });
    const clearDrop = () => ["meme", "sfx"].forEach((r) => row(r).classList.remove("drop"));
    c.addEventListener("dragleave", clearDrop);
    c.addEventListener("drop", (e) => {
      clearDrop();
      const time = Math.max(0, timeAtClientX(e.clientX));
      const meme = e.dataTransfer.getData("text/meme"), sfx = e.dataTransfer.getData("text/sfx");
      if (!meme && !sfx) return;
      e.preventDefault();
      if (!P) return;
      meme ? addMeme(meme, time) : insertSfx(sfx, time);
    });
    $("#memeCat").onchange = () => {
      store.set("memeCat", $("#memeCat").value);
      renderMemeLib();
    };
    $("#memeLib").addEventListener("dragstart", (e) => {
      const m = e.target.closest("[data-meme]");
      if (m) e.dataTransfer.setData("text/meme", m.dataset.meme);
    });
    $("#memeLib").addEventListener("click", (e) => {
      const m = e.target.closest("[data-meme]");
      if (m) openMemePreview(m.dataset.meme);
    });
    $("#memeSearch").oninput = renderMemeLib;
    renderSfxLib();
    showLibTab(libTab());
    $("#libTabs").onclick = (e) => { const b = e.target.closest("[data-lib]"); if (b) showLibTab(b.dataset.lib); };
    $("#sfxCat").onchange = () => {
      store.set("sfxCat", $("#sfxCat").value);
      renderSfxLib();
    };
    $("#sfxSearch").oninput = renderSfxLib;
    $("#sfxLib").addEventListener("dragstart", (e) => {
      const m = e.target.closest("[data-sfx]");
      if (m) e.dataTransfer.setData("text/sfx", m.dataset.sfx);
    });
    $("#sfxLib").addEventListener("click", (e) => {
      const m = e.target.closest("[data-sfx]");
      if (!m) return;
      if (!e.target.closest("[data-sact=add]")) return previewSfx(m.dataset.sfx);
      if (!P) return toast("Chưa có video đang làm", true);
      insertSfx(m.dataset.sfx);
    });
    $("#memeInsert").onclick = () => {
      const path = previewPath;
      closeMemePreview();
      if (path && P) insertMeme(path);
    };
    $("#memeModalClose").onclick = $("#memeModalCancel").onclick = closeMemePreview;
    $("#memeModal").onclick = (e) => { if (e.target.id === "memeModal") closeMemePreview(); };

    const cv = canvas();
    cv.addEventListener("pointerdown", onStageDown);
    cv.addEventListener("pointermove", onStageMove);
    cv.addEventListener("pointerup", onStageUp);
    cv.addEventListener("pointercancel", onStageUp);
    cv.addEventListener("wheel", onStageWheel, { passive: false });

    $("#playBtn").onclick = () => (playing ? pause() : play());
    $("#zoom").oninput = () => {
      pps = +$("#zoom").value;
      store.set("tlZoom", pps);
      renderTimeline();
    };
    $("#fitBtn").onclick = fitZoom;
    $("#addText").onclick = addText;
    $("#addMusic").onclick = addMusic;
    $("#undoBtn").onclick = undo;
    $("#renderBtn").onclick = startRender;
    $("#rebuildAll").onclick = rebuildAll;
    $("#addPosts").onclick = () => showTab("library");
    $("#newProject").onclick = () => closeProject();
    $("#projectList").addEventListener("click", onProjectListClick);
    window.addEventListener("beforeunload", flushOnLeave);
    document.addEventListener("visibilitychange", () => document.hidden && flushOnLeave());
    $("#pName").addEventListener("focus", snapshot);
    $("#pName").addEventListener("input", () => { P.name = $("#pName").value; saveSoon(); });
    $("#edPosts").addEventListener("click", onPostsClick);
    const insp = $("#inspector");
    insp.addEventListener("input", onInspectorInput);
    insp.addEventListener("change", (e) => {
      if (e.target.tagName === "SELECT") return; // select đã xử lý ở "input"
      if (e.target.dataset.f) changed(); // xong một lần sửa -> vẽ lại bảng
    });
    insp.addEventListener("focusin", (e) => { if (e.target.dataset.f) snapshot(); });
    insp.addEventListener("pointerdown", (e) => { if (e.target.type === "range") snapshot(); });
    insp.addEventListener("click", onInspectorClick);

    document.addEventListener("keydown", (e) => {
      if (!$("#memeModal").classList.contains("hidden")) {
        if (e.key === "Escape") closeMemePreview();
        if (e.key === "Enter") $("#memeInsert").click();
        return;
      }
      if (!P || !$("#tab-create").classList.contains("active")) return;
      if (e.target.closest("input, textarea, select")) return;
      if (e.code === "Space") {
        e.preventDefault();
        playing ? pause() : play();
      } else if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        deleteSelected();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "d") {
        e.preventDefault();
        if (sel?.type === "seg") duplicateSeg(sel.id);
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        undo();
      } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        seek(t + (e.key === "ArrowLeft" ? -1 : 1));
      }
    });
    window.addEventListener("resize", () => renderTimeline());
  }

  function onShow() {
    showEditor();
    if (!P) return;
    if (!P.background) pickBackground();
    tl = computeTimeline();
    renderPosts();
    renderInspector();
    renderTimeline();
    syncMedia();
    draw();
    updateTtsStatus();
    clearTimeout(ttsTimer);
    ttsTimer = setTimeout(runTts, 300);
  }

  return {
    init, onShow, onJobs, pause, createFromPosts, addPosts,
    hasProject: () => !!P,
    hasPost: (code) => !!P?.post_codes.includes(code),
  };
})();
