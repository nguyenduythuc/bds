"""
Phân tích lịch sử giá chung cư HN từ batdongsan.db
- Mỗi tin đăng là 1 dòng trong bảng listings (không tổng hợp sẵn)
- Tổng hợp (trung bình, min, max) được tính tại thời điểm truy vấn
"""

import sqlite3
import argparse
import csv
import sys

DB_PATH = "batdongsan.db"

OUTLIER_MIN =  10   # triệu/m² — lọc lỗi nhập liệu
OUTLIER_MAX = 500


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


# ─── Queries ─────────────────────────────────────────────────────────────────

def market_by_crawl_month(con: sqlite3.Connection) -> list[dict]:
    """Tổng quan thị trường theo tháng crawl."""
    rows = con.execute(f"""
        SELECT
            crawl_month,
            COUNT(*)                            AS so_tin,
            COUNT(DISTINCT project_slug)        AS so_du_an,
            ROUND(AVG(price_ty), 3)             AS gia_tb_ty,
            ROUND(AVG(area_m2), 1)              AS dientich_tb_m2,
            ROUND(AVG(price_per_m2), 1)         AS dongia_tb,
            ROUND(MIN(price_per_m2), 1)         AS dongia_min,
            ROUND(MAX(price_per_m2), 1)         AS dongia_max
        FROM listings
        WHERE price_per_m2 BETWEEN {OUTLIER_MIN} AND {OUTLIER_MAX}
        GROUP BY crawl_month
        ORDER BY crawl_month
    """).fetchall()
    return [dict(r) for r in rows]


def market_by_post_month(con: sqlite3.Connection) -> list[dict]:
    """Tổng quan theo tháng đăng tin (gần thực tế hơn tháng crawl)."""
    rows = con.execute(f"""
        SELECT
            post_month,
            COUNT(*)                            AS so_tin,
            COUNT(DISTINCT project_slug)        AS so_du_an,
            ROUND(AVG(price_ty), 3)             AS gia_tb_ty,
            ROUND(AVG(area_m2), 1)              AS dientich_tb_m2,
            ROUND(AVG(price_per_m2), 1)         AS dongia_tb,
            ROUND(MIN(price_per_m2), 1)         AS dongia_min,
            ROUND(MAX(price_per_m2), 1)         AS dongia_max
        FROM listings
        WHERE post_month IS NOT NULL
          AND price_per_m2 BETWEEN {OUTLIER_MIN} AND {OUTLIER_MAX}
        GROUP BY post_month
        ORDER BY post_month
    """).fetchall()
    return [dict(r) for r in rows]


def listings_raw(con: sqlite3.Connection, post_month: str | None = None,
                 crawl_month: str | None = None) -> list[dict]:
    """Toàn bộ tin đăng thô (có thể lọc theo tháng)."""
    conditions = [f"price_per_m2 BETWEEN {OUTLIER_MIN} AND {OUTLIER_MAX}"]
    if post_month:
        conditions.append(f"post_month = '{post_month}'")
    if crawl_month:
        conditions.append(f"crawl_month = '{crawl_month}'")
    where = "WHERE " + " AND ".join(conditions)

    rows = con.execute(f"""
        SELECT
            l.listing_id, l.crawl_month, l.post_month, l.post_date,
            l.project_slug,
            p.project_name  AS du_an,
            p.district      AS quan_project,
            l.district      AS quan_listing,
            l.ward,
            l.price_ty, l.area_m2, l.price_per_m2,
            l.bedrooms, l.bathrooms,
            l.title, l.url, l.crawl_date
        FROM listings l
        LEFT JOIN projects p USING(project_slug)
        {where}
        ORDER BY l.crawl_month DESC, l.post_date DESC
    """).fetchall()
    return [dict(r) for r in rows]


def price_by_project(con: sqlite3.Connection, min_listings: int = 2) -> list[dict]:
    """Lịch sử đơn giá theo dự án × tháng crawl."""
    rows = con.execute(f"""
        SELECT
            COALESCE(p.project_name, l.project_slug) AS du_an,
            l.project_slug,
            COALESCE(p.district, l.district)         AS quan,
            l.crawl_month,
            COUNT(*)                                 AS so_tin,
            ROUND(AVG(l.price_per_m2), 1)            AS dongia_tb,
            ROUND(MIN(l.price_per_m2), 1)            AS dongia_min,
            ROUND(MAX(l.price_per_m2), 1)            AS dongia_max,
            ROUND(AVG(l.price_ty), 3)                AS gia_tb_ty,
            ROUND(AVG(l.area_m2), 1)                 AS dientich_tb
        FROM listings l
        LEFT JOIN projects p USING(project_slug)
        WHERE l.project_slug IS NOT NULL
          AND l.price_per_m2 BETWEEN {OUTLIER_MIN} AND {OUTLIER_MAX}
        GROUP BY l.project_slug, l.crawl_month
        HAVING COUNT(*) >= {min_listings}
        ORDER BY so_tin DESC, l.project_slug, l.crawl_month
    """).fetchall()
    return [dict(r) for r in rows]


