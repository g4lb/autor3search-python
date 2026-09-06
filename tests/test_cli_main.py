from autor3search_python.cli import main as cli_main


def test_no_args_prints_usage_and_exits_2(capsys):
    assert cli_main.main([]) == 2
    assert "usage:" in capsys.readouterr().err


def test_unknown_command_exits_2(capsys):
    assert cli_main.main(["nope"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_help_lists_every_command(capsys):
    assert cli_main.main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in (
        "init",
        "doctor",
        "baseline",
        "profile",
        "eval",
        "status",
        "stop",
        "report",
        "version",
    ):
        assert name in out
