import pytest

from focuslock.domains import normalize, unique, with_variants
from focuslock.errors import InvalidDomainError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("youtube.com", "youtube.com"),
        ("  YouTube.COM  ", "youtube.com"),
        ("https://www.youtube.com/watch?v=abc", "www.youtube.com"),
        ("http://user@m.youtube.com:8080/path#frag", "m.youtube.com"),
        ("netflix.com.", "netflix.com"),
        ("bücher.de", "xn--bcher-kva.de"),
    ],
)
def test_normalize_accepts_urls_and_odd_input(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "localhost",
        "a..b.com",
        "-bad.com",
        "bad-.com",
        "192.168.0.1",
        "a" * 64 + ".com",
        "x y.com",
    ],
)
def test_normalize_rejects_invalid_names(raw: str) -> None:
    with pytest.raises(InvalidDomainError):
        normalize(raw)


def test_with_variants_adds_and_strips_www() -> None:
    assert with_variants("youtube.com") == ["youtube.com", "www.youtube.com"]
    assert with_variants("www.youtube.com") == ["www.youtube.com", "youtube.com"]


def test_unique_keeps_first_seen_order() -> None:
    assert unique(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
