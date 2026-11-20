// Tab Video review: ghép video và ảnh theo thứ tự thành video dọc. Không dùng chung với trình dựng Threads.
(() => {
  const draft = store.get("studioDraft", {});
  let items = draft.items || [];
  let lib = { reviews: [], uploads: [], waiting: {} };
  let selected = 0;
  let playing = false;
  let imageTimer = null;
  let dragFrom = null;

  const player = $("#rsPlayer"), still = $("#rsStill");
  const save = () => store.set("studioDraft", { items, title: $("#rsTitle").value, fit: $("#rsFit").value, voice: $("#rsVoice").value, speed: $("#rsSpeed").value });
  const baseLength = (it) => (it.kind === "image" ? it.duration : Math.max(0, (it.end ?? it.mediaDuration) - it.start));
  // chỉ biết thời lượng giọng sau khi đã bấm nghe; xuất video luôn đo lại
  const spoken = (it) => (it.voiceDur ? it.voiceDur / +$("#rsSpeed").value + 0.25 : 0);
  const lengthOf = (it) => Math.max(baseLength(it), spoken(it));
  const voiceAudio = new Audio();
  const round = (n) => Math.round(n * 10) / 10;

  function renderLibrary() {
    $("#rsReviews").innerHTML = lib.reviews.map((r) => `
      <div class="rs-review">
        <div class="small"><b>${esc(r.username || "Ẩn danh")}</b> <span class="sr-stars">${"★".repeat(r.rating || 0)}</span> <span class="muted">${esc(r.product)}</span></div>
        <div class="small rs-text">${esc(r.text)}</div>
        <div class="row gap wrap">
          ${Array.from({ length: r.videos }, (_, i) => `<button class="sm" data-add-video="${esc(r.stem)}|${esc(r.review_id)}|${i}">+ Video ${i + 1}</button>`).join("")}
          ${r.images.map((u, i) => `<button class="rs-thumb" title="Thêm ảnh" data-add-image="${esc(r.stem)}|${esc(r.review_id)}|${i}"><img src="${esc(u)}" loading="lazy" referrerpolicy="no-referrer" alt="Ảnh ${i + 1}"></button>`).join("")}
        </div>
      </div>`).join("") || '<p class="muted small">Chưa có review nào ở trạng thái Đã đồng ý. Đặt trạng thái này ở tab Review Shopee khi người đăng đã đồng ý.</p>';
    const waiting = Object.values(lib.waiting).reduce((a, b) => a + b, 0);
    $("#rsWaiting").textContent = waiting ? `${waiting} review đã chọn nhưng chưa có đồng ý nên chưa ghép được.` : "";
    $("#rsUploads").innerHTML = lib.uploads.map((u) => `
      <div class="rs-upload">
        <button class="rs-thumb" data-add-upload="${esc(u.id)}" title="Thêm vào video">${u.kind === "video"
          ? `<video src="${esc(u.url)}#t=0.1" preload="metadata" muted></video>` : `<img src="${esc(u.url)}" alt="">`}</button>
        <button class="sm danger" data-del-upload="${esc(u.id)}" title="Xóa file">✕</button>
      </div>`).join("") || '<p class="muted small">Video bạn tự quay, ảnh sản phẩm, ảnh kết quả test.</p>';
  }

  function renderItems() {
    const total = items.reduce((a, it) => a + lengthOf(it), 0);
    $("#rsTotal").textContent = items.length ? `${items.length} mục · ${fmtDur(total)}` : "";
    $("#rsExport").disabled = !items.length;
    $("#rsScript").disabled = !items.length;
    $("#rsEmpty").classList.toggle("hidden", items.length > 0);
    $("#rsList").innerHTML = items.map((it, i) => `
      <div class="rs-item ${i === selected ? "on" : ""}" draggable="true" data-i="${i}">
        <span class="rs-num muted small">${i + 1}</span>
        <div class="rs-mini">${it.kind === "video" ? `<video src="${esc(it.url)}#t=${it.start + 0.1}" preload="metadata" muted></video>` : `<img src="${esc(it.url)}" referrerpolicy="no-referrer" alt="">`}</div>
        <div class="rs-fields">
          <div class="small rs-label">${esc(it.label)}</div>
          <div class="row gap wrap small">
            ${it.kind === "video" ? `
              <label class="inline">Từ <input class="xs" type="number" step="0.1" min="0" max="${it.mediaDuration}" data-f="start" value="${round(it.start)}"></label>
              <label class="inline">đến <input class="xs" type="number" step="0.1" min="0" max="${it.mediaDuration}" data-f="end" value="${round(it.end ?? it.mediaDuration)}"></label>
              <label class="inline"><input type="checkbox" data-f="keep_audio" ${it.keep_audio ? "checked" : ""}> Giữ tiếng</label>`
            : `<label class="inline">Hiện <input class="xs" type="number" step="0.5" min="0.5" max="60" data-f="duration" value="${it.duration}"> giây</label>`}
          </div>
          <div class="rs-voice">
            <textarea rows="2" maxlength="1500" data-f="voice_text" placeholder="${it.source === "upload" ? "Lời đọc của bạn cho đoạn này, ví dụ kết quả test" : "Lời đọc cho đoạn này (để trống nếu không đọc)"}">${esc(it.voice_text || "")}</textarea>
            <button class="sm" data-listen="${i}" title="Nghe thử">🔊</button>
          </div>
          ${spoken(it) > baseLength(it) ? `<div class="muted small">Giọng đọc dài ${round(spoken(it))} giây, dài hơn đoạn này nên khung hình cuối sẽ được giữ.</div>` : ""}
        </div>
        <button class="sm danger" data-remove="${i}" title="Bỏ khỏi video">✕</button>
      </div>`).join("");
    save();
  }

  function stop() {
    playing = false;
    voiceAudio.pause();
    clearTimeout(imageTimer);
    player.pause();
    $("#rsPlay").textContent = "▶ Xem toàn bộ";
  }

  function show(i, autoplay) {
    clearTimeout(imageTimer);
    const it = items[i];
    if (!it) { player.classList.add("hidden"); still.classList.add("hidden"); return; }
    selected = i;
    $$("#rsList .rs-item").forEach((el, k) => el.classList.toggle("on", k === i));
    const isVideo = it.kind === "video";
    player.classList.toggle("hidden", !isVideo);
    still.classList.toggle("hidden", isVideo);
    const frame = $("#rsFrame");
    frame.classList.toggle("crop", $("#rsFit").value === "crop");
    if (isVideo) {
      if (!player.src.endsWith(it.url)) player.src = it.url;
      player.muted = !it.keep_audio;
      const go = () => { player.currentTime = it.start; if (autoplay) player.play().catch(() => {}); };
      player.readyState >= 1 ? go() : player.addEventListener("loadedmetadata", go, { once: true });
    } else {
      player.pause();
      still.src = it.url;
      if (autoplay) imageTimer = setTimeout(next, it.duration * 1000);
    }
  }

  function next() {
    if (!playing) return;
    if (selected + 1 >= items.length) { stop(); return; }
    show(selected + 1, true);
  }

  player.addEventListener("timeupdate", () => {
    const it = items[selected];
    if (playing && it?.kind === "video" && player.currentTime >= (it.end ?? it.mediaDuration)) next();
  });
  player.addEventListener("ended", next);

  function add(entry) {
    items.push({ keep_audio: true, start: 0, end: null, duration: 3, ...entry });
    selected = items.length - 1;
    renderItems();
    show(selected, false);
  }

  async function addReviewMedia(stem, reviewId, kind, index, silent) {
    const r = lib.reviews.find((x) => x.stem === stem && x.review_id === reviewId);
    const label = `${r?.username || "Ẩn danh"}: ${(r?.text || "").slice(0, 60)}`;
    try {
      const d = await api("/api/review-studio/fetch", { method: "POST", body: { stem, review_id: reviewId, kind, index } });
      add({
        source: kind === "video" ? "review_video" : "review_image", kind, stem, review_id: reviewId, index,
        url: d.url, mediaDuration: d.duration, label,
      });
      return true;
    } catch (e) {
      if (!silent) toast(e.message, true);
      return false;
    }
  }

  async function refresh() {
    try { lib = await api("/api/review-studio/library"); } catch (e) { return toast(e.message, true); }
    const allowed = (it) => it.source === "upload"
      ? lib.uploads.some((u) => u.id === it.upload_id)
      : lib.reviews.some((r) => r.stem === it.stem && r.review_id === it.review_id);
    const kept = items.filter(allowed);
    if (kept.length < items.length) toast(`Đã bỏ ${items.length - kept.length} mục: file đã xóa hoặc không còn trạng thái Đã đồng ý.`);
    items = kept;
    selected = Math.min(selected, Math.max(0, items.length - 1));
    renderLibrary();
    renderItems();
    show(selected, false);
  }

  $("#tab-studio").addEventListener("click", async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.dataset.addVideo || t.dataset.addImage) {
      const [stem, id, i] = (t.dataset.addVideo || t.dataset.addImage).split("|");
      t.disabled = true;
      await addReviewMedia(stem, id, t.dataset.addVideo ? "video" : "image", +i);
      t.disabled = false;
    } else if (t.dataset.addUpload) {
      const u = lib.uploads.find((x) => x.id === t.dataset.addUpload);
      if (u) add({ source: "upload", kind: u.kind, upload_id: u.id, url: u.url, mediaDuration: u.duration, label: "File của tôi" });
    } else if (t.dataset.delUpload) {
      if (!confirm("Xóa file này khỏi máy?")) return;
      try {
        await api(`/api/review-studio/uploads/${encodeURIComponent(t.dataset.delUpload)}`, { method: "DELETE" });
        await refresh();
      } catch (err) { toast(err.message, true); }
    } else if (t.dataset.listen !== undefined) {
      const it = items[+t.dataset.listen];
      if (!it?.voice_text?.trim()) return toast("Đoạn này chưa có lời đọc.", true);
      stop();
      t.disabled = true;
      toast("Đang tạo giọng đọc. Lần đầu có thể mất khoảng 1 phút.");
      try {
        const d = await api("/api/tts", { method: "POST", body: { text: it.voice_text, voice: $("#rsVoice").value, normalize: true } });
        it.voiceDur = d.duration;
        renderItems();
        voiceAudio.src = d.url;
        voiceAudio.playbackRate = +$("#rsSpeed").value;
        voiceAudio.play().catch(() => {});
      } catch (err) { toast(err.message, true); t.disabled = false; }
    } else if (t.dataset.remove !== undefined) {
      stop();
      items.splice(+t.dataset.remove, 1);
      selected = Math.min(selected, Math.max(0, items.length - 1));
      renderItems();
      show(selected, false);
    }
  });

  $("#rsList").addEventListener("change", (e) => {
    const f = e.target.dataset.f, row = e.target.closest(".rs-item");
    if (!f || !row || f === "voice_text") return;
    const it = items[+row.dataset.i];
    if (f === "keep_audio") it.keep_audio = e.target.checked;
    else {
      const max = f === "duration" ? 60 : it.mediaDuration;
      const v = Math.min(max, Math.max(f === "duration" ? 0.5 : 0, +e.target.value || 0));
      it[f] = v;
      if (it.kind === "video" && (it.end ?? it.mediaDuration) - it.start < 0.2) {
        toast("Đoạn cắt quá ngắn, đã đặt lại về hết video.", true);
        it.start = 0; it.end = null;
      }
    }
    selected = +row.dataset.i;
    renderItems();
    show(selected, false);
  });
  $("#rsList").addEventListener("input", (e) => {
    if (e.target.dataset.f !== "voice_text") return;
    const it = items[+e.target.closest(".rs-item").dataset.i];
    it.voice_text = e.target.value;
    it.voiceDur = null;
    save();
  });
  $("#rsList").addEventListener("click", (e) => {
    const row = e.target.closest(".rs-item");
    if (!row || e.target.closest("input, button, label")) return;
    stop();
    show(+row.dataset.i, false);
  });
  $("#rsList").addEventListener("dragstart", (e) => {
    if (!e.target.classList?.contains("rs-item")) { e.preventDefault(); return; } // kéo chọn chữ trong ô lời đọc
    dragFrom = +e.target.dataset.i;
  });
  $("#rsList").addEventListener("dragover", (e) => e.preventDefault());
  $("#rsList").addEventListener("drop", (e) => {
    e.preventDefault();
    const row = e.target.closest(".rs-item");
    if (!row || dragFrom === null || Number.isNaN(dragFrom)) return;
    const [moved] = items.splice(dragFrom, 1);
    items.splice(+row.dataset.i, 0, moved);
    selected = +row.dataset.i;
    dragFrom = null;
    renderItems();
    show(selected, false);
  });

  $("#rsPlay").onclick = () => {
    if (playing) return stop();
    if (!items.length) return;
    playing = true;
    $("#rsPlay").textContent = "⏸ Dừng";
    show(selected >= items.length - 1 ? 0 : selected, true);
  };
  $("#rsScript").onclick = async () => {
    if (items.some((it) => it.voice_text?.trim()) && !confirm("Thay toàn bộ lời đọc đang có bằng bản nháp mới?")) return;
    try {
      const { lines } = await api("/api/review-studio/script", {
        method: "POST",
        body: { items: items.map(({ source, stem, review_id }) => ({ source, stem: stem || "", review_id: review_id || "" })) },
      });
      items.forEach((it, i) => { it.voice_text = lines[i]; it.voiceDur = null; });
      renderItems();
      toast("Đã tạo bản nháp. Hãy đọc lại và sửa trước khi xuất.");
    } catch (e) { toast(e.message, true); }
  };
  $("#rsVoice").onchange = () => { items.forEach((it) => { it.voiceDur = null; }); renderItems(); };
  $("#rsSpeed").onchange = () => renderItems();
  $("#rsTitle").oninput = save;
  $("#rsFit").onchange = () => { save(); show(selected, false); };

  $("#rsUpload").onchange = async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    for (const file of files) {
      toast(`Đang thêm ${file.name}...`);
      try {
        const res = await fetch(`/api/review-studio/upload?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
        const d = await res.json();
        if (!res.ok) throw new Error(d.detail || "Không thêm được file");
      } catch (err) { toast(`${file.name}: ${err.message}`, true); }
    }
    await refresh();
  };

  $("#rsExport").onclick = async () => {
    const body = {
      title: $("#rsTitle").value, fit: $("#rsFit").value, voice: $("#rsVoice").value, speed: +$("#rsSpeed").value,
      items: items.map(({ source, stem, review_id, index, upload_id, start, end, duration, keep_audio, voice_text }) => ({
        source, stem: stem || "", review_id: review_id || "", index: index || 0, upload_id: upload_id || "",
        start, end, duration, keep_audio, voice_text: voice_text || "",
      })),
    };
    try {
      await api("/api/review-studio/render", { method: "POST", body });
      toast("Đang xuất video. Xong sẽ có ở tab Video đã xuất.");
      pollJobs();
    } catch (e) { toast(e.message, true); }
  };

  window.ReviewStudio = {
    refresh,
    pause: stop,
    async addPicked(stem) {
      await refresh();
      const picked = lib.reviews.filter((r) => r.stem === stem && r.picked);
      let added = 0;
      for (const r of picked) {
        for (let i = 0; i < r.videos; i++) added += (await addReviewMedia(stem, r.review_id, "video", i, true)) ? 1 : 0;
      }
      showTab("studio");
      const waiting = lib.waiting[stem] || 0;
      toast(`Đã thêm ${added} video.` + (waiting ? ` ${waiting} review chưa có đồng ý nên không được thêm.` : ""), !added);
    },
  };

  $("#rsTitle").value = draft.title || "";
  $("#rsFit").value = draft.fit || "blur";
  $("#rsSpeed").value = draft.speed || "1.1";
  api("/api/voices").then((voices) => {
    $("#rsVoice").innerHTML = voices.map((v) => `<option value="${esc(v.id)}">${esc(v.label)}</option>`).join("");
    $("#rsVoice").value = draft.voice || "Hải Đăng"; // cùng giọng mặc định với trình dựng Threads
    if (!$("#rsVoice").value && voices[0]) $("#rsVoice").value = voices[0].id;
  }).catch((e) => toast(e.message, true));
  renderItems();
  refresh();
})();
