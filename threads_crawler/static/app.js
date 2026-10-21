// Threads → TikTok: giao diện crawl, kho bài, tạo video, video đã xuất.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n ?? 0)).replace(".0", "");
const fmtDur = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.body && typeof opts.body !== "string") {
    init.body = JSON.stringify(opts.body);
    init.headers = { "Content-Type": "application/json" };
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.blob();
}

function toast(msg, error = false) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), error ? 8000 : 4000);
}

const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

const state = {
  queries: [],
  posts: [],
  postsTotal: 0,
  post: null,
  selectedReplies: [],
  segments: [],
  materials: { backgrounds: [], music: [], memes: [] },
  voices: [],
  settings: Object.assign(
    { voice: "Hải Đăng", precision: "fp32", speed: 1.1, theme: "light", title: "", background: "random",
      music: "random:funny", music_volume: 0.15, memes: [] },
    store.get("videoSettings", {}),
    { title: "", memes: [] },
  ),
};
const saveSettings = () => store.set("videoSettings", { ...state.settings, title: "", memes: [] });

// ───────────────────────── Tabs ─────────────────────────

function showTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "library") loadPosts();
  if (name === "videos") loadVideos();
  store.set("tab", name);
}
$$(".tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]");
  if (g) showTab(g.dataset.goto);
});

// ───────────────────────── Tác vụ nền ─────────────────────────

const seenDone = new Set();
let jobsTimer;
async function pollJobs() {
  clearTimeout(jobsTimer);
  let jobs = [];
  try { jobs = await api("/api/jobs"); } catch {}
  const active = jobs.filter((j) => j.status === "queued" || j.status === "running");
  $("#jobsBadge").textContent = active.length;
  $("#jobsBadge").classList.toggle("hidden", !active.length);
  $("#jobsPanel").innerHTML = jobs.length
    ? jobs.map(jobHtml).join("")
    : '<p class="muted small">Chưa có tác vụ nào.</p>';

  for (const j of jobs) {
    if ((j.status === "done" || j.status === "error") && !seenDone.has(j.id)) {
      seenDone.add(j.id);
      if (!firstPoll) onJobFinished(j);
    }
  }
  firstPoll = false;
  updateRenderStatus(jobs);
  jobsTimer = setTimeout(pollJobs, active.length ? 1200 : 4000);
}
let firstPoll = true;

function jobHtml(j) {
  const pct = Math.round(j.progress * 100);
  const status = { queued: "Chờ", running: pct + "%", done: "✓ Xong", error: "✕ Lỗi" }[j.status];
  return `<div class="job">
    <div class="job-title"><span>${esc(j.title)}</span><span class="muted">${status}</span></div>
    <div class="job-msg ${j.status === "error" ? "error" : ""}">${esc(j.error || j.message)}</div>
    ${j.status === "running" || j.status === "queued" ? `<div class="progress"><div style="width:${pct}%"></div></div>` : ""}
  </div>`;
}

function onJobFinished(j) {
  if (j.status === "error") return toast(`${j.title}: ${j.error}`, true);
  if (j.kind === "crawl") {
    const r = j.result || {};
    toast(`Crawl xong: ${r.posts} bài, ${r.replies} bình luận` + (r.errors?.length ? ` (${r.errors.length} lỗi)` : ""));
    loadQueries();
    if ($("#tab-library").classList.contains("active")) loadPosts();
  } else {
    toast("Đã xuất video! Xem ở tab Video đã xuất.");
    loadVideos();
  }
}

$("#jobsBtn").onclick = (e) => {
  e.stopPropagation();
  $("#jobsPanel").classList.toggle("hidden");
};
document.addEventListener("click", (e) => {
  if (!e.target.closest(".jobs-wrap")) $("#jobsPanel").classList.add("hidden");
});

// ───────────────────────── Crawl ─────────────────────────

$("#customKeywords").oninput = updateCrawlSummary;

function customKeywords() {
  return $("#customKeywords").value.split("\n").map((s) => s.trim()).filter(Boolean);
}

function updateCrawlSummary() {
  const n = customKeywords().length;
  $("#crawlSummary").textContent = n ? `Sẽ crawl ${n} từ khóa` : "Chưa nhập từ khóa nào";
}

