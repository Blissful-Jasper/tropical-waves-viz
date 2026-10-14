"""所有可调参数集中在 Config 里；网页、命令行、JSON 配置文件共用同一套参数。"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field, fields

# Wheeler & Kiladis (1999) / Kiladis et al. (2009) 常用滤波参数
# lat_max：只在 ±lat_max 内画该波动的等值线（Kelvin 波是赤道陷获波，e-折尺度约 10°）
# thresh / thresh_std：等值线阈值，分别对应 thresh_mode = "abs"（W m⁻²）和 "std"（该波动 ±10° 标准差的倍数）
# symmetric：先取关于赤道的对称分量再滤波（去掉中纬度天气扰动的漏入，见 README“方法说明”）
WAVE_DEFAULTS = {
    "kelvin": dict(enabled=True, color="#ff3b30", thresh=10.0, thresh_std=1.0, lat_max=15.0, symmetric=False,
                   tmin=2.5, tmax=20.0, kmin=1, kmax=14, hmin=8.0, hmax=90.0),
    "mjo":    dict(enabled=True, color="#22b8ff", thresh=8.0, thresh_std=1.0, lat_max=20.0, symmetric=False,
                   tmin=30.0, tmax=96.0, kmin=1, kmax=5, hmin=None, hmax=None),
    "er":     dict(enabled=True, color="#3ddc4a", thresh=8.0, thresh_std=1.0, lat_max=25.0, symmetric=False,
                   tmin=9.7, tmax=48.0, kmin=-10, kmax=-1, hmin=8.0, hmax=90.0),
}
WAVE_ORDER = ["mjo", "er", "kelvin"]          # 绘图层次：最窄的 Kelvin 在最上层

LABELS = {
    "en": dict(kelvin="Kelvin wave (eastward)", mjo="MJO (eastward)",
               er="Equatorial Rossby (westward)", enhanced="solid: enhanced convection",
               suppressed="dashed: suppressed", lon="Longitude", freq="Frequency (cycles per day)",
               wavenumber="Zonal wavenumber (+ eastward)",
               hov="Hovmöller ({band}): tilt to the right = eastward propagation",
               spec="Wavenumber–frequency spectrum (symmetric / background)",
               ts="Filtered OLR at {lon} ({band})", olr_anom="OLR anomaly",
               title="Convectively coupled equatorial waves"),
    "zh": dict(kelvin="Kelvin 波（东传）", mjo="MJO（东传）", er="赤道 Rossby 波（西传）",
               enhanced="实线：对流增强", suppressed="虚线：对流抑制", lon="经度",
               freq="频率（次/天）", wavenumber="纬向波数（正 = 东传）",
               hov="Hovmöller 图（{band}）：向右下倾斜 = 东传",
               spec="波数-频率谱（对称分量 / 背景谱）",
               ts="{lon} 处的滤波 OLR（{band}）", olr_anom="OLR 异常",
               title="热带对流耦合赤道波动"),
}


@dataclass
class Config:
    # ---- 时间与数据 ----
    start: str = "2025-11-01"
    end: str = "2026-02-28"
    pad: int = 180                       # 滤波缓冲天数（前后各加）
    source: str = "auto"                 # auto | cdr | psl | local | demo
    olr_files: list = field(default_factory=list)
    olr_cache: str = "data/olr_cache"    # 本地 OLR 目录：里面任何 .nc 都会按时间范围自动识别
    # 2024 年以前的年度 OLR 小文件（scripts/make_olr_subsets.py 生成，上传到 GitHub Release）
    data_release: str = "https://github.com/Blissful-Jasper/tropical-waves-viz/releases/download/olr-data-v1"

    # ---- 波动 ----
    waves: dict = field(default_factory=lambda: copy.deepcopy(WAVE_DEFAULTS))
    contour_lat: float = 25.0            # 等值线总纬度上限（再与各波动自己的 lat_max 取较小值）
    thresh_mode: str = "abs"             # abs: 阈值单位 W m⁻² | std: 各波动标准差的倍数（便于公平比较）
    show_suppressed: bool = True
    linewidth: float = 2.0

    # ---- 地图 ----
    proj: str = "strip"                  # strip | globe
    center_lon: float = 120.0
    map_lat: float = 35.0                # 带状图纬度范围 ±map_lat
    coast_color: str = "#e8c547"
    show_grid: bool = True

    # ---- 背景 ----
    bg: str = "olr"                      # olr | mergir | imerg | imerg-late | none
    bg_hour: int = 6                     # MERGIR 取哪个 UTC 整点
    bg_res: float = 0.2
    bg_cache: str = "data/bg_cache"
    bg_cmap: str = "auto"                # auto 或任意 matplotlib colormap 名
    bg_vmin: float | None = None
    bg_vmax: float | None = None

    # ---- Hovmöller / 谱 / 时间序列 ----
    hov_style: str = "filled"            # filled | shaded | lines
    hov_lat: float = 10.0                # 经向平均带 ±hov_lat
    hov_thresh_scale: float = 0.8
    hov_cmap: str = "PuOr_r"
    spec_lat: float = 15.0
    spec_seg: int = 96
    ts_lon: float = 120.0

    # ---- 外观 ----
    theme: str = "dark"                  # dark | light
    lang: str = "en"                     # 图中文字语言 en | zh（zh 需要中文字体）
    font_family: str = ""                # 如 "PingFang SC"、"Noto Sans CJK SC"
    title: str = ""                      # 留空 = 默认标题

    # ---- 动画导出 ----
    layout: str = "map+hov"              # map | map+hov
    frames_per_day: int = 1
    fps: float = 8.0
    dpi: int = 100
    fig_width: float = 12.0
    out_format: str = "gif"              # gif | mp4

    # ------------------------------------------------------------------
    def enabled_waves(self):
        return [w for w in WAVE_ORDER if self.waves.get(w, {}).get("enabled")]

    def lat_limit(self, w):
        return min(self.contour_lat, float(self.waves[w].get("lat_max") or self.contour_lat))

    def thresh(self, w, da=None):
        """实际使用的等值线阈值 (W m⁻²)"""
        p = self.waves[w]
        if self.thresh_mode == "std" and da is not None and da.attrs.get("std_eq", 0) > 0:
            return float(p.get("thresh_std", 1.0)) * float(da.attrs["std_eq"])
        return float(p["thresh"])

    def label(self, key, **kw):
        return LABELS.get(self.lang, LABELS["en"])[key].format(**kw)

    def to_dict(self):
        return asdict(self)

    def to_json(self, **kw):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, **kw)

    @classmethod
    def from_dict(cls, d: dict | None):
        d = dict(d or {})
        names = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in d.items() if k in names and k != "waves"})
        for w, params in (d.get("waves") or {}).items():
            if w in cfg.waves:
                cfg.waves[w].update(params)
        if cfg.source == "cdr":                  # 旧配置兼容：cdr 现在和 auto 一样会自动处理 2024 年以前
            cfg.source = "auto"
        return cfg

    @classmethod
    def from_json(cls, text: str):
        return cls.from_dict(json.loads(text))

    # 缓存用的键：只包含影响该步骤结果的参数
    def data_key(self):
        return json.dumps(dict(start=self.start, end=self.end, pad=self.pad, source=self.source,
                               olr_files=list(self.olr_files), olr_cache=self.olr_cache,
                               data_release=self.data_release), sort_keys=True)

    def filter_key(self):
        keep = ("tmin", "tmax", "kmin", "kmax", "hmin", "hmax", "enabled", "symmetric")
        return json.dumps({w: {k: p.get(k) for k in keep} for w, p in self.waves.items()}, sort_keys=True)
