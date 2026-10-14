"""
Tropical waves explorer —— 热带赤道波动可视化网页
运行：streamlit run app.py
"""
import json
import os
import time
import warnings

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

from tropical_waves import Config, WAVE_DEFAULTS
from tropical_waves import background as bgmod
from tropical_waves import data, filters, plots
from tropical_waves.data import DataError

st.set_page_config(page_title="Tropical waves explorer", page_icon="🌊", layout="wide")

st.markdown("""
<style>
  .block-container {padding-top: 1.6rem; max-width: 1500px;}
  h1 {font-weight: 650; letter-spacing: -0.01em; margin-bottom: 0;}
  .lede {color: #8fa3b3; margin-top: 0.2rem; margin-bottom: 1.2rem; max-width: 72ch;}
  [data-testid="stSidebar"] [data-testid="stExpander"] summary p {font-weight: 600;}
  .wave-key {display:inline-block; width: 0.8em; height: 0.8em; border-radius: 2px; margin-right: 0.35em;
             vertical-align: -0.05em;}
</style>
""", unsafe_allow_html=True)

SOURCE_NAMES = {"auto": "自动（本地缓存优先；1974 年至今，推荐）",
                "psl": "只用 NOAA PSL 插值 OLR（1974-06 ~ 2022-12）",
                "local": "指定本地 netCDF 文件", "demo": "合成演示数据（离线，非真实观测）"}
DATA_FIRST, DATA_LAST = pd.Timestamp("1974-06-01"), pd.Timestamp.now().normalize()
BG_NAMES = {"olr": "OLR（无需账号）", "mergir": "红外亮温 GPM_MERGIR（4 km，需 Earthdata）",
            "imerg": "IMERG Final 逐日降水（需 Earthdata）", "imerg-late": "IMERG Late 逐日降水（近期日期）",
            "none": "无背景"}
WAVE_NAMES = {"kelvin": "Kelvin 波", "mjo": "MJO", "er": "赤道 Rossby 波"}
HOV_NAMES = {"filled": "填色：各波动用自己的颜色", "shaded": "阴影：滤波信号总和（紫/橙）",
             "lines": "线条：灰底 + 等值线"}
CMAPS = ["auto", "gray_r", "Greys", "bone", "cividis", "viridis", "magma", "Blues", "YlGnBu", "PuBu"]
DIVERGING = ["PuOr_r", "BrBG_r", "RdBu_r", "PiYG_r", "coolwarm_r"]
PANELS = ["地图", "Hovmöller", "波数-频率谱", "单点时间序列"]

# ----------------------------------------------------------------------------
# 配置状态：上传 JSON 后，用新的 version 重新初始化所有控件
# ----------------------------------------------------------------------------
ss = st.session_state
ss.setdefault("ver", 0)
base = Config.from_dict(ss.get("base"))


def k(name):
    return f"{name}__{ss.ver}"


# ----------------------------------------------------------------------------
# 缓存的数据步骤
# ----------------------------------------------------------------------------
def _collect(fn, *a, **kw):
    msgs = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = fn(*a, log=msgs.append, **kw)
    return out, msgs, [str(w.message) for w in caught if issubclass(w.category, UserWarning)]


@st.cache_data(show_spinner=False, max_entries=3)
def cached_olr(data_key):
    cfg = Config.from_dict(json.loads(data_key))
    return _collect(data.load_olr, cfg)


@st.cache_data(show_spinner=False, max_entries=6)
def cached_filtered(data_key, filter_key):
    olr = cached_olr(data_key)[0]
    cfg = Config.from_dict({**json.loads(data_key), "waves": json.loads(filter_key)})
    return data.compute_filtered(olr, cfg)


@st.cache_data(show_spinner=False, max_entries=6)
def cached_spectrum(data_key, lat_max, seg):
    olr = cached_olr(data_key)[0]
    return filters.wk_spectrum(olr.values, olr.lat.values, seg_len=seg, overlap=int(seg * 0.6), lat_max=lat_max)


def show_fig(fig):
    st.pyplot(fig, width="stretch")
    plt.close(fig)


# ----------------------------------------------------------------------------
# 侧栏：所有参数
# ----------------------------------------------------------------------------
sb = st.sidebar
sb.markdown("### 参数")

