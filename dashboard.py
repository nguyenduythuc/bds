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
            l.listing_type,
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

tab_proj, tab_lst, tab_chart, tab_trend, tab_map = st.tabs(
    ["📋 Dự án", "🏷️ Tin đăng", "📊 Biểu đồ", "📈 Xu hướng giá", "🗺️ Bản đồ"]
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
        c0, c1, c2, c3, c4 = st.columns(5)
        type_opts = ["can-ho", "shophouse", "nha-rieng", "biet-thu", "dat", "other", "(tất cả)"]
        q_type    = c0.selectbox("Loại BĐS", type_opts, key="lst_type")
        proj_opts = ["(tất cả)"] + sorted(df_l["project_name"].dropna().unique().tolist())
        q_proj    = c1.selectbox("Dự án", proj_opts, key="lst_proj")
        dist_opts = ["(tất cả)"] + sorted(df_l["district"].dropna().unique().tolist())
        q_dist2   = c2.selectbox("Quận", dist_opts, key="lst_dist")

        price_min_val = float(df_l["price_ty"].min(skipna=True) or 0)
        price_max_val = min(float(df_l["price_ty"].max(skipna=True) or 100), 100.0)
        price_range   = c3.slider(
            "Giá (tỷ)", 0.0, price_max_val,
            (0.0, price_max_val), step=0.5, key="lst_price",
        )

        months = ["(tất cả)"] + sorted(df_l["crawl_month"].dropna().unique().tolist(), reverse=True)
        q_month = c4.selectbox("Tháng crawl", months, key="lst_month")

    mask2 = pd.Series(True, index=df_l.index)
    if q_type != "(tất cả)":
        mask2 &= df_l["listing_type"] == q_type
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

    chart_type = st.radio(
        "Loại BĐS", ["can-ho", "shophouse", "nha-rieng", "biet-thu", "(tất cả)"],
        horizontal=True, key="chart_type",
    )
    df_valid = df_l2[df_l2["price_per_m2"].notna() & (df_l2["price_per_m2"] > 0)].copy()
    if chart_type != "(tất cả)":
        df_valid = df_valid[df_valid["listing_type"] == chart_type]
    # Bỏ outlier: giá/m² > 500 gần như chắc chắn lỗi dữ liệu nguồn
    df_valid = df_valid[df_valid["price_per_m2"] <= 500]

    c1, c2 = st.columns(2)

    with c1:
        if chart_type in ("can-ho", "(tất cả)"):
            st.subheader("Giá/m² trung bình theo dự án (top 20)")
            df_proj_chart = df_valid[df_valid["project_name"].notna() & (df_valid["listing_type"] == "can-ho")]
            avg_proj = (
                df_proj_chart.groupby("project_name")["price_per_m2"]
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
        else:
            st.subheader("Giá/m² trung bình theo quận (top 20)")
            st.caption(f"⚠️ '{chart_type}' không gắn với dự án cụ thể — hiển thị theo quận")
            avg_dist = (
                df_valid[df_valid["district"].notna()]
                .groupby("district")["price_per_m2"]
                .mean()
                .nlargest(20)
                .reset_index()
                .rename(columns={"district": "Quận", "price_per_m2": "Giá/m² (triệu)"})
            )
            fig1 = px.bar(
                avg_dist, x="Giá/m² (triệu)", y="Quận",
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


# ─── Tab: Xu hướng giá ───────────────────────────────────────────────────────
with tab_trend:
    df_trend = load_listings()
    df_trend = df_trend[
        df_trend["post_date"].notna() &
        df_trend["price_per_m2"].notna() &
        (df_trend["price_per_m2"] > 0) &
        (df_trend["price_per_m2"] <= 500)   # bỏ outlier lỗi nguồn
    ].copy()
    df_trend["post_date"] = pd.to_datetime(df_trend["post_date"])

    with st.expander("🔍 Bộ lọc", expanded=True):
        r1c1, r1c2 = st.columns(2)
        granularity = r1c1.radio(
            "Đơn vị thời gian", ["Ngày", "Tuần", "Tháng", "Năm"],
            horizontal=True, key="trend_gran",
        )
        gran_map = {"Ngày": "D", "Tuần": "W", "Tháng": "MS", "Năm": "YS"}
        gran_fmt = {"Ngày": "%d/%m/%Y", "Tuần": "%d/%m/%Y", "Tháng": "%m/%Y", "Năm": "%Y"}

        metric = r1c2.radio(
            "Chỉ số", ["Giá/m² TB (triệu)", "Giá TB (tỷ)", "Số tin đăng"],
            horizontal=True, key="trend_metric",
        )

        c1, c2, c3, c4 = st.columns(4)
        trend_type_opts = ["can-ho", "shophouse", "nha-rieng", "biet-thu", "(tất cả)"]
        sel_type = c1.selectbox("Loại BĐS", trend_type_opts, key="trend_type")

        # Project filter
        proj_list = ["(tất cả)"] + sorted(df_trend["project_name"].dropna().unique().tolist())
        sel_projs = c2.multiselect("Dự án (đa chọn)", proj_list[1:], key="trend_proj",
                                   placeholder="Tất cả dự án")

        # District filter
        dist_list = ["(tất cả)"] + sorted(df_trend["district"].dropna().unique().tolist())
        sel_dist  = c3.selectbox("Quận/Huyện", dist_list, key="trend_dist")

    # Apply filters
    df_t = df_trend.copy()
    if sel_type != "(tất cả)":
        df_t = df_t[df_t["listing_type"] == sel_type]
    if sel_projs:
        df_t = df_t[df_t["project_name"].isin(sel_projs)]
    if sel_dist != "(tất cả)":
        df_t = df_t[df_t["district"] == sel_dist]

    if df_t.empty:
        st.warning("Không có data sau khi lọc.")
    else:
        # Remove outliers (top 1% price_per_m2)
        p99 = df_t["price_per_m2"].quantile(0.99)
        df_t = df_t[df_t["price_per_m2"] <= p99]

        freq = gran_map[granularity]
        fmt  = gran_fmt[granularity]

        # Group by: project or aggregate
        group_by_proj = len(sel_projs) > 1

        if group_by_proj:
            # Multi-project: one line per project
            records = []
            for proj, grp in df_t.groupby("project_name"):
                grp2 = grp.set_index("post_date").resample(freq)
                if metric == "Giá/m² TB (triệu)":
                    agg = grp2["price_per_m2"].mean()
                elif metric == "Giá TB (tỷ)":
                    agg = grp2["price_ty"].mean()
                else:
                    agg = grp2["price_per_m2"].count()
                agg = agg.dropna()
                for dt, val in agg.items():
                    records.append({"Ngày": dt, "Giá trị": val, "Dự án": proj})
            df_plot = pd.DataFrame(records)
            color_col = "Dự án"
        else:
            # Single or all projects: one aggregated line
            resampled = df_t.set_index("post_date").resample(freq)
            if metric == "Giá/m² TB (triệu)":
                agg = resampled["price_per_m2"].agg(["mean", "median", "count"])
                agg.columns = ["Trung bình", "Trung vị", "Số tin"]
            elif metric == "Giá TB (tỷ)":
                agg = resampled["price_ty"].agg(["mean", "median", "count"])
                agg.columns = ["Trung bình", "Trung vị", "Số tin"]
            else:
                agg = resampled["price_per_m2"].count().rename("Số tin").to_frame()
                agg["Trung bình"] = agg["Số tin"]
                agg["Trung vị"] = agg["Số tin"]

            agg = agg[agg["Số tin"] >= 3].reset_index()
            agg["post_date_str"] = agg["post_date"].dt.strftime(fmt)

            if metric != "Số tin đăng":
                df_melt = agg[["post_date", "Trung bình", "Trung vị", "Số tin"]].melt(
                    id_vars=["post_date", "Số tin"],
                    value_vars=["Trung bình", "Trung vị"],
                    var_name="Loại", value_name="Giá trị",
                )
                df_plot = df_melt.rename(columns={"post_date": "Ngày", "Loại": "Dự án"})
                color_col = "Dự án"
            else:
                df_plot = agg[["post_date", "Số tin"]].rename(
                    columns={"post_date": "Ngày", "Số tin": "Giá trị"}
                )
                df_plot["Dự án"] = "Tất cả"
                color_col = None

        # Format x-axis labels
        df_plot["Nhãn"] = df_plot["Ngày"].dt.strftime(fmt)

        y_label = metric
        title = f"{metric} theo {granularity.lower()}"
        if sel_projs:
            title += f" — {', '.join(sel_projs[:3])}" + ("..." if len(sel_projs) > 3 else "")
        elif sel_dist != "(tất cả)":
            title += f" — {sel_dist}"

        fig = px.line(
            df_plot, x="Ngày", y="Giá trị",
            color=color_col,
            markers=True,
            title=title,
            labels={"Giá trị": y_label, "Ngày": ""},
            height=480,
        )
        fig.update_traces(line_width=2)
        fig.update_xaxes(tickformat=fmt.replace("%d/%m/%Y", "%d/%m").replace("%m/%Y", "%m/%Y"))
        fig.update_layout(legend_title_text="")
        st.plotly_chart(fig, use_container_width=True)

        # Stats table
        if not group_by_proj and metric != "Số tin đăng":
            st.subheader("Bảng chi tiết")
            disp = agg[["post_date_str", "Trung bình", "Trung vị", "Số tin"]].rename(
                columns={"post_date_str": granularity,
                         "Trung bình": f"{metric} (TB)",
                         "Trung vị": f"{metric} (TV)"}
            )
            disp[f"{metric} (TB)"] = disp[f"{metric} (TB)"].round(1)
            disp[f"{metric} (TV)"] = disp[f"{metric} (TV)"].round(1)
            st.dataframe(disp.sort_values(granularity, ascending=False),
                         use_container_width=True, hide_index=True)

        # Annotation: min/max periods
        if not group_by_proj and "Giá trị" in df_plot.columns:
            agg_single = df_plot[df_plot.get("Dự án", "x") != "Trung vị"] if color_col else df_plot
            if not agg_single.empty:
                max_row = agg_single.loc[agg_single["Giá trị"].idxmax()]
                min_row = agg_single.loc[agg_single["Giá trị"].idxmin()]
                c1, c2 = st.columns(2)
                c1.metric(
                    f"📈 Cao nhất ({max_row['Ngày'].strftime(fmt)})",
                    f"{max_row['Giá trị']:.1f}",
                )
                c2.metric(
                    f"📉 Thấp nhất ({min_row['Ngày'].strftime(fmt)})",
                    f"{min_row['Giá trị']:.1f}",
                )


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
