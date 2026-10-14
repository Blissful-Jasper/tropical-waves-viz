"""所有绘图：地图、Hovmöller、波数-频率谱、单点时间序列，以及动画导出。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, to_rgba
from matplotlib.lines import Line2D
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from cartopy.util import add_cyclic_point

from . import background as bgmod
from .data import at_time, frame_times, olr_anomaly
from .filters import dispersion_freq, filter_region

PC = ccrs.PlateCarree()

THEMES = {
    "dark": dict(fig="#0f1c26", ax="#000000", text="#e6edf2", muted="#8fa3b3",
                 spine="#4a5d6b", marker="#ffd60a", spec_cmap="bone"),
    "light": dict(fig="#ffffff", ax="#f4f6f8", text="#1b2730", muted="#5b6b78",
                  spine="#9aa7b2", marker="#b45309", spec_cmap="Greys"),
}


def theme(cfg):
    return THEMES.get(cfg.theme, THEMES["dark"])


def rc(cfg):
    th = theme(cfg)
    d = {"text.color": th["text"], "axes.labelcolor": th["text"], "xtick.color": th["text"],
         "ytick.color": th["text"], "font.size": 10, "axes.edgecolor": th["spine"],
         "figure.facecolor": th["fig"], "savefig.facecolor": th["fig"], "axes.unicode_minus": False}
    if cfg.font_family:
        d["font.sans-serif"] = [cfg.font_family, "DejaVu Sans"]
        d["font.family"] = "sans-serif"
    return d


def band_label(lat):
    return f"{lat:g}°S–{lat:g}°N"


def lon_label(lon):
    lon = lon % 360
    if lon == 0 or lon == 180:
        return f"{lon:g}°"
    return f"{lon:g}°E" if lon < 180 else f"{360 - lon:g}°W"


def window(cfg, da):
    return da.sel(time=slice(cfg.start, cfg.end))


def source_text(cfg):
    if cfg.source == "demo":
        return "SYNTHETIC DEMO DATA (not observations)"
    if cfg.bg in bgmod.PRODUCTS:
        return f"Background: {bgmod.PRODUCTS[cfg.bg]['label']}  |  Waves: filtered NOAA OLR"
    return "Background & waves: NOAA daily OLR"


def legend_handles(cfg, include_suppressed=True):
    h = [Line2D([], [], color=cfg.waves[w]["color"], lw=2, label=cfg.label(w))
         for w in cfg.enabled_waves()[::-1]]
    h.append(Line2D([], [], color=theme(cfg)["text"], lw=1.5, label=cfg.label("enhanced")))
    if cfg.show_suppressed and include_suppressed:
        h.append(Line2D([], [], color=theme(cfg)["text"], lw=1.5, ls="--", label=cfg.label("suppressed")))
    return h


def bg_lat_max(cfg):
    return min(60.0, cfg.map_lat + 5) if cfg.proj == "strip" else 60.0


# ----------------------------------------------------------------------------
# 地图
# ----------------------------------------------------------------------------
def make_map_axes(fig, rect, cfg):
    if cfg.proj == "strip":
        ax = fig.add_axes(rect, projection=ccrs.PlateCarree(central_longitude=180))
        ax.set_extent([0, 359.9, -cfg.map_lat, cfg.map_lat], crs=PC)
    else:
        ax = fig.add_axes(rect, projection=ccrs.Orthographic(cfg.center_lon, 0))
        ax.set_global()
    ax.set_facecolor("black")
    ax.add_feature(cfeature.COASTLINE, edgecolor=cfg.coast_color, linewidth=0.6, zorder=5)
    if cfg.show_grid:
        step = 15 if cfg.map_lat <= 45 else 30
        gl = ax.gridlines(crs=PC, draw_labels=(cfg.proj == "strip"), linewidth=0.3, color="w",
                          alpha=0.3, linestyle=":", xlocs=range(-180, 181, 60),
                          ylocs=range(-90, 91, step))
        if cfg.proj == "strip":
            gl.top_labels = gl.right_labels = False
            gl.xlabel_style = gl.ylabel_style = {"color": theme(cfg)["text"], "size": 9}
    return ax


def draw_map_frame(ax, cfg, olr, filt, t, bg_path=None):
    """画某一时刻的背景 + 波动等值线，返回新建的 artist 列表（动画时用于删除）"""
    arts = []
    lon = olr.lon.values
    if cfg.bg == "olr":
        f, lonc = add_cyclic_point(at_time(olr, t).values, coord=lon)
        cmap = "gray_r" if cfg.bg_cmap in ("auto", "") else cfg.bg_cmap
        levels = np.linspace(cfg.bg_vmin or 110, cfg.bg_vmax or 300, 39)
        arts.append(ax.contourf(lonc, olr.lat.values, f, levels=levels, cmap=cmap, extend="both",
                                transform=PC, zorder=1))
    elif cfg.bg in bgmod.PRODUCTS and bg_path is not None:
        z = np.load(bg_path)
        blat, blon, bdat = z["lat"], z["lon"], z["data"]
        c0 = 180 if cfg.proj == "strip" else 0
        x = ((blon - c0 + 180) % 360) - 180
        order = np.argsort(x)
        x, bdat = x[order], bdat[:, order]
        dx, dy = float(np.median(np.diff(x))), float(np.median(np.diff(blat)))
        cmap, norm = bgmod.style(cfg.bg, cfg.bg_cmap, cfg.bg_vmin, cfg.bg_vmax)
        if cfg.bg != "mergir":
            bdat = np.where(bdat < norm.vmin, norm.vmin * 0.5, bdat)
        kw = {} if cfg.proj == "strip" else {"regrid_shape": 900}
        arts.append(ax.imshow(bdat, origin="lower", transform=ccrs.PlateCarree(central_longitude=c0),
                              cmap=cmap, norm=norm, interpolation="bilinear", zorder=1,
                              extent=[x[0] - dx / 2, x[-1] + dx / 2, blat[0] - dy / 2, blat[-1] + dy / 2],
                              **kw))
    for w in cfg.enabled_waves():
        da = filt[w]
        fv = at_time(da, t).values.copy()
        fv[np.abs(da.lat.values) > cfg.contour_lat, :] = np.nan
        fv, lonc = add_cyclic_point(fv, coord=da.lon.values)
        p = cfg.waves[w]
        arts.append(ax.contour(lonc, da.lat.values, fv, levels=[-p["thresh"]], colors=[p["color"]],
                               linewidths=cfg.linewidth, linestyles="solid", transform=PC, zorder=6))
        if cfg.show_suppressed:
            arts.append(ax.contour(lonc, da.lat.values, fv, levels=[p["thresh"]], colors=[p["color"]],
                                   linewidths=cfg.linewidth * 0.6, linestyles="--", transform=PC, zorder=6))
    return arts


# ----------------------------------------------------------------------------
# Hovmöller
# ----------------------------------------------------------------------------
def draw_hovmoller(ax, cfg, olr, filt):
    th = theme(cfg)
    ax.set_facecolor(th["ax"] if cfg.hov_style != "filled" else "black")
    band = slice(-cfg.hov_lat, cfg.hov_lat)
    lon = olr.lon.values
    anom = window(cfg, olr_anomaly(olr).sel(lat=band).mean("lat"))
    hy = pd.to_datetime(anom.time.values)
    hb, hx = add_cyclic_point(anom.values, coord=lon)
    hvs = {w: add_cyclic_point(window(cfg, filt[w].sel(lat=band).mean("lat")).values, coord=lon)[0]
           for w in cfg.enabled_waves()}
    sc = cfg.hov_thresh_scale

    if len(hy) >= 2:
        if cfg.hov_style == "filled":
            dim = LinearSegmentedColormap.from_list("dim", ["#000000", "#5a5a5a"])
            ax.contourf(hx, hy, -hb, levels=np.linspace(-60, 60, 25), cmap=dim, extend="both")
            for w in cfg.enabled_waves():
                p = cfg.waves[w]
                thr, col = sc * p["thresh"], p["color"]
                ax.contourf(hx, hy, hvs[w], levels=[-1e9, -2 * thr, -thr],
                            colors=[to_rgba(col, 0.9), to_rgba(col, 0.5)])
                ax.contour(hx, hy, hvs[w], levels=[-thr], colors=[col], linewidths=0.6, linestyles="solid")
                if cfg.show_suppressed:
                    ax.contour(hx, hy, hvs[w], levels=[thr], colors=[col], linewidths=0.5,
                               linestyles="--", alpha=0.6)
        elif cfg.hov_style == "shaded" and hvs:
            tot = sum(hvs.values())
            vmax = 2.5 * sc * max(cfg.waves[w]["thresh"] for w in hvs)
            cf = ax.contourf(hx, hy, tot, levels=np.linspace(-vmax, vmax, 21), cmap=cfg.hov_cmap, extend="both")
            cb = ax.figure.colorbar(cf, ax=ax, fraction=0.025, pad=0.01)
            cb.set_label("filtered OLR (W m$^{-2}$)", fontsize=8)
            cb.ax.tick_params(labelsize=7)
            for w in hvs:
                ax.contour(hx, hy, hvs[w], levels=[-sc * cfg.waves[w]["thresh"]],
                           colors=[cfg.waves[w]["color"]], linewidths=1.0, linestyles="solid")
        else:
            ax.contourf(hx, hy, -hb, levels=np.linspace(-60, 60, 25), cmap="gray", extend="both")
            for w in hvs:
                p = cfg.waves[w]
                ax.contour(hx, hy, hvs[w], levels=[-sc * p["thresh"]], colors=[p["color"]],
                           linewidths=1.2, linestyles="solid")
                if cfg.show_suppressed:
                    ax.contour(hx, hy, hvs[w], levels=[sc * p["thresh"]], colors=[p["color"]],
                               linewidths=0.8, linestyles="--")
        ax.set_ylim(hy[-1], hy[0])               # 时间向下增加
    ax.set_xlim(0, 360)
    ax.set_xticks(range(0, 361, 60))
    ax.set_xticklabels([lon_label(v) if v not in (0, 360) else "0°" for v in range(0, 361, 60)])
    ax.set_xlabel(cfg.label("lon"))
    ax.yaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    ax.set_title(cfg.label("hov", band=band_label(cfg.hov_lat)), fontsize=10)
    marker = ax.axhline(hy[0] if len(hy) else 0, color=th["marker"], lw=1.5)
    return marker


# ----------------------------------------------------------------------------
# 波数-频率谱
# ----------------------------------------------------------------------------
def draw_spectrum(ax, cfg, spec):
    th = theme(cfg)
    ax.set_facecolor(th["ax"])
    k, freq = spec["k"], spec["freq"]
    sel = np.abs(k) <= 15
    ratio = spec["sym"][:, sel] / spec["bg"][:, sel]
    cf = ax.contourf(k[sel], freq, ratio, levels=[1.0, 1.1, 1.2, 1.3, 1.4, 1.6, 1.8, 2.0, 2.5, 3.0],
                     cmap=th["spec_cmap"], extend="max")
    cb = ax.figure.colorbar(cf, ax=ax, fraction=0.04, pad=0.02)
    cb.set_label("symmetric / background", fontsize=8)
    cb.ax.tick_params(labelsize=7)
    # 浅水波频散曲线（等效深度 8、25、90 m）
    kk = np.linspace(-15, 15, 301)
    for h in (8, 25, 90):
        kp, kn = kk[kk > 0], kk[kk < 0]
        ax.plot(kp, dispersion_freq(kp, h, "kelvin"), color=th["muted"], lw=0.8, ls=":")
        ax.plot(kn, dispersion_freq(kn, h, "er"), color=th["muted"], lw=0.8, ls=":")
        ax.text(14.6, min(0.48, float(dispersion_freq(14.6, h, "kelvin"))), f"{h} m",
                color=th["muted"], fontsize=7, ha="right", va="bottom")
    # 各波动的滤波区域
    for w in cfg.enabled_waves():
        p, col = cfg.waves[w], cfg.waves[w]["color"]
        kr, lo, hi = filter_region(p, w)
        ok = lo < hi
        if not ok.any():
            continue
        kr, lo, hi = kr[ok], lo[ok], hi[ok]
        ax.fill_between(kr, lo, hi, color=col, alpha=0.18, lw=0)
        ax.plot(np.r_[kr, kr[::-1], kr[0]], np.r_[lo, hi[::-1], lo[0]], color=col, lw=1.6)
    for per in (3, 6, 30):
        ax.axhline(1 / per, color=th["muted"], lw=0.5, ls="--", alpha=0.6)
        ax.text(-14.6, 1 / per, f"{per} d", color=th["muted"], fontsize=7, va="bottom")
    ax.axvline(0, color=th["muted"], lw=0.5)
    ax.set_xlim(-15, 15)
    ax.set_ylim(0, min(0.5, float(freq.max())))
    ax.set_xlabel(cfg.label("wavenumber"))
    ax.set_ylabel(cfg.label("freq"))
    ax.set_title(cfg.label("spec"), fontsize=10)


# ----------------------------------------------------------------------------
# 单点时间序列
# ----------------------------------------------------------------------------
def draw_timeseries(ax, cfg, olr, filt):
    th = theme(cfg)
    ax.set_facecolor(th["ax"])
    band = slice(-cfg.hov_lat, cfg.hov_lat)
    anom = window(cfg, olr_anomaly(olr).sel(lat=band).mean("lat").sel(lon=cfg.ts_lon % 360, method="nearest"))
    tt = pd.to_datetime(anom.time.values)
    ax.fill_between(tt, 0, anom.values, color=th["muted"], alpha=0.35, lw=0, label=cfg.label("olr_anom"))
    for w in cfg.enabled_waves():
        s = window(cfg, filt[w].sel(lat=band).mean("lat").sel(lon=cfg.ts_lon % 360, method="nearest"))
        ax.plot(pd.to_datetime(s.time.values), s.values, color=cfg.waves[w]["color"], lw=1.6, label=cfg.label(w))
    ax.axhline(0, color=th["muted"], lw=0.6)
    ax.invert_yaxis()                            # 向上 = OLR 负异常 = 对流增强
    ax.set_ylabel("OLR (W m$^{-2}$), up = more convection", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    actual = float(anom.lon)
    ax.set_title(cfg.label("ts", lon=lon_label(actual), band=band_label(cfg.hov_lat)), fontsize=10)
    ax.legend(loc="upper left", fontsize=7, frameon=False, ncol=2)
    ax.grid(alpha=0.15)
    return ax.axvline(tt[0], color=th["marker"], lw=1.5)


# ----------------------------------------------------------------------------
# 单张图（网页面板用）
# ----------------------------------------------------------------------------
def _title(cfg, t):
    tstr = f"{pd.Timestamp(t):%Y-%m-%d %H:%M} UTC" if pd.Timestamp(t) != pd.Timestamp(t).normalize() \
        else f"{pd.Timestamp(t):%Y-%m-%d}"
    return f"{cfg.title or cfg.label('title')}   {tstr}"


def fig_map(cfg, olr, filt, t, bg_path=None):
    with plt.rc_context(rc(cfg)):
        W = cfg.fig_width
        if cfg.proj == "strip":
            mh = 0.9 * W * 2 * cfg.map_lat / 360        # 地图本身的高度（英寸）
            H = mh + 1.5                                 # 上方标题 0.5 + 下方刻度与图例 1.0
            fig = plt.figure(figsize=(W, H))
            ax = make_map_axes(fig, [0.05, 1.0 / H, 0.9, mh / H], cfg)
        else:
            fig = plt.figure(figsize=(W * 0.6, W * 0.66))
            ax = make_map_axes(fig, [0.05, 0.13, 0.9, 0.78], cfg)
        draw_map_frame(ax, cfg, olr, filt, t, bg_path)
        fig.text(0.5, 0.97, _title(cfg, t), ha="center", va="top", fontsize=12, weight="bold")
        fig.text(0.99, 0.005, source_text(cfg), ha="right", va="bottom", fontsize=7, color=theme(cfg)["muted"])
        fig.legend(handles=legend_handles(cfg), loc="lower center", ncol=3 if cfg.proj == "globe" else 5,
                   frameon=False, fontsize=8, bbox_to_anchor=(0.5, 0.02))
    return fig


def fig_hovmoller(cfg, olr, filt, t=None):
    with plt.rc_context(rc(cfg)):
        fig, ax = plt.subplots(figsize=(7, 6), constrained_layout=True)
        m = draw_hovmoller(ax, cfg, olr, filt)
        if t is not None:
            m.set_ydata([pd.Timestamp(t)] * 2)
    return fig


def fig_spectrum(cfg, spec):
    with plt.rc_context(rc(cfg)):
        fig, ax = plt.subplots(figsize=(6.5, 6), constrained_layout=True)
        draw_spectrum(ax, cfg, spec)
    return fig


def fig_timeseries(cfg, olr, filt, t=None):
    with plt.rc_context(rc(cfg)):
        fig, ax = plt.subplots(figsize=(12, 3.2), constrained_layout=True)
        m = draw_timeseries(ax, cfg, olr, filt)
        if t is not None:
            m.set_xdata([pd.Timestamp(t)] * 2)
    return fig


# ----------------------------------------------------------------------------
# 动画
# ----------------------------------------------------------------------------
def build_animation_figure(cfg, olr, filt):
    """按 layout 建立画布，返回 (fig, ax_map, hov_marker 或 None)"""
    W = cfg.fig_width
    with_hov = cfg.layout == "map+hov"
    if cfg.proj == "strip":
        aspect = 2 * cfg.map_lat / 360
        if with_hov:
            fig = plt.figure(figsize=(W, W * 0.633))
            ax = make_map_axes(fig, [0.06, 0.60, 0.88, 0.30], cfg)
            axh = fig.add_axes([0.10, 0.11, 0.80, 0.40])
        else:
            mh = 0.9 * W * aspect
            H = mh + 1.5
            fig = plt.figure(figsize=(W, H))
            ax = make_map_axes(fig, [0.05, 1.0 / H, 0.90, mh / H], cfg)
    else:
        if with_hov:
            fig = plt.figure(figsize=(W * 1.04, W * 0.55))
            ax = make_map_axes(fig, [0.02, 0.10, 0.48, 0.78], cfg)
            axh = fig.add_axes([0.58, 0.14, 0.38, 0.70])
        else:
            fig = plt.figure(figsize=(W * 0.625, W * 0.667))
            ax = make_map_axes(fig, [0.05, 0.12, 0.90, 0.78], cfg)
    marker = draw_hovmoller(axh, cfg, olr, filt) if with_hov else None
    narrow = cfg.proj == "globe" and not with_hov
    fig.legend(handles=legend_handles(cfg), loc="lower center", ncol=3 if narrow else 5,
               frameon=False, fontsize=9, bbox_to_anchor=(0.5, 0.005))
    fig.text(0.99, 0.985, source_text(cfg), ha="right", va="top", fontsize=8,
             color="#ff9f43" if cfg.source == "demo" else theme(cfg)["muted"])
    return fig, ax, marker


def render_animation(cfg, olr, filt, out_path, bg_paths=None, progress=None):
    """逐帧渲染并写出 GIF/MP4；progress(0~1) 回调用于网页进度条"""
    from matplotlib.animation import FFMpegWriter, PillowWriter
    times = frame_times(cfg)
    with plt.rc_context(rc(cfg)):
        fig, ax, marker = build_animation_figure(cfg, olr, filt)
        title = fig.text(0.5, 0.965, "", ha="center", va="top", fontsize=13, weight="bold")
        writer = FFMpegWriter(fps=cfg.fps, bitrate=3000) if out_path.lower().endswith(".mp4") \
            else PillowWriter(fps=cfg.fps)
        with writer.saving(fig, out_path, cfg.dpi):
            for i, t in enumerate(times):
                arts = draw_map_frame(ax, cfg, olr, filt, t, bg_paths[i] if bg_paths else None)
                title.set_text(_title(cfg, t))
                if marker is not None:
                    marker.set_ydata([t, t])
                writer.grab_frame(facecolor=theme(cfg)["fig"])
                for a in arts:
                    a.remove()
                if progress:
                    progress((i + 1) / len(times))
        plt.close(fig)
    return out_path
