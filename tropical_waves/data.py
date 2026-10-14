"""OLR 数据：在线下载（NOAA OLR CDR / PSL）、本地文件、合成演示数据；以及滤波。"""
from __future__ import annotations

import glob
import os
import re
import warnings
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import xarray as xr

from .filters import A, kf_filter


class DataError(Exception):
    """数据获取/读取失败，消息里写清楚原因和解决办法。"""


# (1) NOAA/UMD 逐日 OLR CDR v02r00（1°，约 2 天延迟） 官方页面 http://olr.umd.edu/
CDR_DIRS = [
    "http://olr.umd.edu/CDR/Daily/v02r00/",
    "http://olr.umd.edu/CDR/Daily/v02r00-interim/",
    "https://archive.data.noaa.gov/climatedatarecords/UMD_ESSIC/OLR_CDR/Daily/OLR-D-CDR_01B-21/",
]
CDR_PORTAL = "http://olr.umd.edu/"
# (2) NOAA PSL 插值 OLR（2.5°）——已停更，只到 2022-12-31
PSL_URL = "https://psl.noaa.gov/thredds/dodsC/Datasets/interp_OLR/olr.day.mean.nc"
PSL_LAST = pd.Timestamp("2022-12-31")


# ----------------------------------------------------------------------------
# 读取与规范化
# ----------------------------------------------------------------------------
def normalize(ds):
    """统一变量名/坐标名/经度范围，适配不同版本的 OLR 文件"""
    ren = {c: n for c, n in (("latitude", "lat"), ("longitude", "lon"), ("Latitude", "lat"),
                             ("Longitude", "lon"), ("TIME", "time"), ("Time", "time"))
           if c in ds.dims or c in ds.coords}
    ds = ds.rename(ren)
    cand = [v for v in ds.data_vars if v.lower() in ("olr", "olr_daily", "olr_mean")]
    if not cand:
        cand = [v for v in ds.data_vars if set(ds[v].dims) >= {"time", "lat", "lon"}]
    if not cand:
        raise DataError(f"文件里找不到 OLR 变量，现有变量: {list(ds.data_vars)}")
    da = ds[cand[0]].transpose("time", "lat", "lon")
    da = da.assign_coords(lon=(da.lon % 360)).sortby("lon").sortby("lat")
    da = da.sel(time=~da.get_index("time").duplicated())
    return da.rename("olr")


