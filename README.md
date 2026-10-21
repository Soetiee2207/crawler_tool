# Threads Crawler

Pipeline **Threads → video TikTok**: crawl bài viết Threads theo từ khóa/hashtag, kèm bình luận và ảnh, rồi dựng video dọc 9:16 với giọng đọc tiếng Việt [VieNeu-TTS](https://github.com/Soetiee2207/VieNeu-TTS), video nền, nhạc nền và meme.

## Cài đặt

Cần `uv` và `ffmpeg` (có sẵn trong PATH).

```bash
uv sync
uv run playwright install chromium
```

Lần tạo giọng đọc đầu tiên sẽ tải model VieNeu-TTS (~500MB) vào `.cache/hf/`.

## Giao diện web

```bash
uv run threads-crawler web          # mở http://127.0.0.1:8000
```

- **Crawl**: nhập từ khóa hoặc #hashtag (mỗi dòng một từ khóa).
- **Kho bài**: lọc theo từ khóa, nội dung; xem bài kèm bình luận; bấm "Làm video". Xuất video xong, các bài đã dùng tự động bị xóa khỏi kho và không bị crawl lại.
- **Kho bài → Tạo video**: tick chọn một hoặc nhiều bài, bấm "🎬 Tạo video mới" (hoặc "➕ Thêm vào video đang làm").
- **Trình dựng**:
  - Cột trái: các bài trong video. Bấm vào bình luận để thêm/bớt thẻ. Có thể đổi thứ tự bài hoặc bỏ bài.
  - Giữa: trình phát xem trước. Bấm ▶ hoặc Space để nghe giọng đọc, nhạc, meme cùng video nền; bấm lên thước thời gian để tua.
  - Phải: thuộc tính của mục đang chọn (chữ trên thẻ, lời đọc, vị trí/kích thước meme, nhạc, video nền). Khi không chọn gì thì hiện cài đặt giọng đọc, tốc độ, kiểu thẻ.
  - Timeline gồm các hàng Chữ, Thẻ + giọng, Meme, Nhạc, Nền:
    - Kéo thẻ để đổi thứ tự; kéo mép phải của thẻ để giữ thẻ lâu hơn.
    - Kéo meme từ thư viện thả vào timeline; kéo meme, chữ, nhạc để dời; kéo mép phải để đổi độ dài.
    - Meme và chữ gắn theo thẻ: đổi thứ tự thẻ thì chúng đi theo.
  - Giọng đọc được tạo dần ở nền. Thẻ "≈" là thời lượng ước tính, "🔊" là đã có giọng thật.
  - Ctrl+Z để hoàn tác, Delete để xóa mục đang chọn. Dự án được lưu tự động trong trình duyệt.
- **Video đã xuất**: xem, tải về, xóa.

Thư mục `materials/`:

```
materials/background_videos/*.mp4          video nền (ngang hay dọc đều được, tự cắt 9:16)
materials/background_musics/<thư mục>/*.mp3 nhạc nền, thư mục con = nhóm (funny, sadness...)
materials/meme_video/<thư mục>/*.mp4        meme, phông xanh/xanh dương được tách tự động
```

## Dòng lệnh

```bash
uv run threads-crawler login                       # chỉ cần nếu Threads bắt đăng nhập
uv run threads-crawler search "chuyện công sở" "#tamsu" -n 30 -r 10 --min-likes 100
uv run threads-crawler export -o data/export.json
uv run threads-crawler mark-used <code> [<code> ...]
```

## Dữ liệu

- `data/threads.db`: bảng `posts`. `parent_code` NULL là bài gốc; khác NULL là reply. Bài đã làm video được tự động xóa khỏi `posts` (kèm bình luận và media đã tải) và ghi vào bảng `done_posts`, để lần crawl sau không thêm lại.
- `data/media/<code>/`: ảnh/video đã tải.
- `data/videos/`: video đã xuất (`.mp4` + ảnh bìa `.jpg`), danh sách lưu ở bảng `videos`.
- `data/export.json`: dữ liệu xuất ra dạng JSON.

## Cách hoạt động

Threads render bằng JS, dữ liệu nằm trong JSON nhúng (`script[data-sjs]`) và các response GraphQL. Crawler bắt cả hai nguồn và nhận diện post theo cấu trúc object (`threads_crawler/extract.py`), nên ít bị ảnh hưởng khi Meta đổi giao diện.

## Lưu ý

- Scraping vi phạm điều khoản của Meta. Hãy crawl chậm, số lượng vừa phải, và dùng tài khoản phụ.
- Khi đăng lên TikTok, hãy ghi nguồn tác giả và thêm giá trị riêng (bình luận, giọng đọc) để tránh vi phạm bản quyền.
