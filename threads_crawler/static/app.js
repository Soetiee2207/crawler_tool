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
  if (name === "shopee") window.ShopeeTab?.refresh();
  if (name === "studio") window.ReviewStudio?.refresh();
  else window.ReviewStudio?.pause();
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
  renderJobs(jobs);
  renderJobNow(active);
  for (const j of jobs) {
    if (["done", "error", "cancelled"].includes(j.status) && !seenDone.has(j.id)) {
      seenDone.add(j.id);
      if (!firstPoll) onJobFinished(j);
    }
  }
  firstPoll = false;
  Editor.onJobs(jobs);
  window.ShopeeTab?.onJobs(jobs);
  jobsTimer = setTimeout(pollJobs, active.length ? 1200 : 4000);
}

// Bảng tác vụ cập nhật tại chỗ (không vẽ lại cả bảng mỗi lần hỏi tiến độ) để nút Dừng bấm được,
// phần chi tiết đang mở và vị trí cuộn nhật ký không bị mất.
const openJobs = new Set();
const isActive = (j) => j.status === "queued" || j.status === "running";
const clock = (t) => new Date(t * 1000).toLocaleTimeString("vi-VN", { hour12: false });

function jobStatus(j) {
  if (j.status === "running" && j.cancel_requested) return "Đang dừng…";
  return { queued: "Đang chờ", running: Math.round(j.progress * 100) + "%", done: "✓ Xong", error: "✕ Lỗi",
    cancelled: "⏹ Đã dừng" }[j.status];
}