$("#startCrawl").onclick = async () => {
  try {
    const job = await api("/api/crawl", {
      method: "POST",
      body: {
        keywords: customKeywords(),
        limit: +$("#optLimit").value,
        replies: +$("#optReplies").value,
        min_likes: +$("#optMinLikes").value,
        recent: $("#optRecent").checked,
        media: $("#optMedia").checked,
        show_browser: $("#optShow").checked,
      },
    });
    toast(`Đã thêm tác vụ: ${job.title}`);
    pollJobs();
  } catch (e) { toast(e.message, true); }
};

async function loadQueries() {
  state.queries = await api("/api/queries");
  $("#recentQueries").innerHTML = state.queries.length
    ? '<span class="muted small">Đã crawl:</span>' + state.queries.map((q) =>
      `<span class="chip" data-kw="${esc(q.query)}">${esc(q.query)}<span class="cnt">${q.count}</span></span>`).join("")
    : "";
  const qs = [...new Set(state.queries.map((q) => q.query))];
  const cur = $("#fQuery").value;
  $("#fQuery").innerHTML = '<option value="">Tất cả từ khóa</option>' + qs.map((q) => `<option>${esc(q)}</option>`).join("");
  $("#fQuery").value = cur;
}
$("#recentQueries").onclick = (e) => {
  const c = e.target.closest("[data-kw]");
  if (!c) return;
  const list = customKeywords();
  if (!list.includes(c.dataset.kw)) $("#customKeywords").value = [...list, c.dataset.kw].join("\n");
  updateCrawlSummary();
};

// ───────────────────────── Kho bài ─────────────────────────

let postsOffset = 0;
let postsSeq = 0;
async function loadPosts(append = false) {
  if (!append) postsOffset = 0;
  const seq = ++postsSeq;
  const params = new URLSearchParams({ sort: $("#fSort").value, limit: 30, offset: postsOffset });
  for (const [k, id] of [["query", "#fQuery"], ["q", "#fSearch"], ["used", "#fUsed"]]) {
    if ($(id).value) params.set(k, $(id).value);
  }
  const { items, total } = await api("/api/posts?" + params);
  if (seq !== postsSeq) return; // một lần lọc mới hơn đã chạy
  state.posts = append ? [...state.posts, ...items] : items;
  state.postsTotal = total;
  postsOffset = state.posts.length;
  $("#postsCount").textContent = `${total} bài`;
  $("#postsGrid").innerHTML = state.posts.length
    ? state.posts.map(postCardHtml).join("")
    : '<div class="empty panel muted">Chưa có bài nào. Sang tab Crawl để lấy dữ liệu.</div>';
  $("#loadMore").classList.toggle("hidden", state.posts.length >= total);
}

function avatarHtml(p, size = 36) {
  return p.avatar_src
    ? `<img class="avatar" src="${esc(p.avatar_src)}" style="width:${size}px;height:${size}px" alt="">`
    : `<div class="avatar" style="width:${size}px;height:${size}px">${esc(p.username[0]?.toUpperCase())}</div>`;
}

function postCardHtml(p) {
  const imgs = p.media.filter((m) => m.src && m.type === "image").slice(0, 4);
  return `<div class="post-card ${p.used ? "used" : ""}" data-code="${esc(p.code)}">
    <div class="post-head">${avatarHtml(p)}
      <div class="who"><b>${esc(p.username)}${p.user_verified ? " ✔️" : ""}</b>
        <span class="muted small">${p.published_at ? new Date(p.published_at).toLocaleDateString("vi-VN") : ""}</span></div>
    </div>
    <div class="post-text">${esc(p.text)}</div>
    ${imgs.length ? `<div class="post-media">${imgs.map((m) => `<img src="${esc(m.src)}" loading="lazy">`).join("")}</div>` : ""}
    <div class="stats"><span>❤️ ${fmt(p.like_count)}</span><span>💬 ${fmt(p.reply_count)}</span><span>📥 ${p.saved_replies} đã lưu</span></div>
    <div class="tags">
      <span class="tag">🔎 ${esc(p.query)}</span>
      ${p.used ? '<span class="tag ok">✓ đã làm video</span>' : ""}
    </div>
    <div class="post-actions">
      <button class="primary" data-act="make">🎬 Làm video</button>
      <button data-act="view">Xem</button>
      <a href="${esc(p.url)}" target="_blank" rel="noopener"><button>Threads ↗</button></a>
      <button class="danger" data-act="del" title="Xóa">🗑</button>
    </div>
  </div>`;
}

