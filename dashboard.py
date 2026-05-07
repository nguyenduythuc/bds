"""
Streamlit dashboard — batdongsan.com.vn data
Run: streamlit run dashboard.py
"""

import sqlite3
import math
import pandas as pd
import plotly.express as px
import streamlit as st

DB_PATH = "batdongsan.db"
PAGE_SIZE = 30

st.set_page_config(
    page_title="BĐS Hà Nội",
    page_icon="🏠",
    layout="wide",
)

# ── Data loading ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_projects() -> pd.DataFrame:
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
        SELECT
            project_slug, project_name, developer, district, ward,
            status, price_min, price_max, total_units,
            lat, lng, geocoded,
            first_seen, crawl_date
        FROM projects
        ORDER BY project_name
    """, con)
    con.close()
    return df


@st.cache_data(ttl=60)
def load_listings() -> pd.DataFrame:
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
        SELECT
            l.listing_id, l.project_slug,
            p.project_name,
            l.title, l.url,
            l.price_ty, l.area_m2, l.price_per_m2,
            l.bedrooms, l.bathrooms,
            l.district, l.ward,
            l.post_date, l.post_month,
            l.crawl_date, l.crawl_month
        FROM listings l
        LEFT JOIN projects p ON p.project_slug = l.project_slug
        ORDER BY l.crawl_date DESC, l.post_date DESC
    """, con)
    con.close()
    return df


@st.cache_data(ttl=60)
def load_stats() -> dict:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    n_proj = cur.execute("SELECT count(*) FROM projects").fetchone()[0]
    n_lst  = cur.execute("SELECT count(*) FROM listings").fetchone()[0]
    last   = cur.execute(
        "SELECT max(crawl_date) FROM listings"
    ).fetchone()[0]
    n_geo  = cur.execute(
        "SELECT count(*) FROM projects WHERE geocoded=1"
    ).fetchone()[0]
    con.close()
    return {"n_proj": n_proj, "n_lst": n_lst, "last_crawl": last, "n_geo": n_geo}


# ── Helpers ───────────────────────────────────────────────────────────────────

def paginate(df: pd.DataFrame, key: str) -> pd.DataFrame:
    total = len(df)
    n_pages = max(1, math.ceil(total / PAGE_SIZE))
    col1, col2 = st.columns([3, 1])
    with col2:
        page = st.number_input(
            f"Trang (/{n_pages})", min_value=1, max_value=n_pages,
            value=1, step=1, key=key,
        )
    with col1:
        st.caption(f"{total:,} bản ghi — trang {page}/{n_pages}")
    start = (page - 1) * PAGE_SIZE
    return df.iloc[start : start + PAGE_SIZE]


# ── Layout ────────────────────────────────────────────────────────────────────

st.title("🏠 BĐS Chung cư Hà Nội")

stats = load_stats()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Dự án", f"{stats['n_proj']:,}")
c2.metric("Tin đăng", f"{stats['n_lst']:,}")
c3.metric("Có toạ độ", f"{stats['n_geo']:,} / {stats['n_proj']}")
c4.metric("Crawl gần nhất", stats["last_crawl"] or "—")

tab_proj, tab_lst, tab_chart, tab_map = st.tabs(
    ["📋 Dự án", "🏷️ Tin đăng", "📊 Biểu đồ", "🗺️ Bản đồ"]
)

