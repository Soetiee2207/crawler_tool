// Sân khấu 1080×1920 mở trong OBS (Browser Source). Chỉ vẽ lại trạng thái server gửi qua SSE.
// Vùng y < 270 và y > 1420 bị giao diện TikTok che (tên kênh, comment) nên chỉ vẽ nền ở đó.
const W = 1080, H = 1920, GROUND = 1180;
const FONT = '"Noto Sans", "Segoe UI", system-ui, sans-serif';
const ctx = document.getElementById("stage").getContext("2d");

let world = { level: 1, xp: 0, xp_next: 100, diamonds: 0, weather: "clear", night: false, buildings: [], next_building: null, top: [], villagers: [], test: false, status: "idle" };
let nightMix = 0;
let petJump = 0, petScale = 1, petEat = 0;
const villagers = new Map();
const particles = [];
const toasts = [];
// Chỗ cố định cho từng công trình [x, độ cao so với mặt đất, cỡ]: chừa giữa cho thú cưng, hàng sau vẽ trước.
const BUILDING_SLOTS = [[120, -15, 145], [950, -15, 145], [310, -30, 125], [775, -30, 125], [205, -145, 105], [875, -145, 105], [400, -165, 90], [680, -165, 90], [1000, -190, 110]];
let toast = null;
const clouds = Array.from({ length: 5 }, (_, i) => ({ x: i * 260, y: 300 + (i % 3) * 110, s: 0.7 + (i % 3) * 0.3 }));
const stars = Array.from({ length: 70 }, (_, i) => ({ x: (i * 373) % W, y: 40 + (i * 197) % 900, p: i }));

const rand = (a, b) => a + Math.random() * (b - a);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
const rgb = (c) => `rgb(${c[0]},${c[1]},${c[2]})`;
const hue = (s) => [...s].reduce((h, c) => (h + c.codePointAt(0)) * 137 % 360, 7);

// ── Âm thanh và nhân vật AI ─────────────────────────────────────────────────
// OBS tự phát được âm thanh. Xem trước trong tab LIVE thì im lặng tới khi bấm nút nghe thử (setStageAudio).
const PREVIEW = new URLSearchParams(location.search).has("preview");
let audioOn = !PREVIEW, audioBlocked = false, hostBroken = false;
let manifest = { music: [], sfx: {}, host: {} };
const settings = () => world.settings ?? {};
const music = new Audio();
let musicGroup = null, musicKey = "";
let lastSfx = 0, lastBlip = 0, audioCtx = null;

function playOrFlag(el) {
  return el.play().catch((e) => { if (e.name === "NotAllowedError") audioBlocked = !PREVIEW; });
}

function nextTrack() {
  const s = settings();
  const list = manifest.music.filter((m) => !s.music_group || m.group === s.music_group);
  const pool = list.length ? list : manifest.music;
  if (!pool.length) return;
  const others = pool.filter((m) => pool.length === 1 || new URL(m.url, location.href).href !== music.src);
  music.src = others[Math.floor(Math.random() * others.length)].url;
  playOrFlag(music);
}
music.onended = nextTrack;
music.onerror = () => setTimeout(() => { if (settings().music_on && audioOn) nextTrack(); }, 3000);

function syncAudio() {
  const s = settings();
  music.volume = clamp((s.music_volume ?? 0.3) * (reacting ? 0.3 : 1), 0, 1);
  const want = audioOn && s.music_on && manifest.music.length > 0;
  if (!want) { music.pause(); return; }
  if (!music.src || musicGroup !== s.music_group) { musicGroup = s.music_group; nextTrack(); }
  else if (music.paused) playOrFlag(music);
}

window.setStageAudio = (on) => {
  audioOn = on;
  if (on) audioCtx?.resume();
  syncAudio();
};
addEventListener("pointerdown", () => { audioBlocked = false; audioCtx?.resume(); syncAudio(); });