with sb.expander("数据与时间", expanded=True):
    source = st.selectbox("OLR 数据源", list(SOURCE_NAMES),
                          index=list(SOURCE_NAMES).index(base.source) if base.source in SOURCE_NAMES else 0,
                          format_func=SOURCE_NAMES.get, key=k("source"))
    c1, c2 = st.columns(2)
    # Streamlit 默认只允许选 value ±10 年，必须显式给出范围，否则选不到 2015 年以前
    start = c1.date_input("开始", pd.Timestamp(base.start), min_value=DATA_FIRST, max_value=DATA_LAST,
                          key=k("start"))
    end = c2.date_input("结束", pd.Timestamp(base.end), min_value=DATA_FIRST, max_value=DATA_LAST,
                        key=k("end"))
    pad = st.slider("滤波缓冲（天，前后各加）", 120, 365, base.pad, 10, key=k("pad"),
                    help="滤波需要比显示时段更长的序列。缓冲越长，波数-频率谱越平滑。")
    olr_files, olr_cache = base.olr_files, base.olr_cache
    if source == "local":
        olr_files = st.text_area("文件路径（每行一个，可用通配符）", "\n".join(base.olr_files) or "data/olr_cache/*.nc",
                                 key=k("files")).split()
    if source in ("auto", "psl"):
        olr_cache = st.text_input("OLR 缓存目录", base.olr_cache, key=k("olr_cache"),
                                  help="目录里的任何 .nc（如 PSL 的 olr.day.mean.nc、CDR 年度文件）都会按时间范围"
                                       "自动识别；缺的年份再在线下载到这里。也可用环境变量 TWV_OLR_DIRS 追加目录。")
        if pd.Timestamp(start) < pd.Timestamp("2024-01-01") and source == "auto":
            st.caption("2024 年以前：依次使用本地文件 → GitHub Release 年度小文件 → NCEI → PSL OPeNDAP。"
                       "2023 年 PSL 没有数据，需要 OLR CDR。")

waves = {}
with sb.expander("波动与滤波", expanded=True):
    thresh_mode = st.radio("阈值单位", ["abs", "std"], ["abs", "std"].index(base.thresh_mode), horizontal=True,
                           format_func={"abs": "W m⁻²", "std": "标准差倍数 σ"}.get, key=k("tmode"),
                           help="各波动滤波后的方差差别很大（Kelvin 频段最宽、方差最大）。"
                                "按 σ 设阈值时，每种波动都在自身强度的同一分位画线，更便于比较。")
    tabs = st.tabs([WAVE_NAMES[w] for w in ("kelvin", "mjo", "er")])
    for tab, w in zip(tabs, ("kelvin", "mjo", "er")):
        p = base.waves[w]
        with tab:
            c1, c2 = st.columns([3, 2])
            en = c1.checkbox("显示", p["enabled"], key=k(f"{w}_en"))
            col = c2.color_picker("颜色", p["color"], key=k(f"{w}_col"), label_visibility="collapsed")
            thr, thr_std = float(p["thresh"]), float(p.get("thresh_std", 1.0))
            if thresh_mode == "abs":
                thr = st.slider("等值线阈值 (W m⁻²)", 2.0, 30.0, thr, 0.5, key=k(f"{w}_thr"),
                                help="滤波 OLR 低于 −阈值 画实线（对流增强），高于 +阈值 画虚线。")
            else:
                thr_std = st.slider("等值线阈值 (σ)", 0.5, 3.0, thr_std, 0.1, key=k(f"{w}_thrs"),
                                    help="σ = 显示时段内 10°S–10°N 滤波 OLR 的标准差。")
            c1, c2 = st.columns(2)
            lat_max = c1.number_input("等值线纬度 ±", 5.0, 30.0, float(p.get("lat_max") or 25.0), 1.0,
                                      key=k(f"{w}_latmax"),
                                      help="Kelvin 波是赤道陷获波，建议 ±15°；更高纬度的信号多为中纬度扰动漏入。")
            sym = c2.checkbox("对称分量滤波", bool(p.get("symmetric")), key=k(f"{w}_sym"),
                              help="先取 (OLR(φ)+OLR(−φ))/2 再滤波（Wheeler–Kiladis 做法），进一步压制"
                                   "中纬度信号；画出的图会关于赤道对称。")
            c1, c2 = st.columns(2)
            tmin = c1.number_input("最短周期 (天)", 1.0, 200.0, float(p["tmin"]), 0.5, key=k(f"{w}_tmin"))
            tmax = c2.number_input("最长周期 (天)", 1.0, 400.0, float(p["tmax"]), 1.0, key=k(f"{w}_tmax"))
            kmin = c1.number_input("最小波数", -30, 30, int(p["kmin"]), key=k(f"{w}_kmin"),
                                   help="正数 = 东传，负数 = 西传")
            kmax = c2.number_input("最大波数", -30, 30, int(p["kmax"]), key=k(f"{w}_kmax"))
            hmin = hmax = None
            if WAVE_DEFAULTS[w]["hmin"] is not None:
                hmin = c1.number_input("最小等效深度 (m)", 1.0, 500.0, float(p["hmin"] or 8), 1.0, key=k(f"{w}_hmin"))
                hmax = c2.number_input("最大等效深度 (m)", 1.0, 500.0, float(p["hmax"] or 90), 1.0, key=k(f"{w}_hmax"))
            waves[w] = dict(enabled=en, color=col, thresh=thr, thresh_std=thr_std, lat_max=lat_max,
                            symmetric=sym, tmin=tmin, tmax=tmax,
                            kmin=int(kmin), kmax=int(kmax), hmin=hmin, hmax=hmax)
    show_suppressed = st.toggle("显示对流抑制位相（虚线）", base.show_suppressed, key=k("supp"))
    contour_lat = st.slider("等值线纬度总上限 ±", 10.0, 30.0, base.contour_lat, 1.0, key=k("clat"))
    linewidth = st.slider("等值线粗细", 0.5, 4.0, base.linewidth, 0.25, key=k("lw"))

