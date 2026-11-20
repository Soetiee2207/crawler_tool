"""Thế giới ảo cho TikTok LIVE: thả tim nuôi thú cưng, quà xây thị trấn, comment đổi thời tiết."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import media
from .media import MATERIALS_DIR

log = logging.getLogger(__name__)

# (số kim cương tích lũy để mở, emoji, tên công trình)
BUILDINGS = [
    (1, "🏕️", "Lều trại"),
    (10, "🏠", "Nhà gỗ"),
    (30, "🌳", "Cây cổ thụ"),
    (60, "⛲", "Đài phun nước"),
    (100, "🏪", "Tiệm tạp hóa"),
    (200, "🎡", "Vòng đu quay"),
    (400, "🗼", "Tháp canh"),
    (800, "🏰", "Lâu đài"),
    (1500, "🚀", "Tên lửa"),
]
# Từ có dấu mới tính, vì "mua" hay "nang" không dấu thường mang nghĩa khác; không dấu thì gõ kèm "!".
WEATHER_WORDS = {
    "mưa": "rain", "!mua": "rain",
    "tuyết": "snow", "!tuyet": "snow",
    "nắng": "sun", "!nang": "sun",
    "đêm": "night", "!dem": "night",
}
WEATHER_COOLDOWN = 15.0  # giây giữa hai lần đổi thời tiết, tránh màn hình giật liên tục
PET_ACTIONS = {"nhảy": "jump", "!nhay": "jump", "ăn": "eat", "!an": "eat"}
MAX_VILLAGERS = 14
BIG_GIFT = 100  # kim cương: từ mức này trở lên bắn pháo hoa lớn
TOP_GIFTERS = 5
# Loại âm thanh -> (nhóm trong materials/sfx, từ khóa tên file). Chọn sẵn để quà không kêu tiếng súng hay "bruh".
SFX_CHOICES = {
    "gift": ("đúng_sai", r"ka.?ching|cha.?ching|cash|money|apple.?pay|(?<!in)correct"),
    "biggift": ("vui_mừng", r"cheer"),
    "build": ("đúng_sai", r"ta.?da|(?<!in)correct"),
    "levelup": ("vui_mừng", r"yippee"),
    "follow": ("thông_báo", r"discordjoin|messenger|notification"),
    "share": ("chuyển_cảnh", r"whoosh|swoosh"),
    "weather": ("chuyển_cảnh", r"whoosh"),
    "pet": ("chuyển_cảnh", r"^pop"),
}
HOST_DIR = MATERIALS_DIR / "live_host"  # video nhân vật AI: idle (lặp), gift, levelup (phát một lần khi có sự kiện)
HOST_CLIPS = ("idle", "gift", "levelup")
HOST_EXTS = (".webm", ".mp4")  # .webm trước: trình duyệt nhúng trong OBS có thể không giải mã được H.264


def xp_to_next(level: int) -> int:
    return 100 * level


class World:
    """Trạng thái thế giới. `apply` nhận một sự kiện, trả về các hiệu ứng để sân khấu vẽ."""

    def __init__(self, data: dict | None = None):
        d = data or {}
        self.level: int = d.get("level", 1)
        self.xp: int = d.get("xp", 0)
        self.diamonds: int = d.get("diamonds", 0)
        self.weather: str = d.get("weather", "clear")
        self.night: bool = d.get("night", False)
        self.gifters: dict[str, int] = d.get("gifters", {})
        self.villagers: list[str] = d.get("villagers", [])
        self.weather_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "xp": self.xp,
            "diamonds": self.diamonds,
            "weather": self.weather,
            "night": self.night,
            "gifters": self.gifters,
            "villagers": self.villagers,
        }

    def public(self) -> dict:
        built = [{"emoji": e, "name": n} for need, e, n in BUILDINGS if self.diamonds >= need]
        nxt = next(({"need": need, "emoji": e, "name": n} for need, e, n in BUILDINGS if self.diamonds < need), None)
        top = sorted(self.gifters.items(), key=lambda kv: -kv[1])[:TOP_GIFTERS]
        return {
            "level": self.level,
            "xp": self.xp,
            "xp_next": xp_to_next(self.level),
            "diamonds": self.diamonds,
            "weather": self.weather,
            "night": self.night,
            "buildings": built,
            "next_building": nxt,
            "top": [{"name": n, "diamonds": v} for n, v in top],
            "villagers": self.villagers,
        }

    def _gain_xp(self, amount: int) -> list[dict]:
        self.xp += amount
        fx = []
        while self.xp >= xp_to_next(self.level):
            self.xp -= xp_to_next(self.level)
            self.level += 1
            fx += [{"fx": "sound", "kind": "levelup"}, {"fx": "levelup", "level": self.level}]
        return fx

    def _add_villager(self, name: str) -> list[dict]:
        if not name or name in self.villagers:
            return []
        self.villagers.append(name)
        del self.villagers[:-MAX_VILLAGERS]
        return [{"fx": "villager", "name": name}]

    def apply(self, ev: dict, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        user = str(ev.get("user") or "").strip()[:24]
        match ev.get("type"):
            case "like":
                count = max(1, int(ev.get("count") or 1))
                return [{"fx": "hearts", "count": min(count, 30)}, *self._gain_xp(count)]
            case "comment":
                return self._comment(user, str(ev.get("text") or ""), now)
            case "gift":
                return self._gift(user, str(ev.get("name") or "quà"), int(ev.get("diamonds") or 0))
            case "follow":
                return [
                    {"fx": "sound", "kind": "follow"},
                    {"fx": "toast", "text": f"{user} đã follow và chuyển tới thị trấn!"},
                    *self._add_villager(user),
                    *self._gain_xp(20),
                ]
            case "share":
                return [{"fx": "sound", "kind": "share"}, {"fx": "firework", "count": 1}, {"fx": "toast", "text": f"{user} đã chia sẻ LIVE!"}, *self._gain_xp(10)]
            case "join":
                return self._add_villager(user)
        return []

    def _comment(self, user: str, text: str, now: float) -> list[dict]:
        fx = [*self._add_villager(user), *self._gain_xp(1)]
        words = re.findall(r"!?\w+", text.lower())
        for w in words:
            if (action := PET_ACTIONS.get(w)):
                fx += [{"fx": "sound", "kind": "pet"}, {"fx": "pet", "action": action, "name": user}]
                break
        for w in words:
            if (want := WEATHER_WORDS.get(w)) and now - self.weather_at >= WEATHER_COOLDOWN:
                self.weather_at = now
                if want == "sun":
                    self.weather, self.night = "clear", False
                elif want == "night":
                    self.night = True
                else:
                    self.weather = want
                fx.append({"fx": "sound", "kind": "weather"})
                fx.append({"fx": "toast", "text": f"{user} đã đổi trời: {w.lstrip('!')}"})
                break
        return fx

    def _gift(self, user: str, name: str, diamonds: int) -> list[dict]:
        diamonds = max(0, diamonds)
        before = {b[0] for b in BUILDINGS if self.diamonds >= b[0]}
        self.diamonds += diamonds
        if user:
            self.gifters[user] = self.gifters.get(user, 0) + diamonds
        unlocked = any(self.diamonds >= b[0] and b[0] not in before for b in BUILDINGS)
        sound = "build" if unlocked else "biggift" if diamonds >= BIG_GIFT else "gift"
        fx = [
            {"fx": "sound", "kind": sound},  # đứng đầu: sân khấu chỉ phát âm đầu tiên của một loạt
            {"fx": "toast", "text": f"🎁 {user} tặng {name} (+{diamonds} 💎)"},
            {"fx": "firework", "count": 5 if diamonds >= BIG_GIFT else 1},
            *self._add_villager(user),
            *self._gain_xp(diamonds * 10),
        ]
        for need, emoji, bname in BUILDINGS:
            if self.diamonds >= need and need not in before:
                fx.append({"fx": "build", "emoji": emoji, "name": bname, "by": user})
        return fx


def media_manifest() -> dict:
    """Danh sách nhạc, SFX theo loại và video nhân vật AI để sân khấu tải."""
    def files(kind: str) -> list[tuple[str, str]]:
        folder, exts = media.KINDS[kind]
        base = MATERIALS_DIR / folder
        found = [f for f in sorted(base.rglob("*")) if f.is_file() and f.suffix.lower() in exts] if base.exists() else []
        return [(f.relative_to(base).as_posix(), f.parent.name if f.parent != base else "") for f in found]

    music = [{"url": f"/api/live/music/{quote(p)}", "group": g} for p, g in files("music")]
    sfx_files = files("sfx")
    sfx = {
        kind: [
            f"/api/materials/file/sfx/{quote(p)}"
            for p, g in sfx_files
            if g == group and re.search(pattern, Path(p).name, re.I)
        ]
        for kind, (group, pattern) in SFX_CHOICES.items()
    }
    host = {c: f"/api/live/host/{c}" for c in HOST_CLIPS if _host_file(c)}
    return {"music": music, "music_groups": sorted({m["group"] for m in music if m["group"]}), "sfx": sfx, "host": host}


def _host_file(clip: str) -> Path | None:
    return next((f for ext in HOST_EXTS if (f := HOST_DIR / f"{clip}{ext}").is_file()), None)


class LiveSettings(BaseModel):
    music_on: bool = True
    music_volume: float = Field(0.3, ge=0, le=1)
    music_group: str = Field("funny", max_length=64)  # rỗng = mọi nhóm
    sfx_on: bool = True
    sfx_volume: float = Field(0.8, ge=0, le=1)
    host_on: bool = True


class LiveHub:
    """Giữ thế giới, kết nối TikTok, và phát trạng thái tới các sân khấu đang mở (SSE).

    Mọi thao tác chạy trên event loop của server nên không cần khóa.
    """

    def __init__(self, data_dir: Path):
        self.path = data_dir / "live_world.json"
        self.settings_path = data_dir / "live_settings.json"
        self.settings = self._load_settings()
        self.world = self._load()
        self.sandbox: World | None = None  # chế độ thử: không lưu, sân khấu hiện nhãn THỬ
        self.status = "idle"  # idle | connecting | live
        self.username = ""
        self.error = ""
        self.client = None
        self.subscribers: set[asyncio.Queue] = set()
        self.saved_at = 0.0

    def _load(self) -> World:
        try:
            return World(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return World()
        except (OSError, ValueError) as e:
            log.warning("Không đọc được %s: %s", self.path, e)
            return World()

    def _load_settings(self) -> LiveSettings:
        try:
            return LiveSettings.model_validate_json(self.settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return LiveSettings()
        except (OSError, ValueError) as e:
            log.warning("Không đọc được %s: %s", self.settings_path, e)
            return LiveSettings()

    def update_settings(self, settings: LiveSettings) -> None:
        self.settings = settings
        self.settings_path.write_text(settings.model_dump_json(), encoding="utf-8")
        self.broadcast()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.world.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        self.saved_at = time.time()

    def snapshot(self) -> dict:
        w = self.sandbox or self.world
        return {
            **w.public(),
            "test": self.sandbox is not None,
            "status": self.status,
            "username": self.username,
            "error": self.error,
            "settings": self.settings.model_dump(),
        }

    def broadcast(self, effects: list[dict] | None = None) -> None:
        msg = json.dumps({"state": self.snapshot(), "effects": effects or []}, ensure_ascii=False)
        for q in self.subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # mỗi tin chứa toàn bộ trạng thái nên bỏ một tin không làm lệch sân khấu

    def real_event(self, ev: dict) -> None:
        effects = self.world.apply(ev)
        if ev["type"] == "gift" or time.time() - self.saved_at > 5:
            self.save()
        self.broadcast(effects)

    def simulate(self, ev: dict) -> None:
        if self.status != "idle":
            raise HTTPException(409, "Đang kết nối LIVE thật, không thể giả lập")
        if self.sandbox is None:
            self.sandbox = World(copy.deepcopy(self.world.to_dict()))
        self.broadcast(self.sandbox.apply(ev))

    def exit_sandbox(self) -> None:
        self.sandbox = None
        self.broadcast()

    def reset(self) -> None:
        if self.status != "idle":
            raise HTTPException(409, "Hãy ngắt kết nối LIVE trước khi đặt lại")
        self.world = World()
        self.sandbox = None
        self.save()
        self.broadcast()

    async def connect(self, username: str) -> None:
        if self.status != "idle":
            raise HTTPException(409, "Đang có kết nối LIVE")
        from TikTokLive import TikTokLiveClient
        from TikTokLive.events import (
            CommentEvent,
            ConnectEvent,
            DisconnectEvent,
            FollowEvent,
            GiftEvent,
            JoinEvent,
            LikeEvent,
            ShareEvent,
        )

        self.username = username.strip().lstrip("@")
        self.sandbox = None
        self.error = ""
        self.status = "connecting"
        self.broadcast()
        client = TikTokLiveClient(unique_id=f"@{self.username}")
        self.client = client

        def nick(e) -> str:
            return (e.user.nick_name or e.user.unique_id or "") if e.user else ""

        @client.on(ConnectEvent)
        async def _connected(_):
            self.status = "live"
            self.broadcast()

        @client.on(DisconnectEvent)
        async def _disconnected(_):
            self._mark_stopped(client, "")

        @client.on(LikeEvent)
        async def _like(e):
            self.real_event({"type": "like", "user": nick(e), "count": e.count})

        @client.on(CommentEvent)
        async def _comment(e):
            self.real_event({"type": "comment", "user": nick(e), "text": e.content})

        @client.on(GiftEvent)
        async def _gift(e):
            if e.streaking or e.gift is None:  # combo: chỉ tính ở sự kiện cuối, mang đủ repeat_count
                return
            self.real_event({
                "type": "gift",
                "user": nick(e),
                "name": e.gift.name,
                "diamonds": max(1, e.repeat_count) * e.gift.diamond_count,
            })

        @client.on(FollowEvent)
        async def _follow(e):
            self.real_event({"type": "follow", "user": nick(e)})

        @client.on(ShareEvent)
        async def _share(e):
            self.real_event({"type": "share", "user": nick(e)})

        @client.on(JoinEvent)
        async def _join(e):
            self.real_event({"type": "join", "user": nick(e)})

        try:
            task = await client.start()
        except Exception as e:  # thư viện ném nhiều loại lỗi: chưa live, sai tên, hết hạn mức ký...
            log.warning("Không kết nối được LIVE @%s: %r", self.username, e)
            self._mark_stopped(client, f"{type(e).__name__}: {e}")
            raise HTTPException(502, self.error) from e

        def _ended(t: asyncio.Task) -> None:
            err = t.exception() if not t.cancelled() else None
            self._mark_stopped(client, f"{type(err).__name__}: {err}" if err else "")

        task.add_done_callback(_ended)

    def _mark_stopped(self, client, error: str) -> None:
        if self.client is not client:
            return  # sự kiện đến trễ của một kết nối đã thay thế
        self.client = None
        self.status = "idle"
        self.error = error
        self.save()
        self.broadcast()

    async def disconnect(self) -> None:
        client = self.client
        if client is None:
            return
        self._mark_stopped(client, "")
        try:
            await client.disconnect(close_client=True)
        except Exception as e:
            log.warning("Lỗi khi ngắt kết nối LIVE: %r", e)

    async def stream(self) -> AsyncIterator[str]:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.subscribers.add(q)
        try:
            yield f"data: {json.dumps({'state': self.snapshot(), 'effects': []}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    yield f"data: {await asyncio.wait_for(q.get(), 15)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            self.subscribers.discard(q)


class ConnectRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^@?[\w.]+$")


class SimulateRequest(BaseModel):
    type: Literal["like", "comment", "gift", "follow", "share", "join"]
    user: str = Field("Khách thử", max_length=24)
    count: int = Field(1, ge=1, le=1000)
    text: str = Field("", max_length=200)
    name: str = Field("Quà thử", max_length=40)
    diamonds: int = Field(1, ge=0, le=100000)


def make_router(data_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/api/live")
    hub = LiveHub(data_dir)

    @router.get("/stream")
    async def stream():
        return StreamingResponse(
            hub.stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @router.get("/media")
    def media_list():
        return media_manifest()

    # Bản mp3 gốc thay vì bản chuẩn hóa AAC của /api/materials: trình duyệt nhúng trong OBS có thể không có AAC.
    @router.get("/music/{path:path}")
    def music_file(path: str):
        try:
            return FileResponse(media.safe_material(path, "music"))
        except (FileNotFoundError, KeyError):
            raise HTTPException(404)

    @router.get("/host/{clip}")
    def host_file(clip: str):
        if clip not in HOST_CLIPS or not (f := _host_file(clip)):
            raise HTTPException(404)
        return FileResponse(f)

    @router.post("/settings")
    async def settings(req: LiveSettings):
        hub.update_settings(req)
        return hub.snapshot()

    @router.post("/connect")
    async def connect(req: ConnectRequest):
        await hub.connect(req.username)
        return hub.snapshot()

    @router.post("/disconnect")
    async def disconnect():
        await hub.disconnect()
        return hub.snapshot()

    @router.post("/simulate")
    async def simulate(req: SimulateRequest):
        hub.simulate(req.model_dump())
        return hub.snapshot()

    @router.post("/sandbox/exit")
    async def exit_sandbox():
        hub.exit_sandbox()
        return hub.snapshot()

    @router.post("/reset")
    async def reset():
        hub.reset()
        return hub.snapshot()

    return router