$("#postsGrid").onclick = async (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const code = btn.closest("[data-code]").dataset.code;
  if (btn.dataset.act === "make") openCreate(code);
  if (btn.dataset.act === "view") openDrawer(code);
  if (btn.dataset.act === "del" && confirm("Xóa bài này và bình luận của nó khỏi kho?")) {
    await api(`/api/posts/${code}`, { method: "DELETE" });
    loadPosts();
  }
};
for (const id of ["#fQuery", "#fUsed", "#fSort"]) $(id).onchange = () => loadPosts();
let searchTimer;
$("#fSearch").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadPosts(), 300); };
$("#loadMore").onclick = () => loadPosts(true);

async function openDrawer(code) {
  const p = await api(`/api/posts/${code}`);
  $("#drawerContent").innerHTML = `
    <div class="post-head">${avatarHtml(p, 44)}<div class="who"><b>${esc(p.username)}</b>
      <span class="muted small">❤️ ${fmt(p.like_count)} · 💬 ${fmt(p.reply_count)}</span></div></div>
    <p style="white-space:pre-wrap">${esc(p.text)}</p>
    <div class="post-media">${p.media.filter((m) => m.src).map((m) => m.type === "video"
      ? `<video src="${esc(m.src)}" controls style="height:200px"></video>`
      : `<img src="${esc(m.src)}" style="height:200px">`).join("")}</div>
    <h3>${p.replies.length} bình luận đã lưu</h3>
    <div class="replies" style="max-height:none">${p.replies.map((r) => replyHtml(r, p, null)).join("")}</div>
    <button class="primary big" onclick="openCreate('${esc(p.code)}')">🎬 Làm video từ bài này</button>`;
  $("#drawer").classList.remove("hidden");
}
$("#drawerClose").onclick = () => $("#drawer").classList.add("hidden");
$("#drawer").onclick = (e) => { if (e.target.id === "drawer") $("#drawer").classList.add("hidden"); };

function replyHtml(r, post, order = 0) {
  const self = r.username === post.username;
  return `<div class="reply ${order ? "on" : ""}" data-code="${esc(r.code)}">
    ${order !== null ? `<div class="order">${order || ""}</div>` : ""}
    <div class="body">
      <b>${esc(r.username)}</b>${self ? '<span class="self">tác giả viết tiếp</span>' : ""}
      ${r.reply_to_username && !self ? `<span class="muted small"> → @${esc(r.reply_to_username)}</span>` : ""}
      <span class="muted small"> · ❤️ ${fmt(r.like_count)}</span>
      <p>${esc(r.text)}</p>
    </div></div>`;
}

// ───────────────────────── Tạo video ─────────────────────────

async function openCreate(code) {
  $("#drawer").classList.add("hidden");
  const p = await api(`/api/posts/${code}`);
  state.post = p;
  // Tác giả viết tiếp được chọn sẵn, theo thứ tự thời gian.
  state.selectedReplies = p.replies.filter((r) => r.username === p.username).map((r) => r.code);
  state.segments = [];
  state.settings.memes = [];
  state.settings.title = "";
  $("#sTitle").value = "";
  $("#createEmpty").classList.add("hidden");
  $("#createMain").classList.remove("hidden");
  $("#cPost").innerHTML = `<div class="post-head">${avatarHtml(p)}<div class="who"><b>${esc(p.username)}</b>
      <span class="muted small">❤️ ${fmt(p.like_count)} · 💬 ${fmt(p.reply_count)}</span></div>
      <a href="${esc(p.url)}" target="_blank" rel="noopener"><button class="ghost sm">Threads ↗</button></a></div>
    <div class="post-text" style="margin-top:8px">${esc(p.text)}</div>`;
  renderReplies();
  renderSegments();
  renderMemeList();
  showTab("create");
  buildScript();
}

function renderReplies() {
  const p = state.post;
  const sorted = [...p.replies].sort((a, b) => (b.username === p.username) - (a.username === p.username) || b.like_count - a.like_count);
  $("#cReplies").innerHTML = sorted.length
    ? sorted.map((r) => replyHtml(r, p, state.selectedReplies.indexOf(r.code) + 1)).join("")
    : '<p class="muted">Bài này chưa có bình luận đã lưu.</p>';
}
$("#cReplies").onclick = (e) => {
  const el = e.target.closest("[data-code]");
  if (!el) return;
  const c = el.dataset.code;
  const i = state.selectedReplies.indexOf(c);
  i >= 0 ? state.selectedReplies.splice(i, 1) : state.selectedReplies.push(c);
  renderReplies();
};
$("#repliesTop").onclick = () => {
  const others = state.post.replies.filter((r) => r.username !== state.post.username && !state.selectedReplies.includes(r.code));
  others.sort((a, b) => b.like_count - a.like_count).slice(0, 3).forEach((r) => state.selectedReplies.push(r.code));
  renderReplies();
};
$("#repliesNone").onclick = () => { state.selectedReplies = []; renderReplies(); };
$("#buildScript").onclick = () => buildScript();

