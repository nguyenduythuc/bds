"""
Crawl chung cư HN từ batdongsan.com.vn

Modes:
  projects  — Khám phá tất cả dự án từ /du-an/can-ho-chung-cu-ha-noi
  listings  — Crawl tin đăng cho từng dự án đã có trong DB
  all       — Cả hai (default)
  generic   — Crawl trang listing tổng (legacy)

Schema:
  projects  (project_slug PK, project_name, developer, district, ward,
             status, price_min, price_max, total_units, listing_url,
             lat, lng, geocoded, first_seen, crawl_date, detail_crawled)
  listings  (listing_id + crawl_date PK → mỗi ngày 1 bản ghi / listing,
             crawl_month derived, project_slug FK, price_ty,
             area_m2, price_per_m2, bedrooms, bathrooms,
             district, ward, post_date, post_month, title, url)
  crawl_runs (id, mode, started_at, finished_at, pages_done, items_new, total_pages)

Dedup:
  listings: INSERT OR IGNORE với PK (listing_id, crawl_date)
    → chạy nhiều lần / ngày: không duplicate
    → chạy hàng ngày: mỗi ngày có 1 snapshot
    → GROUP BY crawl_month để xem theo tháng
"""

import re
import math
import asyncio
import sqlite3
import logging
import argparse
import time
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_URL          = "https://batdongsan.com.vn"
LIST_SLUG         = "ban-can-ho-chung-cu-tp-ha-noi"
LIST_URL          = f"{BASE_URL}/{LIST_SLUG}"
PROJECT_LIST_URL  = f"{BASE_URL}/du-an/can-ho-chung-cu-ha-noi"
PROJECT_API_URL   = (f"{BASE_URL}/microservice-architecture-router"
                     f"/ProjectNet/ProjectSearch/GetProjectListData")
# Categories có chứa chung cư Hà Nội (verified 2026-05):
#   155 = Căn hộ chung cư (568 dự án)
#   158 = Nhà ở xã hội     (16 dự án)
#   160 = Khu đô thị mới  (143 dự án) — Goldmark City, Ecopark...
#   161 = Khu phức hợp     (33 dự án)
PROJECT_CATE_IDS  = [155, 158, 160, 161]
PROJECT_CITY_CODE = "HN"
PROJECT_PAGE_SIZE = 10    # cards per page (confirmed from API)
DB_PATH           = "batdongsan.db"
PAGE_DELAY        = 1.5   # giây giữa các API calls (nhẹ hơn DOM scraping)
NAV_TIMEOUT       = 40_000

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
            developer       TEXT,
            district        TEXT,
            ward            TEXT,
            status          TEXT,
            price_min       REAL,
            price_max       REAL,
            total_units     INTEGER,
            listing_url     TEXT,
            lat             REAL,
            lng             REAL,
            geocoded        INTEGER DEFAULT 0,
            first_seen      TEXT,
            crawl_date      TEXT,
            detail_crawled  TEXT
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
            PRIMARY KEY (listing_id, crawl_date)
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

    # Migrate existing tables — additive only, never drops
    existing_proj = {row[1] for row in con.execute("PRAGMA table_info(projects)")}
    for col, defn in [
        ("developer",      "TEXT"),
        ("status",         "TEXT"),
        ("price_min",      "REAL"),
        ("price_max",      "REAL"),
        ("total_units",    "INTEGER"),
        ("listing_url",    "TEXT"),
        ("detail_crawled", "TEXT"),
        ("project_id",     "TEXT"),
    ]:
        if col not in existing_proj:
            con.execute(f"ALTER TABLE projects ADD COLUMN {col} {defn}")
            log.info(f"Migration: added projects.{col}")

    existing_lst = {row[1] for row in con.execute("PRAGMA table_info(listings)")}
    for col, defn in [
        ("post_date",     "TEXT"),
        ("post_month",    "TEXT"),
        ("project_slug",  "TEXT"),
        ("listing_type",  "TEXT"),  # can-ho | shophouse | nha-rieng | dat | other
    ]:
        if col not in existing_lst:
            con.execute(f"ALTER TABLE listings ADD COLUMN {col} {defn}")
            log.info(f"Migration: added listings.{col}")

    # Backfill listing_type từ url nếu chưa có
    if "listing_type" not in existing_lst:
        con.execute("""
            UPDATE listings SET listing_type = CASE
                WHEN url LIKE '%/ban-can-ho-chung-cu-%' THEN 'can-ho'
                WHEN url LIKE '%/ban-shophouse-%'        THEN 'shophouse'
                WHEN url LIKE '%/ban-nha-rieng-%'        THEN 'nha-rieng'
                WHEN url LIKE '%/ban-dat-%'              THEN 'dat'
                WHEN url LIKE '%/ban-nha-biet-thu-%'     THEN 'biet-thu'
                ELSE 'other'
            END
        """)
        log.info("Migration: backfilled listing_type from url")

    existing_runs = {row[1] for row in con.execute("PRAGMA table_info(crawl_runs)")}
    for col, defn in [
        ("mode",        "TEXT"),
        ("items_new",   "INTEGER DEFAULT 0"),
        ("total_pages", "INTEGER DEFAULT 0"),
    ]:
        if col not in existing_runs:
            con.execute(f"ALTER TABLE crawl_runs ADD COLUMN {col} {defn}")
            log.info(f"Migration: added crawl_runs.{col}")

    con.execute("CREATE INDEX IF NOT EXISTS idx_listings_project   ON listings(project_slug)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_listings_postmonth ON listings(post_month)")
    con.commit()
    return con