def http_get(url, dest=None, timeout=60, progress=None):
    """带浏览器 UA 的下载；dest=None 时返回文本"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (tropical-waves-viz)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if dest is None:
            return r.read().decode("utf-8", "ignore")
        tot = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress and tot:
                    progress(done / tot)


def cdr_candidates(year):
    """某一年 OLR CDR 文件的候选 URL：先读目录列表拿真实文件名，再按命名规则猜。Final 优先。"""
    found = []
    for d in CDR_DIRS:
        try:
            html = http_get(d, timeout=20)
            pat = r'href="([^"?]*OLR-Daily_v02r00[^"]*_s%d0101[^"]*\.nc)"' % year
            found += [urljoin(d, n) for n in re.findall(pat, html)]
        except Exception:
            pass
    guesses = [f"OLR-Daily_v02r00_s{year}0101_e{year}1231.nc",
               f"OLR-Daily_v02r00-preliminary_s{year}0101_e{year}1231.nc",
               f"OLR-Daily_v02r00-preliminary_s{year}0101_latest.nc"]
    found += [d + g for d in CDR_DIRS for g in guesses]
    return sorted(dict.fromkeys(found), key=lambda u: ("preliminary" in u, "latest" in u))


def get_cdr_year(year, cache_dir, log=print):
    """返回某年 OLR CDR 的本地文件：先查缓存目录（可手动放入），再在线下载"""
    os.makedirs(cache_dir, exist_ok=True)
    local = sorted(glob.glob(os.path.join(cache_dir, f"*_s{year}0101*.nc")),
                   key=lambda f: ("preliminary" in f, "latest" in f))
    for f in local:
        age = (pd.Timestamp.now() - pd.Timestamp(os.path.getmtime(f), unit="s")).days
        if "latest" not in f or age < 1:
            log(f"{year}: 使用本地文件 {f}")
            return f
    errors = []
    for url in cdr_candidates(year):
        fn = os.path.join(cache_dir, url.rsplit("/", 1)[-1])
        try:
            log(f"{year}: 下载 {url}")
            http_get(url, fn + ".part", timeout=120)
            os.replace(fn + ".part", fn)
            return fn
        except Exception as e:
            errors.append(f"{url} -> {e}")
            if os.path.exists(fn + ".part"):
                os.remove(fn + ".part")
    if local:
        warnings.warn(f"{year} 年在线更新失败，使用缓存中的旧文件 {local[0]}")
        return local[0]
    raise DataError(
        f"获取 {year} 年 OLR 文件失败（尝试了 {len(errors)} 个地址，例如：{errors[0] if errors else ''}）。\n"
        f"解决办法：浏览器打开 {CDR_PORTAL} ，在 Daily Mean OLR 的 Data access 下载文件名含 "
        f"_s{year}0101 的 .nc 文件，保持文件名不变放进 {cache_dir}/ 目录，然后重新加载。")


def load_olr(cfg, log=print):
    """按配置读取 OLR，返回 (time, lat, lon) 的逐日 DataArray（含前后缓冲期）"""
    t0 = pd.Timestamp(cfg.start) - pd.Timedelta(days=cfg.pad)
    t1 = pd.Timestamp(cfg.end) + pd.Timedelta(days=cfg.pad)
    if pd.Timestamp(cfg.end) < pd.Timestamp(cfg.start):
        raise DataError("结束日期早于开始日期。")

    if cfg.source == "demo":
        return make_demo_olr(cfg.start, cfg.end, cfg.pad)
    if cfg.source == "local":
        paths = sorted(p for f in cfg.olr_files for p in (glob.glob(f) or [f]))
        if not paths:
            raise DataError("本地文件模式下没有找到任何文件，请检查路径或通配符。")
        log(f"读取本地文件: {paths}")
        das = [normalize(xr.open_dataset(p)) for p in paths]
    elif cfg.source == "psl":
        if pd.Timestamp(cfg.end) > PSL_LAST:
            raise DataError("PSL 插值 OLR 只到 2022-12-31，近期日期请选择 OLR CDR 数据源。")
        log(f"读取 PSL 插值 OLR: {PSL_URL}")
        das = [normalize(xr.open_dataset(PSL_URL))]
    else:
        years = list(range(t0.year, t1.year + 1))
        if min(years) < 2024:
            raise DataError("2023 年及以前的 CDR 是一个 1979–2023 合并大文件，请从 "
                            f"{CDR_PORTAL} 手动下载后用“本地文件”模式；2022 年以前也可选 PSL 数据源。")
        das = [normalize(xr.open_dataset(get_cdr_year(y, cfg.olr_cache, log))) for y in years]

    olr = xr.concat(das, "time") if len(das) > 1 else das[0]
    olr = olr.sortby("time")
    olr = olr.sel(time=~olr.get_index("time").duplicated(keep="last"))
    olr = olr.sel(time=slice(t0, t1)).load()
    if olr.sizes["time"] == 0:
        raise DataError(f"所选时间段 {t0.date()}~{t1.date()} 在数据里是空的。")
    return clean_olr(olr, cfg, log)


def clean_olr(olr, cfg, log=print):
    """统一成逐日、补缺测、1° 粗化到 2°，并检查首尾缓冲是否足够"""
    olr["time"] = olr.indexes["time"].normalize()
    olr = olr.resample(time="1D").mean()
    if olr.isnull().any():
        log(f"补全 {int(olr.isnull().sum())} 个缺测格点")
        olr = olr.interpolate_na("time").fillna(olr.mean("time"))
        if olr.isnull().any():
            olr = olr.interpolate_na("lon").fillna(float(olr.mean()))
    res = float(abs(olr.lon[1] - olr.lon[0]))
    if res < 1.5:
        f = int(round(2.0 / res))
        olr = olr.coarsen(lat=f, lon=f, boundary="trim").mean()
        log(f"{res:g}° 网格粗化为 {res * f:g}°")
    first, last = pd.Timestamp(olr.time.values[0]), pd.Timestamp(olr.time.values[-1])
    if last < pd.Timestamp(cfg.end):
        raise DataError(f"数据只到 {last.date()}，早于结束日期 {cfg.end}，请把结束日期提前。")
    if (last - pd.Timestamp(cfg.end)).days < 90:
        warnings.warn(f"结束日期之后只有 {(last - pd.Timestamp(cfg.end)).days} 天数据，"
                      "末端几帧的滤波（尤其 MJO）可能失真；建议结束日期比数据末端早约 90 天。")
    if (pd.Timestamp(cfg.start) - first).days < 90:
        warnings.warn("开始日期之前的缓冲不足 90 天，开头几帧的滤波结果可能不可靠。")
    return olr


def make_demo_olr(start, end, pad_days, seed=0):
    """合成数据：气候态 + Kelvin 波包 + MJO + ER 波 + 红噪声。仅用于测试和演示界面！"""
    rng = np.random.default_rng(seed)
    time = pd.date_range(pd.Timestamp(start) - pd.Timedelta(days=pad_days),
                         pd.Timestamp(end) + pd.Timedelta(days=pad_days), freq="D")
    lat = np.arange(-60, 60.1, 2.5)
    lon = np.arange(0, 360, 2.5)
    td = np.arange(len(time))[:, None, None].astype(float)
    LA, LO = lat[None, :, None], np.deg2rad(lon)[None, None, :]
    clim = 255 - 55 * np.exp(-((LA - 5) / 12) ** 2) * (1 + 0.35 * np.cos(LO - np.deg2rad(130))) + 0 * td
    mjo_env = 0.55 + 0.45 * np.cos(LO - np.deg2rad(110))
    mjo = -22 * (0.6 + 0.4 * np.sin(2 * np.pi * td / 400)) * mjo_env * np.exp(-(LA / 12) ** 2) \
        * np.cos(2 * LO - 2 * np.pi * td / 48)
    kel = np.zeros_like(clim)
    c_deg = 15 * 86400 / (2 * np.pi * A) * 360
    for _ in range(int(len(time) / 9)):
        t_c, lon_c = rng.uniform(0, len(time)), rng.uniform(0, 360)
        k = rng.integers(4, 9)
        per = 360 / k / c_deg
        dl = (np.rad2deg(LO) - lon_c - c_deg * (td - t_c) + 180) % 360 - 180
        env = np.exp(-((td - t_c) / 6) ** 2) * np.exp(-(dl / 50) ** 2)
        kel += -28 * env * np.exp(-(LA / 8) ** 2) * np.cos(k * LO - 2 * np.pi * td / per + rng.uniform(0, 6.28))
    er = -14 * (np.exp(-((LA - 10) / 7) ** 2) + np.exp(-((LA + 10) / 7) ** 2)) \
        * np.cos(-4 * LO - 2 * np.pi * td / 25) * (0.5 + 0.5 * np.cos(LO - np.deg2rad(160)))
    noise = rng.normal(0, 14, clim.shape)
    for ax_ in (1, 2):
        noise = (np.roll(noise, 1, ax_) + noise + np.roll(noise, -1, ax_)) / 3
    for i in range(1, len(time)):
        noise[i] = 0.6 * noise[i - 1] + 0.8 * noise[i]
    return xr.DataArray((clim + mjo + kel + er + noise).astype("f4"),
                        coords=dict(time=time, lat=lat, lon=lon), dims=("time", "lat", "lon"), name="olr")


# ----------------------------------------------------------------------------
# 派生量
# ----------------------------------------------------------------------------
def compute_filtered(olr, cfg, lat_band=30.0):
    """对启用的波动做滤波，返回 {波名: DataArray(time, lat, lon)}，只含 ±lat_band"""
    trop = olr.sel(lat=slice(-lat_band, lat_band))
    out = {}
    for w in cfg.enabled_waves():
        fw = kf_filter(trop.values, cfg.waves[w], w)
        out[w] = xr.DataArray(fw.astype("f4"), coords=trop.coords, dims=trop.dims, name=w)
    return out


def olr_anomaly(olr):
    """去掉 91 天滑动平均（季节变化）后的 OLR 异常"""
    return olr - olr.rolling(time=91, center=True, min_periods=30).mean()


def frame_times(cfg):
    """动画每一帧的时刻（MERGIR 背景时加上 bg_hour）"""
    hour = cfg.bg_hour if cfg.bg == "mergir" else 0
    step = pd.Timedelta(hours=24 / max(1, cfg.frames_per_day))
    return pd.date_range(pd.Timestamp(cfg.start) + pd.Timedelta(hours=hour),
                         pd.Timestamp(cfg.end) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1), freq=step)


def at_time(da, t):
    """取某时刻的场：整天直接取，日内时刻做线性插值"""
    t = pd.Timestamp(t)
    if t == t.normalize():
        return da.sel(time=t, method="nearest")
    return da.interp(time=t)
