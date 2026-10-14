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
        try:
            return _download(url, cache_dir, log)
        except Exception as e:
            errors.append(f"{url} -> {e}")
    if local:
        warnings.warn(f"{year} 年在线更新失败，使用缓存中的旧文件 {local[0]}")
        return local[0]
    raise DataError(
        f"获取 {year} 年 OLR 文件失败（尝试了 {len(errors)} 个地址，例如：{errors[0] if errors else ''}）。\n"
        f"解决办法：浏览器打开 {CDR_PORTAL} ，在 Daily Mean OLR 的 Data access 下载文件名含 "
        f"_s{year}0101 的 .nc 文件，保持文件名不变放进 {cache_dir}/ 目录，然后重新加载。")


def _download(url, cache_dir, log=print, name=None):
    fn = os.path.join(cache_dir, name or url.rsplit("/", 1)[-1])
    log(f"下载 {url}")
    try:
        http_get(url, fn + ".part", timeout=180)
        with xr.open_dataset(fn + ".part"):          # 确认是可读的 netCDF，而不是错误网页
            pass
        os.replace(fn + ".part", fn)
    finally:
        if os.path.exists(fn + ".part"):
            os.remove(fn + ".part")
    _SCAN.pop(fn, None)
    return fn


# ----------------------------------------------------------------------------
# 2024 年以前：GitHub Release 年度小文件 → NCEI 年度文件 → PSL OPeNDAP 子集
# ----------------------------------------------------------------------------
NCEI_DIR = "https://www.ncei.noaa.gov/data/outgoing-longwave-radiation-daily/access/"
PSL_FILE_URL = "https://downloads.psl.noaa.gov/Datasets/interp_OLR/olr.day.mean.nc"
MAP_LAT = 62.0                                    # 只保留 ±62°，地图最多到 ±60°


def subset_name(year):
    return f"olr_psl_{year}.nc"


def write_subset(da, path):
    """把一年的 OLR 存成紧凑的 int16 netCDF（约 3 MB/年，可放 GitHub Release）"""
    da = da.sel(lat=slice(-MAP_LAT, MAP_LAT)).astype("f4").rename("olr")
    da.attrs.update(units="W m-2", long_name="Daily mean OLR")
    da.to_dataset().to_netcdf(path + ".part", encoding={"olr": dict(
        dtype="int16", scale_factor=0.1, add_offset=0.0, _FillValue=-32768, zlib=True, complevel=5)})
    os.replace(path + ".part", path)
    _SCAN.pop(path, None)
    return path


def get_old_year(year, cfg, log=print):
    """下载 2024 年以前的一年 OLR，成功返回文件路径，失败返回 None（错误写进日志）"""
    cache = cfg.olr_cache
    os.makedirs(cache, exist_ok=True)
    errors = []
    if cfg.data_release and year <= PSL_LAST.year:          # (1) 本仓库 Release 里的年度小文件
        try:
            return _download(cfg.data_release.rstrip("/") + "/" + subset_name(year), cache, log)
        except Exception as e:
            errors.append(f"GitHub Release: {e}")
    try:                                                     # (2) NCEI CDR v01r02 年度文件
        names = []
        try:
            names = re.findall(r'href="(olr-daily_v01r02[^"]*_%d0101_%d1231\.nc)"' % (year, year),
                               http_get(NCEI_DIR, timeout=30))
        except Exception:
            pass
        names += [f"olr-daily_v01r02_{year}0101_{year}1231.nc",
                  f"olr-daily_v01r02-preliminary_{year}0101_{year}1231.nc"]
        for n in dict.fromkeys(names):
            try:
                return _download(NCEI_DIR + n, cache, log)
            except Exception as e:
                errors.append(f"NCEI {n}: {e}")
    except Exception as e:
        errors.append(f"NCEI: {e}")
    if year <= PSL_LAST.year:                                # (3) PSL OPeNDAP 只取这一年
        try:
            log(f"{year}: 通过 OPeNDAP 读取 PSL 插值 OLR 子集")
            with xr.open_dataset(PSL_URL) as ds:
                da = normalize(ds).sel(time=str(year)).load()
            return write_subset(da, os.path.join(cache, subset_name(year)))
        except Exception as e:
            errors.append(f"PSL OPeNDAP: {e}")
    for e in errors:
        log(f"  {year} 失败: {e}")
    return None


