const $ = (id) => document.getElementById(id);

async function render() {
  const s = await chrome.storage.local.get({ enabled: false, added: 0, seen: 0, videos: 0, product: "", total: 0, error: "" });
  $("enabled").checked = s.enabled;
  $("stats").textContent = "";
  $("stats").append(`Đã nhận ${s.seen} review (${s.videos} có video), lưu mới ${s.added}.`);
  if (s.product) $("stats").append(` Sản phẩm gần nhất ${s.product}: ${s.total} review.`);
  $("error").textContent = s.error;
}

$("enabled").onchange = () => chrome.storage.local.set({ enabled: $("enabled").checked, error: "" });
$("reset").onclick = () => chrome.storage.local.set({ added: 0, seen: 0, videos: 0, product: "", total: 0, error: "" });
chrome.storage.onChanged.addListener(render);
render();
