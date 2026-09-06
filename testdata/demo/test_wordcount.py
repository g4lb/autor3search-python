from wordcount import count_words

# A realistic log line, plus one multi-kilobyte alphanumeric token — a base64
# blob, an embedded payload, a serialized trace — repeated many times. Short
# natural words, and even a short identifier like a UUID or a SHA-256 digest,
# do not separate the two implementations: CPython's own in-place resize for
# `s = s + ch` already makes short-string concatenation cheap in practice, so
# the quadratic cost only dominates once a single token is roughly
# kilobyte-scale (measured: a 36-char UUID makes the "optimized" version
# LOSE by +4.4%; separation only becomes real in the hundreds of characters).
_LONG_TOKEN = "a1B2c3D4e5F6g7H8i9J0" * 300
BENCH_INPUT = ("The Quick, Brown Fox! jumps over 2 lazy dogs. " + _LONG_TOKEN + " ") * 40


def test_count_words():
    assert count_words("the quick brown the") == {"the": 2, "quick": 1, "brown": 1}


def test_count_words_punctuation():
    assert count_words("Hello, WORLD! hello?") == {"hello": 2, "world": 1}


def test_count_words_benchmark(benchmark):
    result = benchmark(count_words, BENCH_INPUT)
    assert result
