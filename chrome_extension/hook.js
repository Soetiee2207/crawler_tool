// Chạy trong trang Shopee: sao một bản các response đánh giá mà trang tự tải. Không gửi request nào mới, không thao tác trên trang.
(() => {
  const isRating = (url) => /\/api\//.test(url) && /rating/i.test(url);
  const send = (url, body) => {
    if (typeof body === "string" && body) window.postMessage({ source: "shopee-review-capture", url, body }, location.origin);
  };

  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    const res = await origFetch.call(this, input, init);
    try {
      const url = new URL(res.url || input?.url || input, location.href).href;
      if (isRating(url)) res.clone().text().then((body) => send(url, body), () => {});
    } catch {}
    return res;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    try {
      const full = new URL(url, location.href).href;
      if (isRating(full)) {
        this.addEventListener("load", () => {
          try {
            if (this.responseType === "" || this.responseType === "text") send(full, this.responseText);
            else if (this.responseType === "json") send(full, JSON.stringify(this.response));
          } catch {}
        });
      }
    } catch {}
    return origOpen.call(this, method, url, ...rest);
  };
})();
