"""高分辨率背景：NASA Earthdata 的 GPM_MERGIR 红外亮温 / IMERG 降水（需免费账号）。"""
from __future__ import annotations

import glob
import os
import warnings

import numpy as np
import pandas as pd
import xarray as xr

from .data import DataError

PRODUCTS = {
    "mergir": dict(short_name="GPM_MERGIR", version="1", var="Tb", units="K",
                   label="IR brightness temperature (NASA GPM_MERGIR, 4 km)"),
    "imerg": dict(short_name="GPM_3IMERGDF", version="07", var="precipitation", units="mm/day",
                  label="Daily precipitation (NASA IMERG Final V07)"),
    "imerg-late": dict(short_name="GPM_3IMERGDL", version="07", var="precipitation", units="mm/day",
                       label="Daily precipitation (NASA IMERG Late V07)"),
}

EULA_HELP = (
    "Earthdata 账号还没有接受 GES DISC 的使用协议（EULA）。请打开 "
    "https://urs.earthdata.nasa.gov/profile ：EULAs → Accept New EULAs，接受 "
    "“NASA GESDISC DATA ARCHIVE”；再到 Applications → Authorized Apps → Approve More "
    "Applications 授权同名应用（操作时关闭浏览器网页翻译）。")


def bg_key(kind, t):
    return f"{t:%Y%m%d%H}" if kind == "mergir" else f"{t:%Y%m%d}"


def bg_tag(kind, key):
    # MERGIR: merg_2025120100_4km-pixel.nc4 ; IMERG: 3B-DAY(-L).MS.MRG.3IMERG.20251201-S000000-E235959.V07B.nc4
    return f"merg_{key}_" if kind == "mergir" else f".{key}-S000000"


def process_file(kind, raw_file, out_file, res, lat_max):
    """原始文件 -> 第一个时次 -> 裁纬度 -> 粗化 -> 规则网格 -> 小 npz"""
    with xr.open_dataset(raw_file, decode_times=False) as ds:
        ds = ds.rename({c: n for c, n in (("latitude", "lat"), ("longitude", "lon")) if c in ds.dims})
        da = ds[PRODUCTS[kind]["var"]]
        if "time" in da.dims:
            da = da.isel(time=0)
        da = da.transpose("lat", "lon").sortby("lat").sel(lat=slice(-lat_max - 1, lat_max + 1)).load()
    da = da.where(da > -9000)
    f = max(1, int(round(res / float(abs(da.lon[1] - da.lon[0])))))
    if f > 1:
        da = da.coarsen(lat=f, lon=f, boundary="trim").mean()
    da = da.assign_coords(lon=da.lon % 360).sortby("lon")
    da = xr.concat([da.assign_coords(lon=da.lon - 360), da, da.assign_coords(lon=da.lon + 360)], "lon")
    da = da.interp(lon=np.arange(res / 2, 360, res), lat=np.arange(-lat_max + res / 2, lat_max, res))
    np.savez_compressed(out_file, lat=da.lat.values.astype("f4"), lon=da.lon.values.astype("f4"),
                        data=da.values.astype("f4"))


def earthdata_login():
    """优先用环境变量 EARTHDATA_USERNAME/PASSWORD，其次 ~/.netrc；网页里不做交互式输入。"""
    try:
        import earthaccess
    except ImportError as e:
        raise DataError("需要安装 earthaccess：pip install earthaccess") from e
    auth = None
    if os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD"):
        auth = earthaccess.login(strategy="environment", persist=True)
    else:
        try:
            auth = earthaccess.login(strategy="netrc")
        except Exception:
            auth = None
    if not auth or not getattr(auth, "authenticated", False):
        raise DataError("Earthdata 登录失败：请在侧栏填写 Earthdata 账号密码，"
                        "或在 ~/.netrc 中配置 machine urs.earthdata.nasa.gov。")
    return earthaccess