# ----------------------------------------------------------------------------
# 本地文件扫描：缓存目录里所有 .nc（含 olr.day.mean.nc）按时间覆盖自动选用
# ----------------------------------------------------------------------------
_SCAN = {}


def _priority(path):
    n = os.path.basename(path)
    if n.startswith("OLR-Daily_v02r00"):
        return 1 if "preliminary" in n else 0         # CDR v02r00：Final 优先
    if n.startswith("olr-daily_v01r02"):
        return 2
    if n.startswith("olr_psl_"):
        return 3
    return 4                                          # 其它（如 PSL olr.day.mean.nc 大文件）


def scan_file(path):
    """返回 (首日, 末日)；不是 OLR 文件返回 None。结果按修改时间缓存"""
    key = (os.path.getmtime(path), os.path.getsize(path))
    hit = _SCAN.get(path)
    if hit and hit[0] == key:
        return hit[1]
    rng = None
    try:
        with xr.open_dataset(path) as ds:
            da = normalize(ds)
            tt = da.get_index("time").normalize()
            rng = (tt.min(), tt.max(), grid_res(da))
    except Exception:
        rng = None
    _SCAN[path] = (key, rng)
    return rng


def search_dirs(cfg):
    dirs = [cfg.olr_cache] + [d for d in os.environ.get("TWV_OLR_DIRS", "").split(os.pathsep) if d]
    return [d for d in dict.fromkeys(os.path.expanduser(d) for d in dirs) if os.path.isdir(d)]


def local_inventory(cfg, extra=()):
    files = sorted({f for d in search_dirs(cfg) for f in glob.glob(os.path.join(d, "*.nc"))} | set(extra))
    inv = [(f, scan_file(f)) for f in files]
    inv = [(f, r) for f, r in inv if r is not None]
    return sorted(inv, key=lambda x: (_priority(x[0]), x[0]))


def plan_pieces(inv, t0, t1):
    """为 [t0, t1] 选文件：能被单个文件完整覆盖就只用它（避免拼接不同数据源），否则按优先级逐段补齐"""
    for f, (a, b, _) in inv:
        if a <= t0 and b >= t1:
            return [(f, t0, t1)], pd.DatetimeIndex([])
    need = pd.date_range(t0, t1, freq="D")
    pieces = []
    for f, (a, b, _) in inv:
        sub = need[(need >= a) & (need <= b)]
        if len(sub):
            pieces.append((f, sub[0], sub[-1]))
            need = need.difference(sub)
    return pieces, need


def open_piece(path, a, b):
    with xr.open_dataset(path) as ds:
        da = normalize(ds)
        da = da.sel(time=slice(a, b + pd.Timedelta(hours=23)), lat=slice(-MAP_LAT - 2, MAP_LAT + 2)).load()
    return da


def grid_res(da):
    return float(np.median(np.abs(np.diff(da.lon.values))))


def harmonize(das, log=print):
    """不同数据源统一到同一网格：全是 1° 就粗化到 2°；混有 2.5° 时全部插值到 2.5°"""
    res = [grid_res(d) for d in das]
    if max(res) >= 2.4:
        tlat = np.arange(-60.0, 60.01, 2.5)
        tlon = np.arange(0.0, 360.0, 2.5)
        out = []
        for d, r in zip(das, res):
            native = (abs(r - 2.5) < 0.05 and all(np.isclose(d.lat.values, v).any() for v in tlat)
                      and all(np.isclose(d.lon.values, v).any() for v in tlon))
            if native:
                d = d.sel(lat=tlat, lon=tlon, method="nearest").assign_coords(lat=tlat, lon=tlon)
            else:
                if r < 1.5:                                   # 先 3x3 平滑再插值，近似面积平均
                    d = d.rolling(lat=3, lon=3, center=True, min_periods=1).mean()
                d = xr.concat([d.assign_coords(lon=d.lon - 360), d, d.assign_coords(lon=d.lon + 360)], "lon")
                d = d.interp(lat=tlat, lon=tlon)
                log(f"{r:g}° 网格插值到 2.5°")
            out.append(d)
        return out
    out = []
    for d, r in zip(das, res):
        if r < 1.5:
            f = int(round(2.0 / r))
            d = d.coarsen(lat=f, lon=f, boundary="trim").mean()
            log(f"{r:g}° 网格粗化为 {r * f:g}°")
        out.append(d)
    return out