# ─── Tab: Projects ────────────────────────────────────────────────────────────
with tab_proj:
    df_p = load_projects()

    with st.expander("🔍 Bộ lọc", expanded=True):
        c1, c2, c3 = st.columns(3)
        q_name = c1.text_input("Tìm tên dự án")
        statuses = ["(tất cả)"] + sorted(df_p["status"].dropna().unique().tolist())
        q_status = c2.selectbox("Trạng thái", statuses)
        districts = ["(tất cả)"] + sorted(df_p["district"].dropna().unique().tolist())
        q_dist = c3.selectbox("Quận/Huyện", districts)

    mask = pd.Series(True, index=df_p.index)
    if q_name:
        mask &= df_p["project_name"].str.contains(q_name, case=False, na=False)
    if q_status != "(tất cả)":
        mask &= df_p["status"] == q_status
    if q_dist != "(tất cả)":
        mask &= df_p["district"] == q_dist

    df_filt = df_p[mask].copy()

    cols_show = ["project_name", "developer", "district", "status",
                 "price_min", "price_max", "total_units", "first_seen"]
    page_df = paginate(df_filt[cols_show], "proj_page")

    st.dataframe(
        page_df.rename(columns={
            "project_name": "Tên dự án",
            "developer": "Chủ đầu tư",
            "district": "Quận",
            "status": "Trạng thái",
            "price_min": "Giá min (tỷ)",
            "price_max": "Giá max (tỷ)",
            "total_units": "Số căn",
            "first_seen": "Lần đầu thấy",
        }),
        use_container_width=True,
        height=550,
    )


# ─── Tab: Listings ────────────────────────────────────────────────────────────
with tab_lst:
    df_l = load_listings()

    with st.expander("🔍 Bộ lọc", expanded=True):
        c1, c2, c3, c4 = st.columns(4)
        proj_opts = ["(tất cả)"] + sorted(df_l["project_name"].dropna().unique().tolist())
        q_proj    = c1.selectbox("Dự án", proj_opts, key="lst_proj")
        dist_opts = ["(tất cả)"] + sorted(df_l["district"].dropna().unique().tolist())
        q_dist2   = c2.selectbox("Quận", dist_opts, key="lst_dist")

        price_min_val = float(df_l["price_ty"].min(skipna=True) or 0)
        price_max_val = float(df_l["price_ty"].max(skipna=True) or 100)
        price_range   = c3.slider(
            "Giá (tỷ)", price_min_val, price_max_val,
            (price_min_val, price_max_val), step=0.5, key="lst_price",
        )

        months = ["(tất cả)"] + sorted(df_l["crawl_month"].dropna().unique().tolist(), reverse=True)
        q_month = c4.selectbox("Tháng crawl", months, key="lst_month")

    mask2 = pd.Series(True, index=df_l.index)
    if q_proj != "(tất cả)":
        mask2 &= df_l["project_name"] == q_proj
    if q_dist2 != "(tất cả)":
        mask2 &= df_l["district"] == q_dist2
    if q_month != "(tất cả)":
        mask2 &= df_l["crawl_month"] == q_month
    mask2 &= (df_l["price_ty"].isna() |
              ((df_l["price_ty"] >= price_range[0]) & (df_l["price_ty"] <= price_range[1])))

    df_filt2 = df_l[mask2].copy()

    cols_lst = ["project_name", "title", "price_ty", "area_m2", "price_per_m2",
                "bedrooms", "bathrooms", "district", "post_date", "crawl_date", "url"]
    page_lst = paginate(df_filt2[cols_lst], "lst_page")

    # Make URL clickable
    def make_link(url):
        if url:
            return f'<a href="{url}" target="_blank">🔗</a>'
        return ""

    display = page_lst.rename(columns={
        "project_name": "Dự án",
        "title": "Tiêu đề",
        "price_ty": "Giá (tỷ)",
        "area_m2": "DT (m²)",
        "price_per_m2": "Giá/m² (tr)",
        "bedrooms": "PN",
        "bathrooms": "WC",
        "district": "Quận",
        "post_date": "Ngày đăng",
        "crawl_date": "Crawl date",
        "url": "Link",
    })

    st.dataframe(
        display,
        use_container_width=True,
        height=550,
        column_config={
            "Link": st.column_config.LinkColumn("Link"),
        },
    )