def prepare(kind, times, cache_dir, res=0.2, lat_max=60, keep_raw=False, progress=None, log=print):
    """为每个时刻准备背景 npz（已有缓存直接复用），返回与 times 等长的路径列表（缺失为 None）"""
    raw_dir, proc_dir = os.path.join(cache_dir, "raw"), os.path.join(cache_dir, "proc")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(proc_dir, exist_ok=True)
    keys = list(dict.fromkeys(bg_key(kind, pd.Timestamp(t)) for t in times))
    proc = {k: os.path.join(proc_dir, f"{kind}_{k}_{res:g}_{lat_max:g}.npz") for k in keys}
    todo = [k for k in keys if not os.path.exists(proc[k])]

    local = {os.path.basename(f): f for f in glob.glob(os.path.join(raw_dir, "*"))}
    for k in list(todo):                      # 手动下载放在 raw/ 里的文件
        hit = [f for n, f in local.items() if bg_tag(kind, k) in n]
        if hit:
            process_file(kind, hit[0], proc[k], res, lat_max)
            todo.remove(k)

    if todo:
        earthaccess = earthdata_login()
        prod = PRODUCTS[kind]
        t0 = pd.Timestamp(todo[0][:8])
        t1 = pd.Timestamp(todo[-1][:8]) + pd.Timedelta(hours=23, minutes=59)
        log(f"Earthdata 搜索 {prod['short_name']} {t0.date()} ~ {t1.date()}")
        grans = earthaccess.search_data(short_name=prod["short_name"], version=prod["version"],
                                        temporal=(t0.strftime("%Y-%m-%dT%H:%M:%S"),
                                                  t1.strftime("%Y-%m-%dT%H:%M:%S")))
        want = {}
        for g in grans:
            name = g.data_links()[0].rsplit("/", 1)[-1]
            for k in todo:
                if bg_tag(kind, k) in name:
                    want[k] = (g, name)
        missing = [k for k in todo if k not in want]
        if missing:
            warnings.warn(f"Earthdata 上没找到 {len(missing)} 个时刻的文件（如 {missing[:3]}），这些帧背景留空。"
                          "IMERG Final 约有 3.5 个月延迟，近期日期请选 IMERG Late。")
        ks = [k for k in todo if k in want]
        for i in range(0, len(ks), 12):
            batch = ks[i:i + 12]
            try:
                earthaccess.download([want[k][0] for k in batch], local_path=raw_dir, show_progress=False)
            except Exception as e:
                if "Eula" in type(e).__name__ or "EULA" in str(e).upper():
                    raise DataError(EULA_HELP) from e
                raise
            for k in batch:
                fn = os.path.join(raw_dir, want[k][1])
                process_file(kind, fn, proc[k], res, lat_max)
                if not keep_raw:
                    os.remove(fn)
            if progress:
                progress(min(1.0, (i + len(batch)) / len(ks)))
    return [proc[bg_key(kind, pd.Timestamp(t))] if os.path.exists(proc[bg_key(kind, pd.Timestamp(t))])
            else None for t in times]


def style(kind, cmap="auto", vmin=None, vmax=None):
    """背景配色：亮温越冷越白；降水越大越白"""
    from matplotlib.colors import LinearSegmentedColormap, LogNorm, Normalize
    if kind == "mergir":
        cm = LinearSegmentedColormap.from_list(
            "ir", [(0, "#ffffff"), (0.18, "#e6e6e6"), (0.45, "#8c8c8c"), (0.75, "#2a2a2a"), (1, "#050505")])
        norm = Normalize(vmin or 195, vmax or 300)
    else:
        cm = LinearSegmentedColormap.from_list("pr", ["#101318", "#3b4350", "#8a95a5", "#d9dee6", "#ffffff"])
        norm = LogNorm(max(vmin or 1, 0.1), vmax or 100)
    if cmap and cmap != "auto":
        import matplotlib
        cm = matplotlib.colormaps[cmap]
    return cm, norm
