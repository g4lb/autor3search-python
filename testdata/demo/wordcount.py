"""A word counter with an obvious, real performance bug: quadratic string building."""


def count_words(s):
    """Return how many times each lowercase alphanumeric word appears in s."""
    counts = {}
    for field in s.split():
        word = ""
        for ch in field:
            if "A" <= ch <= "Z":
                ch = chr(ord(ch) + 32)
            if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
                word = word + ch  # quadratic: rebuilds the string every character
        if word:
            counts[word] = counts.get(word, 0) + 1
    return counts