function playSound(kind) {
  const s = settings(), list = manifest.sfx[kind] ?? [], now = performance.now();
  if (kind) reactHost(kind);
  if (!audioOn || !s.sfx_on || !list.length || now - lastSfx < 250) return;
  lastSfx = now;
  const a = new Audio(list[Math.floor(Math.random() * list.length)]);
  a.volume = s.sfx_volume ?? 0.8;
  playOrFlag(a);
}

// Tiếng "pop" tự tổng hợp cho thả tim: tim đến liên tục nên không dùng file sfx.
function blip() {
  const s = settings(), now = performance.now();
  if (!audioOn || !s.sfx_on || now - lastBlip < 450) return;
  lastBlip = now;
  audioCtx ??= new AudioContext();
  if (audioCtx.state !== "running") return;
  const o = audioCtx.createOscillator(), g = audioCtx.createGain(), t = audioCtx.currentTime;
  o.frequency.setValueAtTime(rand(620, 880), t);
  o.frequency.exponentialRampToValueAtTime(1300, t + 0.09);
  g.gain.setValueAtTime(0.12 * (s.sfx_volume ?? 0.8), t);
  g.gain.exponentialRampToValueAtTime(0.001, t + 0.12);
  o.connect(g).connect(audioCtx.destination);
  o.start(t);
  o.stop(t + 0.13);
}

const hostIdle = Object.assign(document.createElement("video"), { muted: true, loop: true, playsInline: true });
const hostReact = Object.assign(document.createElement("video"), { playsInline: true });
let reacting = false;
hostIdle.onerror = () => { hostBroken = manifest.host.idle != null; };
hostReact.onended = hostReact.onerror = () => { reacting = false; syncAudio(); };

function reactHost(kind) {
  const clip = kind === "levelup" ? "levelup" : ["gift", "biggift", "build"].includes(kind) ? "gift" : null;
  const url = clip && manifest.host[clip];
  if (!url || reacting || !settings().host_on) return;
  reacting = true;
  hostReact.src = url;
  hostReact.muted = !audioOn;
  hostReact.volume = 1;
  hostReact.play().catch(() => { hostReact.muted = true; hostReact.play().catch(() => { reacting = false; }); });
  syncAudio();
}

function drawHost() {
  const v = reacting && hostReact.readyState >= 2 ? hostReact : hostIdle;
  if (!settings().host_on || !manifest.host.idle || v.readyState < 2) return;
  const x = 640, y = 486, w = 280, h = 274;
  // Cắt giữ phần trên khung hình, nơi thường có khuôn mặt.
  const scale = Math.max(w / v.videoWidth, h / v.videoHeight);
  const sw = w / scale, sh = h / scale, sx = (v.videoWidth - sw) / 2, sy = (v.videoHeight - sh) * 0.15;
  ctx.save();
  ctx.beginPath(); ctx.roundRect(x, y, w, h, 22); ctx.clip();
  ctx.drawImage(v, sx, sy, sw, sh, x, y, w, h);
  ctx.restore();
  ctx.strokeStyle = "#fff"; ctx.lineWidth = 4;
  ctx.beginPath(); ctx.roundRect(x, y, w, h, 22); ctx.stroke();
  // Nhãn cố định: người xem có thể tặng tiền nên phải biết đây không phải người thật.
  ctx.fillStyle = "rgba(10,12,25,.82)"; pill(x + 10, y + h - 50, 204, 40, 12);
  ctx.fillStyle = "#fff"; ctx.font = `700 24px ${FONT}`; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText("🤖 Nhân vật AI", x + 20, y + h - 29);
}

function syncVillagers() {
  const names = new Set(world.villagers);
  for (const n of villagers.keys()) if (!names.has(n)) villagers.delete(n);
  for (const n of names) {
    if (!villagers.has(n)) villagers.set(n, { x: rand(80, W - 80), dir: Math.random() < 0.5 ? -1 : 1, speed: rand(25, 60), y: GROUND + rand(25, 85), phase: rand(0, 6), hue: hue(n) });
  }
}

function firework() {
  const x = rand(160, W - 160), y = rand(420, 850), h = rand(0, 360);
  for (let i = 0; i < 60; i++) {
    const a = (i / 60) * Math.PI * 2, v = rand(180, 480);
    particles.push({ kind: "spark", x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: rand(0.9, 1.5), color: `hsl(${h + rand(-25, 25)},100%,65%)` });
  }
}

