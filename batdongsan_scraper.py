"""
Crawl chung cư HN từ batdongsan.com.vn
- Mỗi tin đăng lưu 1 dòng riêng (không tổng hợp)
- Dự án mới tự động geocode (Nominatim OSM) sau mỗi lần crawl

Schema:
  projects  (project_slug PK, project_name, district, ward, lat, lng, ...)
  listings  (listing_id + crawl_month PK, project_slug FK, price_ty, area_m2,
             price_per_m2, bedrooms, bathrooms, post_date, post_month, ...)
"""

import re
import asyncio
import sqlite3
import logging
import argparse
import time
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_URL    = "https://batdongsan.com.vn"
LIST_SLUG   = "ban-can-ho-chung-cu-tp-ha-noi"
LIST_URL    = f"{BASE_URL}/{LIST_SLUG}"
DB_PATH     = "batdongsan.db"
PAGE_DELAY  = 2.5
NAV_TIMEOUT = 40_000

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


# ─── Database ─────────────────────────────────────────────────────────────────

def init_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            project_slug    TEXT PRIMARY KEY,
            project_id      TEXT,
            project_name    TEXT,
            district        TEXT,
            ward            TEXT,
            lat             REAL,
            lng             REAL,
            geocoded        INTEGER DEFAULT 0,
            first_seen      TEXT,
            crawl_date      TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            listing_id      TEXT,
            crawl_month     TEXT,
            project_slug    TEXT,
            title           TEXT,
            url             TEXT,
            price_ty        REAL,
            area_m2         REAL,
            price_per_m2    REAL,
            bedrooms        INTEGER,
            bathrooms       INTEGER,
            district        TEXT,
            ward            TEXT,
            post_date       TEXT,
            post_month      TEXT,
            crawl_date      TEXT,
            PRIMARY KEY (listing_id, crawl_month)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS crawl_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mode            TEXT,
            started_at      TEXT,
            finished_at     TEXT,
            pages_done      INTEGER DEFAULT 0,
            items_new       INTEGER DEFAULT 0,
            total_pages     INTEGER DEFAULT 0
        )
    """)

    # Migrate existing listings table: add columns introduced in new schema
    existing = {row[1] for row in con.execute("PRAGMA table_info(listings)")}
    for col, definition in [
        ("post_date",    "TEXT"),
        ("post_month",   "TEXT"),
        ("project_slug", "TEXT"),
    ]:
        if col not in existing:
            con.execute(f"ALTER TABLE listings ADD COLUMN {col} {definition}")
            log.info(f"Migration: added column listings.{col}")

    # Migrate crawl_runs: add columns introduced in new schema
    existing_runs = {row[1] for row in con.execute("PRAGMA table_info(crawl_runs)")}
    for col, definition in [
        ("mode",        "TEXT"),
        ("items_new",   "INTEGER DEFAULT 0"),
        ("total_pages", "INTEGER DEFAULT 0"),
    ]:
        if col not in existing_runs:
            con.execute(f"ALTER TABLE crawl_runs ADD COLUMN {col} {definition}")
            log.info(f"Migration: added column crawl_runs.{col}")

    con.execute("CREATE INDEX IF NOT EXISTS idx_listings_project   ON listings(project_slug)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_listings_postmonth ON listings(post_month)")
    con.commit()
    return con


# ─── Parsing helpers ──────────────────────────────────────────────────────────

def parse_price(text: str) -> float | None:
    """'11,96 tỷ' → 11.96  |  '800 triệu' → 0.8  |  'Thỏa thuận' → None"""
    t = text.lower().strip()
    m = re.search(r"([\d,\.]+)\s*tỷ", t)
    if m:
        return float(m.group(1).replace(",", "."))
    m = re.search(r"([\d,\.]+)\s*triệu", t)
    if m:
        return round(float(m.group(1).replace(",", ".")) / 1000, 4)
    return None


def parse_area(text: str) -> float | None:
    m = re.search(r"([\d,\.]+)\s*m", text, re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


def parse_price_m2(text: str) -> float | None:
    """'115 tr/m²' → 115.0"""
    m = re.search(r"([\d,\.]+)\s*tr", text, re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


def extract_project_from_url(href: str) -> tuple[str | None, str | None]:
    """
    '/ban-can-ho-chung-cu-imperia-sky-park-phuong-dai-mo-tp-ha-noi/pr123'
    → ('imperia-sky-park', 'Imperia Sky Park')
    """
    path = href.lstrip("/")
    if not path.startswith("ban-can-ho-chung-cu-"):
        return None, None
    rest = path[len("ban-can-ho-chung-cu-"):]
    rest = rest.split("/")[0]
    slug = re.sub(
        r"-(phuong|quan|huyen|thi-xa|thi-tran)-[\w-]+-tp-ha-noi$|-tp-ha-noi$|-ha-noi$",
        "", rest,
    )
    if not slug or len(slug) < 3:
        return None, None
    return slug, slug.replace("-", " ").title()


def parse_district_ward(location_text: str) -> tuple[str, str]:
    """
    'P. Đại Mỗ (Q. Nam Từ Liêm cũ)' → ('Nam Từ Liêm', 'Đại Mỗ')
    'Q. Hoàng Mai'                    → ('Hoàng Mai', '')
    """
    ward_m = re.search(r"(?:^|\n)\s*P\.\s*([^(\n]+?)(?:\s*\(|$)", location_text)
    ward = ward_m.group(1).strip() if ward_m else ""

    dist_m = re.search(r"\(\s*(?:Q\.|H\.|Huyện)\s*(.+?)\s*(?:cũ)?\s*\)", location_text)
    if not dist_m:
        dist_m = re.search(r"(?:Q\.|Quận)\s*([^\n(]+?)(?:\s*\(|$)", location_text)
    district = dist_m.group(1).strip() if dist_m else ""
    return district, ward


def resolve_post_date(text: str, today: datetime) -> tuple[str | None, str | None]:
    """
    Chuyển ngày đăng tương đối → (post_date yyyy-mm-dd, post_month yyyy-mm)
    Ví dụ: '2 ngày trước' → ('2026-05-01', '2026-05')
    """
    if not text:
        return None, None
    t = text.lower().strip()

    # Ngày tuyệt đối: dd/mm/yyyy hoặc dd-mm-yyyy
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", t)
    if m:
        try:
            d = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            return d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")
        except ValueError:
            pass

    if "hôm nay" in t or "vừa đăng" in t:
        d = today
    elif "hôm qua" in t:
        d = today - timedelta(days=1)
    else:
        m_h  = re.search(r"(\d+)\s*giờ", t)
        m_d  = re.search(r"(\d+)\s*ngày", t)
        m_w  = re.search(r"(\d+)\s*tuần", t)
        m_mo = re.search(r"(\d+)\s*tháng", t)

        if m_h:
            d = today
        elif m_d:
            d = today - timedelta(days=int(m_d.group(1)))
        elif m_w:
            d = today - timedelta(weeks=int(m_w.group(1)))
        elif m_mo:
            d = today - timedelta(days=int(m_mo.group(1)) * 30)
        else:
            return None, None

    return d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")


# ─── Geocoding (Nominatim) ────────────────────────────────────────────────────

def geocode_nominatim(query: str) -> tuple[float | None, float | None]:
    """Gọi Nominatim OSM. Tuân thủ rate-limit 1 req/giây."""
    params = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "vn",
    })
    url = f"https://nominatim.openstreetmap.org/search?{params}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "batdongsan-hanoi-research/1.0 duythucbk@gmail.com"}
    )
    try:
        time.sleep(1.2)  # Nominatim ToS: tối đa 1 req/giây
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        log.warning(f"Nominatim lỗi cho '{query}': {e}")
    return None, None


def geocode_all_projects(con: sqlite3.Connection) -> None:
    """Geocode tất cả dự án có geocoded=0."""
    rows = con.execute("""
        SELECT project_slug, project_name, district, ward
        FROM projects
        WHERE geocoded = 0 AND project_name IS NOT NULL
        ORDER BY project_slug
    """).fetchall()

    log.info(f"Geocoding {len(rows)} dự án chưa có tọa độ...")
    ok = fail = 0

    for slug, name, district, ward in rows:
        # Thử query chi tiết nhất trước
        parts = [p for p in [name, ward, district, "Hà Nội"] if p]
        lat, lng = geocode_nominatim(", ".join(parts))

        if lat is None and district:
            lat, lng = geocode_nominatim(f"{name}, {district}, Hà Nội")

        if lat is None:
            lat, lng = geocode_nominatim(f"{name}, Hà Nội")

        geocoded = 1 if lat else -1
        con.execute(
            "UPDATE projects SET lat=?, lng=?, geocoded=? WHERE project_slug=?",
            (lat, lng, geocoded, slug),
        )
        con.commit()

        if lat:
            ok += 1
            log.info(f"  ✓ {name} → ({lat:.5f}, {lng:.5f})")
        else:
            fail += 1
            log.warning(f"  ✗ {name} [{slug}] không tìm được tọa độ")

    log.info(f"Geocoding xong: {ok} thành công, {fail} thất bại.")


# ─── DB helpers ───────────────────────────────────────────────────────────────

def upsert_project(
    con: sqlite3.Connection,
    slug: str, name: str, district: str, ward: str,
    crawl_month: str, crawl_date: str,
) -> None:
    """Thêm dự án nếu chưa có; bổ sung district/ward nếu còn trống."""
    con.execute("""
        INSERT INTO projects (project_slug, project_name, district, ward, first_seen, crawl_date)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_slug) DO UPDATE SET
            project_name = COALESCE(projects.project_name, excluded.project_name),
            district     = CASE WHEN projects.district = '' OR projects.district IS NULL
                                THEN excluded.district ELSE projects.district END,
            ward         = CASE WHEN projects.ward = '' OR projects.ward IS NULL
                                THEN excluded.ward ELSE projects.ward END
    """, (slug, name, district, ward, crawl_month, crawl_date))


def save_listings(con: sqlite3.Connection, listings: list[dict]) -> int:
    saved = 0
    for item in listings:
        try:
            con.execute("""
                INSERT OR IGNORE INTO listings
                    (listing_id, crawl_month, project_slug, title, url,
                     price_ty, area_m2, price_per_m2, bedrooms, bathrooms,
                     district, ward, post_date, post_month, crawl_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                item["listing_id"], item["crawl_month"], item["project_slug"],
                item["title"], item["url"],
                item["price_ty"], item["area_m2"], item["price_per_m2"],
                item["bedrooms"], item["bathrooms"],
                item["district"], item["ward"],
                item["post_date"], item["post_month"], item["crawl_date"],
            ))
            saved += con.execute("SELECT changes()").fetchone()[0]
        except Exception as e:
            log.warning(f"DB insert lỗi cho {item['listing_id']}: {e}")
    con.commit()
    return saved


