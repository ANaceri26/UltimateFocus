"""Entry point for FocusLock.exe, the windowed app people double-click.

PyInstaller needs a script file. This one always opens the window; the console build
(focuslock-cli.exe, from run_focuslock.py) is for commands like `password set`.
"""

import sys

from focuslock.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(["gui", *sys.argv[1:]]))
