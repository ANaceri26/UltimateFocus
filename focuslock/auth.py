"""Password hashing with scrypt.

The password is never stored. We store a *hash*: a one-way fingerprint. To check a guess,
hash it again with the same salt and parameters and compare. Plain SHA-256 is far too
fast (billions of guesses per second on a GPU); scrypt is deliberately slow and needs
16 MiB of memory per guess, which makes brute force expensive. The random salt means two
people with the same password get different hashes, so precomputed tables are useless.
"""

import hashlib
import hmac
import os

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LENGTH = 32
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 6


def _derive(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length, maxmem=64 * 1024 * 1024
    )


def hash_password(password: str) -> str:
    """Return 'scrypt$N$r$p$salt$hash' (hex), so parameters can be raised later."""
    salt = os.urandom(SALT_BYTES)
    key = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, KEY_LENGTH)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(key_hex)
        actual = _derive(password, bytes.fromhex(salt_hex), int(n), int(r), int(p), len(expected))
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)
