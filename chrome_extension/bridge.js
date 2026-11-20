// Chuyển dữ liệu từ hook.js (trong trang) sang background.js (được gọi tới app trên máy).
window.addEventListener("message", (e) => {
  if (e.source !== window || e.data?.source !== "shopee-review-capture") return;
  chrome.runtime.sendMessage({ type: "rating", url: e.data.url, body: e.data.body }).catch(() => {});
});
