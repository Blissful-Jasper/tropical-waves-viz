"""滤波器方向与选择性：纯正弦波只应通过对应的波动滤波器。"""
import numpy as np
import pytest

from tropical_waves.config import WAVE_DEFAULTS
from tropical_waves.filters import kf_filter, wk_spectrum

T = np.arange(730)[:, None, None]
LON = np.deg2rad(np.arange(0, 360, 2.5))[None, None, :]
LAT3 = np.zeros((1, 3, 1))


def wave(k, period):
    """k>0 东传，k<0 西传"""
    return np.cos(k * LON - 2 * np.pi * T / period) + LAT3


@pytest.mark.parametrize("signal, expect", [
    (wave(6, 5), "kelvin"),      # 东传、快
    (wave(2, 48), "mjo"),        # 东传、慢
    (wave(-4, 25), "er"),        # 西传
])
def test_each_wave_passes_only_its_filter(signal, expect):
    for name, p in WAVE_DEFAULTS.items():
        out = kf_filter(signal, p, name)
        ratio = out[200:-200].std() / signal.std()
        if name == expect:
            assert ratio > 0.95, (name, ratio)
        else:
            assert ratio < 0.05, (name, ratio)


def test_spectrum_peak_in_right_place():
    lat = np.arange(-15, 15.1, 2.5)
    x = np.cos(6 * LON - 2 * np.pi * T / 5) * np.ones((1, lat.size, 1))
    x = x + 0.1 * np.random.default_rng(0).normal(size=x.shape)
    spec = wk_spectrum(x, lat, seg_len=96, overlap=60, lat_max=15)
    i, j = np.unravel_index(np.argmax(spec["sym"]), spec["sym"].shape)
    assert spec["k"][j] == 6
    assert abs(spec["freq"][i] - 0.2) < 0.02
