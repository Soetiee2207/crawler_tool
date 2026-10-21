// Threads → TikTok: khung giao diện, tác vụ nền, crawl, kho bài, video đã xuất.
// Trình dựng video nằm ở editor.js.
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
  pick: new Set(), // bài đang tick chọn trong Kho bài
  materials: { backgrounds: [], music: [], memes: [] },
  voices: [],
};

// ───────────────────────── Tabs ─────────────────────────

function showTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "library") loadPosts();
  if (name === "videos") loadVideos();
  if (name === "create") Editor.onShow();
  else Editor.pause();
  updatePickBar();
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
let firstPoll = true;
async function pollJobs() {
  clearTimeout(jobsTimer);
  let jobs = [];
  try { jobs = await api("/api/jobs"); } catch {}
  const active = jobs.filter((j) => j.status === "queued" || j.status === "running");
  $("#jobsBadge").textContent = active.length;
  $("#jobsBadge").classList.toggle("hidden", !active.length);
  $("#jobsPanel").innerHTML = jobs.length ? jobs.map(jobHtml).join("") : '<p class="muted small">Chưa có tác vụ nào.</p>';
  for (const j of jobs) {
    if ((j.status === "done" || j.status === "error") && !seenDone.has(j.id)) {
      seenDone.add(j.id);
      if (!firstPoll) onJobFinished(j);
    }
  }
  firstPoll = false;
  Editor.onJobs(jobs);
  jobsTimer = setTimeout(pollJobs, active.length ? 1200 : 4000);
}

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
    loadSuggestions();
    if ($("#tab-library").classList.contains("active")) loadPosts();
  } else {
    toast("Đã xuất video! Các bài đã dùng được xóa khỏi kho. Xem video ở tab Video đã xuất.");
    loadVideos();
    loadQueries();
    loadSuggestions();
    if ($("#tab-library").classList.contains("active")) loadPosts();
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

function customKeywords() {
  return $("#customKeywords").value.split("\n").map((s) => s.trim()).filter(Boolean);
}

function updateCrawlSummary() {
  const n = customKeywords().length;
  $("#crawlSummary").textContent = n ? `Sẽ crawl ${n} từ khóa` : "Chưa nhập từ khóa nào";
  const added = new Set(customKeywords().map((k) => k.toLowerCase()));
  $$("#tab-crawl [data-kw]").forEach((c) => c.classList.toggle("added", added.has(c.dataset.kw.toLowerCase())));
}

function toggleKeyword(kw, on) {
  const list = customKeywords();
  const has = list.some((k) => k.toLowerCase() === kw.toLowerCase());
  if (on ?? !has) {
    if (!has) list.push(kw);
  } else {
    list.splice(list.findIndex((k) => k.toLowerCase() === kw.toLowerCase()), 1);
  }
  $("#customKeywords").value = list.join("\n");
  updateCrawlSummary();
}

// ── Gợi ý từ khóa ──

let suggest = { groups: [], from_data: [], performance: [] };
let suggestGroup = store.get("suggestGroup", 0);

async function loadSuggestions() {
  try { suggest = await api("/api/keywords"); } catch { return; }
  renderSuggestions();
}

function kwChip(kw, extra = "") {
  return `<span class="chip" data-kw="${esc(kw)}">${esc(kw)}${extra}</span>`;
}

function renderSuggestions() {
  const g = suggest.groups[suggestGroup] || suggest.groups[0];
  $("#suggestTabs").innerHTML = suggest.groups.map((x, i) =>
    `<button class="${i === suggestGroup ? "active" : ""}" data-group="${i}">${x.emoji} ${esc(x.name)}</button>`).join("");
  $("#suggestBody").innerHTML = g
    ? g.keywords.map((k) => kwChip(k.keyword, k.crawled ? `<span class="done" title="Đã crawl ${k.crawled} bài">✓${k.crawled}</span>` : "")).join("") +
      `<button class="ghost sm" data-addgroup>+ Thêm cả nhóm</button>`
    : "";
  $("#suggestData").innerHTML = suggest.from_data.length
    ? suggest.from_data.map((d) => kwChip(d.keyword, `<span class="cnt">${d.count}</span>`)).join("")
    : '<span class="muted small">Crawl thêm để có gợi ý từ dữ liệu thật.</span>';
  const maxAvg = Math.max(1, ...suggest.performance.map((p) => p.avg_likes));
  $("#suggestPerf").innerHTML = suggest.performance.length ? `<table class="perf">
    <tr><th>Từ khóa</th><th class="num">Bài</th><th class="num">Like TB</th><th class="num">Cao nhất</th><th class="num">Đã làm</th></tr>
    ${suggest.performance.map((p) => `<tr>
      <td><span class="chip" data-kw="${esc(p.query)}" title="Bấm để crawl lại">${esc(p.query)}</span>
        <div class="bar" style="width:${Math.round((p.avg_likes / maxAvg) * 100)}%"></div></td>
      <td class="num">${p.posts}</td><td class="num">${fmt(p.avg_likes)}</td><td class="num">${fmt(p.max_likes)}</td><td class="num">${p.used}</td></tr>`).join("")}
  </table>` : '<span class="muted small">Chưa crawl từ khóa nào.</span>';
  updateCrawlSummary();
}

$("#tab-crawl").addEventListener("click", (e) => {
  const tab = e.target.closest("[data-group]");
  if (tab) {
    suggestGroup = +tab.dataset.group;
    store.set("suggestGroup", suggestGroup);
    return renderSuggestions();
  }
  if (e.target.closest("[data-addgroup]")) {
    suggest.groups[suggestGroup].keywords.forEach((k) => toggleKeyword(k.keyword, true));
    return;
  }
  const chip = e.target.closest("[data-kw]");
  if (chip) toggleKeyword(chip.dataset.kw);
});
$("#customKeywords").oninput = updateCrawlSummary;

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
  const cur = $("#fQuery").value;
  $("#fQuery").innerHTML = '<option value="">Tất cả từ khóa</option>' +
    state.queries.map((q) => `<option>${esc(q.query)}</option>`).join("");
  $("#fQuery").value = cur;
}