function applyEffect(e) {
  switch (e.fx) {
    case "sound":
      playSound(e.kind);
      break;
    case "hearts":
      blip();
      for (let i = 0; i < e.count; i++) particles.push({ kind: "heart", x: W / 2 + rand(-260, 260), y: GROUND - rand(0, 100), vx: rand(-30, 30), vy: rand(-280, -130), life: rand(1.2, 2.2) });
      petJump = Math.max(petJump, 0.35);
      break;
    case "firework":
      for (let i = 0; i < e.count; i++) setTimeout(firework, i * 280);
      break;
    case "levelup":
      toasts.push(`🎉 Thú cưng lên cấp ${e.level}!`);
      petJump = 1;
      firework();
      break;
    case "build":
      toasts.push(`${e.emoji} ${e.by || "Mọi người"} vừa mở khóa ${e.name}!`);
      for (let i = 0; i < 3; i++) setTimeout(firework, i * 250);
      break;
    case "pet":
      if (e.action === "jump") petJump = 1;
      if (e.action === "eat") { petEat = 2; particles.push({ kind: "emoji", emoji: "🍎", x: W / 2, y: GROUND - 400, vx: 0, vy: 180, life: 1.2 }); }
      break;
    case "toast":
      toasts.push(e.text);
      break;
  }
}

function drawSky(t) {
  const day = [[92, 182, 255], [196, 232, 255]], night = [[12, 16, 48], [52, 38, 96]];
  const dim = world.weather === "clear" ? 0 : 0.28;
  const top = mix(mix(day[0], [120, 130, 150], dim), night[0], nightMix);
  const bot = mix(mix(day[1], [190, 195, 205], dim), night[1], nightMix);
  const g = ctx.createLinearGradient(0, 0, 0, GROUND);
  g.addColorStop(0, rgb(top));
  g.addColorStop(1, rgb(bot));
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, H);
  ctx.globalAlpha = nightMix;
  ctx.fillStyle = "#fff";
  for (const s of stars) { ctx.globalAlpha = nightMix * (0.4 + 0.6 * Math.abs(Math.sin(t + s.p))); ctx.fillRect(s.x, s.y, 4, 4); }
  ctx.globalAlpha = 1;
  ctx.fillStyle = nightMix > 0.5 ? "#f4f1d0" : "#ffd84d";
  if (world.weather === "clear" || nightMix > 0.5) { ctx.beginPath(); ctx.arc(860, 480, 72, 0, 7); ctx.fill(); }
  ctx.fillStyle = world.weather === "clear" ? "rgba(255,255,255,.85)" : "rgba(170,175,190,.92)";
  if (nightMix > 0.5) ctx.fillStyle = "rgba(120,125,160,.45)";
  for (const c of clouds) {
    ctx.beginPath();
    ctx.arc(c.x, c.y, 55 * c.s, 0, 7); ctx.arc(c.x + 70 * c.s, c.y - 20 * c.s, 70 * c.s, 0, 7); ctx.arc(c.x + 150 * c.s, c.y, 50 * c.s, 0, 7);
    ctx.fill();
  }
}

function drawGround() {
  ctx.fillStyle = rgb(mix([96, 190, 110], [28, 58, 62], nightMix));
  ctx.beginPath();
  ctx.moveTo(0, GROUND - 90);
  ctx.quadraticCurveTo(300, GROUND - 200, 620, GROUND - 80);
  ctx.quadraticCurveTo(860, GROUND - 170, W, GROUND - 90);
  ctx.lineTo(W, H); ctx.lineTo(0, H);
  ctx.fill();
  ctx.fillStyle = rgb(mix([72, 160, 86], [20, 44, 50], nightMix));
  ctx.fillRect(0, GROUND, W, H - GROUND);
  if (world.weather === "snow") { ctx.fillStyle = "rgba(255,255,255,.7)"; ctx.fillRect(0, GROUND, W, 34); }
}