# ─── Parsing helpers ──────────────────────────────────────────────────────────

def _clean_number(s: str) -> float | None:
    """
    Chuẩn hoá chuỗi số VN → float.
    '11,96' → 11.96  |  '6.583,2' → 6583.2  |  '6.583.2' → None (dị dạng)
    """
    s = s.strip()
    # Trường hợp '6.583,2': dấu . là nghìn, , là thập phân
    if re.fullmatch(r"\d{1,3}(\.\d{3})+,\d+", s):
        return float(s.replace(".", "").replace(",", "."))
    # Trường hợp '11,96': , là thập phân
    if re.fullmatch(r"\d+,\d+", s):
        return float(s.replace(",", "."))
    # Trường hợp '11.96' hoặc '1234': dấu . là thập phân
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    return None


def parse_price(text: str) -> float | None:
    """'11,96 tỷ' → 11.96  |  '800 triệu' → 0.8  |  'Thỏa thuận' → None"""
    t = text.lower().strip()
    m = re.search(r"([\d,\.]+)\s*tỷ", t)
    if m:
        v = _clean_number(m.group(1))
        return v
    m = re.search(r"([\d,\.]+)\s*triệu", t)
    if m:
        v = _clean_number(m.group(1))
        return round(v / 1000, 4) if v is not None else None
    return None


def parse_area(text: str) -> float | None:
    m = re.search(r"([\d,\.]+)\s*m", text, re.IGNORECASE)
    if not m:
        return None
    return _clean_number(m.group(1))


