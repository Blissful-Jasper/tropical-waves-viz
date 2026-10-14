"""用合成数据跑一遍：配置读写、各面板出图、导出 GIF、网页脚本无异常。"""
import os

import matplotlib.pyplot as plt
import pytest

from tropical_waves import Config, data, filters, plots

ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="module")
def demo():
    cfg = Config(source="demo", start="2025-12-01", end="2025-12-06")
    olr = data.load_olr(cfg)
    return cfg, olr, data.compute_filtered(olr, cfg)


def test_config_roundtrip():
    cfg = Config(start="2025-01-01")
    cfg.waves["mjo"]["color"] = "#123456"
    back = Config.from_json(cfg.to_json())
    assert back.start == "2025-01-01" and back.waves["mjo"]["color"] == "#123456"


@pytest.mark.parametrize("proj", ["strip", "globe"])
@pytest.mark.parametrize("hov_style", ["filled", "shaded", "lines"])
def test_panels(demo, proj, hov_style):
    cfg, olr, filt = demo
    cfg = Config.from_dict({**cfg.to_dict(), "proj": proj, "hov_style": hov_style})
    t = "2025-12-03"
    for fig in (plots.fig_map(cfg, olr, filt, t), plots.fig_hovmoller(cfg, olr, filt, t),
                plots.fig_timeseries(cfg, olr, filt, t),
                plots.fig_spectrum(cfg, filters.wk_spectrum(olr.values, olr.lat.values))):
        plt.close(fig)


def test_render_gif(demo, tmp_path):
    cfg, olr, filt = demo
    out = tmp_path / "x.gif"
    plots.render_animation(cfg, olr, filt, str(out))
    assert out.stat().st_size > 10_000


def test_cli_print_config(capsys):
    import sys
    sys.path.insert(0, ROOT)
    import cli
    assert cli.main(["--source", "demo", "--set", "waves.kelvin.enabled=false", "--print-config"]) == 0
    assert '"enabled": false' in capsys.readouterr().out


def test_streamlit_app_runs():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=180)
    at.session_state["base"] = Config(source="demo", start="2025-12-01", end="2025-12-05").to_dict()
    at.run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