function drawBuildings() {
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  for (let i = world.buildings.length - 1; i >= 0; i--) {
    const [x, dy, size] = BUILDING_SLOTS[i];
    ctx.font = `${size}px ${FONT}`;
    ctx.fillText(world.buildings[i].emoji, x, GROUND + dy);
  }
}

function drawPet(t, dt) {
  const target = Math.min(1 + (world.level - 1) * 0.1, 1.9);
  petScale += (target - petScale) * Math.min(1, dt * 2);
  petJump = Math.max(0, petJump - dt * 1.4);
  petEat = Math.max(0, petEat - dt);
  const hop = Math.sin(Math.min(petJump, 1) * Math.PI) * 170;
  const squash = 1 + Math.sin(t * 3) * 0.025;
  const r = 105 * petScale, x = W / 2, y = GROUND + 45 - r * squash - hop;

  ctx.fillStyle = "rgba(0,0,0,.2)";
  ctx.beginPath(); ctx.ellipse(x, GROUND + 50, r * (1 - hop / 500), 20 * petScale, 0, 0, 7); ctx.fill();
  const body = ctx.createRadialGradient(x - r * 0.35, y - r * 0.45, r * 0.1, x, y, r * 1.1);
  body.addColorStop(0, "#ffd0e4"); body.addColorStop(1, "#fe5f8f");
  ctx.fillStyle = "#fe5f8f";
  for (const side of [-1, 1]) { ctx.beginPath(); ctx.ellipse(x + side * r * 0.62, y - r * 0.82, r * 0.24, r * 0.4, side * 0.45, 0, 7); ctx.fill(); }
  ctx.fillStyle = body;
  ctx.beginPath(); ctx.ellipse(x, y, r / squash, r * squash, 0, 0, 7); ctx.fill();

  const blink = Math.sin(t * 0.9) > 0.985;
  ctx.fillStyle = "#2a1a2e";
  for (const side of [-1, 1]) {
    ctx.beginPath(); ctx.ellipse(x + side * r * 0.34, y - r * 0.12, r * 0.09, blink ? 3 : r * 0.13, 0, 0, 7); ctx.fill();
    if (!blink) { ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(x + side * r * 0.34 + r * 0.03, y - r * 0.17, r * 0.035, 0, 7); ctx.fill(); ctx.fillStyle = "#2a1a2e"; }
  }
  ctx.strokeStyle = "#2a1a2e"; ctx.lineWidth = 5 * petScale; ctx.lineCap = "round";
  ctx.beginPath();
  if (petEat > 0) { const o = r * (0.05 + 0.07 * Math.abs(Math.sin(t * 9))); ctx.ellipse(x, y + r * 0.28, r * 0.13, o, 0, 0, 7); }
  else ctx.arc(x, y + r * 0.14, r * 0.17, 0.2, Math.PI - 0.2);
  ctx.stroke();
  ctx.fillStyle = "rgba(255,110,150,.55)";
  for (const side of [-1, 1]) { ctx.beginPath(); ctx.ellipse(x + side * r * 0.58, y + r * 0.13, r * 0.13, r * 0.08, 0, 0, 7); ctx.fill(); }
}

function drawVillagers(t, dt) {
  const list = [...villagers.entries()].sort((a, b) => a[1].y - b[1].y);
  for (const [name, v] of list) {
    v.x += v.dir * v.speed * dt;
    if (v.x < 60 || v.x > W - 60) v.dir *= -1;
    const by = v.y - Math.abs(Math.sin(t * 6 + v.phase)) * 12;
    ctx.fillStyle = `hsl(${v.hue},75%,62%)`;
    ctx.beginPath(); ctx.ellipse(v.x, by, 27, 31, 0, 0, 7); ctx.fill();
    ctx.fillStyle = "#222";
    ctx.beginPath(); ctx.arc(v.x + v.dir * 8 - 6, by - 7, 4, 0, 7); ctx.arc(v.x + v.dir * 8 + 7, by - 7, 4, 0, 7); ctx.fill();
    const label = name.length > 12 ? name.slice(0, 11) + "…" : name;
    ctx.font = `600 25px ${FONT}`; ctx.textAlign = "center";
    ctx.lineWidth = 6; ctx.strokeStyle = "rgba(0,0,0,.65)"; ctx.fillStyle = "#fff"; ctx.lineJoin = "round";
    ctx.strokeText(label, v.x, by - 42); ctx.fillText(label, v.x, by - 42);
  }
}

