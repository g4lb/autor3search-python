from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

import pytest

from autor3search import __version__
from autor3search.cli import main as cli_main


def test_version_prints_a_version(capsys):
    assert cli_main.main(["version"]) == 0
    out = capsys.readouterr().out
    assert out.strip()
    assert "autor3search-python" in out


def test_version_is_the_installed_distribution_version():
    """The distribution name is looked up by a string that can drift from
    pyproject.toml. When it does, importlib raises and __version__ degrades to
    the sentinel — which still prints, so the test above stays green while every
    install reports a fake version into results.tsv. Assert the real thing."""
    try:
        expected = _dist_version("autor3search")
    except PackageNotFoundError:
        pytest.skip("not installed; running from a source checkout")
    assert __version__ == expected
    assert __version__ != "0.0.0+unknown"
