import sys

from autor3search_python import doctor
from autor3search_python.cli import main as cli_main


def by_name(findings, name):
    return next(f for f in findings if f.name == name)


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


def test_missing_pytest_benchmark_is_a_failure():
    """Without it nothing can be measured at all."""
    f = doctor.check_benchmark_tooling(sys.executable)
    assert f.severity in (doctor.Severity.OK, doctor.Severity.FAIL)
    if f.severity is doctor.Severity.FAIL:
        assert "pip install" in f.detail


def test_doctor_command_always_exits_zero(tmp_path, capsys):
    """Informational: it reports, it does not gate."""
    assert cli_main.main(["doctor", "-C", str(tmp_path)]) == 0
    assert "git repo" in capsys.readouterr().out


def test_doctor_output_marks_severities(git_repo, capsys):
    cli_main.main(["doctor", "-C", str(git_repo)])
    out = capsys.readouterr().out
    assert "OK" in out
