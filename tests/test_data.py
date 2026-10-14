"""数据层：本地文件自动识别、跨数据源拼接、2024 年以前的数据、阈值与纬度设置。"""
import numpy as np
import pandas as pd
import pytest
import xarray as xr

from tropical_waves import Config, data


def fake_olr(path, start, end, res, name="olr", lat_desc=False):
    t = pd.date_range(start, end, freq="D")
    lat = np.arange(-90 + res / 2, 90, res) if res < 2 else np.arange(-90, 90.01, res)   # CDR 格心 / PSL 格点
    if lat_desc:
        lat = lat[::-1]
    lon = np.arange(res / 2 if res < 2 else 0, 360, res)
    rng = np.random.default_rng(0)
    v = 240 + rng.normal(0, 10, (t.size, lat.size, lon.size)).astype("f4")
    xr.Dataset({name: (("time", "lat", "lon"), v)}, coords=dict(time=t, lat=lat, lon=lon)).to_netcdf(path)
    return path


@pytest.fixture
def cache(tmp_path):
    fake_olr(tmp_path / "olr.day.mean.nc", "2009-01-01", "2022-12-31", 2.5, lat_desc=True)   # 类似 PSL
    fake_olr(tmp_path / "OLR-Daily_v02r00_s20240101_e20241231.nc", "2024-01-01", "2024-12-31", 1.0)
    fake_olr(tmp_path / "OLR-Daily_v02r00_s20250101_e20251231.nc", "2025-01-01", "2025-12-31", 1.0)
    return tmp_path


def test_pre2024_uses_any_local_file(cache):
    cfg = Config(start="2010-01-01", end="2010-03-31", olr_cache=str(cache), data_release="")
    logs = []
    olr = data.load_olr(cfg, log=logs.append)
    assert any("olr.day.mean.nc" in m for m in logs)
    assert olr.lat.values[0] < olr.lat.values[-1] and abs(float(olr.lon[1] - olr.lon[0]) - 2.5) < 1e-6
    assert pd.Timestamp(olr.time.values[0]) == pd.Timestamp("2009-07-05")


def test_cdr_years_are_joined_and_coarsened(cache):
    cfg = Config(start="2024-07-01", end="2025-03-31", olr_cache=str(cache), data_release="")
    olr = data.load_olr(cfg, log=lambda m: None)
    assert abs(float(olr.lon[1] - olr.lon[0]) - 2.0) < 1e-6
    assert not np.isnan(olr.values).any()


def test_gap_year_2023_gives_clear_error(cache, monkeypatch):
    cfg = Config(start="2023-03-01", end="2023-04-01", olr_cache=str(cache), data_release="")
    monkeypatch.setattr(data, "NCEI_DIR", "http://127.0.0.1:9/")      # 离线：在线获取必然失败
    monkeypatch.setattr(data, "PSL_URL", "http://127.0.0.1:9/x.nc")
    with pytest.raises(data.DataError, match="2023"):
        data.load_olr(cfg, log=lambda m: None)


def test_harmonize_mixed_grids():
    t = pd.date_range("2020-01-01", periods=3)
    a = xr.DataArray(np.ones((3, 73, 144)), dims=("time", "lat", "lon"),
                     coords=dict(time=t, lat=np.arange(-90, 90.1, 2.5), lon=np.arange(0, 360, 2.5)))
    b = xr.DataArray(np.ones((3, 180, 360)), dims=("time", "lat", "lon"),
                     coords=dict(time=t + pd.Timedelta(days=3), lat=np.arange(-89.5, 90, 1.0),
                                 lon=np.arange(0.5, 360, 1.0)))
    out = data.harmonize([a, b], log=lambda m: None)
    assert out[0].shape[1:] == out[1].shape[1:]
    assert np.allclose(out[1].values, 1.0)


def test_wave_lat_limit_and_std_threshold():
    cfg = Config()
    assert cfg.lat_limit("kelvin") == 15.0 and cfg.lat_limit("er") == 25.0
    da = xr.DataArray(np.zeros(2), attrs={"std_eq": 9.0})
    assert cfg.thresh("kelvin", da) == cfg.waves["kelvin"]["thresh"]
    cfg.thresh_mode = "std"
    cfg.waves["kelvin"]["thresh_std"] = 1.5
    assert cfg.thresh("kelvin", da) == pytest.approx(13.5)


def test_old_cdr_config_maps_to_auto():
    assert Config.from_dict({"source": "cdr"}).source == "auto"
