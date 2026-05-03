"""
Crawl tin đăng chung cư Hà Nội từ bds.com.vn
- Thu thập: listing_id, tiêu đề, URL, giá, diện tích, giá/m², quận, ngày đăng, tên dự án
- Lưu vào SQLite với timestamp crawl (theo tháng)
- Chạy hàng tháng để xây dựng lịch sử giá theo dự án
"""

import re
import time
import sqlite3
import logging
import requests
from datetime import datetime
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_URL = "https://bds.com.vn"
LIST_URL = "https://bds.com.vn/mua-ban-can-ho-chung-cu-ha-noi"
DB_PATH = "bds_data.db"
DELAY = 1.5  # giây giữa các request

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
    "Referer": "https://bds.com.vn/",
}

# ─── Patterns trích xuất tên dự án từ tiêu đề ────────────────────────────────

# Từ chỉ BĐS không phải căn hộ (dùng để lọc listing và loại khỏi tên dự án)
NON_APARTMENT_KEYWORDS = re.compile(
    r"\bđất\b|\bnền\b|\bliền\s*kề\b|\bbiệt\s*thự\b|\bnhà\s*phố\b|\bnhà\s*riêng\b"
    r"|\bkho\b|\bnhà\s*xưởng\b|\btrang\s*trại\b|\bnghỉ\s*dưỡng\b",
    re.IGNORECASE,
)

# Từ phổ thông KHÔNG nên đứng đầu tên dự án
_BAD_START = re.compile(
    r"^(?:bán|cần|chính|chủ|căn|cần|nhận|nhà|tòa|ở|tại|tại|hơn|chỉ|giá|dt|diện|"
    r"tầng|phòng|ngủ|toilet|wc|pn|sổ|full|nội|thất|gia|rẻ|nhất|gấp|xuất|ngoại|"
    r"giao|mới|đẹp|cực|siêu|vip|hot|số|mã|thông|tin|mô|tả)\b",
    re.IGNORECASE,
)

def extract_project(title: str) -> str | None:
    # 1. Ưu tiên nhất: "dự án [TÊN]"
    m = re.search(
        r"(?:dự\s*án)\s+([A-Za-zÀ-ỹ0-9][A-Za-zÀ-ỹ0-9\s\-\.]{2,40}?)(?:\s*[,\-\|]|\s+\d|\s+(?:tầng|phòng|pn|m²|m2|dt|sổ|giá)|$)",
        title, re.IGNORECASE,
    )
    if m:
        name = _clean_project_name(m.group(1))
        if name and not _BAD_START.match(name):
            return name

    # 2. "chung cư [TÊN]" / "cc [TÊN]" — bỏ qua các tính từ chung ("cao cấp", "mới")
    m = re.search(
        r"(?:chung\s*cư|cc)\s+(?:cao\s*cấp\s+|mới\s+|nổi\s*tiếng\s+)?([A-Za-zÀ-ỹ0-9][A-Za-zÀ-ỹ0-9\s\-\.]{2,40}?)(?:\s*[,\-\|]|\s+\d|\s+(?:tầng|phòng|pn|m²|m2|dt|sổ|giá)|$)",
        title, re.IGNORECASE,
    )
    if m:
        name = _clean_project_name(m.group(1))
        if name and not _BAD_START.match(name):
            return name

    # 3. Tên project dạng "The X", "X Residences", v.v.
    eng_keywords = (
        r"residences?|complex|city|park|tower|plaza|view|lake|home|green|garden|"
        r"court|mall|central|bay|sky|royal|premium|luxury|times\s*city|"
        r"vinhomes?|gamuda|ciputra|mipec|goldmark|paragon|sunshine|imperia|landmark|seasons"
    )
    m = re.search(
        rf"([A-Za-zÀ-ỹ][A-Za-zÀ-ỹ0-9\s\-\.{{0,35}}]{{3,35}}(?:{eng_keywords}))",
        title, re.IGNORECASE,
    )
    if m:
        name = _clean_project_name(m.group(1))
        if name and not _BAD_START.match(name):
            return name

    return None


def _clean_project_name(raw: str) -> str | None:
    name = raw.strip()
    # Cắt bỏ phần thừa ở cuối
    name = re.sub(r"\s+(?:giá|dt|m2|m²|tầng|phòng|pn|wc|vị\s*trí|tại|ở|sổ|full|nội|thất|"
                  r"chỉ|nhỉnh|nhận|nhà|ở\s*ngay|cầm\s*tay|sđcc|full\s*nt).*$",
                  "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+", " ", name)
    if 4 <= len(name) <= 50:
        return name.title()
    return None

