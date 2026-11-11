from pathlib import Path
import ctypes
import subprocess
import sys

hosts = Path(r"C:\Windows\System32\drivers\etc\hosts")
domains = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")

if not ctypes.windll.shell32.IsUserAnAdmin():
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{Path(__file__).resolve()}"', None, 1)
    sys.exit()

content = hosts.read_text(encoding="utf-8", errors="replace")
blocked = set()

for line in content.splitlines():
    fields = line.split("#", 1)[0].split()
    if fields and fields[0] == "0.0.0.0":
        blocked.update(fields[1:])

missing = [domain for domain in domains if domain not in blocked]

if missing:
    with hosts.open("a", encoding="utf-8") as file:
        file.write(("\n" if content and not content.endswith("\n") else "") + "".join(f"0.0.0.0 {domain}\n" for domain in missing))
    subprocess.run(["ipconfig", "/flushdns"], check=True)

print("YouTube domains blocked.")
input("Press Enter to exit...")
