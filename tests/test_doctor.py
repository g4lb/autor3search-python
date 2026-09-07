from pathlib import Path

import pytest

from autor3search_python import config, doctor
from autor3search_python.cli import main as cli_main
from tests.conftest import git

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
        "imports",
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


def test_a_plain_pure_python_module_actually_imports_ok(tmp_path):
    """The real check: attempt the import, don't guess from proxy files."""
    (tmp_path / "mod.py").write_text("x = 1\n")
    f = doctor.check_imports(tmp_path)
    assert f.severity is doctor.Severity.OK
    assert "mod" in f.detail


def test_a_src_layout_package_is_discovered_and_imports_ok(tmp_path):
    pkg = tmp_path / "src" / "widget"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n")
    f = doctor.check_imports(tmp_path)
    assert f.severity is doctor.Severity.OK
    assert "widget" in f.detail


def test_no_top_level_package_or_module_is_not_applicable(tmp_path):
    (tmp_path / "README.md").write_text("nothing to import\n")
    assert doctor.check_imports(tmp_path).severity is doctor.Severity.NOT_APPLICABLE


def test_a_broken_import_is_a_failure_carrying_the_real_error(tmp_path):
    """No heuristic can substitute for actually trying: the message must carry
    the interpreter's own error, not a guess about why it might fail."""
    pkg = tmp_path / "broken"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import this_module_does_not_exist_anywhere\n")
    f = doctor.check_imports(tmp_path)
    assert f.severity is doctor.Severity.FAIL
    assert "ModuleNotFoundError" in f.detail
    assert "this_module_does_not_exist_anywhere" in f.detail


def test_extension_signals_explain_a_failure_but_are_not_a_verdict_alone(tmp_path):
    """Demoted: a signal is an explanation attached to a real failure, not a
    verdict of its own — the opposite of what the old proxy check did."""
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    pkg = tmp_path / "broken"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import this_module_does_not_exist_anywhere\n")
    f = doctor.check_imports(tmp_path)
    assert f.severity is doctor.Severity.FAIL
    assert "ModuleNotFoundError" in f.detail
    assert "Cargo.toml" in f.detail


def test_extension_signals_alone_do_not_fail_or_warn_when_import_succeeds(tmp_path):
    """The negative case for the above: a Cargo.toml sitting beside a package
    that imports fine must not drag the verdict down to WARN or FAIL — the old
    check did exactly that, on nothing more than the file's presence."""
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    (tmp_path / "mod.py").write_text("x = 1\n")
    f = doctor.check_imports(tmp_path)
    assert f.severity is doctor.Severity.OK
    assert "Cargo.toml" not in f.detail


def _write_generated_version_package(repo: Path) -> None:
    """A src/-layout package whose `__init__.py` needs a gitignored `_version.py`.

    Mirrors humanize under hatch-vcs: `_version.py` is generated at build
    time, gitignored, and does not exist in a fresh checkout.
    """
    (repo / ".gitignore").write_text("_version.py\n")
    pkg = repo / "src" / "widget"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from widget._version import __version__\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add widget package")


def test_missing_gitignored_version_file_fails_with_the_real_error_and_names_it(git_repo):
    """Finding 1 + 2, exactly: a fresh checkout (no generated `_version.py`)
    must FAIL — the old proxy check reported this repository OK — carrying
    the real ModuleNotFoundError, and naming the gitignored file responsible
    even though it does not exist here (`git check-ignore` needs no file on
    disk, only a pattern to test against)."""
    _write_generated_version_package(git_repo)

    f = doctor.check_imports(git_repo)

    assert f.severity is doctor.Severity.FAIL
    assert "ModuleNotFoundError" in f.detail
    assert "widget._version" in f.detail
    assert "src/widget/_version.py" in f.detail
    assert "git add -f src/widget/_version.py" in f.detail


def test_present_but_gitignored_version_file_is_a_warning_not_an_ok(git_repo):
    """The other half: when the generated file happens to exist locally the
    import succeeds, but the pinned baseline worktree (a fresh checkout) will
    not have it — this must not be silently OK."""
    _write_generated_version_package(git_repo)
    (git_repo / "src" / "widget" / "_version.py").write_text('__version__ = "0.0.0"\n')

    f = doctor.check_imports(git_repo)

    assert f.severity is doctor.Severity.WARN
    assert "src/widget/_version.py" in f.detail
    assert "git add -f src/widget/_version.py" in f.detail


def test_a_tracked_version_file_is_a_clean_ok(git_repo):
    """The positive control for the two tests above: once the generated file
    is committed (not merely present), both the import and the gitignore scan
    are clean — this must render OK, not linger at WARN forever."""
    _write_generated_version_package(git_repo)
    (git_repo / "src" / "widget" / "_version.py").write_text('__version__ = "0.0.0"\n')
    git(git_repo, "add", "-f", "src/widget/_version.py")
    git(git_repo, "commit", "-q", "-m", "track the generated version file")

    f = doctor.check_imports(git_repo)

    assert f.severity is doctor.Severity.OK
    assert "_version.py" not in f.detail


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


def test_missing_autor3search_python_is_a_warning_naming_profile_not_a_failure(monkeypatch):
    """`profile` runs `-p autor3search_python.profiling` under `python`, not
    under the interpreter running the harness — but `eval` and `bench` never
    need it there. Point `python` at a venv without the harness installed and
    measurement still works while only `profile` fails at collection; this
    must not be reported as "nothing can be measured", which is false."""

    class _Missing:
        returncode = 1
        stdout = ""
        stderr = "autor3search_python\n"

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: _Missing())
    f = doctor.check_benchmark_tooling("fake-python")
    assert f.severity is doctor.Severity.WARN
    assert "autor3search_python is not importable" in f.detail
    assert "profile" in f.detail
    assert "pip install pytest pytest-benchmark autor3search-python" in f.detail


def test_missing_pytest_or_pytest_benchmark_is_a_failure_not_a_warning(monkeypatch):
    """The other half of the split: these two genuinely mean nothing can be
    measured, so they must stay a FAIL even though autor3search_python alone
    is now only a WARN."""

    class _Missing:
        returncode = 1
        stdout = ""
        stderr = "pytest pytest_benchmark\n"

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: _Missing())
    f = doctor.check_benchmark_tooling("fake-python")
    assert f.severity is doctor.Severity.FAIL
    assert "nothing can be measured" in f.detail
    assert "pip install pytest pytest-benchmark autor3search-python" in f.detail


def test_missing_pytest_outranks_a_also_missing_autor3search_python(monkeypatch):
    """When both a hard and a soft dependency are missing, report the FAIL —
    the WARN alone would understate the situation."""

    class _Missing:
        returncode = 1
        stdout = ""
        stderr = "pytest autor3search_python\n"

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: _Missing())
    f = doctor.check_benchmark_tooling("fake-python")
    assert f.severity is doctor.Severity.FAIL


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
