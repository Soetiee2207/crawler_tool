// Gửi response đánh giá về app. Mặc định tắt; địa chỉ trang lấy từ tab thật, không tin dữ liệu do trang gửi.
const INGEST = "http://127.0.0.1:8000/api/shopee-reviews/ingest";

async function setBadge(enabled, error) {
  await chrome.action.setBadgeText({ text: error ? "!" : enabled ? "ON" : "" });
  await chrome.action.setBadgeBackgroundColor({ color: error ? "#d93025" : "#fe2c55" });
}

async function handle(msg, pageUrl) {
  const s = await chrome.storage.local.get({ enabled: false, added: 0, seen: 0, videos: 0 });
  if (!s.enabled) return;
  try {
    const res = await fetch(INGEST, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page_url: pageUrl, api_url: msg.url, body: msg.body }),
    });
    if (!res.ok) throw new Error(`App trả lỗi ${res.status}`);
    const d = await res.json();
    if (d.ignored) return;
    if (d.rejected) throw new Error("Shopee từ chối request đánh giá của trang.");
    await chrome.storage.local.set({
      added: s.added + d.added, seen: s.seen + d.reviews_seen, videos: s.videos + d.with_video,
      product: d.stem, total: d.total, error: "",
    });
    await setBadge(true, false);
  } catch (e) {
    const text = e instanceof TypeError ? "Không kết nối được app. Hãy chạy: uv run threads-crawler web" : e.message;
    await chrome.storage.local.set({ error: text });
    await setBadge(true, true);
  }
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg?.type === "rating" && typeof msg.body === "string" && sender.tab?.url) handle(msg, sender.tab.url);
});

chrome.storage.onChanged.addListener((changes) => {
  if (changes.enabled) setBadge(changes.enabled.newValue, false);
});
