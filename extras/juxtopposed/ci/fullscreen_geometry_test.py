"""Check restored fullscreen client bounds without capturing or controlling input.

Run against an expendable, standalone Windows test package after launch-test.ps1.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import re
import socket
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument("package", type=Path)
parser.add_argument("work", type=Path)
options = parser.parse_args()
package, work = options.package.resolve(), options.work.resolve()
assert (package / "portable" / "standalone").is_file(), "standalone test package required"
work.mkdir(parents=True, exist_ok=True)
settings = package / "portable" / "vlc" / "vlc-qt-interface.ini"
text = settings.read_text(encoding="utf-8-sig")
match = re.search(r"^geometry=@ByteArray\((.*)\)$", text, re.M)
assert match, "run the ordinary launch test first to save screen geometry"
escapes = {"0": 0, "a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13}
raw = bytearray()
tokens = re.finditer(r"\\x([0-9a-fA-F]{1,2})|\\(.)|([^\\])", match[1])
for token in tokens:
    hex_byte, escaped, literal = token.groups()
    raw.append(int(hex_byte, 16) if hex_byte else escapes.get(escaped, ord(escaped)) if escaped else ord(literal))
assert raw[:6] == bytes.fromhex("01d942000100"), "unexpected geometry format"
assert raw[-2] in (0, 2) and raw[-1] in (0, 4), "unexpected state flags"
raw[-2:] = bytes([0, 4])  # restored interface fullscreen, not video fullscreen
geometry = "@ByteArray(" + "".join(f"\\x{byte:02x}" for byte in raw) + ")"
settings.write_text("[General]\ngeometry=" + geometry + "\n[redesign]\nremember-window-geometry=true\n", encoding="utf-8")
(package / "portable" / "vlcrc").write_text("qt-privacy-ask=0\nredesign-update-check=0\nmetadata-network-access=0\n", encoding="utf-8")

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetProcessDpiAwarenessContext.argtypes = [w.HANDLE]
user32.SetProcessDpiAwarenessContext(w.HANDLE(-4))
enum_callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user32.EnumWindows.argtypes = [enum_callback, w.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.ClientToScreen.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
user32.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
user32.MonitorFromWindow.restype = w.HANDLE

class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("monitor", w.RECT), ("work", w.RECT), ("flags", w.DWORD)]

user32.GetMonitorInfoW.argtypes = [w.HANDLE, ctypes.POINTER(MonitorInfo)]
with socket.socket() as reserve:
    reserve.bind(("127.0.0.1", 0))
    port = reserve.getsockname()[1]
startup = subprocess.STARTUPINFO()
startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startup.wShowWindow = 4
log = work / "fullscreen.log"
process = subprocess.Popen([str(package / "vlc.exe"), "--no-one-instance",
    "--no-one-instance-when-started-from-file", "--no-qt-privacy-ask", "--start-paused",
    "--extraintf=rc", f"--rc-host=127.0.0.1:{port}", "--aout=dummy", "--verbose=2",
    "--file-logging", f"--logfile={log}", str(work / "clip.y4m")], startupinfo=startup)
connection = None
result = {}
try:
    deadline = time.monotonic() + 45
    hwnd = None
    while time.monotonic() < deadline and process.poll() is None:
        windows = []
        @enum_callback
        def collect(window, _):
            pid = w.DWORD()
            user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == process.pid and user32.IsWindowVisible(window):
                rect = w.RECT()
                user32.GetClientRect(window, ctypes.byref(rect))
                windows.append((rect.right * rect.bottom, window))
            return True
        user32.EnumWindows(collect, 0)
        if windows:
            hwnd = max(windows)[1]
        if connection is None:
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=.1)
            except OSError:
                pass
        if hwnd and connection and log.exists() and "Received first picture" in log.read_text(encoding="utf-8-sig", errors="replace"):
            break
        time.sleep(.1)
    assert hwnd and connection, "test window/RC did not initialize"
    time.sleep(2)
    rect, origin = w.RECT(), w.POINT(0, 0)
    assert user32.GetClientRect(hwnd, ctypes.byref(rect))
    assert user32.ClientToScreen(hwnd, ctypes.byref(origin))
    monitor = MonitorInfo()
    monitor.size = ctypes.sizeof(monitor)
    assert user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(monitor))
    result["client"] = [origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom]
    result["monitor"] = [monitor.monitor.left, monitor.monitor.top, monitor.monitor.right, monitor.monitor.bottom]
    result["first_picture"] = "Received first picture" in log.read_text(encoding="utf-8-sig", errors="replace")
    result["fills_monitor"] = result["client"] == result["monitor"]
finally:
    if connection:
        connection.sendall(b"quit\n")
        connection.close()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        result["quit_timeout"] = True
    result["exit_code"] = process.returncode
    (work / "fullscreen-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
assert result.get("first_picture") and result.get("fills_monitor"), "restored fullscreen leaves borders or did not play"
assert result["exit_code"] == 0 and not result.get("quit_timeout"), "VLC did not quit cleanly"