// ───────────────────────── Kho bài ─────────────────────────

let postsOffset = 0;
let postsSeq = 0;
async function loadPosts(append = false) {
  if (!append) postsOffset = 0;
  const seq = ++postsSeq;
  const params = new URLSearchParams({ sort: $("#fSort").value, limit: 30, offset: postsOffset });
  for (const [k, id] of [["query", "#fQuery"], ["q", "#fSearch"]]) {
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
  const picked = state.pick.has(p.code);
  const inVideo = Editor.hasPost(p.code);
  return `<div class="post-card ${picked ? "picked" : ""}" data-code="${esc(p.code)}">
    <label class="pick"><input type="checkbox" data-act="pick" ${picked ? "checked" : ""}> Chọn</label>
    <div class="post-head">${avatarHtml(p)}
      <div class="who"><b>${esc(p.username)}${p.user_verified ? " ✔️" : ""}</b>
        <span class="muted small">${p.published_at ? new Date(p.published_at).toLocaleDateString("vi-VN") : ""}</span></div>
    </div>
    <div class="post-text">${esc(p.text)}</div>
    ${imgs.length ? `<div class="post-media">${imgs.map((m) => `<img src="${esc(m.src)}" loading="lazy">`).join("")}</div>` : ""}
    <div class="stats"><span>❤️ ${fmt(p.like_count)}</span><span>💬 ${fmt(p.reply_count)}</span><span>📥 ${p.saved_replies} đã lưu</span></div>
    <div class="tags">
      <span class="tag">🔎 ${esc(p.query)}</span>
      ${inVideo ? '<span class="tag ok">● trong video đang làm</span>' : ""}
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
  const card = btn.closest("[data-code]");
  const code = card.dataset.code;
  if (btn.dataset.act === "pick") {
    btn.checked ? state.pick.add(code) : state.pick.delete(code);
    card.classList.toggle("picked", btn.checked);
    updatePickBar();
  }
  if (btn.dataset.act === "make") Editor.createFromPosts([code]);
  if (btn.dataset.act === "view") openDrawer(code);
  if (btn.dataset.act === "del" && confirm("Xóa bài này và bình luận của nó khỏi kho?")) {
    await api(`/api/posts/${code}`, { method: "DELETE" });
    state.pick.delete(code);
    loadPosts();
    updatePickBar();
  }
};
for (const id of ["#fQuery", "#fSort"]) $(id).onchange = () => loadPosts();
let searchTimer;
$("#fSearch").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadPosts(), 300); };
$("#loadMore").onclick = () => loadPosts(true);

function updatePickBar() {
  const n = state.pick.size;
  const show = n > 0 && $("#tab-library").classList.contains("active");
  $("#pickBar").classList.toggle("hidden", !show);
  $("#pickCount").textContent = `Đã chọn ${n} bài`;
  $("#pickAdd").classList.toggle("hidden", !Editor.hasProject());
}
$("#pickClear").onclick = () => {
  state.pick.clear();
  $$("#postsGrid .post-card").forEach((c) => {
    c.classList.remove("picked");
    $("[data-act=pick]", c).checked = false;
  });
  updatePickBar();
};
$("#pickCreate").onclick = () => {
  const codes = [...state.pick];
  state.pick.clear();
  Editor.createFromPosts(codes);
};
$("#pickAdd").onclick = () => {
  const codes = [...state.pick];
  state.pick.clear();
  Editor.addPosts(codes);
};

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
    <button class="primary big" id="drawerMake">🎬 Làm video từ bài này</button>`;
  $("#drawerMake").onclick = () => { $("#drawer").classList.add("hidden"); Editor.createFromPosts([p.code]); };
  $("#drawer").classList.remove("hidden");
}
$("#drawerClose").onclick = () => $("#drawer").classList.add("hidden");
$("#drawer").onclick = (e) => { if (e.target.id === "drawer") $("#drawer").classList.add("hidden"); };

function replyHtml(r, post, order = 0) {
  const self = r.username === post.username;
  return `<div class="reply ${order ? "on" : ""}" data-reply="${esc(r.code)}">
    ${order !== null ? `<div class="order">${order || ""}</div>` : ""}
    <div class="body">
      <b>${esc(r.username)}</b>${self ? '<span class="self">tác giả viết tiếp</span>' : ""}
      ${r.reply_to_username && !self ? `<span class="muted small"> → @${esc(r.reply_to_username)}</span>` : ""}
      <span class="muted small"> · ❤️ ${fmt(r.like_count)}</span>
      <p>${esc(r.text)}</p>
    </div></div>`;
}

// ───────────────────────── Video đã xuất ─────────────────────────

async function loadVideos() {
  const vids = await api("/api/videos");
  $("#videosGrid").innerHTML = vids.length ? vids.map((v) => `
    <div class="video-card" data-vid="${v.id}">
      <img class="poster" src="${esc(v.thumb_url || "")}" data-src="${esc(v.url)}" alt="" title="Bấm để phát">
      <div class="meta">
        <b>${esc(v.title)}</b>
        <span class="muted small">⏱ ${fmtDur(v.duration)} · 🎙 ${esc(v.config.voice)} · @${esc(v.username || "")}${(v.config.post_codes || []).length > 1 ? ` +${v.config.post_codes.length - 1} bài` : ""}</span>
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

window.addEventListener("DOMContentLoaded", async () => {
  try {
    const [, voices, materials] = await Promise.all([loadQueries(), api("/api/voices"), api("/api/materials")]);
    state.voices = voices;
    state.materials = materials;
  } catch (e) { toast("Lỗi tải dữ liệu: " + e.message, true); }
  Editor.init();
  loadSuggestions();
  pollJobs();
  showTab(store.get("tab", "crawl"));
});
