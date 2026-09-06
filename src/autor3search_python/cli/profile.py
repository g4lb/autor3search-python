"""profile — where does the time and the memory actually go?"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autor3search_python import config, discover, gitx, profile
from autor3search_python.cli.main import EXIT_OK, EXIT_USAGE


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="autor3search-python profile")
    parser.add_argument("-C", dest="directory", default=".", help="repository root")
    opts = parser.parse_args(args)

    try:
        root = Path(gitx.root(opts.directory))
    except gitx.GitError:
        root = Path(opts.directory)
    try:
        cfg = config.load(root / config.CONFIG_PATH)
    except config.ConfigError as e:
        print(f"autor3search-python profile: {e}", file=sys.stderr)
        return EXIT_USAGE

    node_ids = list(cfg.benchmarks) or discover.node_ids(discover.benchmarks(root))
    if not node_ids:
        print("autor3search-python profile: no benchmarks to profile", file=sys.stderr)
        return EXIT_USAGE

    try:
        report = profile.run_profile(root, node_ids, cfg)
    except profile.ProfileError as e:
        print(f"autor3search-python profile: {e}", file=sys.stderr)
        return EXIT_USAGE

    print("=== CPU ===")
    print(report.cpu_top)
    print("=== allocation sites ===")
    print(report.mem_top)
    print()
    print(f"raw CPU profile:    {report.cpu_path}")
    print(f"    open with:      python -m pstats {report.cpu_path}")
    print(f"raw memory profile: {report.mem_path}")
    return EXIT_OK
