from autor3search_python.cli import main as cli_main


def test_version_prints_a_version(capsys):
    assert cli_main.main(["version"]) == 0
    out = capsys.readouterr().out
    assert out.strip()
    assert "autor3search-python" in out