def load_olr(cfg, log=print):
    """按配置读取 OLR，返回 (time, lat, lon) 的逐日 DataArray（含前后缓冲期）"""
    t0 = pd.Timestamp(cfg.start) - pd.Timedelta(days=cfg.pad)
    t1 = pd.Timestamp(cfg.end) + pd.Timedelta(days=cfg.pad)
    if pd.Timestamp(cfg.end) < pd.Timestamp(cfg.start):
        raise DataError("结束日期早于开始日期。")
    if cfg.source == "demo":
        return make_demo_olr(cfg.start, cfg.end, cfg.pad)
    trail, _log = [], log

    def log(msg):                                     # noqa: F811  记下日志，出错时附在错误信息里
        trail.append(str(msg))
        _log(msg)

    today = pd.Timestamp.now().normalize()
    t1_avail = min(t1, today - pd.Timedelta(days=2))     # 未来日期不可能有数据
    if cfg.source == "local":
        paths = sorted(p for f in cfg.olr_files for p in (glob.glob(os.path.expanduser(f)) or [f]))
        missing = [p for p in paths if not os.path.exists(p)]
        if not paths or missing:
            raise DataError(f"本地文件模式下找不到文件：{missing or cfg.olr_files}，请检查路径或通配符。")
        inv = [(p, scan_file(p)) for p in paths]
        bad = [p for p, r in inv if r is None]
        if bad:
            raise DataError(f"这些文件里读不到 OLR(time, lat, lon)：{bad}")
        inv = sorted(inv, key=lambda x: (_priority(x[0]), x[0]))
        pieces, need = plan_pieces(inv, t0, t1_avail)
    elif cfg.source == "psl":
        if pd.Timestamp(cfg.end) > PSL_LAST:
            raise DataError("PSL 插值 OLR 只到 2022-12-31，近期日期请选择“自动”数据源。")
        inv = [x for x in local_inventory(cfg) if _priority(x[0]) >= 3]
        pieces, need = plan_pieces(inv, t0, min(t1_avail, PSL_LAST))
        for y in sorted(set(need.year)):
            get_old_year(y, cfg, log)
        if len(need):
            inv = [x for x in local_inventory(cfg) if _priority(x[0]) >= 3]
            pieces, need = plan_pieces(inv, t0, min(t1_avail, PSL_LAST))
    else:                                               # auto / cdr：本地优先，缺的年份再在线获取
        pieces, need = plan_pieces(local_inventory(cfg), t0, t1_avail)
        failed = []
        for y in sorted(set(need.year)):
            if y >= 2024:
                try:
                    get_cdr_year(y, cfg.olr_cache, log)
                except DataError as e:
                    log(str(e))
                    failed.append(y)
            elif get_old_year(y, cfg, log) is None:
                failed.append(y)
        if len(need):
            pieces, need = plan_pieces(local_inventory(cfg), t0, t1_avail)
        if failed and len(need):
            raise DataError(
                f"缺少 {', '.join(map(str, failed))} 年的 OLR，在线获取也失败了。\n"
                f"解决办法：把覆盖这些年份的 netCDF 放进 OLR 缓存目录 {cfg.olr_cache}/（或环境变量 "
                f"TWV_OLR_DIRS 指定的目录），文件名随意，程序会按时间范围自动识别。例如 PSL 的 "
                f"olr.day.mean.nc（1974-06~2022-12，{PSL_FILE_URL}），或 {CDR_PORTAL} 的 CDR 文件。"
                + ("\n注意：2023 年只有 OLR CDR 提供（PSL 已停更），需要 CDR 文件。" if 2023 in failed else "")
                + (f"\n2024 年以后：从 {CDR_PORTAL} 下载文件名含 _sYYYY0101 的 CDR 文件放进同一目录。"
                   if max(failed) >= 2024 else "")
                + "\n\n最近的日志：\n" + "\n".join(t[:300] for t in trail[-6:]))
    if not pieces:
        raise DataError(f"所选时间段 {t0.date()}~{t1.date()} 没有可用数据。")
    for f, a, b in pieces:
        log(f"{a.date()} ~ {b.date()}: {f}")
    das_raw = [open_piece(f, a, b) for f, a, b in pieces]
    das = harmonize(das_raw, log)
    if len({grid_res(d) >= 2.4 for d in das_raw}) > 1:
        warnings.warn("所选时段由不同 OLR 数据源拼接而成（如 PSL 插值 OLR + CDR），两者存在几 W m⁻² 的系统差，"
                      "拼接处附近的低频（MJO）滤波可能受影响。")
    olr = xr.concat(das, "time") if len(das) > 1 else das[0]
    olr = olr.sortby("time")
    olr = olr.sel(time=~olr.get_index("time").duplicated(keep="first"))
    if len(need):
        gap = need[(need > olr.time.values[0]) & (need < olr.time.values[-1])]
        if len(gap) > 5:
            raise DataError(f"数据中间缺 {len(gap)} 天（{gap[0].date()} ~ {gap[-1].date()}），无法做时空滤波。"
                            "请补齐这段数据（放入缓存目录），或调整日期。")
    return clean_olr(olr, cfg, log)