async function buildScript() {
  try {
    state.segments = await api("/api/script", {
      method: "POST",
      body: { code: state.post.code, reply_codes: state.selectedReplies, max_lines: +$("#maxLines").value, with_image: $("#withImage").checked },
    });
    renderSegments();
    refreshPreview();
  } catch (e) { toast(e.message, true); }
}

function estimateSeconds() {
  // ~14 ký tự/giây ở tốc độ 1.0 với giọng VieNeu, cộng khoảng nghỉ
  const chars = state.segments.reduce((s, x) => s + (x.read_text || "").length, 0);
  return chars / 14 / state.settings.speed + state.segments.length * 0.35 + 1.2;
}

function renderSegments() {
  const segs = state.segments;
  $("#scriptInfo").textContent = segs.length ? `${segs.length} đoạn · ước tính ~${fmtDur(estimateSeconds())}` : "";
  $("#cSegments").innerHTML = segs.length ? segs.map((s, i) => `
    <div class="segment" data-i="${i}">
      <div class="segment-head">
        <span class="idx">${i + 1}</span><b>${esc(s.username)}</b>
        <span class="kind">${s.kind === "post" ? "Bài viết" : s.reply_to ? "Bình luận" : "Tác giả viết tiếp"}${s.page_label ? " · trang " + s.page_label : ""}${s.image_path ? " · có ảnh" : ""}</span>
        <span class="sp"></span>
        <button class="ghost" data-sact="preview" title="Xem trước thẻ này">👁</button>
        <button class="ghost" data-sact="up" ${i === 0 ? "disabled" : ""}>↑</button>
        <button class="ghost" data-sact="down" ${i === segs.length - 1 ? "disabled" : ""}>↓</button>
        <button class="ghost danger" data-sact="del">✕</button>
      </div>
      <div class="lbl">Chữ trên thẻ</div>
      <textarea rows="3" data-field="display_text">${esc(s.display_text)}</textarea>
      <div class="lbl">Lời đọc</div>
      <textarea rows="2" data-field="read_text">${esc(s.read_text)}</textarea>
    </div>`).join("") : '<p class="muted">Bấm "Tạo kịch bản" sau khi chọn bình luận.</p>';
  $("#previewSeg").innerHTML = segs.map((s, i) => `<option value="${i}">Đoạn ${i + 1}: ${esc(s.username)}</option>`).join("");
  renderMemeList();
}
$("#cSegments").oninput = (e) => {
  const ta = e.target.closest("textarea[data-field]");
  if (!ta) return;
  state.segments[+ta.closest("[data-i]").dataset.i][ta.dataset.field] = ta.value;
  $("#scriptInfo").textContent = `${state.segments.length} đoạn · ước tính ~${fmtDur(estimateSeconds())}`;
};
$("#cSegments").onclick = (e) => {
  const b = e.target.closest("[data-sact]");
  if (!b) return;
  const i = +b.closest("[data-i]").dataset.i;
  const segs = state.segments;
  if (b.dataset.sact === "del") segs.splice(i, 1);
  if (b.dataset.sact === "up") [segs[i - 1], segs[i]] = [segs[i], segs[i - 1]];
  if (b.dataset.sact === "down") [segs[i + 1], segs[i]] = [segs[i], segs[i + 1]];
  if (b.dataset.sact === "preview") {
    $("#previewSeg").value = i;
    return refreshPreview();
  }
  renderSegments();
};

// ── Cài đặt video ──

function renderSpec() {
  const s = state.settings;
  return {
    post_code: state.post.code,
    segments: state.segments,
    voice: s.voice, precision: s.precision, speed: s.speed, theme: s.theme, title: $("#sTitle").value,
    background: s.background, music: s.music, music_volume: s.music_volume,
    memes: s.memes.map(({ thumb, ...m }) => m),
  };
}

