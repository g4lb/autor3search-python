import pytest

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


def test_an_unexpected_exception_is_a_crash_not_a_discard(monkeypatch, capsys):
    """Exit 1 is DISCARD. An unattended loop reading a broken harness as DISCARD
    would `git reset --hard HEAD~1` and confidently keep going, forever.

    TypeError on purpose: eval catches nine exception types and this is not one
    of them, which is exactly the class this last-resort handler exists for.
    """

    def boom(_name):
        def run(_args):
            raise TypeError("something a refactor introduced")

        return run

    monkeypatch.setattr(cli_main, "_load", boom)
    assert cli_main.main(["eval"]) == cli_main.EXIT_CRASH
    err = capsys.readouterr().err
    assert "TypeError" in err  # the traceback, so the human can debug it
    assert "something a refactor introduced" in err


def test_a_crashing_command_does_not_swallow_keyboard_interrupt(monkeypatch):
    """Ctrl+C must still reach eval's own ABORTED handling, not become CRASH."""

    def boom(_name):
        def run(_args):
            raise KeyboardInterrupt

        return run

    monkeypatch.setattr(cli_main, "_load", boom)
    with pytest.raises(KeyboardInterrupt):
        cli_main.main(["eval"])
