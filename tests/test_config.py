import pytest

from autor3search_python import config


def write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_defaults_are_the_documented_ones():
    c = config.default()
    assert c.scope == ("./...",)
    assert c.count == 10
    assert c.benchtime == "1s"
    assert c.min_rounds == 5
    assert c.stat == "median"
    assert c.max_regress_pct == 5.0
    assert c.min_effect_pct == 1.0
    assert c.timeout == "15m"
    assert c.hashseed == 0
    assert c.gc == "enabled"
    assert c.gates.compile_ is True
    assert c.gates.import_ is True


def test_load_applies_defaults_for_omitted_fields(tmp_path):
    c = config.load(write(tmp_path, 'benchmarks = ["tests/test_a.py::test_b"]\n'))
    assert c.benchmarks == ("tests/test_a.py::test_b",)
    assert c.count == 10
    assert c.scope == ("./...",)


def test_load_reads_the_gates_table(tmp_path):
    c = config.load(write(tmp_path, "[gates]\ncompile = false\nimport = false\n"))
    assert c.gates.compile_ is False
    assert c.gates.import_ is False


def test_load_rejects_an_unknown_key(tmp_path):
    with pytest.raises(config.ConfigError, match="unknown"):
        config.load(write(tmp_path, "cout = 10\n"))


@pytest.mark.parametrize(
    "body,message",
    [
        ("count = 3", "count must be at least 4"),
        ("max_regress_pct = -1", "max_regress_pct"),
        ("min_effect_pct = 100", "min_effect_pct"),
        ("min_effect_pct = -0.5", "min_effect_pct"),
        ("scope = []", "at least one"),
        ('scope = ["  "]', "empty"),
        ('stat = "p99"', "stat"),
        ('gc = "sometimes"', "gc"),
        ('benchtime = "quick"', "benchtime"),
        ('timeout = "soon"', "timeout"),
        ("min_rounds = 0", "min_rounds"),
    ],
)
def test_validate_rejects(tmp_path, body, message):
    with pytest.raises(config.ConfigError, match=message):
        config.load(write(tmp_path, body + "\n"))


def test_count_floor_explains_why():
    """The message must say WHY 4 is the floor, not just that it is."""
    with pytest.raises(config.ConfigError) as e:
        config.validate(config.default().__class__(count=3))
    assert "p < 0.05" in str(e.value)


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("1s", 1.0),
        ("15m", 900.0),
        ("200ms", 0.2),
        ("1h", 3600.0),
        ("1m30s", 90.0),
        ("2h30m", 9000.0),
        ("500us", 0.0005),
        ("0s", 0.0),
    ],
)
def test_parse_duration(text, seconds):
    assert config.parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "1", "s", "1x", "100x", "-1s", "1 s", "1.5.5s"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        config.parse_duration(text)


def test_benchtime_rejects_the_iteration_count_form(tmp_path):
    """go test's `100x` form is a real flag value someone may copy across; say why not."""
    with pytest.raises(config.ConfigError, match="fixed-iteration"):
        config.load(write(tmp_path, 'benchtime = "100x"\n'))


def test_missing_file_names_the_next_command(tmp_path):
    with pytest.raises(config.ConfigError, match="init"):
        config.load(tmp_path / "absent.toml")