function jobDetailHtml(j) {
  const end = j.finished_at || Date.now() / 1000;
  const mins = j.started_at ? Math.max(0, Math.round((end - j.started_at) / 60)) : 0;
  const r = j.result || {};
  const skipped = Object.entries(r.skipped || {}).map(([why, n]) => `${n} ${why}`).join(", ");
  return `<div class="muted">Tạo lúc ${clock(j.created_at)}${j.started_at ? ` · bắt đầu ${clock(j.started_at)} · ${mins} phút` : ""}</div>
    ${j.detail.length ? `<ul>${j.detail.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
    ${r.kind === "shopee_reviews" ? `<div>Kết quả: thêm ${r.added} đánh giá, tải ${r.videos_saved} video${r.videos_failed ? `, lỗi ${r.videos_failed} video` : ""}</div>
      ${r.problems.map((e) => `<div class="job-msg error">${esc(e)}</div>`).join("")}` : ""}
    ${j.kind === "crawl" && j.status === "done" && r.kind !== "shopee_reviews" ? `<div>Kết quả: ${r.posts} bài, ${r.replies} bình luận${skipped ? ` · đã loại ${esc(skipped)}` : ""}</div>
      ${(r.errors || []).map((e) => `<div class="job-msg error">${esc(e)}</div>`).join("")}` : ""}
    <div class="job-log">${j.log.map(([t, m]) => `${clock(t)}  ${esc(m)}`).join("\n") || "Chưa chạy"}</div>`;
}

function renderJobs(jobs) {
  const panel = $("#jobsPanel");
  if (!jobs.length) {
    panel.innerHTML = '<p class="muted small">Chưa có tác vụ nào.</p>';
    return;
  }
  panel.querySelector(":scope > p")?.remove();
  const ids = new Set(jobs.map((j) => String(j.id)));
  $$(".job", panel).forEach((el) => ids.has(el.dataset.job) || el.remove());
  jobs.forEach((j, i) => {
    let el = $(`.job[data-job="${j.id}"]`, panel);
    if (!el) {
      el = document.createElement("div");
      el.className = "job";
      el.dataset.job = j.id;
      el.innerHTML = `<div class="job-title" data-togglejob title="Bấm để xem chi tiết"><span class="t"></span><span class="st muted"></span></div>
        <div class="job-msg"></div>
        <div class="job-bar"><div class="progress"><div></div></div>
          <button class="sm danger stop" data-canceljob="${j.id}">⏹ Dừng</button></div>
        <div class="job-detail"></div>`;
    }
    if (panel.children[i] !== el) panel.insertBefore(el, panel.children[i] || null);
    $(".t", el).textContent = j.title;
    $(".st", el).textContent = jobStatus(j);
    const msg = $(".job-msg", el);
    msg.textContent = j.error || j.message;
    msg.classList.toggle("error", j.status === "error");
    $(".job-bar", el).classList.toggle("hidden", !isActive(j));
    $(".progress > div", el).style.width = Math.round(j.progress * 100) + "%";
    $(".stop", el).classList.toggle("hidden-btn", !isActive(j) || j.cancel_requested);
    el.classList.toggle("open", openJobs.has(j.id));
    if (openJobs.has(j.id)) {
      const old = $(".job-log", el);
      const atBottom = !old || old.scrollTop + old.clientHeight >= old.scrollHeight - 4;
      const top = old?.scrollTop;
      $(".job-detail", el).innerHTML = jobDetailHtml(j);
      const log = $(".job-log", el);
      log.scrollTop = atBottom ? log.scrollHeight : top;
    }
  });
}

let runningJobId = null;
function renderJobNow(active) {
  const run = active.find((j) => j.status === "running");
  const waiting = active.filter((j) => j.status === "queued").length;
  $("#jobNow").classList.toggle("hidden", !run);
  runningJobId = run?.id ?? null;
  if (!run) return;
  $("#jobNow .t").textContent = `${run.title} — ${run.message}`;
  $("#jobNow .pct").textContent = jobStatus(run);
  $("#jobNow .q").textContent = waiting ? `+${waiting} chờ` : "";
  $("#jobNow .stop").classList.toggle("hidden-btn", run.cancel_requested);
}

async function cancelJob(id) {
  if (!confirm("Dừng tác vụ này? Phần đã crawl xong vẫn được giữ trong kho.")) return;
  try {
    await api(`/api/jobs/${id}/cancel`, { method: "POST" });
    toast("Đang dừng tác vụ… (dừng sau bước hiện tại)");
    pollJobs();
  } catch (err) { toast(err.message, true); }
}

function onJobFinished(j) {
  if (j.title.startsWith("Review Shopee")) window.ShopeeTab?.refresh();
  if (j.result?.kind === "shopee_reviews") {
    const r = j.result;
    const text = `Thêm ${r.added} đánh giá, tải ${r.videos_saved} video` + (r.videos_failed ? `, lỗi ${r.videos_failed} video` : "");
    return r.blocked ? toast(`Shopee đã chặn nên tool dừng lại. ${text}. Phần đã lấy được vẫn còn, hãy chạy lại sau.`, true)
      : toast(r.problems.length ? `${text}. Có ${r.problems.length} sản phẩm lỗi, xem ở mục Tác vụ.` : `Xong: ${text}.`, r.problems.length > 0);
  }
  if (j.status === "error") return toast(`${j.title}: ${j.error}`, true);
  if (j.status === "cancelled") {
    toast(`Đã hủy: ${j.title}`);
    if (j.kind === "crawl") { loadQueries(); Editor.refreshPosts(); }
    if ($("#tab-library").classList.contains("active")) loadPosts();
    return;
  }
  if (j.kind === "crawl") {
    const r = j.result || {};
    const skipped = Object.entries(r.skipped || {}).map(([why, n]) => `${n} ${why}`).join(", ");
    toast(`Crawl xong: ${r.posts} bài, ${r.replies} bình luận` + (r.errors?.length ? ` (${r.errors.length} lỗi)` : "") +
      (skipped ? `. Đã loại: ${skipped}` : ""));
    loadQueries();
    loadSources();
    Editor.refreshPosts();
    if ($("#tab-library").classList.contains("active")) loadPosts();
  } else {
    toast("Đã xuất video! Xem video ở tab Video đã xuất.");
    loadVideos();
    loadQueries();
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
$("#jobsPanel").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-canceljob]");
  if (btn) return cancelJob(+btn.dataset.canceljob);
  const head = e.target.closest("[data-togglejob]");
  if (!head) return;
  const id = +head.closest(".job").dataset.job;
  openJobs.has(id) ? openJobs.delete(id) : openJobs.add(id);
  pollJobs();
});
$("#jobNow .stop").onclick = () => runningJobId && cancelJob(runningJobId);
$("#jobNow .t").onclick = (e) => {
  e.stopPropagation();
  if (runningJobId) openJobs.add(runningJobId);
  $("#jobsPanel").classList.remove("hidden");
  pollJobs();
};

// ───────────────────────── Crawl ─────────────────────────

const lines = (sel) => $(sel).value.split("\n").map((s) => s.trim()).filter(Boolean);

function updateCrawlSummary() {
  const parts = [[lines("#crawlUrls").length, "link bài"], [lines("#crawlUsers").length, "tài khoản"]]
    .filter(([n]) => n).map(([n, what]) => `${n} ${what}`);
  $("#crawlSummary").textContent = parts.length ? `Sẽ crawl ${parts.join(", ")}` : "Chưa nhập gì để crawl";
  const users = new Set(lines("#crawlUsers").map(userKey));
  $$("#tab-crawl [data-user]").forEach((c) => c.classList.toggle("added", users.has(c.dataset.user.toLowerCase())));
}

// "@Ten", "ten", "https://www.threads.com/@ten" -> "ten"
const userKey = (s) => (s.match(/@([\w.]+)/)?.[1] ?? s).replace(/^@/, "").toLowerCase();

function toggleUser(name, on) {
  const list = lines("#crawlUsers");
  const i = list.findIndex((u) => userKey(u) === name.toLowerCase());
  if (on ?? i < 0) {
    if (i < 0) list.push("@" + name);
  } else {
    list.splice(i, 1);
  }
  $("#crawlUsers").value = list.join("\n");
  updateCrawlSummary();
}

// ── Tài khoản nguồn ──

let sources = { sources: [], suggested: [] };

async function loadSources() {
  try { sources = await api("/api/sources"); } catch { return; }
  renderSources();
}

// datetime('now') của sqlite là giờ UTC không kèm múi giờ
function ago(sqlTime) {
  if (!sqlTime) return "chưa crawl";
  const h = (Date.now() - new Date(sqlTime.replace(" ", "T") + "Z")) / 36e5;
  return h < 1 ? "vừa xong" : h < 24 ? `${Math.floor(h)} giờ trước` : `${Math.floor(h / 24)} ngày trước`;
}

function renderSources() {
  $("#sourceList").innerHTML = sources.sources.length ? `<table class="perf">
    <tr><th>Tài khoản</th><th class="num" title="Bài trong kho (đăng trong 14 ngày)">Trong kho</th>
      <th class="num">Like TB</th><th class="num">Đã làm</th><th>Crawl lần cuối</th><th></th></tr>
    ${sources.sources.map((s) => `<tr>
      <td><span class="chip" data-user="${esc(s.username)}">@${esc(s.username)}</span></td>
      <td class="num">${s.posts} (${s.fresh})</td><td class="num">${s.avg_likes == null ? "–" : fmt(s.avg_likes)}</td>
      <td class="num">${s.used}</td><td class="muted">${ago(s.last_crawled)}</td>
      <td><span class="x" data-unsource="${esc(s.username)}" title="Bỏ khỏi nguồn">✕</span></td></tr>`).join("")}
  </table>` : '<span class="muted small">Chưa có nguồn nào. Thêm tên tài khoản ở trên, chọn từ gợi ý, hoặc bấm 👤+ trên bài trong Kho bài.</span>';
  $("#sourceSuggest").innerHTML = sources.suggested.length
    ? sources.suggested.map((s) => `<span class="chip" data-addsource="${esc(s.username)}"
        title="${s.posts} bài, like TB ${fmt(s.avg_likes)}. Bấm để thêm làm nguồn">+ @${esc(s.username)}<span class="cnt">${s.posts}</span></span>`).join("")
    : '<span class="muted small">Crawl thêm để có gợi ý.</span>';
  updateCrawlSummary();
}

async function addSource(username) {
  try {
    const r = await api("/api/sources", { method: "POST", body: { username } });
    toast(`Đã thêm @${r.username} vào tài khoản nguồn`);
    loadSources();
    return true;
  } catch (e) { toast(e.message, true); }
}

$("#sourceAdd").onclick = async () => {
  const v = $("#sourceInput").value.trim();
  if (v && await addSource(v)) $("#sourceInput").value = "";
};
$("#sourceInput").onkeydown = (e) => { if (e.key === "Enter") $("#sourceAdd").click(); };
$("#addAllSources").onclick = () => sources.sources.forEach((s) => toggleUser(s.username, true));

$("#tab-crawl").addEventListener("click", async (e) => {
  const user = e.target.closest("[data-user]");
  if (user) return toggleUser(user.dataset.user);
  const add = e.target.closest("[data-addsource]");
  if (add) return addSource(add.dataset.addsource);
  const un = e.target.closest("[data-unsource]");
  if (un) {
    if (!confirm(`Bỏ @${un.dataset.unsource} khỏi tài khoản nguồn?`)) return;
    await api(`/api/sources/${encodeURIComponent(un.dataset.unsource)}`, { method: "DELETE" });
    return loadSources();
  }
});
$("#crawlUrls").oninput = $("#crawlUsers").oninput = updateCrawlSummary;

$("#startCrawl").onclick = async () => {
  try {
    const job = await api("/api/crawl", {
      method: "POST",
      body: {
        urls: lines("#crawlUrls"),
        users: lines("#crawlUsers"),
        limit: +$("#optLimit").value,
        replies: +$("#optReplies").value,
        expand_pages: +$("#optExpandPages").value,
        branches: +$("#optBranches").value,
        min_likes: +$("#optMinLikes").value,
        min_replies: +$("#optMinReplies").value,
        max_age_days: +$("#optMaxAge").value,
        clean: $("#optClean").checked,
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
  const cur = $("#fQuery").value;
  $("#fQuery").innerHTML = '<option value="">Tất cả nguồn</option>' +
    state.queries.map((q) => `<option>${esc(q.query)}</option>`).join("");
  $("#fQuery").value = cur;
}

// ───────────────────────── Kho bài ─────────────────────────

let postsOffset = 0;
let starredOnly = false; // chỉ xem bài yêu thích (gắn sao)
let postsSeq = 0;
async function loadPosts(append = false) {
  if (!append) postsOffset = 0;
  const seq = ++postsSeq;
  const params = new URLSearchParams({ sort: $("#fSort").value, limit: 30, offset: postsOffset });
  if (starredOnly) params.set("starred", "true");
  for (const [k, id] of [["query", "#fQuery"], ["q", "#fSearch"]]) {
    if ($(id).value) params.set(k, $(id).value);
  }
  const { items, total, starred } = await api("/api/posts?" + params);
  if (seq !== postsSeq) return; // một lần lọc mới hơn đã chạy
  state.posts = append ? [...state.posts, ...items] : items;
  state.postsTotal = total;
  postsOffset = state.posts.length;
  $("#postsCount").textContent = `${total} bài`;
  updateStarFilter(starred);
  $("#postsGrid").innerHTML = state.posts.length
    ? state.posts.map(postCardHtml).join("")
    : starredOnly
      ? '<div class="empty panel muted">Chưa có bài yêu thích nào. Bấm ☆ trên bài để lưu lại làm content sau.</div>'
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
      ${starBtn(p)}
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
      <button data-act="follow" title="Thêm @${esc(p.username)} vào tài khoản nguồn">👤+</button>
      <button class="danger" data-act="del" title="Xóa">🗑</button>
    </div>
  </div>`;
}

const starBtn = (p) => `<button class="star ${p.starred ? "on" : ""}" data-act="star"
  title="${p.starred ? "Bỏ khỏi yêu thích" : "Lưu vào yêu thích để làm content sau"}">${p.starred ? "★" : "☆"}</button>`;

function updateStarFilter(n) {
  $("#fStarred").classList.toggle("on", starredOnly);
  $("#fStarred").textContent = `${starredOnly ? "★" : "☆"} Yêu thích (${n})`;
}

async function toggleStar(code, btn) {
  const post = state.posts.find((p) => p.code === code);
  const on = !post?.starred;
  try {
    const r = await api(`/api/posts/${code}/star`, { method: "PUT", body: { starred: on } });
    if (post) post.starred = on;
    if (starredOnly && !on) return loadPosts();
    btn.outerHTML = starBtn({ starred: on });
    updateStarFilter(r.starred);
    toast(on ? "⭐ Đã thêm vào Yêu thích" : "Đã bỏ khỏi Yêu thích");
  } catch (e) { toast(e.message, true); }
}

$("#fStarred").onclick = () => {
  starredOnly = !starredOnly;
  loadPosts();
};

$("#clearPosts").onclick = async () => {
  if (!confirm("Xóa TẤT CẢ bài trong kho (kèm bình luận, ảnh/video)?\nBài đã lưu Yêu thích ★ sẽ được giữ lại.")) return;
  try {
    const { deleted } = await api("/api/posts", { method: "DELETE" });
    state.pick.clear();
    updatePickBar();
    await loadQueries();
    loadPosts();
    toast(`Đã xóa ${deleted} bài, giữ lại bài yêu thích.`);
  } catch (e) { toast(e.message, true); }
};

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
  if (btn.dataset.act === "star") return toggleStar(code, btn);
  if (btn.dataset.act === "make") Editor.createFromPosts([code]);
  if (btn.dataset.act === "view") openDrawer(code);
  if (btn.dataset.act === "follow") addSource(state.posts.find((p) => p.code === code).username);
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

// Phần tác giả viết tiếp bài (khác tác giả trả lời người bình luận). Dữ liệu cũ chưa có thread_code.
const isSelfThread = (r, post) => (r.thread_code ? r.thread_code === post.code : r.username === post.username);

// Cây bình luận theo thứ tự đọc trên Threads: mỗi tầng trả lời thụt vào thêm một nấc (tối đa 6 nấc).
function replyHtml(r, post, order = 0) {
  const self = isSelfThread(r, post);
  const depth = Math.min(r.depth || 0, 6);
  return `<div class="reply ${order ? "on" : ""} ${depth ? "sub" : ""}" data-reply="${esc(r.code)}"
    style="${depth ? `margin-left:${depth * 18}px` : ""}" ${r.depth > 6 ? `title="Tầng ${r.depth}"` : ""}>
    ${order !== null ? `<div class="order">${order || ""}</div>` : ""}
    <div class="body">
      <b>${esc(r.username)}</b>${self ? '<span class="self">tác giả viết tiếp</span>'
        : r.reply_parent && r.reply_to_username === r.username ? '<span class="self">viết tiếp</span>'
        : r.username === post.username ? '<span class="self">tác giả</span>' : ""}
      ${r.reply_to_username && !self && r.depth ? `<span class="muted small"> → @${esc(r.reply_to_username)}</span>` : ""}
      <span class="muted small"> · ❤️ ${fmt(r.like_count)}</span>
      <p>${esc(r.text)}</p>
      ${mediaThumbsHtml(r)}
    </div></div>`;
}

function mediaThumbsHtml(p) {
  const items = p.media.filter((m) => m.src);
  if (!items.length) return "";
  return `<div class="post-media">${items.map((m) => m.type === "video"
    ? `<video src="${esc(m.src)}" muted preload="metadata"></video>`
    : `<img src="${esc(m.src)}" loading="lazy">`).join("")}</div>`;
}

// ───────────────────────── Video đã xuất ─────────────────────────

const WEEKDAYS = ["Chủ nhật", "Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy"];

function dayLabel(day) {
  const d = new Date(day + "T00:00");
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((d - today) / 86400000);
  const rel = { 0: " · Hôm nay", 1: " · Ngày mai", "-1": " · Hôm qua" }[diff] ?? (diff < 0 ? " · đã qua" : "");
  return `📅 ${WEEKDAYS[d.getDay()]}, ${d.toLocaleDateString("vi-VN")}${rel}`;
}

function videoCardHtml(v) {
  const past = v.publish_at && new Date(v.publish_at) < new Date();
  return `<div class="video-card" data-vid="${v.id}">
      <img class="poster" src="${esc(v.thumb_url || "")}" data-src="${esc(v.url)}" alt="" title="Bấm để phát">
      <div class="meta">
        ${v.publish_at ? `<span class="when ${past ? "past" : ""}">🕖 ${esc(v.publish_at.slice(11))}${past ? " · đã tới giờ" : ""}</span>` : ""}
        <b>${esc(v.title)}</b>
        <span class="muted small">⏱ ${fmtDur(v.duration)} · 🎙 ${esc(v.config.voice)} · @${esc(v.username || "")}${(v.config.post_codes || []).length > 1 ? ` +${v.config.post_codes.length - 1} bài` : ""}</span>
        <span class="muted small" title="${esc(v.config.background_file)} @ ${v.config.background_start}s">🎞 ${esc((v.config.background_file || "").split("/").pop().slice(0, 28))} · 🎵 ${esc((v.config.music_file || "không nhạc").split("/").pop().slice(0, 24))}</span>
        <label class="field small"><span class="muted">Giờ đăng</span>
          <input type="datetime-local" data-vwhen value="${esc(v.publish_at || "")}"></label>
        <span class="muted small path" title="${esc(v.path)}">📁 ${esc(v.path.split("/").slice(-2).join("/"))}</span>
      </div>
      <div class="actions">
        <a href="${esc(v.url)}" download><button title="Tải về">⬇</button></a>
        <button data-vopen title="Mở thư mục chứa video">📂 Thư mục</button>
        <button data-vcopy title="Copy đường dẫn file">📋</button>
        <button class="danger" data-vdel title="Xóa">🗑</button>
      </div>
    </div>`;
}

let videosCache = [];
async function loadVideos() {
  const vids = (videosCache = await api("/api/videos"));
  if (!vids.length) {
    $("#videosGrid").innerHTML = '<div class="empty panel muted">Chưa có video nào được xuất.</div>';
    return;
  }
  const groups = new Map();
  for (const v of vids) {
    const key = v.publish_at ? v.publish_at.slice(0, 10) : "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(v);
  }
  $("#videosGrid").innerHTML = [...groups].map(([day, list]) => `
    <section class="video-day">
      <h3>${day ? dayLabel(day) : "🗂 Chưa hẹn giờ đăng"} <span class="muted small">${list.length} video</span>
        <code class="small" title="${esc(list[0].folder)}">📁 data/videos/${esc(list[0].folder.split("/").pop())}/</code></h3>
      <div class="videos-grid">${list.map(videoCardHtml).join("")}</div>
    </section>`).join("");
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
  const id = e.target.closest("[data-vid]")?.dataset.vid;
  if (!id) return;
  const vid = videosCache.find((v) => String(v.id) === id);
  if (e.target.closest("[data-vopen]")) {
    try { await api(`/api/videos/${id}/open`, { method: "POST" }); } catch (err) { toast(err.message, true); }
  }
  if (e.target.closest("[data-vcopy]")) {
    try {
      await navigator.clipboard.writeText(vid.path);
      toast("Đã copy đường dẫn video");
    } catch { prompt("Đường dẫn video:", vid.path); }
  }
  if (e.target.closest("[data-vdel]") && confirm("Xóa video này (cả file)?")) {
    await api(`/api/videos/${id}`, { method: "DELETE" });
    loadVideos();
  }
};
$("#videosGrid").addEventListener("change", async (e) => {
  if (!e.target.matches("[data-vwhen]")) return;
  const id = e.target.closest("[data-vid]").dataset.vid;
  try {
    await api(`/api/videos/${id}`, { method: "PATCH", body: { publish_at: e.target.value || null } });
    toast(e.target.value ? "Đã đổi giờ đăng, video đã chuyển sang thư mục của ngày đó" : "Đã bỏ hẹn giờ");
  } catch (err) { toast(err.message, true); }
  loadVideos();
});

// ───────────────────────── Khởi động ─────────────────────────

window.addEventListener("DOMContentLoaded", async () => {
  try {
    const [, voices, materials] = await Promise.all([loadQueries(), api("/api/voices"), api("/api/materials")]);
    state.voices = voices;
    state.materials = materials;
  } catch (e) { toast("Lỗi tải dữ liệu: " + e.message, true); }
  await Editor.init();
  loadSources();
  pollJobs();
  showTab(store.get("tab", "crawl"));
});