# ─── JS extraction ────────────────────────────────────────────────────────────

EXTRACT_JS = """
() => {
    return Array.from(document.querySelectorAll('.re__card-full')).map(card => {
        const link  = card.querySelector('.js__product-link-for-product-id');
        const spans = Array.from(card.querySelectorAll('.re__card-config span'))
                          .map(s => s.innerText.trim())
                          .filter(s => s && s !== '·');
        const locEl  = card.querySelector('.re__card-location');
        const timeEl = card.querySelector('.re__card-published-info-published-at');
        return {
            id:       link?.dataset?.productId || '',
            href:     link?.getAttribute('href') || '',
            title:    (link?.title || '').substring(0, 150),
            config:   spans,
            location: locEl?.innerText?.trim() || '',
            time:     timeEl?.innerText?.trim() || '',
        };
    });
}
"""

PAGINATION_JS = """
() => {
    const links = Array.from(document.querySelectorAll('[class*="pagination"] a'));
    const nums = links.map(a => {
        const m = a.href.match(/\/p(\d+)$/);
        return m ? parseInt(m[1]) : 0;
    }).filter(n => n > 0);
    return nums.length ? Math.max(...nums) : 1;
}
"""


# ─── Card parsing ─────────────────────────────────────────────────────────────

def parse_cards(
    raw_cards: list[dict],
    crawl_date: str,
    crawl_month: str,
    today: datetime,
) -> list[dict]:
    results = []
    for c in raw_cards:
        lid = c.get("id", "").strip()
        if not lid:
            continue

        cfg = c.get("config", [])
        price_ty     = parse_price(cfg[0])     if len(cfg) > 0 else None
        area_m2      = parse_area(cfg[1])      if len(cfg) > 1 else None
        price_per_m2 = parse_price_m2(cfg[2])  if len(cfg) > 2 else None
        bedrooms     = int(cfg[3]) if len(cfg) > 3 and cfg[3].isdigit() else None
        bathrooms    = int(cfg[5]) if len(cfg) > 5 and cfg[5].isdigit() else None

        if price_per_m2 is None and price_ty and area_m2 and area_m2 > 0:
            price_per_m2 = round(price_ty * 1000 / area_m2, 2)

        district, ward = parse_district_ward(c.get("location", ""))
        project_slug, project_name = extract_project_from_url(c.get("href", ""))
        post_date, post_month = resolve_post_date(c.get("time", ""), today)

        href = c.get("href", "")
        url = BASE_URL + href.split("/pr")[0] if href else ""

        results.append({
            "listing_id":    lid,
            "crawl_month":   crawl_month,
            "project_slug":  project_slug,
            "_project_name": project_name,
            "title":         c.get("title", ""),
            "url":           url,
            "price_ty":      price_ty,
            "area_m2":       area_m2,
            "price_per_m2":  price_per_m2,
            "bedrooms":      bedrooms,
            "bathrooms":     bathrooms,
            "district":      district,
            "ward":          ward,
            "post_date":     post_date,
            "post_month":    post_month,
            "crawl_date":    crawl_date,
        })
    return results