function drawWeather(dt) {
  if (world.weather === "rain") {
    for (let i = 0; i < 5; i++) particles.push({ kind: "rain", x: rand(-40, W), y: rand(-40, 0), vx: 130, vy: 1500, life: 1.2 });
  } else if (world.weather === "snow") {
    if (Math.random() < 0.4) particles.push({ kind: "snow", x: rand(0, W), y: -10, vx: rand(-40, 40), vy: rand(90, 190), life: 12, r: rand(4, 9) });
  }
  for (let i = particles.length - 1; i >= 0; i--) {
    const p = particles[i];
    p.life -= dt;
    p.x += p.vx * dt;
    p.y += p.vy * dt;
    if (p.kind === "spark") p.vy += 300 * dt;
    if (p.life <= 0 || ((p.kind === "rain" || p.kind === "snow") && p.y > GROUND + 40)) { particles.splice(i, 1); continue; }
    switch (p.kind) {
      case "rain": ctx.strokeStyle = "rgba(190,220,255,.65)"; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(p.x + 3, p.y + 32); ctx.stroke(); break;
      case "snow": ctx.fillStyle = "rgba(255,255,255,.9)"; ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 7); ctx.fill(); break;
      case "spark": ctx.globalAlpha = clamp(p.life, 0, 1); ctx.fillStyle = p.color; ctx.fillRect(p.x - 4, p.y - 4, 8, 8); ctx.globalAlpha = 1; break;
      case "heart": ctx.globalAlpha = clamp(p.life, 0, 1); ctx.font = `48px ${FONT}`; ctx.textAlign = "center"; ctx.fillText("💖", p.x, p.y); ctx.globalAlpha = 1; break;
      case "emoji": ctx.font = `80px ${FONT}`; ctx.textAlign = "center"; ctx.fillText(p.emoji, p.x, p.y); break;
    }
  }
}

function pill(x, y, w, h, r = h / 2) {
  ctx.beginPath(); ctx.roundRect(x, y, w, h, r); ctx.fill();
}

function bar(x, y, w, h, ratio, color) {
  ctx.fillStyle = "rgba(0,0,0,.45)"; pill(x, y, w, h);
  ctx.fillStyle = color; pill(x, y, Math.max(h, w * clamp(ratio, 0, 1)), h);
}