let previewSeq = 0;
async function refreshPreview() {
  if (!state.post || !state.segments.length) return;
  const seq = ++previewSeq;
  $(".preview-box").classList.add("loading");
  try {
    const blob = await api(`/api/preview?segment=${$("#previewSeg").value || 0}`, { method: "POST", body: renderSpec() });
    if (seq !== previewSeq) return;
    const img = $("#previewImg");
    if (img.src) URL.revokeObjectURL(img.src);
    img.src = URL.createObjectURL(blob);
    $("#previewEmpty").classList.add("hidden");
  } catch (e) { toast("Xem trước lỗi: " + e.message, true); }
  finally { if (seq === previewSeq) $(".preview-box").classList.remove("loading"); }
}
$("#previewBtn").onclick = refreshPreview;
$("#previewSeg").onchange = refreshPreview;
let titleTimer;
$("#sTitle").oninput = () => { clearTimeout(titleTimer); titleTimer = setTimeout(refreshPreview, 600); };

async function loadVoices() {
  state.voices = await api("/api/voices");
  $("#sVoice").innerHTML = state.voices.map((v) => `<option value="${esc(v.id)}">${esc(v.label)}</option>`).join("");
  $("#sVoice").value = state.settings.voice;
}
$("#sVoice").onchange = () => { state.settings.voice = $("#sVoice").value; saveSettings(); };
$("#sPrecision").onchange = () => { state.settings.precision = $("#sPrecision").value; saveSettings(); };
$("#sSpeed").oninput = () => {
  state.settings.speed = +$("#sSpeed").value;
  $("#speedVal").textContent = state.settings.speed.toFixed(2).replace(/0$/, "") + "x";
  saveSettings();
  if (state.segments.length) $("#scriptInfo").textContent = `${state.segments.length} đoạn · ước tính ~${fmtDur(estimateSeconds())}`;
};
$("#sampleBtn").onclick = async () => {
  const btn = $("#sampleBtn");
  btn.disabled = true;
  btn.textContent = "⏳ Đang tạo...";
  try {
    const text = state.segments[0]?.read_text?.slice(0, 160) || undefined;
    const blob = await api("/api/voices/sample", { method: "POST", body: { voice: state.settings.voice, precision: state.settings.precision, ...(text ? { text } : {}) } });
    const a = $("#sampleAudio");
    a.src = URL.createObjectURL(blob);
    a.playbackRate = state.settings.speed;
    a.play();
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = "▶ Nghe thử"; }
};
$("#sTheme").onclick = (e) => {
  const b = e.target.closest("[data-v]");
  if (!b) return;
  state.settings.theme = b.dataset.v;
  saveSettings();
  $$("#sTheme button").forEach((x) => x.classList.toggle("active", x === b));
  refreshPreview();
};

async function loadMaterials() {
  state.materials = await api("/api/materials");
  const { backgrounds, music, memes } = state.materials;

  $("#bgGrid").innerHTML = `<div class="thumb ${state.settings.background === "random" ? "on" : ""}" data-bg="random">🎲<br>Ngẫu nhiên</div>` +
    backgrounds.map((b) => `<div class="thumb ${state.settings.background === b.path ? "on" : ""}" data-bg="${esc(b.path)}" title="${esc(b.name)} (${fmtDur(b.duration)})">
      <img src="/api/materials/thumb/backgrounds/${encodeURIComponent(b.path)}" loading="lazy"><span class="cap">${esc(b.name)}</span></div>`).join("");

  const cats = [...new Set(music.map((m) => m.category))];
  $("#sMusic").innerHTML = '<option value="">🔇 Không nhạc</option>' +
    cats.map((c) => `<option value="random:${esc(c)}">🎲 Ngẫu nhiên: ${esc(c || "chung")}</option>`).join("") +
    cats.map((c) => `<optgroup label="${esc(c || "chung")}">${music.filter((m) => m.category === c).map((m) =>
      `<option value="${esc(m.path)}">${esc(m.name)} (${fmtDur(m.duration)})</option>`).join("")}</optgroup>`).join("");
  $("#sMusic").value = state.settings.music;
  if ($("#sMusic").value !== state.settings.music) $("#sMusic").value = "";
  updateMusicPlayer();

  const memeCats = [...new Set(memes.map((m) => m.category))];
  $("#memeLib").innerHTML = memes.length ? memeCats.map((c) => `
    <div class="meme-cat">${esc(c || "chung")}</div>
    <div class="meme-row">${memes.filter((m) => m.category === c).map((m) => `
      <div class="meme-item" data-meme="${esc(m.path)}" title="${esc(m.name)} (${m.duration.toFixed(1)}s) — bấm để thêm">
        <img src="/api/materials/thumb/memes/${encodeURIComponent(m.path)}" loading="lazy"></div>`).join("")}</div>`).join("")
    : '<p class="muted small">Chưa có meme trong materials/meme_video</p>';
}

