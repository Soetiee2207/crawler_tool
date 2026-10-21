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
- **Kho bài**: lọc theo từ khóa, nội dung, trạng thái đã làm video; xem bài kèm bình luận; bấm "Làm video".
- **Tạo video**:
  - Chọn bình luận theo thứ tự đọc. Phần "tác giả viết tiếp" được chọn sẵn.
  - Sửa chữ trên thẻ và lời đọc.
  - Chọn giọng đọc, nghe thử, chỉnh tốc độ. Có chế độ Nhanh (int8).
  - Chọn video nền. Tool chỉ cắt ngẫu nhiên một đoạn vừa đủ thời lượng.
  - Chọn nhạc nền theo thư mục hoặc từng bài.
  - Thêm meme: chọn đoạn xuất hiện, độ trễ, vị trí 3×3, kích thước, âm lượng. Phông xanh được tự tách.
  - Có ảnh xem trước bố cục.
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
uv run threads-crawler export -o data/export.json --unused
uv run threads-crawler mark-used <code> [<code> ...]
```

## Dữ liệu

- `data/threads.db`: bảng `posts`. `parent_code` NULL là bài gốc; khác NULL là reply. Cột `used` đánh dấu bài đã làm video.
- `data/media/<code>/`: ảnh/video đã tải.
- `data/videos/`: video đã xuất (`.mp4` + ảnh bìa `.jpg`), danh sách lưu ở bảng `videos`.
- `data/export.json`: dữ liệu xuất ra dạng JSON.

## Cách hoạt động

Threads render bằng JS, dữ liệu nằm trong JSON nhúng (`script[data-sjs]`) và các response GraphQL. Crawler bắt cả hai nguồn và nhận diện post theo cấu trúc object (`threads_crawler/extract.py`), nên ít bị ảnh hưởng khi Meta đổi giao diện.

## Lưu ý

- Scraping vi phạm điều khoản của Meta. Hãy crawl chậm, số lượng vừa phải, và dùng tài khoản phụ.
- Khi đăng lên TikTok, hãy ghi nguồn tác giả và thêm giá trị riêng (bình luận, giọng đọc) để tránh vi phạm bản quyền.
