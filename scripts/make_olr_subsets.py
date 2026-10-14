#!/usr/bin/env python3
"""
把大的 OLR 文件（如 PSL 的 olr.day.mean.nc，约 350 MB）切成按年的紧凑小文件，
用于上传到 GitHub Release，让部署在云端的网页也能读 2024 年以前的数据。

每个文件：±62° 纬度、int16 压缩，约 3 MB/年，文件名 olr_psl_YYYY.nc。

用法
  python scripts/make_olr_subsets.py ../olr_cache/olr.day.mean.nc --out release_data
  gh release create olr-data-v1 release_data/*.nc --title "OLR yearly subsets" \
      --notes "NOAA PSL interpolated OLR (Liebmann & Smith 1996), 1974-06 ~ 2022-12, ±62°"
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import xarray as xr  # noqa: E402

from tropical_waves.data import normalize, subset_name, write_subset  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default="release_data")
    ap.add_argument("--years", nargs=2, type=int, metavar=("FIRST", "LAST"))
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    for f in a.files:
        with xr.open_dataset(f) as ds:
            da = normalize(ds)
            years = sorted(set(da.time.dt.year.values.tolist()))
            if a.years:
                years = [y for y in years if a.years[0] <= y <= a.years[1]]
            for y in years:
                out = os.path.join(a.out, subset_name(y))
                write_subset(da.sel(time=str(y)).load(), out)
                print(f"{out}  {os.path.getsize(out) / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