def price_by_district(con: sqlite3.Connection) -> list[dict]:
    """Lịch sử đơn giá theo quận × tháng crawl."""
    rows = con.execute(f"""
        SELECT
            COALESCE(p.district, l.district)    AS quan,
            l.crawl_month,
            COUNT(*)                            AS so_tin,
            COUNT(DISTINCT l.project_slug)      AS so_du_an,
            ROUND(AVG(l.price_per_m2), 1)       AS dongia_tb,
            ROUND(MIN(l.price_per_m2), 1)       AS dongia_min,
            ROUND(MAX(l.price_per_m2), 1)       AS dongia_max
        FROM listings l
        LEFT JOIN projects p USING(project_slug)
        WHERE COALESCE(p.district, l.district) != ''
          AND COALESCE(p.district, l.district) IS NOT NULL
          AND l.price_per_m2 BETWEEN {OUTLIER_MIN} AND {OUTLIER_MAX}
        GROUP BY quan, l.crawl_month
        ORDER BY quan, l.crawl_month
    """).fetchall()
    return [dict(r) for r in rows]


def all_projects(con: sqlite3.Connection) -> list[dict]:
    """Danh sách dự án kèm tọa độ và thống kê giá tổng hợp."""
    rows = con.execute(f"""
        SELECT
            p.project_slug,
            p.project_name  AS du_an,
            p.district      AS quan,
            p.ward          AS phuong,
            p.lat, p.lng,
            p.geocoded,
            p.first_seen,
            COUNT(l.listing_id)                 AS tong_tin,
            COUNT(DISTINCT l.crawl_month)       AS so_thang,
            MIN(l.crawl_month)                  AS thang_dau,
            MAX(l.crawl_month)                  AS thang_cuoi,
            ROUND(AVG(l.price_per_m2), 1)       AS dongia_tb,
            ROUND(MIN(l.price_per_m2), 1)       AS dongia_min,
            ROUND(MAX(l.price_per_m2), 1)       AS dongia_max
        FROM projects p
        LEFT JOIN listings l
            ON p.project_slug = l.project_slug
            AND l.price_per_m2 BETWEEN {OUTLIER_MIN} AND {OUTLIER_MAX}
        GROUP BY p.project_slug
        ORDER BY tong_tin DESC
    """).fetchall()
    return [dict(r) for r in rows]


# ─── Output helpers ───────────────────────────────────────────────────────────

def export_csv(rows: list[dict], filename: str) -> None:
    if not rows:
        print(f"  (Không có dữ liệu để xuất {filename})")
        return
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"  ✓ {len(rows)} dòng → {filename}")


def print_table(rows: list[dict], title: str, max_rows: int = 50) -> None:
    if not rows:
        print(f"\n[{title}] Chưa có dữ liệu.")
        return
    display = rows[:max_rows]
    print(f"\n{'='*100}")
    print(f"  {title}")
    print(f"{'='*100}")
    headers = list(display[0].keys())
    widths  = [max(len(str(h)), max(len(str(r.get(h, ""))) for r in display)) for h in headers]
    fmt     = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for r in display:
        print(fmt.format(*[str(r.get(h, "")) for h in headers]))
    if len(rows) > max_rows:
        print(f"  ... ({len(rows) - max_rows} dòng nữa — dùng --export để xuất đầy đủ)")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Phân tích lịch sử giá chung cư HN")
    ap.add_argument("--db",           default=DB_PATH)
    ap.add_argument("--min-listings", type=int, default=2,
                    help="Số tin tối thiểu/dự án/tháng (default: 2)")
    ap.add_argument("--post-month",   default=None,
                    help="Lọc tin theo tháng đăng, ví dụ: 2026-04")
    ap.add_argument("--crawl-month",  default=None,
                    help="Lọc tin theo tháng crawl, ví dụ: 2026-05")
    ap.add_argument("--export",       action="store_true",
                    help="Xuất CSV")
    args = ap.parse_args()

    try:
        con = connect(args.db)
    except Exception as e:
        print(f"Lỗi mở DB {args.db}: {e}")
        sys.exit(1)

    summary_crawl = market_by_crawl_month(con)
    summary_post  = market_by_post_month(con)
    by_proj       = price_by_project(con, args.min_listings)
    by_dist       = price_by_district(con)
    projects      = all_projects(con)

    print_table(summary_crawl, "Tổng quan theo tháng CRAWL")
    print_table(summary_post,  "Tổng quan theo tháng ĐĂNG TIN (post_month)")
    print_table(by_dist,       "Đơn giá theo Quận × Tháng crawl (triệu/m²)")
    print_table(by_proj,       f"Đơn giá theo Dự án × Tháng crawl (≥{args.min_listings} tin)")
    print_table(projects,      "Danh sách dự án (top 30)", max_rows=30)

    if args.post_month or args.crawl_month:
        raw = listings_raw(con, args.post_month, args.crawl_month)
        label = f"Tin đăng thô"
        if args.post_month:
            label += f" post_month={args.post_month}"
        if args.crawl_month:
            label += f" crawl_month={args.crawl_month}"
        print_table(raw, label, max_rows=100)
        if args.export:
            tag = args.post_month or args.crawl_month
            export_csv(raw, f"listings_raw_{tag}.csv")

    if args.export:
        print("\nXuất CSV:")
        export_csv(summary_crawl, "tong_quan_crawl_month.csv")
        export_csv(summary_post,  "tong_quan_post_month.csv")
        export_csv(by_dist,       "dongia_theo_quan.csv")
        export_csv(by_proj,       "dongia_theo_du_an.csv")
        export_csv(projects,      "danh_sach_du_an.csv")

    con.close()


if __name__ == "__main__":
    main()
