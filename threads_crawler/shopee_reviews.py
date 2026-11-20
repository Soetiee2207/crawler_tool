"""Thu thập đánh giá sản phẩm Shopee ra JSONL và CSV, chạy theo lô và tiếp tục được từ lần chạy trước.

Dùng cửa sổ trình duyệt thật và đọc các response JSON mà trang tự tải, giống `shopee.py`. Gặp trang đăng nhập
hoặc xác minh thì dừng chờ người dùng tự xử lý; nếu không xong hoặc Shopee từ chối thì dừng cả lô, không thử lại:
retry chỉ dành cho lỗi mạng và timeout. Tên người mua được che. Video chỉ tải về khi bật `download_videos`;
quyền dùng lại video vẫn thuộc về người mua, nên module này không nối vào luồng dựng video bán hàng.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx
from playwright.async_api import BrowserContext, Page, Response, async_playwright
from playwright.async_api import Error as PlaywrightError

from .product import mask_name
from .shopee import IMAGE_BASE, LOGIN_WAIT, NEXT_PAGE, PROFILE_DIR, Collector, is_shopee_url, parse_item_name

log = logging.getLogger(__name__)

IDLE_BEFORE_FIRST = 12  # số bước không có gì mới trước khi thấy đánh giá đầu tiên (trang tải chậm, phải cuộn tới)
IDLE_AFTER_FIRST = 4
BLOCK_PATHS = ("/verify/", "/buyer/login")
VIDEO_HOSTS = ("susercontent.com", "shopee.vn", "shopee.com")  # URL lấy từ response nên chỉ tải từ miền của Shopee
MAX_VIDEO_BYTES = 200 * 1024 * 1024
VIDEO_TYPES = ("video/", "application/octet-stream")  # loại trang lỗi HTML trả về thay cho video
CSV_FIELDS = ("product_id", "product_url", "review_id", "username", "review_content", "rating", "review_date",
              "video_url", "video_count")
STATE_FILE = "state.json"
MARKS_FILE = "marks.json"
IMAGES_SUFFIX = ".images.json"  # ảnh của review để file riêng: không đổi các cột JSONL và CSV đã thống nhất
_file_lock = threading.Lock()  # web nhận dữ liệu từ extension song song, các file này phải ghi lần lượt
ALL_REVIEWS = "all_reviews.jsonl"
PRODUCT_FILE = re.compile(r"\d+_\d+\.jsonl")


def _no_progress(frac: float, msg: str) -> None:
    pass


class ShopeeBlocked(RuntimeError):
    """Shopee chặn hoặc đòi xác minh mà không được xử lý; tiếp tục chỉ làm nặng thêm."""


class TransientError(RuntimeError):
    """Lỗi tạm thời phía máy chủ (HTTP 5xx, 429), đáng thử lại."""


@dataclass
class Options:
    limit: int = 200
    delay: float = 3.0  # giây giữa hai trang đánh giá, hai video
    item_delay: float = 10.0  # giây giữa hai sản phẩm
    timeout: float = 45.0  # giây cho mỗi lần tải trang, thao tác hoặc tải video
    retries: int = 2  # số lần thử lại sau lần đầu
    retry_wait: float = 5.0  # giây chờ trước lần thử lại đầu, sau đó gấp đôi
    verify_wait: float = LOGIN_WAIT
    profile: Path = PROFILE_DIR
    download_videos: bool = False
    refresh: bool = False  # quét lại cả sản phẩm đã hết đánh giá ở lần trước
    videos_only: bool = False  # không quét đánh giá, chỉ tải video từ các URL đã lưu
    progress: Callable[[float, str], None] = _no_progress  # giao diện web dùng để hiện tiến độ và dừng tác vụ


@dataclass(frozen=True)
class ItemRef:
    url: str
    shop_id: int
    item_id: int

    @property
    def stem(self) -> str:
        return f"{self.shop_id}_{self.item_id}"


@dataclass
class ItemResult:
    ref: ItemRef
    path: Path
    total: int = 0
    added: int = 0
    status: str = "pending"  # limit | exhausted | truncated | blocked | error
    error: str = ""
    videos_saved: int = 0
    videos_failed: int = 0


def parse_item_ref(url: str) -> ItemRef | None:
    """Nhận link dạng `/ten-sp-i.<shop>.<item>` và `/product/<shop>/<item>`; link rút gọn thì không có id."""
    url = url.strip()
    if not is_shopee_url(url):
        return None
    m = re.search(r"-i\.(\d+)\.(\d+)", urlparse(url).path) or re.search(r"/product/(\d+)/(\d+)", urlparse(url).path)
    return ItemRef(url, int(m.group(1)), int(m.group(2))) if m else None


def normalize_review(key: str, raw: dict, ref: ItemRef) -> dict:
    ctime = raw.get("ctime")
    videos = raw.get("videos") or []
    author = "" if raw.get("anonymous") else str(raw.get("author_username") or "")
    return {
        "product_id": raw.get("itemid") or ref.item_id,
        "product_url": ref.url,
        "review_id": key if key.isdigit() else hashlib.sha1(key.encode()).hexdigest(),
        "username": mask_name(author),
        "review_content": " ".join(raw["comment"].split()),
        "rating": raw.get("rating_star"),
        "review_date": datetime.fromtimestamp(ctime, UTC).isoformat() if ctime else None,
        "video_url": " ".join(v["url"] for v in videos if v.get("url")),  # nhiều video thì cách nhau bằng dấu cách
        "video_count": len(videos),
    }


class ReviewWriter:
    """File JSONL mỗi sản phẩm. Ghi từng dòng ngay khi có để dừng giữa chừng không mất dữ liệu; id đã có thì bỏ qua."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seen: set[str] = set()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._needs_newline = False
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            self._needs_newline = bool(raw) and not raw.endswith("\n")
            for line in raw.splitlines():
                try:
                    self.seen.add(json.loads(line)["review_id"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    log.warning("Bỏ qua dòng hỏng trong %s", path)

    def add(self, reviews: list[dict]) -> int:
        fresh = [r for r in reviews if r["review_id"] not in self.seen]
        if not fresh:
            return 0
        with self.path.open("a", encoding="utf-8") as f:
            if self._needs_newline:  # lần trước bị ngắt giữa dòng
                f.write("\n")
                self._needs_newline = False
            for r in fresh:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                self.seen.add(r["review_id"])
        return len(fresh)


class ReviewCollector(Collector):
    """Thêm việc phân biệt "hết đánh giá" với "API từ chối", vì cả hai đều chỉ thấy là không có gì mới."""

    def __init__(self) -> None:
        super().__init__()
        self.rejected = 0
        self.rating_total: int | None = None  # tổng đánh giá theo Shopee, để biết còn sót không

    async def on_response(self, response: Response) -> None:
        if "/api/" in response.url and "rating" in response.url.lower():
            body = None
            if response.status < 400:
                try:
                    body = await response.json()
                except Exception as e:  # response hủy giữa chừng hoặc không phải JSON
                    log.debug("Không đọc được %s: %r", response.url[:80], e)
                    return
            if response.status >= 400 or (isinstance(body, dict) and body.get("error")):
                self.rejected += 1
                log.warning("API đánh giá từ chối (HTTP %s): %s", response.status, response.url[:100])
                return
            self.rejected = 0
            data = body.get("data") if isinstance(body, dict) else None
            summary = data.get("item_rating_summary") if isinstance(data, dict) else None
            if isinstance(summary, dict) and isinstance(summary.get("rating_total"), int):
                self.rating_total = summary["rating_total"]
        await super().on_response(response)


def _is_block_page(url: str) -> bool:
    return any(p in urlparse(url).path for p in BLOCK_PATHS)


async def _wait_for_user(page: Page, url: str, wait: float, progress: Callable[[float, str], None]) -> None:
    log.warning("Shopee yêu cầu đăng nhập hoặc xác minh. Hãy tự làm trong cửa sổ trình duyệt (chờ tối đa %.0f giây).", wait)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while _is_block_page(page.url):
        if loop.time() > deadline:
            raise ShopeeBlocked("Hết giờ chờ đăng nhập hoặc xác minh")
        progress(0, f"Shopee yêu cầu đăng nhập hoặc xác minh: hãy tự làm trong cửa sổ trình duyệt (còn {deadline - loop.time():.0f} giây)")
        await asyncio.sleep(1)
    await page.goto(url, wait_until="domcontentloaded")  # sau xác minh Shopee có thể không quay lại đúng sản phẩm


async def _next_step(page: Page, c: Collector) -> None:
    """Chưa tới phần đánh giá thì cuộn xuống, tới rồi thì sang trang kế như người dùng bấm."""
    if not c.reviews:
        await page.mouse.wheel(0, 1500)
        return
    button = page.locator(NEXT_PAGE).first
    if not await button.count():
        log.warning("Không thấy nút sang trang đánh giá (chỉ có 1 trang, hoặc giao diện Shopee đã đổi)")
        return
    try:
        await button.click(timeout=3000)
    except PlaywrightError as e:
        log.debug("Không bấm được nút sang trang: %r", e)


async def crawl_item(context: BrowserContext, ref: ItemRef, out_dir: Path, limit: int = 200, delay: float = 3.0,
                     verify_wait: float = LOGIN_WAIT, progress: Callable[[float, str], None] = _no_progress) -> ItemResult:
    """Lần theo các trang đánh giá của một sản phẩm. Lúc chạy tiếp vẫn đi từ trang đầu vì Shopee không cho nhảy trang."""
    result = ItemResult(ref, out_dir / f"{ref.stem}.jsonl")
    writer = ReviewWriter(result.path)
    start = len(writer.seen)
    if start >= limit:
        result.total, result.status = start, "limit"
        return result

    c = ReviewCollector()
    page = await context.new_page()
    page.on("response", c.on_response)
    try:
        await page.goto(ref.url, wait_until="domcontentloaded")
        idle = 0
        while len(writer.seen) < limit:
            if _is_block_page(page.url):
                await _wait_for_user(page, ref.url, verify_wait, progress)
                idle = 0
            progress(len(writer.seen) / limit, f"{ref.stem}: đã có {len(writer.seen)} đánh giá")
            before = len(c.reviews)
            await _next_step(page, c)
            await asyncio.sleep(delay)
            rows = [normalize_review(k, r, ref) for k, r in c.reviews.items()
                    if r.get("itemid") in (None, ref.item_id)]  # trang có thể tải kèm đánh giá sản phẩm khác
            new = [r for r in rows if r["review_id"] not in writer.seen]
            if writer.add(new[:limit - len(writer.seen)]):
                log.info("%s: đã có %d đánh giá", ref.stem, len(writer.seen))
            idle = idle + 1 if len(c.reviews) == before else 0
            if idle >= (IDLE_AFTER_FIRST if c.reviews else IDLE_BEFORE_FIRST):
                if c.rejected:
                    raise ShopeeBlocked(f"API đánh giá từ chối {c.rejected} lần liên tiếp")
                break
        if len(writer.seen) >= limit:
            result.status = "limit"
        elif c.rating_total and len(writer.seen) < c.rating_total:  # Shopee có thể chỉ cho xem tới một số trang
            result.status = "truncated"
            log.warning("%s: chỉ lấy được %d/%d đánh giá", ref.stem, len(writer.seen), c.rating_total)
        else:
            result.status = "exhausted"
    finally:
        result.total, result.added = len(writer.seen), len(writer.seen) - start
        await page.close()
    if not result.total:
        log.warning("%s: không lấy được đánh giá nào (sản phẩm chưa có đánh giá, hoặc trang chưa tải được)", ref.stem)
    return result


async def _retry(attempt: Callable[[], Awaitable], what: str, retries: int, wait: float):
    """Thử lại khi lỗi mạng hoặc timeout, chờ tăng dần. ShopeeBlocked không thuộc diện này: gặp là dừng."""
    for i in range(retries + 1):
        try:
            return await attempt()
        except (PlaywrightError, TransientError) as e:
            if i == retries:
                raise
            log.warning("%s lỗi (lần %d/%d), thử lại sau %.0f giây: %s", what, i + 1, retries + 1, wait * 2 ** i, e)
            await asyncio.sleep(wait * 2 ** i)


def _is_video_host(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme in {"http", "https"} and any(host == h or host.endswith(f".{h}") for h in VIDEO_HOSTS)


async def _save_video(context: BrowserContext, url: str, dest: Path, timeout: float) -> None:
    resp = await context.request.get(url, timeout=timeout * 1000)
    if resp.status == 429 or resp.status >= 500:
        raise TransientError(f"HTTP {resp.status}")
    if not resp.ok:
        raise ValueError(f"HTTP {resp.status}")
    kind = resp.headers.get("content-type", "")
    if not kind.startswith(VIDEO_TYPES):
        raise ValueError(f"không phải video ({kind})")
    body = await resp.body()
    if len(body) > MAX_VIDEO_BYTES:
        raise ValueError(f"quá lớn ({len(body)} byte)")
    part = dest.with_suffix(".part")  # đổi tên sau cùng để file dở không bị tính là đã tải
    part.write_bytes(body)
    part.replace(dest)


async def download_videos(context: BrowserContext, jsonl: Path, video_dir: Path, opts: Options,
                          progress: Callable[[float, str], None] = _no_progress) -> tuple[int, int]:
    """Tải video của các đánh giá trong file JSONL, file đã có thì bỏ qua. Trả về (số tải mới, số lỗi)."""
    if not jsonl.exists():
        return 0, 0
    saved = failed = 0
    for row in _read_jsonl(jsonl):
        for i, url in enumerate((row.get("video_url") or "").split()):
            dest = video_dir / f"{row['review_id']}_{i}.mp4"
            if dest.exists():
                continue
            video_dir.mkdir(parents=True, exist_ok=True)
            progress(0.95, f"Đang tải video {dest.name} (đã tải {saved}, lỗi {failed})")
            try:
                if not _is_video_host(url):
                    raise ValueError("URL không thuộc miền của Shopee")
                await _retry(lambda: _save_video(context, url, dest, opts.timeout), f"Tải {dest.name}",
                             opts.retries, opts.retry_wait)
                saved += 1
            except (ValueError, OSError, PlaywrightError, TransientError) as e:  # URL có thể đã hết hạn
                failed += 1
                log.warning("Không tải được video %s: %s", dest.name, e)
            await asyncio.sleep(opts.delay)
    return saved, failed


def _read_state(out_dir: Path) -> dict[str, str]:
    try:
        return json.loads((out_dir / STATE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        log.warning("%s hỏng, coi như chưa chạy lần nào", out_dir / STATE_FILE)
        return {}


def _write_state(out_dir: Path, state: dict[str, str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"{STATE_FILE}.tmp"
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(out_dir / STATE_FILE)


async def crawl_context(context: BrowserContext, refs: list[ItemRef], out_dir: Path, opts: Options) -> list[ItemResult]:
    """Chạy tuần tự từng sản phẩm. Lỗi một sản phẩm thì sang sản phẩm kế; bị chặn hoặc đóng trình duyệt thì dừng lô."""
    context.set_default_timeout(opts.timeout * 1000)
    context.set_default_navigation_timeout(opts.timeout * 1000)
    closed: list[bool] = []
    context.on("close", lambda *_: closed.append(True))
    state = _read_state(out_dir)
    results: list[ItemResult] = []
    crawled = False
    for i, ref in enumerate(refs):
        path = out_dir / f"{ref.stem}.jsonl"
        before = len(ReviewWriter(path).seen)
        log.info("[%d/%d] %s", i + 1, len(refs), ref.url)

        def item_progress(frac: float, msg: str, i: int = i) -> None:
            opts.progress((i + frac) / len(refs), msg)

        item_progress(0, f"{ref.stem}: đang mở sản phẩm {i + 1}/{len(refs)}")
        if opts.videos_only:
            result = ItemResult(ref, path, total=before, status=state.get(ref.stem, "exhausted"))
        elif state.get(ref.stem) == "exhausted" and not opts.refresh:
            log.info("%s: lần trước đã hết đánh giá, bỏ qua (dùng --refresh để quét lại)", ref.stem)
            result = ItemResult(ref, path, total=before, status="exhausted")
        else:
            if crawled:
                await asyncio.sleep(opts.item_delay)
            crawled = True
            try:
                result = await _retry(lambda: crawl_item(context, ref, out_dir, opts.limit, opts.delay, opts.verify_wait,
                                                         item_progress),
                                      ref.stem, 0 if closed else opts.retries, opts.retry_wait)
            except ShopeeBlocked as e:
                log.error("Dừng cả lô: %s. Chạy lại sau, phần đã lấy được giữ nguyên.", e)
                result = _failed(ref, path, before, "blocked", str(e))
            except PlaywrightError as e:
                log.error("Lỗi ở %s: %s", ref.stem, e)
                result = _failed(ref, path, before, "error", str(e))
        if opts.download_videos and (opts.videos_only or result.status not in {"blocked", "error"}):
            result.videos_saved, result.videos_failed = await download_videos(
                context, path, out_dir / f"{ref.stem}_videos", opts, item_progress)
        results.append(result)
        state[ref.stem] = result.status
        _write_state(out_dir, state)
        if result.status == "blocked" or closed:
            break
    return results


def _failed(ref: ItemRef, path: Path, before: int, status: str, error: str) -> ItemResult:
    total = len(ReviewWriter(path).seen)
    return ItemResult(ref, path, total=total, added=total - before, status=status, error=error)


async def crawl_reviews(refs: list[ItemRef], out_dir: Path, opts: Options) -> list[ItemResult]:
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(  # hiện cửa sổ để người dùng tự xác minh khi cần
            str(opts.profile), headless=False, viewport={"width": 1280, "height": 900}, locale="vi-VN"
        )
        try:
            return await crawl_context(context, refs, out_dir, opts)
        finally:
            await context.close()
            export_all(out_dir)  # cả khi bị dừng giữa chừng: phần đã lấy được vẫn có CSV


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # dòng dở do bị ngắt lúc ghi
    return rows


def _csv_safe(value):
    """Nội dung do người lạ viết: chặn Excel hiểu `=...` thành công thức."""
    return f"'{value}" if isinstance(value, str) and value[:1] in ("=", "+", "-", "@") else value


def export_csv(jsonl: Path) -> Path:
    out = jsonl.with_suffix(".csv")
    with out.open("w", encoding="utf-8-sig", newline="") as dst:  # Excel cần BOM
        writer = csv.DictWriter(dst, CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in _read_jsonl(jsonl):
            writer.writerow({k: _csv_safe(v) for k, v in row.items()})
    return out


def merge_outputs(out_dir: Path) -> Path:
    """Gộp mọi sản phẩm trong thư mục thành all_reviews.jsonl và .csv, viết lại từ đầu sau mỗi lần chạy."""
    merged = out_dir / ALL_REVIEWS
    out_dir.mkdir(parents=True, exist_ok=True)
    with merged.open("w", encoding="utf-8") as f:
        for path in sorted(out_dir.iterdir()):
            if PRODUCT_FILE.fullmatch(path.name):
                for row in _read_jsonl(path):
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
    export_csv(merged)
    return merged


def export_all(out_dir: Path) -> Path:
    if out_dir.is_dir():
        for path in out_dir.iterdir():
            if PRODUCT_FILE.fullmatch(path.name):
                export_csv(path)
    return merge_outputs(out_dir)


def ingest_response(out_dir: Path, page_url: str, api_url: str, body: object) -> dict:
    """Lưu response đánh giá mà extension sao từ trang người dùng đang tự xem."""
    if not is_shopee_url(api_url) or not (ref := parse_item_ref(page_url)):
        return {"ignored": True}
    if not isinstance(body, dict) or body.get("error"):
        return {"rejected": True, "stem": ref.stem}
    page = urlparse(page_url.strip())
    ref = ItemRef(urlunparse(page._replace(query="", fragment="")), ref.shop_id, ref.item_id)  # bỏ tham số theo dõi
    # sản phẩm có id phụ: trang và API có thể mang id khác với id trên đường dẫn
    ids = {ref.item_id}
    for query in (page.query, urlparse(api_url).query):
        params = parse_qs(query)
        ids |= {int(v) for key in ("vItemId", "itemid") for v in params.get(key, []) if v.isdigit()}
    c = Collector()
    c.add(body)
    rows = [normalize_review(k, r, ref) for k, r in c.reviews.items() if r.get("itemid") in (None, *ids)]
    with _file_lock:
        writer = ReviewWriter(out_dir / f"{ref.stem}.jsonl")
        added = writer.add(rows)
        images = review_images(out_dir, ref.stem)
        fresh = {normalize_review(k, r, ref)["review_id"]: [IMAGE_BASE + n for n in r["images"]]
                 for k, r in c.reviews.items() if r.get("images") and r.get("itemid") in (None, *ids)}
        if fresh:
            (out_dir / f"{ref.stem}{IMAGES_SUFFIX}").write_text(json.dumps({**images, **fresh}), encoding="utf-8")
        state = _read_state(out_dir)
        state.setdefault(ref.stem, "browser")
        _write_state(out_dir, state)
        if added:
            export_all(out_dir)
    return {"stem": ref.stem, "added": added, "reviews_seen": len(rows), "total": len(writer.seen),
            "with_video": sum(bool(r["video_count"]) for r in rows)}


def review_images(out_dir: Path, stem: str) -> dict[str, list[str]]:
    try:
        return json.loads((out_dir / f"{stem}{IMAGES_SUFFIX}").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _read_marks(out_dir: Path) -> dict:
    try:
        return json.loads((out_dir / MARKS_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def set_mark(out_dir: Path, stem: str, review_id: str, picked: bool, claim: str, permission: str) -> bool:
    """Lưu lựa chọn cho một review: có dùng không, nhận định cần test, trạng thái xin phép. False nếu không có review đó."""
    path = out_dir / f"{stem}.jsonl"
    if not PRODUCT_FILE.fullmatch(path.name) or not path.exists():
        return False
    if not any(r.get("review_id") == review_id for r in _read_jsonl(path)):
        return False
    with _file_lock:
        marks = _read_marks(out_dir)
        marks.setdefault(stem, {})[review_id] = {"picked": picked, "claim": claim, "permission": permission}
        tmp = out_dir / f"{MARKS_FILE}.tmp"
        tmp.write_text(json.dumps(marks, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out_dir / MARKS_FILE)
    return True


def picked_reviews(out_dir: Path, stem: str) -> list[dict] | None:
    """Các review đã chọn của sản phẩm, kèm `mark`; None nếu không có sản phẩm này."""
    path = out_dir / f"{stem}.jsonl"
    if not PRODUCT_FILE.fullmatch(path.name) or not path.exists():
        return None
    marks = _read_marks(out_dir).get(stem, {})
    return [{**r, "mark": marks[r["review_id"]]} for r in _read_jsonl(path) if marks.get(r["review_id"], {}).get("picked")]


def picked_or_marked(out_dir: Path, stem: str) -> list[dict]:
    """Mọi review đã có đánh dấu (đã chọn hoặc đã ghi trạng thái xin phép), kèm `mark`."""
    path = out_dir / f"{stem}.jsonl"
    if not PRODUCT_FILE.fullmatch(path.name) or not path.exists():
        return []
    marks = _read_marks(out_dir).get(stem, {})
    return [{**r, "mark": marks[r["review_id"]]} for r in _read_jsonl(path) if r["review_id"] in marks]


def clear_picks(out_dir: Path, stem: str) -> None:
    """Bỏ chọn tất cả, giữ lại nhận định và trạng thái xin phép đã ghi."""
    with _file_lock:
        marks = _read_marks(out_dir)
        for mark in marks.get(stem, {}).values():
            mark["picked"] = False
        tmp = out_dir / f"{MARKS_FILE}.tmp"
        tmp.write_text(json.dumps(marks, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out_dir / MARKS_FILE)


def test_list_csv(rows: list[dict]) -> str:
    """Bảng ghi kết quả test: mỗi review đã chọn một dòng, hai cột cuối để người dùng tự điền."""
    labels = {"none": "Chưa xin phép", "asked": "Đã hỏi", "granted": "Đã đồng ý", "denied": "Từ chối"}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Nhận định cần test", "Nội dung review", "Số sao", "Xin phép", "Link video", "Kết quả test", "Ghi chú"])
    for r in rows:
        mark = r["mark"]
        writer.writerow([_csv_safe(v) for v in (mark.get("claim", ""), r["review_content"], r.get("rating"),
                                                labels.get(mark.get("permission"), ""), r.get("video_url", ""), "", "")])
    return "\ufeff" + out.getvalue()  # Excel cần BOM


def _fetch_video(url: str, dest: Path, timeout: float, types: tuple[str, ...] = VIDEO_TYPES) -> None:
    """Tải trực tiếp từ CDN, không cần trình duyệt. Không theo chuyển hướng để không ra ngoài miền của Shopee."""
    part = dest.with_suffix(".part")
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=False) as resp:
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        kind = resp.headers.get("content-type", "")
        if not kind.startswith(types):
            raise ValueError(f"sai loại file ({kind})")
        size = 0
        with part.open("wb") as f:
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    raise ValueError("video quá lớn")
                f.write(chunk)
    part.replace(dest)


def fetch_with_retry(url: str, dest: Path, types: tuple[str, ...] = VIDEO_TYPES, retries: int = 2, delay: float = 1.0,
                     timeout: float = 45.0) -> None:
    for attempt in range(retries + 1):
        try:
            return _fetch_video(url, dest, timeout, types)
        except (httpx.TransportError, TransientError):
            if attempt == retries:
                raise
            time.sleep(delay * 2 ** attempt)


def download_picked(out_dir: Path, stem: str, delay: float = 1.0, retries: int = 2,
                    timeout: float = 45.0, progress: Callable[[float, str], None] = _no_progress) -> dict:
    """Tải video của các review đã chọn về xem. Trả về số tải mới, số lỗi."""
    rows = picked_reviews(out_dir, stem) or []
    video_dir = out_dir / f"{stem}_videos"
    targets = [(r, i, u) for r in rows for i, u in enumerate((r.get("video_url") or "").split())]
    saved, failed, problems = 0, 0, []
    for n, (r, i, url) in enumerate(targets):
        dest = video_dir / f"{r['review_id']}_{i}.mp4"
        progress(n / len(targets), f"Đang tải video {n + 1}/{len(targets)}")
        if not dest.exists():
            video_dir.mkdir(parents=True, exist_ok=True)
            try:
                if not _is_video_host(url):
                    raise ValueError("URL không thuộc miền của Shopee")
                fetch_with_retry(url, dest, VIDEO_TYPES, retries, delay, timeout)
                saved += 1
                if n + 1 < len(targets):
                    time.sleep(delay)
            except (ValueError, OSError, httpx.HTTPError, TransientError) as e:
                failed += 1
                problems.append(f"{dest.name}: {e}")
                log.warning("Không tải được video %s: %s", dest.name, e)
    return {"saved": saved, "failed": failed, "problems": problems}


def list_products(out_dir: Path) -> list[dict]:
    """Tóm tắt các sản phẩm đã thu thập cho giao diện, mới cập nhật lên trước."""
    if not out_dir.is_dir():
        return []
    state = _read_state(out_dir)
    marks = _read_marks(out_dir)
    paths = [p for p in out_dir.iterdir() if PRODUCT_FILE.fullmatch(p.name)]
    items = []
    for path in sorted(paths, key=lambda p: -p.stat().st_mtime):
        rows = _read_jsonl(path)
        url = rows[0]["product_url"] if rows else ""
        video_dir = out_dir / f"{path.stem}_videos"
        rated = [r["rating"] for r in rows if r.get("rating")]
        items.append({
            "stem": path.stem,
            "product_id": path.stem.split("_")[1],
            "name": parse_item_name(url),
            "url": url,
            "status": state.get(path.stem),
            "total": len(rows),
            "picked": sum(m.get("picked", False) for m in marks.get(path.stem, {}).values()),
            "with_video": sum(bool(r.get("video_count")) for r in rows),
            "video_urls": sum(_is_video_host(u) for r in rows for u in (r.get("video_url") or "").split()),
            "videos_saved": sum(1 for _ in video_dir.glob("*.mp4")) if video_dir.is_dir() else 0,
            "avg_rating": round(sum(rated) / len(rated), 1) if rated else None,
            "updated_at": path.stat().st_mtime,
            "jsonl": str(path),
            "csv": str(path.with_suffix(".csv")),
        })
    return items


def read_reviews(out_dir: Path, stem: str, page: int = 1, size: int = 20, video_only: bool = False,
                 rating: int = 0, q: str = "", picked_only: bool = False) -> dict | None:
    """Một trang đánh giá kèm bộ lọc; None nếu không có sản phẩm này. `stem` phải đúng dạng để không ra ngoài thư mục."""
    path = out_dir / f"{stem}.jsonl"
    if not PRODUCT_FILE.fullmatch(path.name) or not path.exists():
        return None
    rows = _read_jsonl(path)
    marks = _read_marks(out_dir).get(stem, {})
    images = review_images(out_dir, stem)
    counts = {str(n): sum(r.get("rating") == n for r in rows) for n in range(1, 6)}
    q = q.strip().lower()
    found = [r for r in rows if (not video_only or r.get("video_count"))
             and (not picked_only or marks.get(r["review_id"], {}).get("picked"))
             and (not rating or r.get("rating") == rating)
             and (not q or q in (r.get("review_content") or "").lower())]
    start = (page - 1) * size
    items = []
    for r in found[start:start + size]:
        videos = []
        for i, url in enumerate((r.get("video_url") or "").split()):
            if not _is_video_host(url):  # URL lấy từ response, không đưa thẳng ra làm link
                continue
            local = out_dir / f"{stem}_videos" / f"{r['review_id']}_{i}.mp4"
            videos.append({"url": url, "local": str(local) if local.exists() else None})
        items.append({**r, "videos": videos, "images": images.get(r["review_id"], []), "mark": marks.get(r["review_id"])})
    return {"items": items, "total": len(found), "all": len(rows), "page": page, "size": size, "rating_counts": counts}
