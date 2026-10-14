#!/usr/bin/env python3
"""
命令行渲染动画（适合高分辨率批量出图），与网页共用同一套参数。

示例
  python cli.py --source demo --out demo.gif
  python cli.py --start 2025-11-01 --end 2026-02-28 --bg mergir --out ir.mp4
  python cli.py --config tropical_waves_config.json --out talk.gif       # 网页里导出的配置
  python cli.py --set waves.kelvin.enabled=false --set hov_style=shaded --set dpi=150
"""
import argparse
import json
import sys
import warnings

from tropical_waves import Config
from tropical_waves import background as bgmod
from tropical_waves import data, plots
from tropical_waves.data import DataError


def apply_set(d, expr):
    """--set a.b.c=value，value 按 JSON 解析（true/false/数字/null），解析失败当字符串"""
    key, _, raw = expr.partition("=")
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        val = raw
    node = d
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = val


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tropical waves animation (Kelvin / MJO / ER)")
    ap.add_argument("--config", help="网页“配置与说明”里下载的 JSON")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--source", choices=["cdr", "psl", "local", "demo"])
    ap.add_argument("--olr-file", nargs="+", help="本地 OLR 文件（自动切换到 local 数据源）")
    ap.add_argument("--bg", choices=["olr", "mergir", "imerg", "imerg-late", "none"])
    ap.add_argument("--proj", choices=["strip", "globe"])
    ap.add_argument("--layout", choices=["map", "map+hov"])
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="修改任意参数，可多次使用，如 --set waves.mjo.color=\"#00aaff\"")
    ap.add_argument("--out", default="tropical_waves.gif", help=".gif 或 .mp4")
    ap.add_argument("--print-config", action="store_true", help="只打印最终参数，不渲染")
    a = ap.parse_args(argv)

    d = Config.from_json(open(a.config, encoding="utf-8").read()).to_dict() if a.config else Config().to_dict()
    for name in ("start", "end", "source", "bg", "proj", "layout"):
        if getattr(a, name):
            d[name] = getattr(a, name)
    if a.olr_file:
        d["source"], d["olr_files"] = "local", a.olr_file
    for expr in a.set:
        apply_set(d, expr)
    cfg = Config.from_dict(d)
    if a.print_config:
        print(cfg.to_json())
        return 0

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            olr = data.load_olr(cfg)
            print("滤波中 …")
            filt = data.compute_filtered(olr, cfg)
            bg_paths = None
            if cfg.bg in bgmod.PRODUCTS:
                bg_paths = bgmod.prepare(cfg.bg, data.frame_times(cfg), cfg.bg_cache, cfg.bg_res,
                                         plots.bg_lat_max(cfg),
                                         progress=lambda f: print(f"\r背景 {f:5.0%}", end="", flush=True))
                print()
    except DataError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1

    n = len(data.frame_times(cfg))
    print(f"渲染 {n} 帧 → {a.out}")
    plots.render_animation(cfg, olr, filt, a.out, bg_paths,
                           progress=lambda f: print(f"\r渲染 {f:5.0%}", end="", flush=True))
    print(f"\n已保存: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
