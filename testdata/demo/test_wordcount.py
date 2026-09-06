from wordcount import count_words

# A realistic log line, plus one long alphanumeric token — a hash, an id, a
# base64 blob — repeated many times. Short natural words alone do not separate
# the two implementations: CPython's own in-place resize for `s = s + ch`
# already makes short-string concatenation cheap in practice, so the quadratic
# cost only shows up once a single token is long enough for it to dominate.
_LONG_TOKEN = "a1B2c3D4e5F6g7H8i9J0" * 300
BENCH_INPUT = ("The Quick, Brown Fox! jumps over 2 lazy dogs. " + _LONG_TOKEN + " ") * 40


def test_count_words():
    assert count_words("the quick brown the") == {"the": 2, "quick": 1, "brown": 1}


def test_count_words_punctuation():
    assert count_words("Hello, WORLD! hello?") == {"hello": 2, "world": 1}


def test_count_words_benchmark(benchmark):
    result = benchmark(count_words, BENCH_INPUT)
    assert result