$("#bgGrid").onclick = (e) => {
  const t = e.target.closest("[data-bg]");
  if (!t) return;
  state.settings.background = t.dataset.bg;
  saveSettings();
  $$("#bgGrid .thumb").forEach((x) => x.classList.toggle("on", x === t));
  refreshPreview();
};
function updateMusicPlayer() {
  const v = $("#sMusic").value;
  const a = $("#musicAudio");
  a.classList.toggle("hidden", !v || v.startsWith("random:"));
  if (v && !v.startsWith("random:")) {
    a.src = `/api/materials/file/music/${encodeURIComponent(v)}`;
    a.volume = Math.min(1, state.settings.music_volume * 3);
  } else a.removeAttribute("src");
}
$("#sMusic").onchange = () => { state.settings.music = $("#sMusic").value; saveSettings(); updateMusicPlayer(); };
$("#sMusicVol").oninput = () => {
  state.settings.music_volume = +$("#sMusicVol").value;
  $("#musicVolVal").textContent = Math.round(state.settings.music_volume * 100) + "%";
  $("#musicAudio").volume = Math.min(1, state.settings.music_volume * 3);
  saveSettings();
};

// ── Meme ──

const POSITIONS = ["top-left", "top-center", "top-right", "middle-left", "middle-center", "middle-right", "bottom-left", "bottom-center", "bottom-right"];

$("#memeLib").onclick = (e) => {
  const m = e.target.closest("[data-meme]");
  if (!m) return;
  state.settings.memes.push({ path: m.dataset.meme, segment: 0, offset: 0.5, position: "bottom-right", size: 0.4, volume: 1, chroma: null });
  renderMemeList();
  refreshPreview();
};

function renderMemeList() {
  const segs = state.segments;
  $("#memeList").innerHTML = state.settings.memes.map((m, i) => `
    <div class="meme-cfg" data-mi="${i}">
      <img src="/api/materials/thumb/memes/${encodeURIComponent(m.path)}">
      <div class="cfg">
        <label class="full">Xuất hiện ở
          <select data-mf="segment">${(segs.length ? segs : [{ username: "đầu video" }]).map((s, k) =>
            `<option value="${k}" ${k === m.segment ? "selected" : ""}>Đoạn ${k + 1}: ${esc(s.username)}</option>`).join("")}</select></label>
        <label>Trễ thêm (giây)<input type="number" step="0.5" min="0" value="${m.offset}" data-mf="offset"></label>
        <label>Kích thước <b>${Math.round(m.size * 100)}%</b><input type="range" min="0.15" max="1" step="0.05" value="${m.size}" data-mf="size"></label>
        <label>Vị trí<div class="pos-grid">${POSITIONS.map((p) =>
          `<button data-pos="${p}" class="${p === m.position ? "on" : ""}" title="${p}"></button>`).join("")}</div></label>
        <label>Âm thanh meme <b>${Math.round(m.volume * 100)}%</b><input type="range" min="0" max="1.5" step="0.05" value="${m.volume}" data-mf="volume">
          Tách nền xanh<select data-mf="chroma">
            <option value="auto" ${m.chroma === null ? "selected" : ""}>Tự động</option>
            <option value="on" ${m.chroma === true ? "selected" : ""}>Bật</option>
            <option value="off" ${m.chroma === false ? "selected" : ""}>Tắt</option></select></label>
        <button class="ghost danger sm rm full" data-mrm>Xóa meme</button>
      </div>
    </div>`).join("");
}