def parse_price_m2(text: str) -> float | None:
    """'115 tr/m²' → 115.0"""
    m = re.search(r"([\d,\.]+)\s*tr", text, re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


def parse_project_prices(text: str) -> tuple[float | None, float | None]:
    """
    'Từ 3 tỷ đến 5 tỷ'  → (3.0, 5.0)
    'Từ 50 triệu/m²'     → (None, None)  — m²-based prices skipped
    '3 - 5 tỷ'           → (3.0, 5.0)
    """
    t = text.lower()
    vals = re.findall(r"([\d,\.]+)\s*tỷ", t)
    floats = [float(v.replace(",", ".")) for v in vals]
    if len(floats) >= 2:
        return min(floats), max(floats)
    if len(floats) == 1:
        return floats[0], None
    return None, None


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


def slug_from_du_an_href(href: str) -> tuple[str | None, str | None]:
    """
    '/du-an-can-ho/vinhomes-ocean-park-pj1234' → ('vinhomes-ocean-park', '1234')
    '/du-an/imperia-sky-park-ha-noi'           → ('imperia-sky-park', None)
    Returns (project_slug, project_id)
    """
    path = href.lstrip("/").split("?")[0].rstrip("/")
    if not path.startswith("du-an"):
        return None, None

    # Lấy phần cuối của path (project slug + optional pj{id})
    last = path.split("/")[-1]

    # Tách project_id từ suffix -pj{digits}
    project_id = None
    m = re.search(r"-pj(\d+)$", last)
    if m:
        project_id = m.group(1)
        last = last[:m.start()]

    # Strip location suffixes
    slug = re.sub(
        r"-(phuong|quan|huyen|thi-xa|thi-tran)-[\w-]+-tp-ha-noi$"
        r"|-tp-ha-noi$|-ha-noi$|-tp-hcm$",
        "", last,
    )
    return (slug if slug and len(slug) >= 3 else None), project_id


def parse_district_ward(location_text: str) -> tuple[str, str]:
    ward_m = re.search(r"(?:^|\n)\s*P\.\s*([^(\n]+?)(?:\s*\(|$)", location_text)
    ward = ward_m.group(1).strip() if ward_m else ""

    dist_m = re.search(r"\(\s*(?:Q\.|H\.|Huyện)\s*(.+?)\s*(?:cũ)?\s*\)", location_text)
    if not dist_m:
        dist_m = re.search(r"(?:Q\.|Quận)\s*([^\n(]+?)(?:\s*\(|$)", location_text)
    district = dist_m.group(1).strip() if dist_m else ""
    return district, ward


def resolve_post_date(text: str, today: datetime) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    t = text.lower().strip()

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
    params = urllib.parse.urlencode({
        "q": query, "format": "json", "limit": 1, "countrycodes": "vn",
    })
    url = f"https://nominatim.openstreetmap.org/search?{params}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "batdongsan-hanoi-research/1.0 duythucbk@gmail.com"}
    )
    try:
        time.sleep(1.2)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        log.warning(f"Nominatim lỗi cho '{query}': {e}")
    return None, None


def geocode_all_projects(con: sqlite3.Connection) -> None:
    rows = con.execute("""
        SELECT project_slug, project_name, district, ward
        FROM projects
        WHERE geocoded = 0 AND project_name IS NOT NULL
        ORDER BY project_slug
    """).fetchall()

    log.info(f"Geocoding {len(rows)} dự án chưa có tọa độ...")
    ok = fail = 0
    for slug, name, district, ward in rows:
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
    slug: str,
    name: str,
    district: str,
    ward: str,
    crawl_month: str,
    crawl_date: str,
    *,
    developer: str = "",
    status: str = "",
    price_min: float | None = None,
    price_max: float | None = None,
    total_units: int | None = None,
    listing_url: str = "",
    detail_crawled: str = "",
    project_id: str = "",
) -> None:
    con.execute("""
        INSERT INTO projects
            (project_slug, project_name, developer, district, ward,
             status, price_min, price_max, total_units, listing_url,
             first_seen, crawl_date, detail_crawled, project_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(project_slug) DO UPDATE SET
            project_name   = COALESCE(projects.project_name,  excluded.project_name),
            developer      = CASE WHEN excluded.developer != '' THEN excluded.developer
                                  ELSE projects.developer END,
            district       = CASE WHEN (projects.district = '' OR projects.district IS NULL)
                                  THEN excluded.district ELSE projects.district END,
            ward           = CASE WHEN (projects.ward = '' OR projects.ward IS NULL)
                                  THEN excluded.ward ELSE projects.ward END,
            status         = CASE WHEN excluded.status != '' THEN excluded.status
                                  ELSE projects.status END,
            price_min      = COALESCE(excluded.price_min, projects.price_min),
            price_max      = COALESCE(excluded.price_max, projects.price_max),
            total_units    = COALESCE(excluded.total_units, projects.total_units),
            listing_url    = CASE WHEN excluded.listing_url != '' THEN excluded.listing_url
                                  ELSE projects.listing_url END,
            detail_crawled = CASE WHEN excluded.detail_crawled != '' THEN excluded.detail_crawled
                                  ELSE projects.detail_crawled END,
            project_id     = CASE WHEN excluded.project_id != '' THEN excluded.project_id
                                  ELSE projects.project_id END
    """, (
        slug, name, developer, district, ward,
        status, price_min, price_max, total_units, listing_url,
        crawl_month, crawl_date, detail_crawled, project_id,
    ))


