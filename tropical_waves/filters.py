"""赤道波动的波数-频率滤波（Wheeler & Kiladis 1999）与波数-频率谱。"""
from __future__ import annotations

import numpy as np
from scipy.signal import detrend
from scipy.signal.windows import tukey

G, A, OMEGA = 9.81, 6.371e6, 7.292e-5
BETA = 2 * OMEGA / A


def dispersion_freq(k, h, wave):
    """浅水方程赤道波频散关系。k: 纬向波数(正=东传)，h: 等效深度(m) -> 频率(cycles/day)"""
    c = np.sqrt(G * h)
    kk = np.asarray(k, dtype=float) / A                 # rad/m
    if wave == "kelvin":
        w = c * kk
    elif wave == "er":                                  # n = 1 赤道 Rossby 波
        w = -BETA * kk / (kk ** 2 + 3 * BETA / c)
    else:
        raise ValueError(wave)
    return w * 86400 / (2 * np.pi)


def wave_mask(f, s, p, wave):
    """频率-波数平面上的保留区域。f: 频率(cpd), s: numpy FFT 的整数波数"""
    FF, SS = np.meshgrid(f, s, indexing="ij")
    nu = np.abs(FF)
    k = -SS * np.sign(FF)          # numpy FFT 约定下换算成“正=东传”的物理波数
    mask = (nu >= 1.0 / p["tmax"]) & (nu <= 1.0 / p["tmin"]) & (k >= p["kmin"]) & (k <= p["kmax"])
    if p.get("hmin") is not None and wave in ("kelvin", "er"):
        with np.errstate(divide="ignore", invalid="ignore"):
            f1 = dispersion_freq(k, p["hmin"], wave)
            f2 = dispersion_freq(k, p["hmax"], wave)
        mask &= (nu >= np.minimum(f1, f2)) & (nu <= np.maximum(f1, f2))
    return mask


def kf_filter(x, p, wave, obs_per_day=1):
    """
    对 (time, lat, lon) 数组做时空二维 FFT 滤波（Python 版 NCL kf_filter）。
    p: 含 tmin/tmax/kmin/kmax/hmin/hmax 的字典。
    """
    nt, nx = x.shape[0], x.shape[-1]
    t = np.arange(nt)
    flat = x.reshape(nt, -1).astype("f8")
    coef = np.polyfit(t, flat, 1)
    flat = (flat - (np.outer(t, coef[0]) + coef[1])) * tukey(nt, 0.1)[:, None]
    F = np.fft.fft2(flat.reshape(x.shape), axes=(0, -1))
    f = np.fft.fftfreq(nt, d=1.0 / obs_per_day)
    s = np.fft.fftfreq(nx, d=1.0 / nx)
    F *= wave_mask(f, s, p, wave)[:, None, :]
    return np.real(np.fft.ifft2(F, axes=(0, -1)))


def filter_region(p, wave, kres=0.05):
    """返回滤波区域在 (k, 频率) 平面上的边界，用于在谱图上画框。"""
    k = np.arange(p["kmin"], p["kmax"] + kres / 2, kres)
    lo = np.full_like(k, 1.0 / p["tmax"])
    hi = np.full_like(k, 1.0 / p["tmin"])
    if p.get("hmin") is not None and wave in ("kelvin", "er"):
        f1, f2 = dispersion_freq(k, p["hmin"], wave), dispersion_freq(k, p["hmax"], wave)
        lo = np.maximum(lo, np.minimum(f1, f2))
        hi = np.minimum(hi, np.maximum(f1, f2))
    return k, lo, hi


def _smooth121(a, axis, passes):
    for _ in range(passes):
        a = 0.25 * np.roll(a, 1, axis) + 0.5 * a + 0.25 * np.roll(a, -1, axis)
    return a


def wk_spectrum(x, lat, seg_len=96, overlap=60, lat_max=15.0, obs_per_day=1):
    """
    Wheeler–Kiladis 波数-频率谱（简化版）。
    x: (time, lat, lon) 的 OLR；返回 k(正=东传)、频率(cpd)、对称分量功率、背景谱。
    """
    sel = np.abs(lat) <= lat_max
    x, la = x[:, sel, :], lat[sel]
    symmetric_grid = np.allclose(la, -la[::-1])
    xs = 0.5 * (x + x[:, ::-1, :]) if symmetric_grid else x
    xa = 0.5 * (x - x[:, ::-1, :]) if symmetric_grid else x
    nt, _, nx = x.shape
    seg_len = min(seg_len, nt)
    step = max(1, seg_len - overlap)
    starts = list(range(0, nt - seg_len + 1, step))
    win = tukey(seg_len, 0.2)[:, None, None]

    def power(arr):
        acc = 0.0
        for s0 in starts:
            seg = detrend(arr[s0:s0 + seg_len], axis=0) * win
            F = np.fft.fft2(seg, axes=(0, 2)) / (seg_len * nx)
            acc = acc + (np.abs(F) ** 2).mean(axis=1)
        return acc / len(starts)

    Ps, Pa = power(xs), power(xa)
    f = np.fft.fftfreq(seg_len, d=1.0 / obs_per_day)
    s = np.fft.fftfreq(nx, d=1.0 / nx)
    pos = f > 0
    order = np.argsort(-s)
    k = (-s)[order]
    freq = f[pos]
    Ps, Pa = Ps[pos][:, order], Pa[pos][:, order]
    bg = _smooth121(_smooth121(0.5 * (Ps + Pa), 0, 15), 1, 10)
    return dict(k=k, freq=freq, sym=Ps, bg=bg, n_segments=len(starts), symmetric=symmetric_grid)
