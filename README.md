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

- **Crawl**: nhập từ khóa hoặc #hashtag (mỗi dòng một từ khóa), link bài, hoặc tài khoản.
  - Mặc định chỉ giữ bài đăng trong 14 ngày, có ít nhất 3 bình luận, bỏ bài quá ngắn (không ảnh) và bài quảng cáo; lấy kết quả ở cả tab Nổi bật lẫn Mới nhất rồi giữ N bài hot nhất (tương tác / tuổi bài). Crawl xong sẽ báo số bài bị loại theo từng lý do.
  - **Tài khoản nguồn**: danh sách tài khoản chuyên đăng đúng mảng content, crawl đều đặn để có bài mới. Thêm bằng tay, từ gợi ý (tác giả hay có bài like cao), hoặc nút 👤+ trên bài trong Kho bài.
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
- **Sản phẩm**: nhập tên, giá, ảnh, lời giới thiệu và dán đánh giá thật (mỗi dòng một đánh giá), tool tạo thẻ giới thiệu, thẻ đánh giá kiểu Threads có số sao và nguồn, thẻ kêu gọi, rồi mở trong trình dựng. Giỏ hàng gắn trên TikTok khi đăng video.
  - Dán link Shopee rồi bấm **Lấy thông tin** để điền sẵn tên, giá, mô tả, ảnh và đánh giá chữ. Tool mở cửa sổ trình duyệt; đăng nhập hoặc xác minh thì tự làm trong đó. Video và ảnh của người mua không được tải về.
  - Trong trình dựng, nút **Thêm video của tôi** đưa video bạn có quyền dùng vào nhóm `video của tôi` của thư viện meme (`materials/meme_video/video_của_tôi/`), rồi chỉnh vị trí, cỡ, âm lượng, độ dài như meme.
- **LIVE**: thế giới ảo phản ứng theo tương tác thật trên TikTok LIVE. Thả tim làm thú cưng lớn lên, quà mở khóa công trình, comment `mưa`/`tuyết`/`nắng`/`đêm`/`nhảy`/`ăn` đổi cảnh, người comment hoặc follow trở thành cư dân. Thêm `http://127.0.0.1:8000/stage.html` vào OBS làm Browser Source 1080×1920. Tiến trình lưu ở `data/live_world.json`. Việc đọc sự kiện dùng thư viện không chính thức `TikTokLive`, nên có thể hỏng khi TikTok thay đổi.
  - Âm thanh: nhạc lấy từ `materials/background_musics`, hiệu ứng từ `materials/sfx`, chỉnh trong tab LIVE. Trong Browser Source phải tick "Control audio via OBS" thì âm thanh mới lên live.
  - Nhân vật AI: chép `idle` (lặp liên tục), và tùy chọn `gift`, `levelup` (phát khi có quà, lên cấp), dạng `.webm` hoặc `.mp4`, vào `materials/live_host/`. Sân khấu luôn gắn nhãn "Nhân vật AI".

Shopee có thể chặn phiên tự động. Khi đó dùng extension trong `chrome_extension/`: vào `chrome://extensions`, bật Developer mode, chọn Load unpacked, trỏ tới thư mục này. Giữ `threads-crawler web` chạy ở cổng 8000 và bật extension. Khi bạn tự xem review trên Chrome, các response trang đã nhận được lưu vào tab **Review Shopee**. Extension không tự bấm, không tự mở trang và không gửi request mới tới Shopee. Trong tab, bấm **Chọn làm video** trên từng bình luận, ghi nhận định và trạng thái xin phép (lưu ở `data/shopee_reviews/marks.json`), rồi dùng thanh dưới cùng: **Đưa vào Video review** chuyển video sang trình ghép, **Danh sách test** tải bảng CSV để ghi kết quả, **Tải video đã chọn** tải video của các bình luận đó về xem.

Tab **Video review** là trình ghép riêng, không liên quan tới trình dựng Threads: chọn video và ảnh, kéo để đổi thứ tự, cắt đoạn, giữ hoặc tắt tiếng, rồi xuất video dọc 1080×1920 không nhạc (chọn nhạc khi đăng TikTok). Nguồn gồm file bạn tự tải lên (`data/review_studio/uploads/`) và video, ảnh trong các review ở trạng thái **Đã đồng ý**. Ảnh review được lưu khi extension ghi lại trang đánh giá, nên review đã ghi từ trước cần xem lại một lần trên Chrome.