def save_listings(con: sqlite3.Connection, listings: list[dict]) -> int:
    saved = 0
    for item in listings:
        try:
            con.execute("""
                INSERT OR IGNORE INTO listings
                    (listing_id, crawl_month, project_slug, title, url,
                     listing_type,
                     price_ty, area_m2, price_per_m2, bedrooms, bathrooms,
                     district, ward, post_date, post_month, crawl_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                item["listing_id"], item["crawl_month"], item["project_slug"],
                item["title"], item["url"],
                item.get("listing_type", "other"),
                item["price_ty"], item["area_m2"], item["price_per_m2"],
                item["bedrooms"], item["bathrooms"],
                item["district"], item["ward"],
                item["post_date"], item["post_month"], item["crawl_date"],
            ))
            saved += con.execute("SELECT changes()").fetchone()[0]
        except Exception as e:
            log.warning(f"DB insert lỗi cho {item.get('listing_id')}: {e}")
    con.commit()
    return saved


# ─── JS snippets ──────────────────────────────────────────────────────────────

# Dùng cho trang listing (ban-can-ho-chung-cu-...)
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
        const m = (a.href || '').match(/\/p(\d+)(?:\?|$)/);
        return m ? parseInt(m[1]) : 0;
    }).filter(n => n > 0);
    return nums.length ? Math.max(...nums) : 1;
}
"""

# Dùng cho trang dự án (/du-an/can-ho-chung-cu-ha-noi)
# Selectors đã verify qua --inspect (05-2026)
PROJECT_EXTRACT_JS = """
() => {
    const cards = Array.from(document.querySelectorAll('.js__project-card'));
    return cards.map(card => {
        const anchor = card.querySelector('a.re__clearfix');
        const href   = anchor?.getAttribute('href') || '';

        const titleEl  = card.querySelector('h3.re__prj-card-title');
        const locEl    = card.querySelector('.re__prj-card-location');
        const priceEl  = card.querySelector('.re__prj-card-config-value');
        const devEl    = card.querySelector('.re__prj-card-contact-avatar');
        const unitsEl  = card.querySelector('[aria-label*="\\u0103n h\\u1ed9"]');

        // Status: lấy text của label bên trong div status
        const statusDiv = card.querySelector(
            '.re__project-open, .re__project-finish, .re__project-prepare, .re__project-na'
        );
        const statusLabel = statusDiv?.querySelector('label');

        // Config values: [diện tích ha, số căn hộ (icon home), số tòa (icon building)]
        const configs = Array.from(card.querySelectorAll('.re__prj-card-config-value'))
                            .map(el => ({
                                text: el.innerText.trim(),
                                label: el.getAttribute('aria-label') || '',
                            }));

        return {
            href:       href,
            name:       (titleEl?.innerText  || '').trim(),
            developer:  (devEl?.getAttribute('aria-label') || devEl?.innerText || '').trim(),
            location:   (locEl?.getAttribute('title') || locEl?.innerText || '').trim(),
            status:     (statusLabel?.innerText || statusDiv?.innerText || '').trim(),
            configs:    configs,
        };
    }).filter(p => p.href || p.name);
}
"""


# ─── Card parsing (cho trang listing) ────────────────────────────────────────

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

        # Phân loại BĐS từ URL
        if "/ban-can-ho-chung-cu-" in href:
            listing_type = "can-ho"
        elif "/ban-shophouse-" in href:
            listing_type = "shophouse"
        elif "/ban-nha-rieng-" in href:
            listing_type = "nha-rieng"
        elif "/ban-dat-" in href:
            listing_type = "dat"
        elif "/ban-nha-biet-thu-" in href:
            listing_type = "biet-thu"
        else:
            listing_type = "other"

        results.append({
            "listing_id":    lid,
            "crawl_month":   crawl_month,
            "project_slug":  project_slug,
            "_project_name": project_name,
            "title":         c.get("title", ""),
            "url":           url,
            "listing_type":  listing_type,
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

async def nav_with_retry(page, url: str, selector: str = ".re__card-full", retries: int = 3) -> bool:
    for attempt in range(1, retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            await page.wait_for_selector(selector, timeout=15_000)
            return True
        except Exception as e:
            log.warning(f"Nav lần {attempt}/{retries} thất bại cho {url}: {e}")
            if attempt < retries:
                await asyncio.sleep(attempt * 3)
    return False


# ─── Project API helpers ──────────────────────────────────────────────────────

def _api_body(page_index: int, cate_id: int) -> str:
    params = {
        "TextSearch": "", "CateId": cate_id, "CityCode": PROJECT_CITY_CODE,
        "DistrictId": 0, "PriceLevel": -1, "PriceMin": -1, "PriceMax": -1,
        "LegacyId": 0, "StagesAsString": "", "OrderBy": 1, "PageIndex": page_index,
    }
    return urllib.parse.urlencode(params)


def parse_project_cards(html: str, crawl_date: str, crawl_month: str, con) -> int:
    """Parse project cards từ HTML fragment của API. Upsert vào DB. Trả về số mới."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".js__project-card")
    count = 0
    for card in cards:
        anchor = card.select_one("a.re__clearfix")
        href   = anchor.get("href", "") if anchor else ""
        name   = card.select_one("h3.re__prj-card-title")
        name   = name.get_text(strip=True) if name else ""

        # Developer từ aria-label của avatar
        dev_el    = card.select_one(".re__prj-card-contact-avatar")
        developer = (dev_el.get("aria-label") or dev_el.get_text(strip=True)) if dev_el else ""

        # Location từ title attribute (đầy đủ hơn innerText)
        loc_el   = card.select_one(".re__prj-card-location")
        location = loc_el.get("title") or loc_el.get_text(strip=True) if loc_el else ""

        # Status: label bên trong div status
        status_div = card.select_one(
            ".re__project-open, .re__project-finish, .re__project-prepare, .re__project-na"
        )
        status_lbl = status_div.select_one("label") if status_div else None
        status     = status_lbl.get_text(strip=True) if status_lbl else ""

        # Số căn hộ từ aria-label
        total_units = None
        for cfg in card.select(".re__prj-card-config-value"):
            lbl = cfg.get("aria-label", "")
            m   = re.search(r"([\d\.]+)\s*căn", lbl)
            if m:
                try:
                    total_units = int(m.group(1).replace(".", "").replace(",", ""))
                except ValueError:
                    pass

        slug, project_id = slug_from_du_an_href(href)
        if not slug and name:
            slug = re.sub(r"[^\w\-]+", "-", name.lower()).strip("-")
        if not slug:
            continue

        district, ward = parse_district_ward(location)
        listing_url = f"nha-dat-ban-{slug}"

        upsert_project(
            con, slug, name, district, ward, crawl_month, crawl_date,
            developer=developer,
            status=status,
            total_units=total_units,
            listing_url=listing_url,
            detail_crawled=crawl_date,
            project_id=project_id or "",
        )
        count += 1
        log.debug(f"  {name} [{slug}] pj={project_id} dev={developer}")
    con.commit()
    return count


# ─── Crawl: project discovery (API-based) ─────────────────────────────────────

async def crawl_projects(max_pages: int, db_path: str, inspect: bool = False, skip_geocode: bool = False) -> None:
    """
    Khám phá tất cả dự án chung cư HN qua API — crawl đa category.
    PROJECT_CATE_IDS: [155=căn hộ, 158=NOXH, 160=khu đô thị, 161=khu phức hợp]
    Playwright chỉ load 1 lần để bypass Cloudflare,
    sau đó dùng page.evaluate(fetch()) cho mọi trang và category.
    """
    con = init_db(db_path)
    now = datetime.now()
    crawl_date  = now.strftime("%Y-%m-%d")
    crawl_month = now.strftime("%Y-%m")
    run_start   = now.isoformat()
    pages_done  = projects_new = total_pages_all = 0

    FETCH_JS = f"""async (body) => {{
        const resp = await fetch('{PROJECT_API_URL}', {{
            method: 'POST',
            headers: {{
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': '*/*',
            }},
            body: body,
        }});
        return {{ status: resp.status, text: await resp.text() }};
    }}"""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=UA, locale="vi-VN", viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        log.info(f"Loading {PROJECT_LIST_URL} (Cloudflare bypass)...")
        ok = await nav_with_retry(page, PROJECT_LIST_URL, ".js__project-card", retries=3)
        if not ok:
            log.error("Không load được trang dự án.")
            await browser.close()
            return
        log.info(f"Page loaded. Crawl {len(PROJECT_CATE_IDS)} categories: {PROJECT_CATE_IDS}")

        async def fetch_page(page_index: int, cate_id: int) -> dict | None:
            for attempt in range(1, 4):
                try:
                    result = await page.evaluate(FETCH_JS, _api_body(page_index, cate_id))
                    if result["status"] != 200:
                        log.warning(f"  API cate={cate_id} trang {page_index} status={result['status']}")
                        return None
                    return json.loads(result["text"])
                except Exception as e:
                    log.warning(f"  fetch_page(cate={cate_id}, pg={page_index}) lần {attempt}: {e}")
                    if attempt < 3:
                        await asyncio.sleep(attempt * 2)
            return None

        for cate_id in PROJECT_CATE_IDS:
            log.info(f"─── Category {cate_id} ───")
            data = await fetch_page(1, cate_id)
            if not data:
                log.error(f"Không lấy được data trang 1 cate={cate_id}.")
                continue

            html1 = data.get("projectListContent", "")
            m_total = re.search(r"(\d+)\s*</span>\s*dự\s*án", html1)
            total_projects = int(m_total.group(1)) if m_total else 0
            total_pages = math.ceil(total_projects / PROJECT_PAGE_SIZE) if total_projects else 1
            if max_pages > 0:
                total_pages = min(total_pages, max_pages)
            log.info(f"  Tổng: {total_projects} dự án → {total_pages} trang")
            total_pages_all += total_pages

            new = parse_project_cards(html1, crawl_date, crawl_month, con)
            projects_new += new
            pages_done   += 1
            log.info(f"  Cate {cate_id} trang 1/{total_pages} — +{new}")

            for pnum in range(2, total_pages + 1):
                await asyncio.sleep(PAGE_DELAY)
                data = await fetch_page(pnum, cate_id)
                if not data:
                    log.warning(f"  Bỏ qua cate={cate_id} trang {pnum}")
                    continue
                new = parse_project_cards(data.get("projectListContent", ""), crawl_date, crawl_month, con)
                projects_new += new
                pages_done   += 1
                log.info(f"  Cate {cate_id} trang {pnum}/{total_pages} — +{new} (tổng: {projects_new})")

            await asyncio.sleep(PAGE_DELAY * 2)

        await browser.close()

    total_in_db = con.execute("SELECT count(*) FROM projects").fetchone()[0]
    con.execute("""
        INSERT INTO crawl_runs (mode, started_at, finished_at, pages_done, items_new, total_pages)
        VALUES (?,?,?,?,?,?)
    """, ("projects", run_start, datetime.now().isoformat(), pages_done, projects_new, total_pages_all))
    con.commit()
    log.info(f"Projects xong: {projects_new} dự án mới, {total_in_db} tổng trong DB.")
    if not skip_geocode:
        geocode_all_projects(con)
    else:
        log.info("Skip geocoding (--skip-geocode).")
    con.close()


async def _crawl_one_project(page, base: str, slug: str, max_pages: int,
                              crawl_date: str, crawl_month: str,
                              now: datetime, con) -> int:
    """Crawl tất cả trang listing của 1 dự án. Trả về số tin mới."""
    ok = await nav_with_retry(page, base, ".re__card-full", retries=2)
    if not ok:
        log.warning(f"    Skip {slug} — không load được")
        return 0

    tot = await page.evaluate(PAGINATION_JS)
    if max_pages > 0:
        tot = min(tot, max_pages)

    async def do_page() -> int:
        raw   = await page.evaluate(EXTRACT_JS)
        cards = parse_cards(raw, crawl_date, crawl_month, now)
        for c in cards:
            # Chỉ gán project_slug cho căn hộ — nhà riêng/shophouse/biệt thự
            # trong khu vực không thuộc dự án, không nên gán nhầm
            if c.get("listing_type") == "can-ho":
                c["project_slug"] = slug
        return save_listings(con, cards)

    new = await do_page()
    for pnum in range(2, tot + 1):
        await asyncio.sleep(PAGE_DELAY)
        url = f"{base}/p{pnum}"
        ok2 = await nav_with_retry(page, url, ".re__card-full", retries=2)
        if ok2:
            new += await do_page()
        else:
            log.warning(f"    Skip trang {pnum} của {slug}")
    return new


# ─── Crawl: per-project listings ─────────────────────────────────────────────

async def crawl_project_listings(
    max_pages: int,
    db_path: str,
) -> None:
    """Crawl tin đăng cho từng dự án đã có trong DB."""
    con = init_db(db_path)
    now = datetime.now()
    crawl_date  = now.strftime("%Y-%m-%d")
    crawl_month = now.strftime("%Y-%m")
    run_start   = now.isoformat()
    total_new   = 0

    # Lấy dự án chưa có listing nào hôm nay — idempotent, resume-safe
    projects = con.execute("""
        SELECT p.project_slug, p.project_name, p.listing_url
        FROM projects p
        WHERE p.listing_url IS NOT NULL AND p.listing_url != ''
          AND p.project_slug NOT IN (
              SELECT DISTINCT project_slug FROM listings
              WHERE crawl_date = ? AND project_slug IS NOT NULL
          )
        ORDER BY p.project_slug
    """, (crawl_date,)).fetchall()

    if not projects:
        log.info("Tất cả dự án đã được crawl hôm nay.")
        con.close()
        return

    total_proj = con.execute(
        "SELECT count(*) FROM projects WHERE listing_url IS NOT NULL AND listing_url != ''"
    ).fetchone()[0]
    already = total_proj - len(projects)
    log.info(f"Crawl listings: {len(projects)} dự án còn lại | {already} đã có hôm nay | tháng {crawl_month}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=UA, locale="vi-VN", viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        PROJECT_TIMEOUT = 120  # giây tối đa cho 1 dự án (tránh hang vô hạn)

        for slug, name, listing_url in projects:
            base = BASE_URL + "/" + listing_url.lstrip("/")
            log.info(f"  Dự án: {name} [{slug}] → {base}")

            try:
                proj_new = await asyncio.wait_for(
                    _crawl_one_project(page, base, slug, max_pages,
                                       crawl_date, crawl_month, now, con),
                    timeout=PROJECT_TIMEOUT,
                )
                total_new += proj_new
                log.info(f"  ✓ {name}: +{proj_new} tin mới")
            except asyncio.TimeoutError:
                log.warning(f"  ⏱ Timeout {PROJECT_TIMEOUT}s — skip {slug}, tạo page mới")
                try:
                    await page.close()
                    page = await ctx.new_page()
                except Exception:
                    pass
            except Exception as e:
                log.warning(f"  ✗ Lỗi {slug}: {e}")

            await asyncio.sleep(PAGE_DELAY)

        await browser.close()

    con.execute("""
        INSERT INTO crawl_runs (mode, started_at, finished_at, pages_done, items_new, total_pages)
        VALUES (?,?,?,?,?,?)
    """, ("project-listings", run_start, datetime.now().isoformat(),
          len(projects), total_new, len(projects)))
    con.commit()
    con.close()
    log.info(f"Xong: {total_new} tin mới từ {len(projects)} dự án.")


# ─── Crawl: generic listing (legacy) ─────────────────────────────────────────

async def crawl_listings(max_pages: int, start_page: int, db_path: str) -> None:
    """Crawl trang listing tổng (legacy mode — không phân theo dự án)."""
    con = init_db(db_path)
    now = datetime.now()
    crawl_date  = now.strftime("%Y-%m-%d")
    crawl_month = now.strftime("%Y-%m")
    run_start   = now.isoformat()
    pages_done  = listings_new = total_pages = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=UA, locale="vi-VN", viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        log.info(f"[generic] Crawl listings tháng {crawl_month}")
        url1 = LIST_URL if start_page == 1 else f"{LIST_URL}/p{start_page}"
        if not await nav_with_retry(page, url1):
            log.error("Không thể load trang đầu.")
            await browser.close()
            return

        total_pages = await page.evaluate(PAGINATION_JS)
        if max_pages > 0:
            total_pages = min(total_pages, start_page - 1 + max_pages)
        log.info(f"Tổng trang: {total_pages}")

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
        log.info(f"Trang {start_page}/{total_pages} — +{new} tin")

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

        await browser.close()

    con.execute("""
        INSERT INTO crawl_runs (mode, started_at, finished_at, pages_done, items_new, total_pages)
        VALUES (?,?,?,?,?,?)
    """, ("generic", run_start, datetime.now().isoformat(), pages_done, listings_new, total_pages))
    con.commit()
    geocode_all_projects(con)
    con.close()
    log.info(f"Generic xong: {pages_done} trang, {listings_new} tin mới.")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Crawl chung cư HN từ batdongsan.com.vn")
    ap.add_argument(
        "--mode", default="all",
        choices=["all", "projects", "listings", "generic"],
        help=(
            "all=khám phá dự án + crawl listings theo dự án | "
            "projects=chỉ khám phá dự án | "
            "listings=chỉ crawl listings theo dự án đã có | "
            "generic=crawl trang listing tổng (legacy)"
        ),
    )
    ap.add_argument("--max-project-pages", type=int, default=0,
                    help="Giới hạn số trang /du-an/ (0=tất cả)")
    ap.add_argument("--max-listing-pages", type=int, default=0,
                    help="Giới hạn số trang listing per dự án (0=tất cả)")
    ap.add_argument("--max-pages",  type=int, default=0,
                    help="Alias cho --max-listing-pages (legacy / generic mode)")
    ap.add_argument("--start-page", type=int, default=1,
                    help="Bắt đầu từ trang nào — chỉ dùng cho mode=generic")
    ap.add_argument("--db", default=DB_PATH, help="Đường dẫn file SQLite")
    ap.add_argument("--inspect", action="store_true",
                    help="Dump HTML trang /du-an/ ra /tmp để kiểm tra selectors, rồi thoát")
    ap.add_argument("--skip-geocode", action="store_true",
                    help="Bỏ qua bước geocoding (Nominatim) sau khi crawl projects")
    args = ap.parse_args()

    max_lst = args.max_listing_pages or args.max_pages

    if args.mode == "projects":
        asyncio.run(crawl_projects(args.max_project_pages, args.db,
                                   inspect=args.inspect, skip_geocode=args.skip_geocode))

    elif args.mode == "listings":
        asyncio.run(crawl_project_listings(max_lst, args.db))

    elif args.mode == "all":
        asyncio.run(crawl_projects(args.max_project_pages, args.db,
                                   inspect=args.inspect, skip_geocode=args.skip_geocode))
        asyncio.run(crawl_project_listings(max_lst, args.db))

    else:  # generic
        asyncio.run(crawl_listings(max_lst or args.max_pages, args.start_page, args.db))