with sb.expander("地图"):
    proj = st.selectbox("投影", ["strip", "globe"], ["strip", "globe"].index(base.proj), key=k("proj"),
                        format_func={"strip": "热带带状（全球经度）", "globe": "地球仪视角"}.get)
    center_lon = st.slider("地球仪中心经度", 0.0, 359.0, base.center_lon, 5.0, key=k("clon"),
                           disabled=proj != "globe")
    map_lat = st.slider("带状图纬度范围 ±", 15.0, 60.0, base.map_lat, 5.0, key=k("mlat"),
                        disabled=proj != "strip")
    c1, c2 = st.columns(2)
    coast_color = c1.color_picker("海岸线颜色", base.coast_color, key=k("coast"))
    show_grid = c2.checkbox("经纬网格", base.show_grid, key=k("grid"))

with sb.expander("背景"):
    bg = st.selectbox("背景场", list(BG_NAMES), list(BG_NAMES).index(base.bg), format_func=BG_NAMES.get,
                      key=k("bg"))
    bg_hour = st.slider("云图时刻 (UTC)", 0, 23, base.bg_hour, key=k("bghour"), disabled=bg != "mergir")
    bg_cmap = st.selectbox("背景配色", CMAPS, CMAPS.index(base.bg_cmap) if base.bg_cmap in CMAPS else 0,
                           key=k("bgcmap"))
    c1, c2 = st.columns(2)
    vmin_txt = c1.text_input("色标下限", "" if base.bg_vmin is None else str(base.bg_vmin), key=k("vmin"),
                             placeholder="自动")
    vmax_txt = c2.text_input("色标上限", "" if base.bg_vmax is None else str(base.bg_vmax), key=k("vmax"),
                             placeholder="自动")
    bg_res = st.select_slider("背景分辨率 (度)", [0.1, 0.2, 0.3, 0.5], base.bg_res, key=k("bgres"))
    if bg in bgmod.PRODUCTS:
        st.caption("需要 Earthdata 账号，并在账号里接受 NASA GESDISC DATA ARCHIVE 的 EULA。"
                   "也可以在 ~/.netrc 里配置，下面留空。")
        u = st.text_input("Earthdata 用户名", key="ed_user")
        pw = st.text_input("Earthdata 密码", type="password", key="ed_pass")
        if u and pw:
            os.environ["EARTHDATA_USERNAME"], os.environ["EARTHDATA_PASSWORD"] = u, pw

