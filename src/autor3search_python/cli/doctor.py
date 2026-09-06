"""doctor — is this machine fit to measure? Informational; always exits 0."""

from __future__ import annotations

import argparse
from pathlib import Path

from autor3search_python import config, doctor, gitx
from autor3search_python.cli.main import EXIT_OK

_LABELS = {
    doctor.Severity.OK: "OK  ",
    doctor.Severity.WARN: "WARN",
    doctor.Severity.FAIL: "FAIL",
    doctor.Severity.NOT_APPLICABLE: "n/a ",
}


def _configured_python(directory: str) -> str:
    """The interpreter a run would actually use, so pytest-benchmark is
    checked against the one that will run the benchmarks — not against
    whichever interpreter happens to be running the harness itself."""
    try:
        root = Path(gitx.root(directory))
    except gitx.GitError:
        return ""
    try:
        return config.load(root / config.CONFIG_PATH).python
    except config.ConfigError:
        return ""


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="autor3search-python doctor")
    parser.add_argument("-C", dest="directory", default=".", help="repository root")
    opts = parser.parse_args(args)

    findings = doctor.check(opts.directory, python=_configured_python(opts.directory))
    width = max(len(f.name) for f in findings)
    for f in findings:
        print(f"{_LABELS[f.severity]}  {f.name.ljust(width)}  {f.detail}")

    worst = max((f.severity for f in findings), default=doctor.Severity.OK)
    print()
    if worst >= doctor.Severity.FAIL:
        print("Some checks FAILED. Fix them before trusting any number this tool produces.")
    elif worst == doctor.Severity.WARN:
        print("Warnings above. Numbers will still be produced; read them with that in mind.")
    else:
        print("This machine looks fit to measure.")
    # Always 0: doctor reports, it does not gate.
    return EXIT_OK