def clean_olr(olr, cfg, log=print):
    """统一成逐日、补缺测，并检查首尾缓冲是否足够"""
    olr["time"] = olr.indexes["time"].normalize()
    olr = olr.resample(time="1D").mean()
    if olr.isnull().any():
        log(f"补全 {int(olr.isnull().sum())} 个缺测格点")
        olr = olr.interpolate_na("time").fillna(olr.mean("time"))
        if olr.isnull().any():
            olr = olr.interpolate_na("lon").fillna(float(olr.mean()))
    olr = olr.astype("f4")
    first, last = pd.Timestamp(olr.time.values[0]), pd.Timestamp(olr.time.values[-1])
    log(f"实际数据范围 {first.date()} ~ {last.date()}，网格 {olr.sizes['lat']}×{olr.sizes['lon']}")
    if last < pd.Timestamp(cfg.end):
        raise DataError(f"数据只到 {last.date()}，早于结束日期 {cfg.end}，请把结束日期提前。")
    if first > pd.Timestamp(cfg.start):
        raise DataError(f"数据从 {first.date()} 开始，晚于开始日期 {cfg.start}，请把开始日期推后。")
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
    """
    对启用的波动做滤波，返回 {波名: DataArray(time, lat, lon)}，只含 ±lat_band。
    symmetric=True 的波动先取关于赤道的对称分量 (x(φ)+x(−φ))/2 再滤波（Wheeler & Kiladis 1999 的做法），
    可去掉中纬度天气尺度扰动在 Kelvin/ER 频段里的“漏入”信号。
    """
    trop = olr.sel(lat=slice(-lat_band, lat_band))
    lat = trop.lat.values
    sym_ok = np.allclose(lat, -lat[::-1])
    out = {}
    for w in cfg.enabled_waves():
        p = cfg.waves[w]
        x = trop.values
        if p.get("symmetric"):
            if not sym_ok:
                warnings.warn("纬度网格关于赤道不对称，无法取对称分量，已改用原始场滤波。")
            else:
                x = 0.5 * (x + x[:, ::-1, :])
        fw = kf_filter(x, p, w)
        da = xr.DataArray(fw.astype("f4"), coords=trop.coords, dims=trop.dims, name=w)
        core = da.sel(time=slice(cfg.start, cfg.end), lat=slice(-10, 10))
        da.attrs["std_eq"] = float(core.std()) if core.size else float("nan")
        out[w] = da
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
