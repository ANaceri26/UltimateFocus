import re
from collections.abc import Iterable

from .errors import InvalidDomainError

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
MAX_DOMAIN_LENGTH = 253


def normalize(raw: str) -> str:
    """Turn user input such as 'https://WWW.YouTube.com/watch?v=x' into 'www.youtube.com'.

    Rules follow RFC 1035 / RFC 1123: labels are 1-63 chars of a-z, 0-9 and '-', may not
    start or end with '-', and the whole name is at most 253 chars. Unicode names are
    converted to their ASCII 'xn--' (punycode) form, which is what DNS actually carries.
    """
    if not isinstance(raw, str):
        raise InvalidDomainError("domain must be text")
    text = raw.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    for separator in ("/", "?", "#"):
        text = text.split(separator, 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = text.split(":", 1)[0].rstrip(".")
    if not text:
        raise InvalidDomainError(f"not a domain: {raw!r}")
    try:
        text = text.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidDomainError(f"not a valid domain: {raw!r}") from exc
    labels = text.split(".")
    if len(text) > MAX_DOMAIN_LENGTH or len(labels) < 2:
        raise InvalidDomainError(f"not a valid domain: {raw!r}")
    if not all(_LABEL.match(label) for label in labels):
        raise InvalidDomainError(f"not a valid domain: {raw!r}")
    if labels[-1].isdigit():
        raise InvalidDomainError(f"IP addresses cannot be blocked by name: {raw!r}")
    return text


def unique(items: Iterable[str]) -> list[str]:
    """Remove duplicates but keep first-seen order (a dict remembers insertion order)."""
    return list(dict.fromkeys(items))


def with_variants(domain: str) -> list[str]:
    """The hosts file has no wildcards, so 'youtube.com' must be listed with 'www.' too."""
    if domain.startswith("www."):
        return unique([domain, domain[4:]]) if domain.count(".") >= 2 else [domain]
    return [domain, f"www.{domain}"]