Mỗi mục có ô lời đọc riêng, đọc bằng giọng VieNeu. **Tạo lời đọc từ dữ liệu** điền bản nháp từ tên sản phẩm, số sao và nội dung review đã crawl. Đây là mẫu câu, không dùng mô hình AI viết, và không viết thay kết quả test của bạn. Khi có giọng đọc, tiếng gốc của clip giảm xuống 15%; nếu lời đọc dài hơn đoạn đó, khung hình cuối được giữ cho đọc hết.

Thu thập đánh giá Shopee thành dữ liệu, trên tab **Review Shopee** của giao diện web (có công tắc tải video, tiến độ, nút dừng, bảng xem review và video) hoặc bằng dòng lệnh:

```bash
uv run threads-crawler shopee-reviews "https://shopee.vn/ten-sp-i.123.456" -n 500
uv run threads-crawler shopee-reviews -f links.txt --delay 5 --timeout 60 --retries 3

# Chỉ cài phần crawler, không dùng uv:
pip install -r requirements.txt && playwright install chromium
python -m threads_crawler.cli shopee-reviews -f links.txt
```

Mỗi sản phẩm có `data/shopee_reviews/<shop>_<item>.jsonl` và `.csv`, toàn bộ gộp ở `all_reviews.jsonl` và `.csv`. Các cột: `product_id`, `product_url`, `review_id`, `username`, `review_content`, `rating`, `review_date`, `video_url`, `video_count`. Nhiều video thì `video_url` cách nhau bằng dấu cách.

Chạy lại lệnh để lấy tiếp: đánh giá đã có được bỏ qua, sản phẩm đã hết đánh giá không mở lại (trừ khi thêm `--refresh`). Lỗi mạng và timeout được thử lại, chờ tăng dần. Tool mở cửa sổ trình duyệt; đăng nhập hoặc xác minh thì tự làm trong đó. Nếu quá giờ chờ hoặc API từ chối mà không lấy thêm được gì, cả lô dừng với mã thoát 1, không thử lại, và phần đã lấy được giữ nguyên. Tên người mua được che, ảnh không được lấy.

Thêm `--download-videos` để tải video của người mua vào `data/shopee_reviews/<shop>_<item>_videos/`, chạy lại sẽ thử lại video lỗi. Quyền với video vẫn thuộc về người mua: cần họ đồng ý trước khi đăng lại hoặc dùng để bán hàng. Tab **Sản phẩm** vẫn không tải video.

Thư mục `materials/`:

```
materials/background_videos/*.mp4          video nền (ngang hay dọc đều được, tự cắt 9:16)
materials/background_musics/<thư mục>/*.mp3 nhạc nền, thư mục con = nhóm (funny, sadness...)
materials/meme_video/<thư mục>/*.mp4        meme, phông xanh/xanh dương được tách tự động
```

## Dòng lệnh

```bash
uv run threads-crawler login                       # chỉ cần nếu Threads bắt đăng nhập
uv run threads-crawler search "sếp mình" "#congso" -n 30 -r 10 --min-likes 100
uv run threads-crawler sources -n 10                # bài mới của mọi tài khoản nguồn
# lọc: --max-age 14 (0 = mọi lúc), --min-replies 3, --tabs both|top|recent, --no-filter
# bình luận: -r 10 bình luận gốc, --branches 3 nhánh mỗi bình luận (lần tới tận cùng), --expand-pages 60
uv run threads-crawler export -o data/export.json
uv run threads-crawler mark-used <code> [<code> ...]
uv run threads-crawler greenscreen -n 50           # tải meme phông xanh từ greenscreenmemes.com (tự phân loại cảm xúc)
uv run threads-crawler greenscreen -c "cười" -n 20 # chỉ tải meme nhóm cảm xúc này
uv run threads-crawler memes -n 50                 # tải meme từ vlipsy.com
uv run threads-crawler sfx -n 100                  # tải hiệu ứng âm thanh từ myinstants.com
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