function drawHud(dt) {
  const panel = "rgba(10,12,25,.58)";
  ctx.fillStyle = panel; pill(40, 280, W - 200, 186, 26);
  ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillStyle = "#fff";
  ctx.font = `700 36px ${FONT}`;
  ctx.fillText(`Cấp ${world.level}`, 68, 318);
  ctx.font = `500 26px ${FONT}`;
  ctx.fillText(`Thả tim để lớn: ${world.xp}/${world.xp_next}`, 240, 320);
  bar(68, 346, W - 256, 22, world.xp / world.xp_next, "#fe5f8f");
  const n = world.next_building;
  ctx.font = `500 26px ${FONT}`; ctx.fillStyle = "#fff";
  ctx.fillText(n ? `Tặng quà để mở ${n.emoji} ${n.name}: ${world.diamonds}/${n.need} 💎` : `Đã xây xong thị trấn! ${world.diamonds} 💎`, 68, 400);
  bar(68, 424, W - 256, 22, n ? world.diamonds / n.need : 1, "#ffc83d");

  if (world.top.length) {
    const h = 58 + world.top.length * 44;
    ctx.fillStyle = panel; pill(40, 486, 400, h, 22);
    ctx.fillStyle = "#ffd84d"; ctx.font = `700 26px ${FONT}`; ctx.fillText("🏆 Nhà tài trợ", 62, 516);
    ctx.font = `500 25px ${FONT}`;
    world.top.forEach((g, i) => {
      ctx.fillStyle = "#fff";
      const label = g.name.length > 15 ? g.name.slice(0, 14) + "…" : g.name;
      ctx.fillText(`${i + 1}. ${label}`, 62, 560 + i * 44);
      ctx.textAlign = "right"; ctx.fillText(`${g.diamonds} 💎`, 422, 560 + i * 44); ctx.textAlign = "left";
    });
  }

  ctx.fillStyle = panel; pill(40, 1300, W - 200, 104, 22);
  ctx.fillStyle = "#fff"; ctx.font = `600 27px ${FONT}`; ctx.textAlign = "center";
  ctx.fillText("Comment: mưa · tuyết · nắng · đêm · nhảy · ăn", (W - 120) / 2, 1332);
  ctx.font = `500 25px ${FONT}`; ctx.fillStyle = "#cfd5e6";
  ctx.fillText("Comment hoặc follow để chuyển tới sống trong thị trấn", (W - 120) / 2, 1373);

  toasts.splice(0, toasts.length - 6);  // quà dồn dập: bỏ thông báo cũ để không chiếu lại sự kiện đã qua lâu
  if (!toast && toasts.length) toast = { text: toasts.shift(), life: toasts.length > 1 ? 1.2 : 3 };
  if (toast) {
    toast.life -= dt;
    ctx.globalAlpha = clamp(toast.life * 3, 0, 1);
    ctx.font = `700 34px ${FONT}`;
    const tw = Math.min(ctx.measureText(toast.text).width + 70, W - 170);
    ctx.fillStyle = "rgba(254,44,85,.94)"; pill((W - 120 - tw) / 2, 772, tw, 76);
    ctx.fillStyle = "#fff"; ctx.textAlign = "center";
    ctx.fillText(toast.text, (W - 120) / 2, 811, W - 220);
    ctx.globalAlpha = 1;
    if (toast.life <= 0) toast = null;
  }

  const warning = PREVIEW ? "" : audioBlocked ? "BẤM VÀO ĐÂY ĐỂ BẬT ÂM THANH" : hostBroken ? "KHÔNG PHÁT ĐƯỢC VIDEO AI, ĐỔI SANG .WEBM" : "";
  if (warning) {
    ctx.fillStyle = "#ff5a5a"; pill(190, 130, 700, 58, 14);
    ctx.fillStyle = "#fff"; ctx.font = `800 28px ${FONT}`; ctx.textAlign = "center";
    ctx.fillText(warning, 540, 160, 670);
  }

  if (world.test) {
    ctx.fillStyle = "#ffcc00"; pill(330, 200, 420, 62, 14);
    ctx.fillStyle = "#111"; ctx.font = `800 30px ${FONT}`; ctx.textAlign = "center";
    ctx.fillText("CHẾ ĐỘ THỬ, KHÔNG PHÁT", 540, 232, 396);
  }
}

let last = performance.now();
function frame(now) {
  const dt = Math.min((now - last) / 1000, 0.1), t = now / 1000;
  last = now;
  nightMix += ((world.night ? 1 : 0) - nightMix) * Math.min(1, dt * 1.5);
  for (const c of clouds) { c.x += 16 * c.s * dt; if (c.x > W + 80) c.x = -300; }
  ctx.textBaseline = "alphabetic";
  drawSky(t);
  drawGround();
  drawBuildings();
  drawPet(t, dt);
  drawVillagers(t, dt);
  drawWeather(dt);
  drawHud(dt);
  drawHost();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

fetch("/api/live/media").then((r) => r.json()).then((m) => {
  manifest = m;
  if (m.host.idle) { hostIdle.src = m.host.idle; hostIdle.play().catch(() => {}); }
  syncAudio();
});

new EventSource("/api/live/stream").onmessage = (m) => {
  const { state, effects } = JSON.parse(m.data);
  world = state;
  syncVillagers();
  const key = JSON.stringify(state.settings);
  if (key !== musicKey) { musicKey = key; syncAudio(); }
  effects.forEach(applyEffect);
};
