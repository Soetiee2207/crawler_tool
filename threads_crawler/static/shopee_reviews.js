// Tab Review Shopee: chạy thu thập, theo dõi tiến độ, xem review và video đã lấy.
(() => {
  const JOB_PREFIX = "Review Shopee";
  const ITEM_LINK = /^https?:\/\/([\w-]+\.)?shopee\.vn\/\S*(-i\.\d+\.\d+|\/product\/\d+\/\d+)/i;
  const PERMISSIONS = { none: "Chưa xin phép", asked: "Đã hỏi, chờ trả lời", granted: "Đã đồng ý", denied: "Từ chối" };
  const STATUS = {
    exhausted: ["Đã lấy hết", "ok"],
    limit: ["Đủ số lượng", "ok"],
    browser: ["Từ Chrome", "ok"],
    truncated: ["Còn sót", "warn"],
    blocked: ["Bị chặn", "bad"],
    error: ["Lỗi", "bad"],
  };

  let products = [];
  let selected = store.get("srSelected", null);
  let page = 1;
  let jobId = null;
  let searchTimer;
  let loadSeq = 0;
  let shownTotal = null;

  const lines = () => $("#srUrls").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const current = () => products.find((p) => p.stem === selected);
  const statusTag = (s) => {
    const [text, cls] = STATUS[s] || ["Chưa xong", "warn"];
    return `<span class="sr-status ${cls}">${text}</span>`;
  };

  function renderForm() {
    const urls = lines(), bad = urls.filter((u) => !ITEM_LINK.test(u)).length;
    const info = $("#srUrlsInfo");
    info.textContent = !urls.length ? "" : bad
      ? `${urls.length - bad} link hợp lệ, ${bad} link chưa đúng. Link rút gọn s.shopee.vn không dùng được, hãy mở rồi chép link đầy đủ.`
      : `${urls.length} sản phẩm`;
    info.classList.toggle("sr-bad", bad > 0);
    const on = $("#srVideos").checked;
    $("#srVideosInfo").textContent = on
      ? "Đang bật: tải file video về máy."
      : "Đang tắt: chỉ lưu link video.";
    $("#srRights").classList.toggle("hidden", !on);
    $("#srStart").textContent = on ? "Bắt đầu thu thập và tải video" : "Bắt đầu thu thập";
  }

  async function start(body) {
    try {
      await api("/api/shopee-reviews", { method: "POST", body });
      toast("Đã đưa vào hàng đợi. Cửa sổ trình duyệt sẽ mở khi tới lượt.");
      pollJobs();
      return true;
    } catch (e) {
      toast(e.message, true);
      return false;
    }
  }

  const tuning = () => ({
    delay: +$("#srDelay").value, timeout: +$("#srTimeout").value, retries: +$("#srRetries").value,
  });

  $("#srStart").onclick = async () => {
    const limit = +$("#srLimit").value;
    store.set("srForm", { limit, videos: $("#srVideos").checked });
    if (await start({
      urls: lines(), limit, download_videos: $("#srVideos").checked, refresh: $("#srRefresh").checked, ...tuning(),
    })) $("#srUrls").value = "";
    renderForm();
  };

  function renderProducts() {
    const box = $("#srProducts");
    box.innerHTML = products.length ? products.map((p) => `
      <button class="sr-product ${p.stem === selected ? "on" : ""}" data-stem="${esc(p.stem)}">
        <span class="sr-product-head"><b>${esc(p.name)}</b>${statusTag(p.status)}</span>
        <span class="muted small">${p.total} review · ${p.with_video} có video · đã chọn ${p.picked} · đã tải ${p.videos_saved} video${p.avg_rating ? ` · ★ ${p.avg_rating}` : ""}</span>
      </button>`).join("") : '<p class="muted small">Chưa có sản phẩm nào. Dán link ở trên rồi bấm Bắt đầu.</p>';
  }

  function renderPickBar() {
    const p = current(), n = p?.picked || 0;
    $("#srPickBar").classList.toggle("hidden", !n);
    if (!n) return;
    $("#srPickCount").textContent = `Đã chọn ${n} bình luận`;
    $("#srTestList").href = `/api/shopee-reviews/${encodeURIComponent(p.stem)}/test-list.csv`;
  }

  function renderDetailHead() {
    renderPickBar();
    const p = current();
    $("#srEmpty").classList.toggle("hidden", !!p);
    $("#srDetail").classList.toggle("hidden", !p);
    if (!p) return;
    $("#srName").textContent = p.name;
    $("#srMeta").innerHTML = `${statusTag(p.status)} Mã ${esc(p.product_id)} · ${p.total} review · ${p.with_video} có video · đã tải ${p.videos_saved}/${p.video_urls} video`
      + (p.status === "truncated" ? " · Shopee báo còn review chưa lấy được" : "");
    $("#srActions").innerHTML = `
      ${p.url ? `<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer"><button class="sm">Mở trên Shopee</button></a>` : ""}
      ${p.csv ? `<a href="${esc(p.csv)}" download><button class="sm">CSV</button></a>` : ""}
      ${p.jsonl ? `<a href="${esc(p.jsonl)}" download><button class="sm">JSONL</button></a>` : ""}`;
  }

  function reviewHtml(r) {
    const stars = r.rating ? "★".repeat(r.rating) + "☆".repeat(Math.max(0, 5 - r.rating)) : "";
    const date = r.review_date ? new Date(r.review_date).toLocaleDateString("vi-VN") : "";
    const clips = r.videos.map((v) => (v.local
      ? `<video src="${esc(v.local)}" controls preload="metadata"></video>`
      : `<a class="sr-clip-link" href="${esc(v.url)}" target="_blank" rel="noopener noreferrer">▶ Xem video gốc<em class="small">chưa tải về</em></a>`)).join("");
    const noLink = r.video_count > r.videos.length
      ? `<div class="muted small">${r.video_count - r.videos.length} video không có link dùng được.</div>` : "";
    const mark = r.mark || { picked: false, claim: "", permission: "none" };
    return `<article class="sr-review ${mark.picked ? "picked" : ""}" data-review="${esc(r.review_id)}">
      <div class="sr-review-head"><b>${esc(r.username || "Ẩn danh")}</b><span class="sr-stars">${stars}</span><span class="muted small">${esc(date)}</span></div>
      <div class="sr-text">${esc(r.review_content) || '<span class="muted">Không có nội dung chữ</span>'}</div>
      ${clips ? `<div class="sr-clips">${clips}</div>` : ""}${noLink}
      ${r.images.length ? `<div class="sr-pics">${r.images.map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer"><img src="${esc(u)}" loading="lazy" referrerpolicy="no-referrer" alt=""></a>`).join("")}</div>` : ""}
      <div class="sr-mark">
        <button class="sm sr-pick">${mark.picked ? "✓ Đã chọn" : "Chọn làm video"}</button>
        <input class="sr-claim" maxlength="500" placeholder="Nhận định cần test, ví dụ: pin dùng được 8 tiếng" value="${esc(mark.claim)}">
        <select class="sr-permission">${Object.entries(PERMISSIONS).map(([k, v]) => `<option value="${k}" ${k === mark.permission ? "selected" : ""}>${v}</option>`).join("")}</select>
      </div>
    </article>`;
  }

  async function loadReviews() {
    if (!current()) return;
    const seq = ++loadSeq; // đổi bộ lọc liên tiếp: bỏ response về muộn của lần trước
    const q = new URLSearchParams({
      page, video_only: $("#srVideoOnly").checked, picked_only: $("#srPickedOnly").checked, rating: $("#srRating").value || 0, q: $("#srSearch").value,
    });
    let d;
    try { d = await api(`/api/shopee-reviews/${encodeURIComponent(selected)}?${q}`); } catch (e) { return toast(e.message, true); }
    if (seq !== loadSeq) return;
    shownTotal = current()?.total ?? null;
    const rating = $("#srRating").value;
    $("#srRating").innerHTML = `<option value="0">Mọi số sao (${d.all})</option>`
      + [5, 4, 3, 2, 1].map((n) => `<option value="${n}">${n} sao (${d.rating_counts[n]})</option>`).join("");
    $("#srRating").value = rating || "0";
    $("#srList").innerHTML = d.items.map(reviewHtml).join("")
      || `<p class="empty muted">${d.all ? "Không có review nào khớp bộ lọc." : "Sản phẩm này chưa có review nào được lấy."}</p>`;
    const pages = Math.max(1, Math.ceil(d.total / d.size));
    $("#srPager").innerHTML = pages < 2 ? "" : `<button class="sm" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>← Trước</button>
      <span class="muted small">Trang ${page}/${pages} · ${d.total} review</span>
      <button class="sm" data-page="${page + 1}" ${page >= pages ? "disabled" : ""}>Sau →</button>`;
  }

  async function refresh() {
    let d;
    try { d = await api("/api/shopee-reviews"); } catch (e) { return toast(e.message, true); }
    products = d.items;
    if (!current()) selected = products[0]?.stem ?? null;
    $("#srAll").innerHTML = d.all_csv
      ? `Tải tất cả: <a href="${esc(d.all_csv)}" download>CSV</a> · <a href="${esc(d.all_jsonl)}" download>JSONL</a>` : "";
    renderProducts();
    renderDetailHead();
    await loadReviews();
  }

  function select(stem) {
    selected = stem;
    store.set("srSelected", stem);
    page = 1;
    $("#srVideoOnly").checked = false;
    $("#srPickedOnly").checked = false;
    $("#srRating").value = "0";
    $("#srSearch").value = "";
    renderProducts();
    renderDetailHead();
    loadReviews();
  }

  function onJobs(jobs) {
    const j = jobs.find((x) => x.title.startsWith(JOB_PREFIX) && (x.status === "queued" || x.status === "running"));
    const wasRunning = jobId !== null;
    jobId = j?.id ?? null;
    $("#srJob").classList.toggle("hidden", !j);
    if (!j) {
      if (wasRunning) refresh();
      return;
    }
    $("#srJobTitle").textContent = j.title;
    $("#srJob .progress > div").style.width = Math.round(j.progress * 100) + "%";
    $("#srJobMsg").textContent = j.status === "queued" ? "Đang chờ tác vụ crawl khác xong…" : j.message;
    $("#srJob").classList.toggle("attention", /đăng nhập|xác minh/i.test(j.message));
    $("#srJobStop").classList.toggle("hidden-btn", j.cancel_requested);
  }

  $("#srUrls").oninput = renderForm;
  $("#srVideos").onchange = renderForm;
  $("#srJobStop").onclick = () => jobId && cancelJob(jobId);
  $("#srProducts").addEventListener("click", (e) => {
    const b = e.target.closest("[data-stem]");
    if (b) select(b.dataset.stem);
  });
  $("#srPickStudio").onclick = () => { if (current()) window.ReviewStudio?.addPicked(current().stem); };
  $("#srPickClear").onclick = async () => {
    const p = current();
    if (!p) return;
    try {
      await api(`/api/shopee-reviews/${encodeURIComponent(p.stem)}/picks`, { method: "DELETE" });
      await refresh();
    } catch (e) { toast(e.message, true); }
  };
  $("#srPickVideos").onclick = async () => {
    const p = current();
    if (!p) return;
    try {
      await api(`/api/shopee-reviews/${encodeURIComponent(p.stem)}/download-picked`, { method: "POST" });
      pollJobs();
    } catch (e) { toast(e.message, true); }
  };
  $("#srPager").addEventListener("click", (e) => {
    const b = e.target.closest("[data-page]");
    if (b && !b.disabled) { page = +b.dataset.page; loadReviews(); }
  });
  async function saveMark(el) {
    const box = el.closest(".sr-review"), picked = box.classList.contains("picked");
    try {
      await api(`/api/shopee-reviews/${encodeURIComponent(selected)}/marks/${encodeURIComponent(box.dataset.review)}`, {
        method: "PUT",
        body: { picked, claim: $(".sr-claim", box).value, permission: $(".sr-permission", box).value },
      });
    } catch (e) { toast(e.message, true); }
  }
  $("#srList").addEventListener("click", async (e) => {
    const b = e.target.closest(".sr-pick");
    if (!b) return;
    const on = b.closest(".sr-review").classList.toggle("picked");
    b.textContent = on ? "✓ Đã chọn" : "Chọn làm video";
    await saveMark(b);
    const p = current();
    if (p) { p.picked += on ? 1 : -1; renderProducts(); renderPickBar(); }
  });
  $("#srList").addEventListener("change", (e) => {
    if (e.target.matches(".sr-claim, .sr-permission")) saveMark(e.target);
  });

  // Extension ghi review khi người dùng duyệt: cập nhật danh sách, không vẽ lại khi đang gõ hoặc xem video.
  setInterval(async () => {
    if (!$("#tab-shopee").classList.contains("active") || document.hidden) return;
    let d;
    try { d = await api("/api/shopee-reviews"); } catch { return; }
    const busy = $("#srList").contains(document.activeElement) || $$("#srList video").some((v) => !v.paused);
    products = d.items;
    if (!current()) selected = products[0]?.stem ?? null;
    if (!document.activeElement?.closest?.("#srProducts")) renderProducts();
    renderDetailHead();
    if (current() && current().total !== shownTotal && !busy) loadReviews();
  }, 4000);

  const refilter = () => { page = 1; loadReviews(); };
  $("#srVideoOnly").onchange = refilter;
  $("#srPickedOnly").onchange = refilter;
  $("#srRating").onchange = refilter;
  $("#srSearch").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(refilter, 300); };

  const saved = store.get("srForm", {});
  if (saved.limit) $("#srLimit").value = saved.limit;
  $("#srVideos").checked = !!saved.videos;
  renderForm();
  refresh();

  window.ShopeeTab = { refresh, onJobs };
})();