with sb.expander("Hovmöller、谱与时间序列"):
    hov_style = st.selectbox("Hovmöller 样式", list(HOV_NAMES), list(HOV_NAMES).index(base.hov_style),
                             format_func=HOV_NAMES.get, key=k("hovstyle"))
    hov_cmap = st.selectbox("阴影配色（阴影样式）", DIVERGING,
                            DIVERGING.index(base.hov_cmap) if base.hov_cmap in DIVERGING else 0,
                            key=k("hovcmap"), disabled=hov_style != "shaded")
    hov_lat = st.slider("经向平均带 ±", 2.5, 20.0, base.hov_lat, 2.5, key=k("hovlat"))
    hov_scale = st.slider("Hovmöller 阈值系数", 0.3, 2.0, base.hov_thresh_scale, 0.1, key=k("hovsc"),
                          help="经向平均后振幅变小，所以阈值 = 地图阈值 × 该系数。画面太乱就调大。")
    spec_lat = st.slider("谱分析纬度带 ±", 5.0, 25.0, base.spec_lat, 2.5, key=k("speclat"))
    spec_seg = st.select_slider("谱分析分段长度 (天)", [64, 96, 128, 180], base.spec_seg, key=k("specseg"))

with sb.expander("外观"):
    theme = st.radio("主题", ["dark", "light"], ["dark", "light"].index(base.theme), horizontal=True,
                     format_func={"dark": "深色", "light": "浅色"}.get, key=k("theme"))
    lang = st.radio("图中文字", ["en", "zh"], ["en", "zh"].index(base.lang), horizontal=True,
                    format_func={"en": "English", "zh": "中文"}.get, key=k("lang"))
    font_family = st.text_input("字体（中文需要，如 PingFang SC / Noto Sans CJK SC）", base.font_family,
                                key=k("font"))
    title = st.text_input("标题（留空 = 默认）", base.title, key=k("title"))
    fig_width = st.slider("图宽 (英寸)", 8.0, 18.0, base.fig_width, 0.5, key=k("figw"))


def _num(txt):
    try:
        return float(txt) if str(txt).strip() else None
    except ValueError:
        return None


cfg = Config.from_dict(dict(
    start=str(start), end=str(end), pad=pad, source=source, olr_files=olr_files, olr_cache=olr_cache,
    data_release=base.data_release, thresh_mode=thresh_mode,
    waves=waves, contour_lat=contour_lat, show_suppressed=show_suppressed, linewidth=linewidth,
    proj=proj, center_lon=center_lon, map_lat=map_lat, coast_color=coast_color, show_grid=show_grid,
    bg=bg, bg_hour=bg_hour, bg_res=bg_res, bg_cache=base.bg_cache, bg_cmap=bg_cmap,
    bg_vmin=_num(vmin_txt), bg_vmax=_num(vmax_txt),
    hov_style=hov_style, hov_lat=hov_lat, hov_thresh_scale=hov_scale, hov_cmap=hov_cmap,
    spec_lat=spec_lat, spec_seg=spec_seg, ts_lon=ss.get(k("tslon"), base.ts_lon),
    theme=theme, lang=lang, font_family=font_family, title=title, fig_width=fig_width,
    layout=ss.get(k("layout"), base.layout), frames_per_day=ss.get(k("fpd"), base.frames_per_day),
    fps=ss.get(k("fps"), base.fps), dpi=ss.get(k("dpi"), base.dpi),
    out_format=ss.get(k("fmt"), base.out_format),
))

# ----------------------------------------------------------------------------
# 页面主体
# ----------------------------------------------------------------------------
st.title("Tropical waves explorer")
keys_html = " ".join(f'<span class="wave-key" style="background:{cfg.waves[w]["color"]}"></span>{WAVE_NAMES[w]}'
                     for w in ("kelvin", "mjo", "er") if cfg.waves[w]["enabled"])
st.markdown(f'<p class="lede">热带对流以行星尺度的波动形式传播。下面的面板共用同一个日期：'
            f'拖动日期，看 {keys_html} 的对流信号在地图和 Hovmöller 图上如何移动。</p>',
            unsafe_allow_html=True)

if pd.Timestamp(cfg.end) < pd.Timestamp(cfg.start):
    st.error("结束日期早于开始日期，请在侧栏“数据与时间”里调整。")
    st.stop()
if not cfg.enabled_waves():
    st.warning("所有波动都被关闭了，请在侧栏“波动与滤波”里至少打开一种。")

try:
    with st.spinner("读取并滤波 OLR 数据（第一次需要下载，之后会使用缓存）…"):
        olr, logs, warns = cached_olr(cfg.data_key())
        filt = cached_filtered(cfg.data_key(), cfg.filter_key())
except DataError as e:
    st.error(str(e))
    st.stop()
except Exception as e:                                   # noqa: BLE001
    st.error(f"读取数据时出错：{type(e).__name__}: {e}")
    st.stop()
