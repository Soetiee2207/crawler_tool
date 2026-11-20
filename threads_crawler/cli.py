"""Command line entry point: `threads-crawler web | login | search | post | user | sources | export | mark-used | memes | sfx | shopee-reviews`."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .crawler import BASE_URL, DEFAULT_PROFILE_DIR, open_browser
from .pipeline import CrawlOptions, CrawlTarget, crawl, parse_post_url, parse_username
from .quality import QualityFilter
from .storage import Store

log = logging.getLogger("threads_crawler")


async def cmd_login(args: argparse.Namespace) -> None:
    async with open_browser(args.profile, headless=False) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(f"{BASE_URL}/login")
        print("Đăng nhập Threads trong cửa sổ trình duyệt, sau đó quay lại đây và nhấn Enter...")
        await asyncio.to_thread(input)
    print(f"Đã lưu phiên đăng nhập vào {args.profile}/")


async def cmd_search(args: argparse.Namespace) -> None:
    quality = QualityFilter(
        max_age_days=args.max_age, min_likes=args.min_likes, min_replies=args.min_replies,
        min_chars=0 if args.no_filter else QualityFilter.min_chars, skip_ads=not args.no_filter,
    )
    opts = CrawlOptions(
        limit=args.limit, replies=args.replies, branches=args.branches, expand_pages=args.expand_pages, tabs=args.tabs, quality=quality,
        media=not args.no_media, headless=not args.show, profile=args.profile,
    )
    store = Store(args.data)
    match args.command:
        case "sources":
            bad, targets = [], [CrawlTarget(s["username"], "user") for s in store.list_sources()]
            if not targets:
                print("Chưa có tài khoản nguồn nào. Thêm ở tab Crawl trên giao diện web.")
                return
        case "post":
            bad = [u for u in args.queries if not parse_post_url(u)]
            targets = [CrawlTarget(u, "post") for u in args.queries if u not in bad]
        case "user":
            bad = [u for u in args.queries if not parse_username(u)]
            targets = [CrawlTarget(parse_username(u), "user") for u in args.queries if u not in bad]
        case _:
            bad, targets = [], [CrawlTarget(q) for q in args.queries]
    for u in bad:
        log.warning("Bỏ qua, không hiểu: %s", u)
    stats = await crawl(store, targets, opts)
    for err in stats.errors:
        log.warning(err)
    if stats.skipped:
        print("Đã loại: " + ", ".join(f"{n} {why}" for why, n in stats.skipped.most_common()))
    print(f"Xong. Dữ liệu lưu ở {args.data}/threads.db, media ở {args.data}/media/")


def cmd_export(args: argparse.Namespace) -> None:
    n = Store(args.data).export_json(args.out, query=args.query)
    print(f"Đã xuất {n} bài vào {args.out}")


def cmd_mark_used(args: argparse.Namespace) -> None:
    Store(args.data).archive_used(args.codes)
    print(f"Đã xóa {len(args.codes)} bài khỏi kho (đánh dấu đã làm video)")


async def cmd_memes(args: argparse.Namespace) -> None:
    from . import vlipsy

    clips = await vlipsy.fetch_clips(args.source, limit=args.limit, max_duration=args.max_duration)
    added = await asyncio.to_thread(vlipsy.download_clips, clips)
    for cat, n in sorted(added.items(), key=lambda kv: -kv[1]):
        print(f"  {cat}: +{n}")
    print(f"Xong: {sum(added.values())} meme mới / {len(clips)} clip tìm được (đã có thì bỏ qua)")


async def cmd_greenscreen(args: argparse.Namespace) -> None:
    from . import greenscreen

    clips = await asyncio.to_thread(greenscreen.fetch_clips, limit=args.limit, category=args.category)
    print(f"Đã tìm thấy {len(clips)} clip tiềm năng từ greenscreenmemes.com...")
    added = await asyncio.to_thread(
        greenscreen.download_clips, clips, max_duration=args.max_duration, max_workers=args.workers
    )
    for cat, n in sorted(added.items(), key=lambda kv: -kv[1]):
        print(f"  {cat}: +{n}")
    print(f"Xong: {sum(added.values())} green screen meme mới trong materials/meme_video/ (đã có thì bỏ qua)")


def cmd_sfx(args: argparse.Namespace) -> None:
    from . import sfx

    added = sfx.crawl(args.sources or sfx.DEFAULT_SOURCES, limit=args.limit, pages=args.pages,
                      max_duration=args.max_duration)
    for cat, n in sorted(added.items(), key=lambda kv: -kv[1]):
        print(f"  {cat}: +{n}")
    print(f"Xong: {sum(added.values())} sfx mới trong materials/sfx/ (đã có thì bỏ qua)")


async def cmd_shopee_reviews(args: argparse.Namespace) -> None:
    from . import shopee_reviews as sr

    urls = list(args.urls)
    if args.urls_file:
        urls += [u.strip() for u in args.urls_file.read_text(encoding="utf-8").splitlines() if u.strip()]
    refs = []
    for u in urls:
        if ref := sr.parse_item_ref(u):
            refs.append(ref)
        else:
            log.warning("Bỏ qua, không phải link sản phẩm shopee.vn có id: %s", u)
    if not refs:
        raise SystemExit("Không có link sản phẩm hợp lệ.")
    out_dir = args.out or args.data / "shopee_reviews"
    opts = sr.Options(limit=args.limit, delay=args.delay, item_delay=args.item_delay, timeout=args.timeout,
                      retries=args.retries, profile=args.shopee_profile, download_videos=args.download_videos,
                      refresh=args.refresh)
    results = await sr.crawl_reviews(refs, out_dir, opts)
    for r in results:
        print(f"{r.ref.stem}: {r.status}, {r.total} đánh giá (+{r.added}) -> {r.path}" + (f" [{r.error}]" if r.error else ""))
        if args.download_videos:
            print(f"  Video: +{r.videos_saved}, lỗi {r.videos_failed}")
    print(f"Tổng hợp: {out_dir / sr.ALL_REVIEWS} và file .csv cùng tên")
    if len(results) < len(refs):
        print(f"Chưa chạy {len(refs) - len(results)} sản phẩm do bị dừng giữa chừng.")
    if len(results) < len(refs) or any(r.status in {"blocked", "error"} for r in results):
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threads-crawler", description="Crawl dữ liệu Threads")
    parser.add_argument("--data", type=Path, default=Path("data"), help="Thư mục lưu dữ liệu (mặc định: data)")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_DIR, help="Thư mục profile trình duyệt")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    w = sub.add_parser("web", help="Mở giao diện web (crawl, kho bài, tạo video)")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)

    sub.add_parser("login", help="Mở trình duyệt để đăng nhập Threads (chạy 1 lần)")

    crawl_opts = argparse.ArgumentParser(add_help=False)
    crawl_opts.add_argument("-n", "--limit", type=int, default=20,
                            help="Số bài giữ lại mỗi từ khóa/tài khoản, hot nhất trước (mặc định 20)")
    crawl_opts.add_argument("-r", "--replies", type=int, default=10,
                            help="Số bình luận gốc mỗi bài (kèm cây trả lời bên dưới), 0 = bỏ qua (mặc định 10)")
    crawl_opts.add_argument("--branches", type=int, default=3,
                            help="Số câu trả lời giữ ở mỗi bình luận, mọi tầng, lần tới tận cùng (mặc định 3)")
    crawl_opts.add_argument("--expand-pages", type=int, default=60,
                            help="Tối đa số trang mở thêm mỗi bài để lấy nhánh bị ẩn, 0 = không mở (mặc định 60)")
    crawl_opts.add_argument("--max-age", type=int, default=14, help="Bỏ bài cũ hơn N ngày, 0 = không giới hạn (mặc định 14)")
    crawl_opts.add_argument("--min-likes", type=int, default=0, help="Chỉ giữ bài có ít nhất N likes")
    crawl_opts.add_argument("--min-replies", type=int, default=3, help="Chỉ giữ bài có ít nhất N bình luận (mặc định 3)")
    crawl_opts.add_argument("--tabs", choices=["both", "top", "recent"], default="both",
                            help="Lấy kết quả tìm kiếm ở tab nổi bật, mới nhất hay cả hai (mặc định both)")
    crawl_opts.add_argument("--no-filter", action="store_true", help="Không bỏ bài quá ngắn / quảng cáo")
    crawl_opts.add_argument("--no-media", action="store_true", help="Không tải ảnh/video")
    crawl_opts.add_argument("--show", action="store_true", help="Hiện cửa sổ trình duyệt khi crawl")

    s = sub.add_parser("search", parents=[crawl_opts], help="Tìm bài theo từ khóa hoặc #hashtag")
    s.add_argument("queries", nargs="+", help="Từ khóa, ví dụ: 'chuyện công sở' '#tamsu'")
    s = sub.add_parser("post", parents=[crawl_opts], help="Crawl đúng các bài theo link (kèm bình luận)")
    s.add_argument("queries", nargs="+", metavar="url", help="Link bài, vd https://www.threads.com/@user/post/CODE")
    s = sub.add_parser("user", parents=[crawl_opts], help="Crawl các bài của tài khoản")
    s.add_argument("queries", nargs="+", metavar="username", help="Tên tài khoản, vd @user hoặc link trang cá nhân")
    sub.add_parser("sources", parents=[crawl_opts], help="Crawl bài mới của mọi tài khoản nguồn")

    e = sub.add_parser("export", help="Xuất dữ liệu ra JSON (bài gốc kèm replies)")
    e.add_argument("-o", "--out", type=Path, default=Path("data/export.json"))
    e.add_argument("-q", "--query", help="Chỉ xuất bài của từ khóa này")

    m = sub.add_parser("mark-used", help="Đánh dấu bài đã làm video (xóa khỏi kho)")
    m.add_argument("codes", nargs="+", help="Mã bài (code)")

    mm = sub.add_parser("memes", help="Tải meme video từ vlipsy.com vào materials/meme_video/")
    mm.add_argument("source", nargs="?", default="trending",
                    help="'trending' (mặc định) hoặc từ khóa tìm kiếm, vd 'green screen'")
    mm.add_argument("-n", "--limit", type=int, default=100, help="Số clip tối đa (mặc định 100)")
    mm.add_argument("--max-duration", type=float, default=15, help="Bỏ clip dài hơn N giây (mặc định 15)")

    gsm = sub.add_parser("greenscreen", aliases=["gsm"], help="Tải meme video từ greenscreenmemes.com vào materials/meme_video/")
    gsm.add_argument("-n", "--limit", type=int, default=100, help="Số clip tối đa (mặc định 100)")
    gsm.add_argument("--category", "-c", help="Chỉ tải các meme thuộc nhóm này (vd: cười, sốc, buồn_khóc...)")
    gsm.add_argument("--max-duration", type=float, default=20, help="Bỏ clip dài hơn N giây (mặc định 20)")
    gsm.add_argument("--workers", "-w", type=int, default=4, help="Số luồng tải song song (mặc định 4)")

    sx = sub.add_parser("sfx", help="Tải hiệu ứng âm thanh ngắn từ myinstants.com vào materials/sfx/")
    sx.add_argument("sources", nargs="*",
                    help="Nguồn: mã nước (vn, us), 'trending' hoặc tên mục (vd 'sound effects'). "
                         "Mặc định: vn, trending, sound effects, memes, reactions, tiktok trends")
    sx.add_argument("-n", "--limit", type=int, default=200, help="Số âm thanh mới tối đa (mặc định 200)")
    sx.add_argument("--pages", type=int, default=3, help="Số trang mỗi nguồn (mặc định 3)")
    sx.add_argument("--max-duration", type=float, default=6, help="Bỏ âm thanh dài hơn N giây (mặc định 6)")

    sr = sub.add_parser("shopee-reviews", help="Thu thập đánh giá sản phẩm Shopee ra JSONL và CSV (chạy lại để lấy tiếp)")
    sr.add_argument("urls", nargs="*", help="Link sản phẩm shopee.vn")
    sr.add_argument("-f", "--urls-file", type=Path, help="File chứa link, mỗi dòng một link")
    sr.add_argument("-n", "--limit", type=int, default=200, help="Số đánh giá tối đa mỗi sản phẩm (mặc định 200)")
    sr.add_argument("-o", "--out", type=Path, help="Thư mục kết quả (mặc định: <data>/shopee_reviews)")
    sr.add_argument("--timeout", type=float, default=45, help="Số giây tối đa mỗi lần tải trang, thao tác, tải video (mặc định 45)")
    sr.add_argument("--retries", type=int, default=2, help="Số lần thử lại khi lỗi mạng hoặc timeout (mặc định 2)")
    sr.add_argument("--refresh", action="store_true", help="Quét lại cả sản phẩm lần trước đã hết đánh giá")
    sr.add_argument("--download-videos", action="store_true", help="Tải video của người mua vào <shop>_<item>_videos/")
    sr.add_argument("--delay", type=float, default=3, help="Số giây giữa hai trang đánh giá (mặc định 3)")
    sr.add_argument("--item-delay", type=float, default=10, help="Số giây giữa hai sản phẩm (mặc định 10)")
    sr.add_argument("--shopee-profile", type=Path, default=Path(".browser_profile_shopee"),
                    help="Thư mục profile trình duyệt Shopee")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    match args.command:
        case "web":
            from .web import serve

            serve(args.data, args.profile, args.host, args.port)
        case "login":
            asyncio.run(cmd_login(args))
        case "search" | "post" | "user" | "sources":
            asyncio.run(cmd_search(args))
        case "export":
            cmd_export(args)
        case "mark-used":
            cmd_mark_used(args)
        case "memes":
            asyncio.run(cmd_memes(args))
        case "greenscreen" | "gsm":
            asyncio.run(cmd_greenscreen(args))
        case "sfx":
            cmd_sfx(args)
        case "shopee-reviews":
            asyncio.run(cmd_shopee_reviews(args))


if __name__ == "__main__":
    main()
