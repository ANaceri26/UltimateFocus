import os
import shutil
import time
from pathlib import Path


def atomic_write_bytes(
    path: str | Path, data: bytes, *, retries: int = 5, delay: float = 0.1
) -> None:
    """Write data so readers see either the old file or the new one, never half of each.

    The bytes go to a temporary file in the same folder, are flushed to disk with fsync,
    then os.replace swaps it in. A rename within one folder is atomic on NTFS and POSIX.
    Windows may briefly lock the target (antivirus, the DNS client), so PermissionError
    is retried with exponential backoff.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(delay * 2**attempt)
    finally:
        tmp.unlink(missing_ok=True)