for w in warns:
    st.warning(w)
sig = [f"{WAVE_NAMES[w]} σ = {filt[w].attrs.get('std_eq', float('nan')):.1f}" for w in cfg.enabled_waves()]
if sig:
    st.caption("10°S–10°N 滤波 OLR 标准差（W m⁻²）：" + "，".join(sig) +
               "。σ 越大，同一个绝对阈值下等值线范围越大。")
if cfg.source == "demo":
    st.info("当前是合成演示数据，只用于熟悉界面，不能当作真实观测展示。")

tab_dash, tab_export, tab_cfg = st.tabs(["仪表盘", "导出动画", "配置与说明"])

with tab_dash:
    days = pd.date_range(cfg.start, cfg.end, freq="D") + \
        pd.Timedelta(hours=cfg.bg_hour if cfg.bg == "mergir" else 0)
    labels = [d.strftime("%Y-%m-%d %H:%M UTC" if d.hour else "%Y-%m-%d") for d in days]
    c1, c2 = st.columns([4, 2])
    # key 里带上时间范围：改了日期后滑块重新初始化，避免旧值不在新选项里
    picked = c1.select_slider("日期", options=labels, value=labels[0],
                              key=f"day_{cfg.start}_{cfg.end}_{cfg.bg}_{cfg.bg_hour}")
    t = days[labels.index(picked)]
    panels = c2.multiselect("显示面板", PANELS, default=PANELS, key="panels")

    if "地图" in panels:
        bg_path = None
        if cfg.bg in bgmod.PRODUCTS:
            try:
                with st.spinner("准备背景（每个时刻只下载一次）…"):
                    bg_path = bgmod.prepare(cfg.bg, [t], cfg.bg_cache, cfg.bg_res, plots.bg_lat_max(cfg))[0]
                if bg_path is None:
                    st.warning("这个时刻没有找到背景文件，地图只显示等值线。")
            except DataError as e:
                st.warning(f"背景加载失败，先只画等值线。{e}")
            except Exception as e:                       # noqa: BLE001
                st.warning(f"背景加载失败（{type(e).__name__}: {e}），先只画等值线。")
        show_fig(plots.fig_map(cfg, olr, filt, t, bg_path))

    cols = st.columns(2) if {"Hovmöller", "波数-频率谱"} <= set(panels) else [st.container()]
    i = 0
    if "Hovmöller" in panels:
        with cols[i]:
            show_fig(plots.fig_hovmoller(cfg, olr, filt, t))
            st.caption("横轴是经度，纵轴是时间（向下）。色带向右下倾斜 = 东传，向左下倾斜 = 西传；"
                       "倾斜越平，传播越快。")
        i = min(i + 1, len(cols) - 1)
    if "波数-频率谱" in panels:
        with cols[i]:
            with st.spinner("计算波数-频率谱…"):
                spec = cached_spectrum(cfg.data_key(), cfg.spec_lat, cfg.spec_seg)
            show_fig(plots.fig_spectrum(cfg, spec))
            st.caption(f"亮色 = 比背景噪声更强的信号；彩色框 = 各波动的滤波区域，"
                       f"虚线 = 等效深度 8/25/90 m 的理论频散曲线。共 {spec['n_segments']} 段平均，"
                       "缓冲期越长越平滑。")

    if "单点时间序列" in panels:
        st.slider("时间序列所在经度 (°E)", 0.0, 357.5, float(cfg.ts_lon), 2.5, key=k("tslon"))
        show_fig(plots.fig_timeseries(cfg, olr, filt, t))