# ─── Tab: Charts ─────────────────────────────────────────────────────────────
with tab_chart:
    df_l2 = load_listings()
    df_p2 = load_projects()

    df_valid = df_l2[df_l2["price_per_m2"].notna() & (df_l2["price_per_m2"] > 0)].copy()

    c1, c2 = st.columns(2)

    with c1:
        st.subheader("Giá/m² trung bình theo dự án (top 20)")
        avg_proj = (
            df_valid.groupby("project_name")["price_per_m2"]
            .mean()
            .nlargest(20)
            .reset_index()
            .rename(columns={"project_name": "Dự án", "price_per_m2": "Giá/m² (triệu)"})
        )
        fig1 = px.bar(
            avg_proj, x="Giá/m² (triệu)", y="Dự án",
            orientation="h", height=500,
            color="Giá/m² (triệu)", color_continuous_scale="RdYlGn_r",
        )
        fig1.update_layout(showlegend=False, yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig1, use_container_width=True)

    with c2:
        st.subheader("Phân phối giá/m²")
        fig2 = px.histogram(
            df_valid[df_valid["price_per_m2"] < 400],
            x="price_per_m2", nbins=50, height=500,
            labels={"price_per_m2": "Giá/m² (triệu đồng)", "count": "Số tin"},
        )
        fig2.update_layout(bargap=0.05)
        st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Số tin đăng theo quận")
    by_dist = (
        df_valid.groupby("district").size()
        .reset_index(name="Số tin")
        .sort_values("Số tin", ascending=False)
        .head(15)
        .rename(columns={"district": "Quận"})
    )
    fig3 = px.bar(by_dist, x="Quận", y="Số tin", height=350,
                  color="Số tin", color_continuous_scale="Blues")
    fig3.update_layout(showlegend=False)
    st.plotly_chart(fig3, use_container_width=True)

    c3, c4 = st.columns(2)
    with c3:
        st.subheader("Diện tích vs Giá")
        df_scatter = df_valid[df_valid["area_m2"].notna() & (df_valid["area_m2"] < 300)].copy()
        fig4 = px.scatter(
            df_scatter.sample(min(500, len(df_scatter))),
            x="area_m2", y="price_ty",
            color="project_name",
            labels={"area_m2": "Diện tích (m²)", "price_ty": "Giá (tỷ)", "project_name": "Dự án"},
            height=400,
        )
        fig4.update_layout(showlegend=False)
        st.plotly_chart(fig4, use_container_width=True)

    with c4:
        st.subheader("Số phòng ngủ")
        bed_cnt = (
            df_valid["bedrooms"].dropna()
            .astype(int)
            .value_counts()
            .reset_index()
            .rename(columns={"bedrooms": "Số PN", "count": "Số tin"})
            .sort_values("Số PN")
        )
        fig5 = px.pie(bed_cnt, values="Số tin", names="Số PN", height=400,
                      title="Phân bổ số phòng ngủ")
        st.plotly_chart(fig5, use_container_width=True)


# ─── Tab: Map ─────────────────────────────────────────────────────────────────
with tab_map:
    df_map = load_projects()
    df_map = df_map[df_map["lat"].notna() & df_map["lng"].notna()].copy()

    if df_map.empty:
        st.info("Chưa có dự án nào có toạ độ. Chạy `--mode projects` (không có `--skip-geocode`).")
    else:
        # Merge listing count
        df_l_cnt = load_listings().groupby("project_slug").size().reset_index(name="so_tin")
        df_map = df_map.merge(df_l_cnt, on="project_slug", how="left")
        df_map["so_tin"] = df_map["so_tin"].fillna(0)

        fig_map = px.scatter_mapbox(
            df_map,
            lat="lat", lon="lng",
            hover_name="project_name",
            hover_data={"developer": True, "district": True,
                        "status": True, "so_tin": True,
                        "lat": False, "lng": False},
            color="so_tin",
            size="so_tin",
            size_max=20,
            color_continuous_scale="YlOrRd",
            zoom=10,
            height=650,
            mapbox_style="open-street-map",
            labels={"so_tin": "Số tin", "developer": "Chủ đầu tư",
                    "district": "Quận", "status": "Trạng thái"},
        )
        fig_map.update_layout(margin={"l": 0, "r": 0, "t": 0, "b": 0})
        st.plotly_chart(fig_map, use_container_width=True)
        st.caption(f"{len(df_map)} dự án có toạ độ / {stats['n_proj']} tổng")
