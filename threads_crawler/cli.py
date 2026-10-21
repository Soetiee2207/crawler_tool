"""Command line entry point: `threads-crawler web | login | search | export | mark-used`."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .crawler import BASE_URL, DEFAULT_PROFILE_DIR, open_browser
from .pipeline import CrawlOptions, CrawlTarget, crawl
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
    targets = [CrawlTarget(q) for q in args.queries]
    stats = await crawl(Store(args.data), targets, opts)
    for err in stats.errors:
        log.warning(err)
    print(f"Xong. Dữ liệu lưu ở {args.data}/threads.db, media ở {args.data}/media/")


def cmd_export(args: argparse.Namespace) -> None:
    n = Store(args.data).export_json(args.out, query=args.query, only_unused=args.unused)
    print(f"Đã xuất {n} bài vào {args.out}")


def cmd_mark_used(args: argparse.Namespace) -> None:
    Store(args.data).mark_used(args.codes)
    print(f"Đã đánh dấu {len(args.codes)} bài là đã dùng")


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

    s = sub.add_parser("search", help="Tìm bài theo từ khóa hoặc #hashtag")
    s.add_argument("queries", nargs="+", help="Từ khóa, ví dụ: 'chuyện công sở' '#tamsu'")
    s.add_argument("-n", "--limit", type=int, default=20, help="Số bài tối đa mỗi từ khóa (mặc định 20)")
    s.add_argument("-r", "--replies", type=int, default=10, help="Số reply mỗi bài, 0 = bỏ qua (mặc định 10)")
    s.add_argument("--min-likes", type=int, default=0, help="Chỉ giữ bài có ít nhất N likes")
    s.add_argument("--recent", action="store_true", help="Sắp xếp theo mới nhất thay vì nổi bật")
    s.add_argument("--no-media", action="store_true", help="Không tải ảnh/video")
    s.add_argument("--show", action="store_true", help="Hiện cửa sổ trình duyệt khi crawl")

    e = sub.add_parser("export", help="Xuất dữ liệu ra JSON (bài gốc kèm replies)")
    e.add_argument("-o", "--out", type=Path, default=Path("data/export.json"))
    e.add_argument("-q", "--query", help="Chỉ xuất bài của từ khóa này")
    e.add_argument("--unused", action="store_true", help="Chỉ xuất bài chưa dùng làm video")

    m = sub.add_parser("mark-used", help="Đánh dấu bài đã dùng làm video")
    m.add_argument("codes", nargs="+", help="Mã bài (code)")
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
        case "search":
            asyncio.run(cmd_search(args))
        case "export":
            cmd_export(args)
        case "mark-used":
            cmd_mark_used(args)


if __name__ == "__main__":
    main()