with tab_export:
    st.markdown("把当前设置导出成动画，可直接插入 PPT。MP4 体积更小、更清晰（需要 ffmpeg）。")
    c1, c2, c3 = st.columns(3)
    c1.selectbox("版式", ["map+hov", "map"], ["map+hov", "map"].index(cfg.layout), key=k("layout"),
                 format_func={"map+hov": "地图 + Hovmöller", "map": "只有地图"}.get)
    c2.selectbox("格式", ["gif", "mp4"], ["gif", "mp4"].index(cfg.out_format), key=k("fmt"))
    c3.selectbox("每天帧数", [1, 2, 4, 8], [1, 2, 4, 8].index(cfg.frames_per_day) if cfg.frames_per_day in
                 (1, 2, 4, 8) else 0, key=k("fpd"),
                 help=">1 时等值线在日内线性插值；MERGIR 背景会按小时下载，下载量成倍增加。")
    c1.slider("帧率 (fps)", 2.0, 30.0, float(cfg.fps), 1.0, key=k("fps"))
    c2.slider("分辨率 (dpi)", 60, 200, int(cfg.dpi), 10, key=k("dpi"))
    n_frames = len(data.frame_times(cfg))
    c3.metric("帧数", n_frames)

    if st.button("生成动画", type="primary"):
        os.makedirs("outputs", exist_ok=True)
        out = os.path.join("outputs", f"tropical_waves_{cfg.start}_{cfg.end}_{int(time.time())}.{cfg.out_format}")
        bar = st.progress(0.0, "准备中…")
        try:
            bg_paths = None
            if cfg.bg in bgmod.PRODUCTS:
                bar.progress(0.0, "下载并处理背景…")
                bg_paths = bgmod.prepare(cfg.bg, data.frame_times(cfg), cfg.bg_cache, cfg.bg_res,
                                         plots.bg_lat_max(cfg),
                                         progress=lambda f: bar.progress(f * 0.5, "下载并处理背景…"))
            off = 0.5 if bg_paths else 0.0
            plots.render_animation(cfg, olr, filt, out, bg_paths,
                                   progress=lambda f: bar.progress(off + f * (1 - off), f"渲染中 {f:.0%}"))
            bar.progress(1.0, "完成")
            ss["last_anim"] = out
        except DataError as e:
            st.error(str(e))
        except FileNotFoundError as e:
            st.error(f"写出失败：{e}。导出 MP4 需要安装 ffmpeg，或改用 GIF。")
        except Exception as e:                           # noqa: BLE001
            st.error(f"渲染出错：{type(e).__name__}: {e}")

    out = ss.get("last_anim")
    if out and os.path.exists(out):
        with open(out, "rb") as f:
            blob = f.read()
        if out.endswith(".mp4"):
            st.video(blob)
        else:
            st.image(blob)
        st.download_button(f"下载 {os.path.basename(out)}（{len(blob) / 1e6:.1f} MB）", blob,
                           file_name=os.path.basename(out))

with tab_cfg:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### 保存当前设置")
        st.download_button("下载配置 JSON", cfg.to_json(), file_name="tropical_waves_config.json",
                           mime="application/json")
        st.caption("同一份 JSON 也可以给命令行用：`python cli.py --config tropical_waves_config.json`")
    with c2:
        st.markdown("#### 载入设置")
        up = st.file_uploader("选择配置 JSON", type="json")
        if up is not None and ss.get("loaded_name") != (up.name, up.size):
            try:
                ss["base"] = Config.from_json(up.read().decode("utf-8")).to_dict()
                ss["loaded_name"] = (up.name, up.size)
                ss.ver += 1
                st.rerun()
            except Exception as e:                       # noqa: BLE001
                st.error(f"配置文件读取失败：{e}")
        if st.button("恢复默认设置"):
            ss["base"], ss["loaded_name"] = None, None
            ss.ver += 1
            st.rerun()

    st.markdown("#### 各面板在看什么")
    st.markdown("""
- **地图**：背景是云（OLR 或红外亮温，越白云顶越冷、对流越强）或降水；彩色等值线是滤波后各波动的对流信号。
- **Hovmöller**：把赤道附近的信号沿纬度平均后，按“经度 × 时间”展开。传播方向和速度一目了然：MJO 约 5 m/s 缓慢东传，Kelvin 波约 15 m/s 快速东传，赤道 Rossby 波向西传。
- **波数-频率谱**：把一年多的 OLR 分解到“波数 × 频率”平面。亮色的峰正好落在浅水方程赤道波的理论曲线上，说明这些对流信号确实是有组织的波动；彩色框就是滤波时保留的区域，调整侧栏里的周期、波数和等效深度，框会跟着变。
- **单点时间序列**：某一经度上各波动信号随时间的变化，可以直观比较它们的周期。
""")
    if logs:
        with st.expander("数据读取日志"):
            st.code("\n".join(logs))
    st.markdown("#### 数据与方法")
    st.markdown("""
OLR：NOAA Climate Data Record of Daily OLR v02r00（Lee 2025, doi:10.25921/9pt5-by77）；
红外亮温：NCEP/CPC GPM_MERGIR；降水：NASA GPM IMERG V07。
滤波方法：Wheeler & Kiladis (1999, JAS)；Kiladis et al. (2009, Rev. Geophys.)。
""")
