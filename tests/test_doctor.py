from pathlib import Path

import pytest

from autor3search_python import config, doctor
from autor3search_python.cli import main as cli_main

_SYSTEM_PYTHON = "/usr/bin/python3"


def by_name(findings, name):
    return next(f for f in findings if f.name == name)


def label_by_name(out: str, findings) -> dict[str, str]:
    """Parse the doctor CLI's rendered lines back into {finding name: label}.

    Mirrors the exact layout `cli.doctor.run` writes
    (`f"{label}  {name.ljust(width)}  {detail}"`), so it fails if the labels
    or the names they are attached to ever drift apart.
    """
    width = max(len(f.name) for f in findings)
    result = {}
    for line in out.splitlines():
        if len(line) <= 6 + width:
            continue
        label, name = line[:4], line[6 : 6 + width].strip()
        if name:
            result[name] = label
    return result


def test_check_returns_findings_for_the_basics(git_repo):
    findings = doctor.check(git_repo)
    names = {f.name for f in findings}
    assert {
        "python",
        "git",
        "git repo",
        "cpu",
        "pytest-benchmark",
        "coverage",
        "extensions",
        "disk",
    } <= names


def test_python_and_git_are_present_here(git_repo):
    findings = doctor.check(git_repo)
    assert by_name(findings, "python").severity is doctor.Severity.OK
    assert by_name(findings, "git").severity is doctor.Severity.OK
    assert by_name(findings, "git repo").severity is doctor.Severity.OK


def test_outside_a_repository_is_a_failure(tmp_path):
    assert by_name(doctor.check(tmp_path), "git repo").severity is doctor.Severity.FAIL


def test_coverage_in_addopts_is_warned_about(tmp_path):
    """Coverage instruments every call and destroys every timing."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--cov=pkg --cov-report=term"\n'
    )
    f = doctor.check_coverage_addopts(tmp_path)
    assert f.severity is doctor.Severity.WARN
    assert "cov" in f.detail


def test_coverage_is_found_in_pytest_ini_and_setup_cfg_and_tox_ini(tmp_path):
    for name in ("pytest.ini", "setup.cfg", "tox.ini"):
        d = tmp_path / name.replace(".", "_")
        d.mkdir()
        (d / name).write_text("[pytest]\naddopts = --cov=pkg\n")
        assert doctor.check_coverage_addopts(d).severity is doctor.Severity.WARN


def test_no_coverage_is_ok(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-q"\n')
    assert doctor.check_coverage_addopts(tmp_path).severity is doctor.Severity.OK


def test_compiled_extension_signals_are_warned_about(tmp_path):
    """PYTHONPATH injection cannot build these, so the baseline worktree will not import."""
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup(ext_modules=[])\n")
    assert doctor.check_compiled_extensions(tmp_path).severity is doctor.Severity.WARN


def test_cargo_and_pyx_and_meson_also_signal(tmp_path):
    for name, body in (
        ("Cargo.toml", "[package]\n"),
        ("mod.pyx", "x = 1\n"),
        ("meson.build", "project('x')\n"),
    ):
        d = tmp_path / name.replace(".", "_")
        d.mkdir()
        (d / name).write_text(body)
        assert doctor.check_compiled_extensions(d).severity is doctor.Severity.WARN


def test_a_plain_pure_python_repo_is_ok(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    assert doctor.check_compiled_extensions(tmp_path).severity is doctor.Severity.OK


@pytest.mark.skipif(
    not Path(_SYSTEM_PYTHON).exists(), reason="no system python3 to check against here"
)
def test_missing_pytest_benchmark_is_a_failure():
    """Without it nothing can be measured at all.

    Deterministic, not tautological: /usr/bin/python3 is the platform system
    interpreter, never the one this project's own venv installs
    pytest-benchmark into, so this always exercises the FAIL branch rather
    than merely tolerating either outcome.
    """
    f = doctor.check_benchmark_tooling(_SYSTEM_PYTHON)
    assert f.severity is doctor.Severity.FAIL
    assert "pip install" in f.detail


def test_benchmark_tooling_check_survives_unexpected_child_output(monkeypatch):
    """A child process whose stdout doesn't carry two tokens must not crash
    doctor with an IndexError — it should be reported, not raised."""

    class _FakeCompleted:
        returncode = 0
        stdout = "only-one-token\n"
        stderr = ""

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: _FakeCompleted())
    f = doctor.check_benchmark_tooling("fake-python")
    assert f.severity is doctor.Severity.WARN
    assert "unexpected output" in f.detail


def test_doctor_command_always_exits_zero(tmp_path, capsys):
    """Informational: it reports, it does not gate."""
    assert cli_main.main(["doctor", "-C", str(tmp_path)]) == 0
    assert "git repo" in capsys.readouterr().out


def test_doctor_output_labels_a_warning_and_an_ok_finding_distinctly(git_repo, capsys):
    (git_repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "--cov=pkg"\n')
    findings = doctor.check(git_repo)
    cli_main.main(["doctor", "-C", str(git_repo)])
    labels = label_by_name(capsys.readouterr().out, findings)
    assert labels["git repo"] == "OK  "
    assert labels["coverage"] == "WARN"


def test_doctor_output_labels_a_failure(tmp_path, capsys):
    findings = doctor.check(tmp_path)
    cli_main.main(["doctor", "-C", str(tmp_path)])
    labels = label_by_name(capsys.readouterr().out, findings)
    assert labels["git repo"] == "FAIL"


def test_doctor_uses_the_configured_interpreter(git_repo, capsys):
    """cfg.python, when set, is the interpreter that will actually run the
    benchmarks — doctor must check tooling against it, not against whatever
    interpreter happens to be running the harness."""
    cfg_dir = git_repo / ".autor3search"
    cfg_dir.mkdir()
    bogus = "/nonexistent/python-for-doctor-wiring-test"
    (cfg_dir / "config.toml").write_text(f'python = "{bogus}"\n')
    assert config.load(cfg_dir / "config.toml").python == bogus  # sanity on the fixture itself

    cli_main.main(["doctor", "-C", str(git_repo)])
    assert bogus in capsys.readouterr().out


def test_the_measuring_interpreter_must_import_the_harness_itself(monkeypatch):
    """`profile` runs `-p autor3search_python.profiling` under `python`, not
    under the interpreter running the harness. Point `python` at a venv without
    the harness installed and measurement works while profiling fails at
    collection — checked here, so it is a sentence instead of a 3am mystery."""

    class _Missing:
        returncode = 1
        stdout = ""
        stderr = "autor3search_python\n"

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: _Missing())
    f = doctor.check_benchmark_tooling("fake-python")
    assert f.severity is doctor.Severity.FAIL
    assert "autor3search_python is not importable" in f.detail
    assert "pip install pytest pytest-benchmark autor3search-python" in f.detail


def test_the_tooling_check_probes_every_required_module(monkeypatch):
    seen = []

    class _Ok:
        returncode = 0
        stdout = "8.0.0 4.0.0\n"
        stderr = ""

    monkeypatch.setattr(doctor.subprocess, "run", lambda args, **k: seen.append(args) or _Ok())
    assert doctor.check_benchmark_tooling("fake-python").severity is doctor.Severity.OK
    script = seen[0][-1]
    for name in ("pytest", "pytest_benchmark", "autor3search_python"):
        assert name in script
