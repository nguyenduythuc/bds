# batdongsan-hanoi-scraper

Crawl dự án chung cư Hà Nội và tin đăng bán từ **batdongsan.com.vn**.

## Yêu cầu

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Sử dụng

```bash
# Khám phá tất cả dự án (lần đầu)
python batdongsan_scraper.py --mode projects

# Crawl tin đăng theo từng dự án đã khám phá
python batdongsan_scraper.py --mode listings

# Cả hai (default)
python batdongsan_scraper.py --mode all

# Legacy: crawl trang listing tổng (không phân theo dự án)
python batdongsan_scraper.py --mode generic
```

### Tuỳ chọn phổ biến

| Flag | Mặc định | Mô tả |
|---|---|---|
| `--mode` | `all` | `projects \| listings \| all \| generic` |
| `--max-project-pages` | 0 (tất cả) | Giới hạn số trang /du-an/ |
| `--max-listing-pages` | 0 (tất cả) | Giới hạn số trang listing mỗi dự án |
| `--db` | `batdongsan.db` | Đường dẫn file SQLite |
| `--skip-geocode` | off | Bỏ qua geocoding Nominatim sau khi crawl projects |
| `--inspect` | off | Dump HTML trang /du-an/ ra /tmp để debug selectors |

### Chạy daily (cron)

```bash
# Ví dụ: chạy lúc 6 giờ sáng mỗi ngày
0 6 * * * cd /path/to/bds && venv/bin/python batdongsan_scraper.py --mode listings --skip-geocode
```

Mỗi ngày chạy sẽ tạo **1 snapshot mới** cho mỗi tin đăng (PK = `listing_id + crawl_date`). Chạy nhiều lần trong ngày không tạo duplicate.

---

## Schema

### `projects`

| Cột | Kiểu | Mô tả |
|---|---|---|
| `project_slug` | TEXT PK | Định danh dự án, ví dụ `vinhomes-ocean-park` |
| `project_id` | TEXT | ID nội bộ của batdongsan (từ URL `-pj{id}`) |
| `project_name` | TEXT | Tên đầy đủ |
| `developer` | TEXT | Chủ đầu tư |
| `district` | TEXT | Quận/Huyện |
| `ward` | TEXT | Phường/Xã |
| `status` | TEXT | `Đang mở bán` / `Đã bàn giao` / `Sắp mở bán` |
| `price_min` | REAL | Giá thấp nhất (tỷ) |
| `price_max` | REAL | Giá cao nhất (tỷ) |
| `total_units` | INTEGER | Tổng số căn hộ |
| `listing_url` | TEXT | Path dùng để crawl tin đăng, ví dụ `nha-dat-ban-vinhomes-ocean-park` |
| `lat` / `lng` | REAL | Toạ độ (Nominatim) |
| `geocoded` | INTEGER | `1`=thành công, `-1`=thất bại, `0`=chưa geocode |
| `first_seen` | TEXT | `YYYY-MM` lần đầu thấy |
| `crawl_date` | TEXT | Ngày crawl gần nhất |
| `detail_crawled` | TEXT | Ngày crawl detail gần nhất |

### `listings`

| Cột | Kiểu | Mô tả |
|---|---|---|
| `listing_id` | TEXT PK† | ID tin đăng |
| `crawl_date` | TEXT PK† | Ngày crawl (`YYYY-MM-DD`) |
| `crawl_month` | TEXT | Tháng crawl (`YYYY-MM`) — dùng cho GROUP BY tháng |
| `project_slug` | TEXT FK | Liên kết với `projects` |
| `title` | TEXT | Tiêu đề tin |
| `url` | TEXT | URL tin đăng |
| `price_ty` | REAL | Giá (tỷ đồng) |
| `area_m2` | REAL | Diện tích (m²) |
| `price_per_m2` | REAL | Giá/m² (triệu đồng) |
| `bedrooms` | INTEGER | Số phòng ngủ |
| `bathrooms` | INTEGER | Số phòng tắm |
| `district` | TEXT | Quận/Huyện |
| `ward` | TEXT | Phường/Xã |
| `post_date` | TEXT | Ngày đăng tin (`YYYY-MM-DD`) |
| `post_month` | TEXT | Tháng đăng tin (`YYYY-MM`) |

† `PRIMARY KEY (listing_id, crawl_date)` — mỗi tin, mỗi ngày = 1 hàng duy nhất.

### `crawl_runs`

| Cột | Mô tả |
|---|---|
| `id` | Auto PK |
| `mode` | `projects \| project-listings \| generic` |
| `started_at` | ISO timestamp |
| `finished_at` | ISO timestamp |
| `pages_done` | Số trang đã crawl |
| `items_new` | Số bản ghi mới INSERT |
| `total_pages` | Ước tính tổng trang |

---

## Dedup

- **Dự án**: `ON CONFLICT(project_slug) DO UPDATE SET ...` — upsert, không bao giờ mất data cũ.
- **Tin đăng**: `INSERT OR IGNORE` với PK `(listing_id, crawl_date)`.
  - Cùng ngày chạy N lần → chỉ INSERT lần đầu.
  - Ngày tiếp theo chạy → INSERT snapshot mới (crawl_date khác).

## Query mẫu

```sql
-- Số dự án
SELECT count(*) FROM projects;

-- Giá trung bình theo dự án (tháng gần nhất)
SELECT p.project_name, count(*) as so_tin, round(avg(l.price_per_m2), 1) as avg_gia_m2
FROM listings l
JOIN projects p ON p.project_slug = l.project_slug
WHERE l.crawl_month = strftime('%Y-%m', 'now')
GROUP BY l.project_slug
ORDER BY avg_gia_m2 DESC;

-- Lịch sử giá theo tháng của 1 dự án
SELECT crawl_month, count(*) so_tin, round(avg(price_per_m2), 1) avg_m2
FROM listings
WHERE project_slug = 'vinhomes-ocean-park'
GROUP BY crawl_month
ORDER BY crawl_month;

-- Lần crawl gần nhất
SELECT mode, started_at, items_new FROM crawl_runs ORDER BY id DESC LIMIT 5;
```

---

## Kiến trúc

```
Phase 1: Project Discovery (--mode projects)
  POST /microservice-architecture-router/ProjectNet/ProjectSearch/GetProjectListData
  → HTML fragment → BeautifulSoup → upsert projects

Phase 2: Per-Project Listings (--mode listings)
  Playwright navigate batdongsan.com.vn/nha-dat-ban-{slug}/
  → JS extract cards → parse → INSERT OR IGNORE listings
```

Cloudflare bypass: Playwright load trang 1 lần, sau đó dùng `page.evaluate(fetch(...))` trong browser context để gọi API — tránh TLS fingerprint mismatch.