# ─── Navigation ───────────────────────────────────────────────────────────────

async def nav_with_retry(page, url: str, retries: int = 3) -> bool:
    for attempt in range(1, retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            await page.wait_for_selector(".re__card-full", timeout=15_000)
            return True
        except Exception as e:
            log.warning(f"Nav lần {attempt}/{retries} thất bại cho {url}: {e}")
            if attempt < retries:
                await asyncio.sleep(attempt * 3)
    return False


# ─── Crawl listings ──────────────────────────────────────────────────────────

async def crawl_listings(max_pages: int, start_page: int, db_path: str) -> None:
    con = init_db(db_path)
    now = datetime.now()
    crawl_date  = now.strftime("%Y-%m-%d")
    crawl_month = now.strftime("%Y-%m")
    run_start   = now.isoformat()

    pages_done = listings_new = total_pages = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            locale="vi-VN",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        log.info(f"Crawl listings tháng {crawl_month} | DB: {db_path}")
        url1 = LIST_URL if start_page == 1 else f"{LIST_URL}/p{start_page}"
        if not await nav_with_retry(page, url1):
            log.error("Không thể load trang đầu, dừng.")
            await browser.close()
            return

        total_pages = await page.evaluate(PAGINATION_JS)
        if max_pages > 0:
            total_pages = min(total_pages, start_page - 1 + max_pages)
        log.info(f"Tổng số trang: {total_pages} | Bắt đầu từ trang {start_page}")

        async def process_page(p_num: int) -> int:
            raw   = await page.evaluate(EXTRACT_JS)
            cards = parse_cards(raw, crawl_date, crawl_month, now)
            for c in cards:
                if c["project_slug"]:
                    upsert_project(
                        con, c["project_slug"], c["_project_name"],
                        c["district"], c["ward"], crawl_month, crawl_date,
                    )
            return save_listings(con, cards)

        new = await process_page(start_page)
        listings_new += new
        pages_done   += 1
        log.info(f"Trang {start_page}/{total_pages} — +{new} tin (tổng: {listings_new})")

        for pnum in range(start_page + 1, total_pages + 1):
            await asyncio.sleep(PAGE_DELAY)
            url = f"{LIST_URL}/p{pnum}"
            if not await nav_with_retry(page, url):
                log.warning(f"Bỏ qua trang {pnum}")
                continue
            new = await process_page(pnum)
            listings_new += new
            pages_done   += 1
            if pages_done % 10 == 0:
                log.info(f"Trang {pnum}/{total_pages} — +{new} tin (tổng: {listings_new})")
            else:
                log.debug(f"Trang {pnum}/{total_pages} — +{new}")

        await browser.close()

    con.execute("""
        INSERT INTO crawl_runs (mode, started_at, finished_at, pages_done, items_new, total_pages)
        VALUES (?,?,?,?,?,?)
    """, ("listings", run_start, datetime.now().isoformat(), pages_done, listings_new, total_pages))
    con.commit()

    # Geocode dự án mới phát hiện trong lần crawl này
    geocode_all_projects(con)

    con.close()
    log.info(f"Hoàn thành: {pages_done} trang, {listings_new} tin mới → {db_path}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Crawl chung cư HN từ batdongsan.com.vn")
    ap.add_argument("--max-pages",  type=int, default=0,   help="Giới hạn số trang (0=tất cả)")
    ap.add_argument("--start-page", type=int, default=1,   help="Bắt đầu từ trang nào (resume)")
    ap.add_argument("--db",         default=DB_PATH,       help="Đường dẫn file SQLite")
    args = ap.parse_args()

    asyncio.run(crawl_listings(args.max_pages, args.start_page, args.db))
