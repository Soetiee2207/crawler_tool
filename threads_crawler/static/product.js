// Tab Sản phẩm: gửi thông tin sản phẩm, rồi mở bài vừa tạo trong trình dựng.
(() => {
  const readImage = (file) => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(new Error("Không đọc được ảnh"));
    r.readAsDataURL(file);
  });

  let fetchedImage = null;

  async function waitJob(id) {
    for (;;) {
      const j = await api(`/api/jobs/${id}`);
      if (j.status === "done") return j.result;
      if (j.status === "error" || j.status === "cancelled") throw new Error(j.error || "Đã dừng lấy thông tin");
      $("#prodFetchInfo").textContent = j.message;
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  $("#prodFetch").onclick = async () => {
    const btn = $("#prodFetch");
    btn.disabled = true;
    try {
      const job = await api("/api/products/fetch", { method: "POST", body: { url: $("#prodUrl").value } });
      const r = await waitJob(job.id);
      $("#prodName").value = r.name;
      $("#prodPrice").value = r.price;
      $("#prodIntro").value = r.intro;
      $("#prodSource").value = "Shopee";
      $("#prodReviews").value = r.reviews;
      fetchedImage = r.image;
      $("#prodFetchInfo").textContent = `Đã lấy ${r.review_count} đánh giá chữ${r.image ? " và ảnh sản phẩm" : ""}.`
        + (r.with_video ? ` ${r.with_video} đánh giá có video trên Shopee; video của người mua không được tải về.` : "")
        + " Hãy viết lại lời giới thiệu theo cách của bạn.";
    } catch (e) {
      $("#prodFetchInfo").textContent = "";
      toast(e.message, true);
    }
    btn.disabled = false;
  };

  const clipInput = $("#clipUpload");
  clipInput.onchange = async () => {
    const file = clipInput.files[0];
    if (!file) return;
    toast("Đang thêm video...");
    try {
      const res = await fetch(`/api/materials/upload?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || "Không thêm được video");
      state.materials = await api("/api/materials");
      Editor.renderMemeLib();
      toast("Đã thêm vào nhóm \"video của tôi\". Kéo vào hàng Meme trên timeline.");
    } catch (e) { toast(e.message, true); }
    clipInput.value = "";
  };

  $("#prodCreate").onclick = async () => {
    const btn = $("#prodCreate"), val = (s) => $(s).value.trim();
    if (!val("#prodName") || !val("#prodIntro") || !val("#prodSource") || !val("#prodReviews")) {
      return toast("Cần nhập tên, lời giới thiệu, nguồn và đánh giá", true);
    }
    btn.disabled = true;
    try {
      const file = $("#prodImage").files[0];
      const { code } = await api("/api/products", {
        method: "POST",
        body: {
          name: val("#prodName"), intro: val("#prodIntro"), price: val("#prodPrice"), source: val("#prodSource"),
          reviews: $("#prodReviews").value, cta: val("#prodCta"), image: file ? await readImage(file) : fetchedImage,
        },
      });
      ["#prodName", "#prodIntro", "#prodPrice", "#prodReviews", "#prodImage"].forEach((s) => ($(s).value = ""));
      fetchedImage = null;
      await Editor.createFromPosts([code]);
    } catch (e) { toast(e.message, true); }
    btn.disabled = false;
  };
})();