$("#memeList").addEventListener("input", (e) => {
  const f = e.target.closest("[data-mf]");
  if (!f) return;
  const m = state.settings.memes[+f.closest("[data-mi]").dataset.mi];
  const k = f.dataset.mf;
  if (k === "chroma") m.chroma = { auto: null, on: true, off: false }[f.value];
  else m[k] = +f.value;
  if (k === "size" || k === "volume") f.previousElementSibling.textContent = Math.round(m[k] * 100) + "%";
});
$("#memeList").addEventListener("change", (e) => {
  if (e.target.closest("[data-mf]")) refreshPreview();
});
$("#memeList").onclick = (e) => {
  const card = e.target.closest("[data-mi]");
  if (!card) return;
  const i = +card.dataset.mi;
  if (e.target.closest("[data-mrm]")) {
    state.settings.memes.splice(i, 1);
    renderMemeList();
    refreshPreview();
  }
  const pos = e.target.closest("[data-pos]");
  if (pos) {
    e.preventDefault();
    state.settings.memes[i].position = pos.dataset.pos;
    renderMemeList();
    refreshPreview();
  }
};

// ── Xuất video ──

let currentRenderJob = null;
$("#renderBtn").onclick = async () => {
  if (!state.segments.length) return toast("Tạo kịch bản trước đã", true);
  if (state.segments.every((s) => !s.read_text.trim())) return toast("Kịch bản chưa có lời đọc", true);
  try {
    const job = await api("/api/render", { method: "POST", body: renderSpec() });
    currentRenderJob = job.id;
    toast("Đã bắt đầu xuất video. Lần đầu nạp giọng đọc có thể mất ~1 phút.");
    pollJobs();
  } catch (e) { toast(e.message, true); }
};

function updateRenderStatus(jobs) {
  const el = $("#renderStatus");
  const j = jobs.find((x) => x.id === currentRenderJob);
  if (!j) return el.classList.add("hidden");
  el.classList.remove("hidden");
  el.innerHTML = jobHtml(j) + (j.status === "done"
    ? `<button class="ghost sm" data-goto="videos" style="margin-top:6px">Xem video →</button>` : "");
}

// ───────────────────────── Video đã xuất ─────────────────────────

async function loadVideos() {
  const vids = await api("/api/videos");
  $("#videosGrid").innerHTML = vids.length ? vids.map((v) => `
    <div class="video-card" data-vid="${v.id}">
      <img class="poster" src="${esc(v.thumb_url || "")}" data-src="${esc(v.url)}" alt="" title="Bấm để phát">
      <div class="meta">
        <b>${esc(v.title)}</b>
        <span class="muted small">⏱ ${fmtDur(v.duration)} · 🎙 ${esc(v.config.voice)} · @${esc(v.username || "")}</span>
        <span class="muted small">${new Date(v.created_at.replace(" ", "T") + "Z").toLocaleString("vi-VN")}</span>
        <span class="muted small" title="${esc(v.config.background_file)} @ ${v.config.background_start}s">🎞 ${esc((v.config.background_file || "").split("/").pop().slice(0, 28))} · 🎵 ${esc((v.config.music_file || "không nhạc").split("/").pop().slice(0, 24))}</span>
      </div>
      <div class="actions">
        <a href="${esc(v.url)}" download><button>⬇ Tải về</button></a>
        <button class="danger" data-vdel title="Xóa">🗑</button>
      </div>
    </div>`).join("") : '<div class="empty panel muted">Chưa có video nào được xuất.</div>';
}
$("#videosGrid").onclick = async (e) => {
  const poster = e.target.closest(".poster");
  if (poster) {
    const v = document.createElement("video");
    v.src = poster.dataset.src;
    v.controls = true;
    v.autoplay = true;
    poster.replaceWith(v);
  }
  if (e.target.closest("[data-vdel]") && confirm("Xóa video này (cả file)?")) {
    await api(`/api/videos/${e.target.closest("[data-vid]").dataset.vid}`, { method: "DELETE" });
    loadVideos();
  }
};

// ───────────────────────── Khởi động ─────────────────────────

(async function init() {
  const s = state.settings;
  $("#sSpeed").value = s.speed;
  $("#speedVal").textContent = s.speed + "x";
  $("#sPrecision").value = s.precision;
  $("#sMusicVol").value = s.music_volume;
  $("#musicVolVal").textContent = Math.round(s.music_volume * 100) + "%";
  $$("#sTheme button").forEach((b) => b.classList.toggle("active", b.dataset.v === s.theme));
  try {
    await Promise.all([loadQueries(), loadVoices(), loadMaterials()]);
  } catch (e) { toast("Lỗi tải dữ liệu: " + e.message, true); }
  pollJobs();
  const tab = store.get("tab", "crawl");
  showTab(tab === "create" ? "library" : tab);
})();
