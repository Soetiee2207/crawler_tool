"""Command line entry point: `threads-crawler web | login | search | post | user | export | mark-used | memes | sfx`."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .crawler import BASE_URL, DEFAULT_PROFILE_DIR, open_browser
from .pipeline import CrawlOptions, CrawlTarget, crawl, parse_post_url, parse_username
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
    opts = CrawlOptions(
        limit=args.limit, replies=args.replies, min_likes=args.min_likes, recent=args.recent,
        media=not args.no_media, headless=not args.show, profile=args.profile,
    )
    match args.command:
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
    stats = await crawl(Store(args.data), targets, opts)
    for err in stats.errors:
        log.warning(err)
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


def cmd_sfx(args: argparse.Namespace) -> None:
    from . import sfx

    added = sfx.crawl(args.sources or sfx.DEFAULT_SOURCES, limit=args.limit, pages=args.pages,
                      max_duration=args.max_duration)
    for cat, n in sorted(added.items(), key=lambda kv: -kv[1]):
        print(f"  {cat}: +{n}")
    print(f"Xong: {sum(added.values())} sfx mới trong materials/sfx/ (đã có thì bỏ qua)")


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
    crawl_opts.add_argument("-n", "--limit", type=int, default=20, help="Số bài tối đa mỗi từ khóa/tài khoản (mặc định 20)")
    crawl_opts.add_argument("-r", "--replies", type=int, default=10, help="Số reply mỗi bài, 0 = bỏ qua (mặc định 10)")
    crawl_opts.add_argument("--min-likes", type=int, default=0, help="Chỉ giữ bài có ít nhất N likes")
    crawl_opts.add_argument("--recent", action="store_true", help="Sắp xếp theo mới nhất thay vì nổi bật")
    crawl_opts.add_argument("--no-media", action="store_true", help="Không tải ảnh/video")
    crawl_opts.add_argument("--show", action="store_true", help="Hiện cửa sổ trình duyệt khi crawl")

    s = sub.add_parser("search", parents=[crawl_opts], help="Tìm bài theo từ khóa hoặc #hashtag")
    s.add_argument("queries", nargs="+", help="Từ khóa, ví dụ: 'chuyện công sở' '#tamsu'")
    s = sub.add_parser("post", parents=[crawl_opts], help="Crawl đúng các bài theo link (kèm bình luận)")
    s.add_argument("queries", nargs="+", metavar="url", help="Link bài, vd https://www.threads.com/@user/post/CODE")
    s = sub.add_parser("user", parents=[crawl_opts], help="Crawl các bài của tài khoản")
    s.add_argument("queries", nargs="+", metavar="username", help="Tên tài khoản, vd @user hoặc link trang cá nhân")

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

    sx = sub.add_parser("sfx", help="Tải hiệu ứng âm thanh ngắn từ myinstants.com vào materials/sfx/")
    sx.add_argument("sources", nargs="*",
                    help="Nguồn: mã nước (vn, us), 'trending' hoặc tên mục (vd 'sound effects'). "
                         "Mặc định: vn, trending, sound effects, memes, reactions, tiktok trends")
    sx.add_argument("-n", "--limit", type=int, default=200, help="Số âm thanh mới tối đa (mặc định 200)")
    sx.add_argument("--pages", type=int, default=3, help="Số trang mỗi nguồn (mặc định 3)")
    sx.add_argument("--max-duration", type=float, default=6, help="Bỏ âm thanh dài hơn N giây (mặc định 6)")
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
        case "search" | "post" | "user":
            asyncio.run(cmd_search(args))
        case "export":
            cmd_export(args)
        case "mark-used":
            cmd_mark_used(args)
        case "memes":
            asyncio.run(cmd_memes(args))
        case "sfx":
            cmd_sfx(args)


if __name__ == "__main__":
    main()
