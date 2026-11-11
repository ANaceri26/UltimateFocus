"""Entry point for focuslock-cli.exe (PyInstaller needs a script, not `python -m`).

With no command it opens the window, so double-clicking it works too.
"""

from focuslock.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