DISTRICT_MAP = {
    "ba đình": "Ba Đình", "hoàn kiếm": "Hoàn Kiếm", "tây hồ": "Tây Hồ",
    "long biên": "Long Biên", "cầu giấy": "Cầu Giấy", "đống đa": "Đống Đa",
    "hai bà trưng": "Hai Bà Trưng", "hoàng mai": "Hoàng Mai", "thanh xuân": "Thanh Xuân",
    "nam từ liêm": "Nam Từ Liêm", "bắc từ liêm": "Bắc Từ Liêm", "hà đông": "Hà Đông",
    "đan phượng": "Đan Phượng", "hoài đức": "Hoài Đức", "thanh trì": "Thanh Trì",
    "gia lâm": "Gia Lâm", "đông anh": "Đông Anh", "sóc sơn": "Sóc Sơn",
    "mê linh": "Mê Linh", "thường tín": "Thường Tín",
}


# ─── Database ────────────────────────────────────────────────────────────────

def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            listing_id   TEXT,
            title        TEXT,
            url          TEXT,
            price_ty     REAL,       -- giá tỷ VND
            area_m2      REAL,       -- diện tích m²
            price_per_m2 REAL,       -- triệu VND/m²
            district     TEXT,
            city         TEXT,
            date_posted  TEXT,       -- ngày đăng (dd/mm/yyyy)
            project_name TEXT,       -- tên dự án trích xuất
            crawl_date   TEXT,       -- yyyy-mm-dd lúc crawl
            crawl_month  TEXT,       -- yyyy-mm để group theo tháng
            PRIMARY KEY (listing_id, crawl_month)  -- cho phép track same listing qua nhiều tháng
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS crawl_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT,
            finished_at TEXT,
            pages_done  INTEGER,
            listings_new INTEGER
        )
    """)
    con.commit()
    return con


# ─── Parsing helpers ─────────────────────────────────────────────────────────

def parse_price(text: str) -> float | None:
    """'3 tỷ' -> 3.0, '1.5 tỷ' -> 1.5, '800 triệu' -> 0.8"""
    text = text.lower().strip()
    m = re.search(r"([\d,\.]+)\s*tỷ", text)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"([\d,\.]+)\s*triệu", text)
    if m:
        return round(float(m.group(1).replace(",", "")) / 1000, 4)
    return None


def parse_area(text: str) -> float | None:
    """'73 m²' -> 73.0"""
    m = re.search(r"([\d,\.]+)\s*m", text, re.IGNORECASE)
    return float(m.group(1).replace(",", "")) if m else None


def extract_listing_id(url: str) -> str | None:
    m = re.search(r"-p(\d+)\.html", url)
    return m.group(1) if m else None




def normalize_district(location_text: str) -> str:
    """'Quận Hoàng Mai, Hà Nội' -> 'Hoàng Mai'"""
    loc = location_text.lower()
    for key, val in DISTRICT_MAP.items():
        if key in loc:
            return val
    # fallback: lấy phần trước dấu phẩy
    parts = location_text.split(",")
    district = re.sub(r"(?i)quận|huyện|thành phố|thị xã", "", parts[0]).strip()
    return district or location_text


# ─── Scraping ────────────────────────────────────────────────────────────────

def get_soup(url: str, session: requests.Session, retries: int = 3) -> BeautifulSoup | None:
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return BeautifulSoup(resp.text, "lxml")
        except Exception as e:
            wait = attempt * 3
            log.warning(f"Lỗi GET {url} (lần {attempt}/{retries}): {e} — thử lại sau {wait}s")
            if attempt < retries:
                time.sleep(wait)
    return None


def parse_listing_cards(soup: BeautifulSoup) -> list[dict]:
    """Trích dữ liệu từ danh sách listing cards trên trang list."""
    results = []
    for card in soup.select(".item-nhadat"):
        title_tag = card.select_one(".title-item-nhadat")
        if not title_tag:
            continue
        url = title_tag.get("href", "")
        title = title_tag.get_text(strip=True)
        listing_id = extract_listing_id(url)
        if not listing_id:
            continue

        prices = card.select(".price-item-nhadat")
        price_text = prices[0].get_text(strip=True) if len(prices) > 0 else ""
        area_text  = prices[1].get_text(strip=True) if len(prices) > 1 else ""

        price_ty = parse_price(price_text)
        area_m2  = parse_area(area_text)
        price_per_m2 = round(price_ty * 1000 / area_m2, 2) if (price_ty and area_m2 and area_m2 > 0) else None

        loc_tag  = card.select_one(".vaule-item-nhadat.label-adress")
        date_tag = card.select_one(".vaule-item-nhadat.flr")
        location = loc_tag.get_text(strip=True)  if loc_tag  else ""
        date_str = date_tag.get_text(strip=True) if date_tag else ""

        district = normalize_district(location)
        city     = "Hà Nội" if "hà nội" in location.lower() else location.split(",")[-1].strip()

        # Bỏ qua listing không phải căn hộ chung cư
        if NON_APARTMENT_KEYWORDS.search(title):
            continue
        # Bỏ qua listing không phải HN
        if city != "Hà Nội":
            continue

        project_name = extract_project(title)

        results.append({
            "listing_id":   listing_id,
            "title":        title,
            "url":          url if url.startswith("http") else BASE_URL + url,
            "price_ty":     price_ty,
            "area_m2":      area_m2,
            "price_per_m2": price_per_m2,
            "district":     district,
            "city":         city,
            "date_posted":  date_str,
            "project_name": project_name,
        })
    return results


def get_total_pages(soup: BeautifulSoup) -> int:
    """Lấy số trang cuối từ pagination."""
    pages = []
    for a in soup.select("a[href*='page']"):
        m = re.search(r"page(\d+)", a.get("href", ""))
        if m:
            pages.append(int(m.group(1)))
    return max(pages) if pages else 1


def page_url(page: int) -> str:
    if page == 1:
        return LIST_URL
    return f"{LIST_URL}-page{page}"


# ─── Main crawl loop ─────────────────────────────────────────────────────────

def crawl(max_pages: int = 0, db_path: str = DB_PATH) -> None:
    """
    Crawl tất cả listing chung cư HN và lưu vào DB.
    max_pages=0 => crawl tất cả trang.
    """
    con = init_db(db_path)
    session = requests.Session()
    now = datetime.now()
    crawl_date  = now.strftime("%Y-%m-%d")
    crawl_month = now.strftime("%Y-%m")

    run_start = now.isoformat()
    pages_done = 0
    listings_new = 0

    # --- Trang 1: lấy tổng số trang ---
    log.info(f"Bắt đầu crawl tháng {crawl_month}...")
    soup1 = get_soup(page_url(1), session)
    if not soup1:
        log.error("Không thể load trang 1, dừng.")
        return

    total = get_total_pages(soup1)
    if max_pages > 0:
        total = min(total, max_pages)
    log.info(f"Tổng số trang: {total}")

    def save_page(soup: BeautifulSoup) -> int:
        cards = parse_listing_cards(soup)
        saved = 0
        for item in cards:
            # Chỉ lấy listing thuộc Hà Nội
            if item["city"] != "Hà Nội":
                continue
            try:
                con.execute(
                    """INSERT OR IGNORE INTO listings
                       (listing_id, title, url, price_ty, area_m2, price_per_m2,
                        district, city, date_posted, project_name, crawl_date, crawl_month)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["listing_id"], item["title"], item["url"],
                     item["price_ty"], item["area_m2"], item["price_per_m2"],
                     item["district"], item["city"], item["date_posted"],
                     item["project_name"], crawl_date, crawl_month),
                )
                saved += con.execute("SELECT changes()").fetchone()[0]
            except Exception as e:
                log.warning(f"Lỗi insert {item['listing_id']}: {e}")
        con.commit()
        return saved

    # Trang 1
    listings_new += save_page(soup1)
    pages_done += 1
    log.info(f"Trang 1/{total} — đã lưu {listings_new} tin mới")

    # Trang 2+
    for page in range(2, total + 1):
        time.sleep(DELAY)
        soup = get_soup(page_url(page), session)
        if not soup:
            log.warning(f"Bỏ qua trang {page}")
            continue
        new = save_page(soup)
        listings_new += new
        pages_done += 1
        log.info(f"Trang {page}/{total} — +{new} tin mới (tổng: {listings_new})")

    # Ghi log run
    con.execute(
        "INSERT INTO crawl_runs (started_at, finished_at, pages_done, listings_new) VALUES (?,?,?,?)",
        (run_start, datetime.now().isoformat(), pages_done, listings_new),
    )
    con.commit()
    con.close()
    log.info(f"Hoàn thành: {pages_done} trang, {listings_new} tin mới lưu vào {db_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Crawl chung cư HN từ bds.com.vn")
    parser.add_argument("--max-pages", type=int, default=0, help="Giới hạn số trang (0=tất cả)")
    parser.add_argument("--db", default=DB_PATH, help="Đường dẫn file SQLite")
    args = parser.parse_args()
    crawl(max_pages=args.max_pages, db_path=args.db)
