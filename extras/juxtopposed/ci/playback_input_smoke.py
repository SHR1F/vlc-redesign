"""Exercise VLC's real Qt window on a disposable GitHub Windows runner."""

import argparse
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import re
import socket
import subprocess
import time


if os.environ.get("GITHUB_ACTIONS") != "true":
    raise SystemExit("This test sends input only on the disposable CI desktop.")

user32 = c.WinDLL("user32", use_last_error=True)
user32.SetProcessDPIAware()
user32.GetForegroundWindow.restype = w.HWND
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
user32.GetClientRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
user32.ClientToScreen.argtypes = [w.HWND, c.POINTER(w.POINT)]
user32.ShowWindow.argtypes = [w.HWND, c.c_int]
enum_callback = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user32.EnumWindows.argtypes = [enum_callback, w.LPARAM]


class MouseInput(c.Structure):
    _fields_ = [
        ("dx", w.LONG), ("dy", w.LONG), ("data", w.DWORD),
        ("flags", w.DWORD), ("time", w.DWORD), ("extra", c.c_size_t),
    ]


class KeyInput(c.Structure):
    _fields_ = [
        ("vk", w.WORD), ("scan", w.WORD), ("flags", w.DWORD),
        ("time", w.DWORD), ("extra", c.c_size_t),
    ]


class InputData(c.Union):
    _fields_ = [("mouse", MouseInput), ("key", KeyInput)]


class Input(c.Structure):
    _fields_ = [("kind", w.DWORD), ("data", InputData)]


user32.SendInput.argtypes = [w.UINT, c.POINTER(Input), c.c_int]
user32.SendInput.restype = w.UINT


def send(event):
    if user32.SendInput(1, c.byref(event), c.sizeof(event)) != 1:
        raise RuntimeError("SendInput failed on the CI desktop")


def key(vk, up=False):
    send(Input(1, InputData(key=KeyInput(vk, 0, 2 if up else 0, 0, 0))))


def mouse(up=False):
    send(Input(0, InputData(mouse=MouseInput(0, 0, 0, 4 if up else 2, 0, 0))))


def tap(action):
    action()
    time.sleep(.06)
    action(True)


def ctrl_key(vk):
    key(0x11)
    try:
        tap(lambda up=False: key(vk, up))
    finally:
        key(0x11, True)


def wait_for(predicate, label, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.06)
    raise AssertionError(label)


def window_for(pid):
    windows = []

    @enum_callback
    def collect(hwnd, _):
        owner = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, c.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            rect = w.RECT()
            user32.GetClientRect(hwnd, c.byref(rect))
            if rect.right > 200 and rect.bottom > 150:
                windows.append((rect.right * rect.bottom, hwnd))
        return True

    user32.EnumWindows(collect, 0)
    return max(windows, default=(0, None))[1]


def activate(hwnd):
    user32.SetForegroundWindow(hwnd)
    wait_for(lambda: user32.GetForegroundWindow() == hwnd, "VLC window did not activate")


def point_at_video(hwnd):
    rect = w.RECT()
    user32.GetClientRect(hwnd, c.byref(rect))
    center = w.POINT(rect.right // 2, rect.bottom // 2)
    user32.ClientToScreen(hwnd, c.byref(center))
    if not user32.SetCursorPos(center.x, center.y):
        raise RuntimeError("Could not position the CI cursor")
    time.sleep(.15)


class Remote:
    def __init__(self, connection):
        self.connection = connection
        self.connection.settimeout(.12)

    def run(self, command):
        self.connection.sendall((command + "\n").encode())
        result = bytearray()
        while True:
            try:
                part = self.connection.recv(65536)
                if not part:
                    break
                result.extend(part)
            except TimeoutError:
                break
        return result.decode(errors="replace")

    def state(self):
        states = re.findall(r"\b(play|pause|stop) state:", self.run("status"))
        return states[-1] if states else None

    def rate(self):
        values = re.findall(r"(?m)^\s*([+-]?\d+\.\d+)\s*$", self.run("rate"))
        return float(values[-1]) if values else None

    def expect_rate(self, value):
        wait_for(lambda: self.rate() == value, f"Playback rate did not reach {value}")


def make_clip(path):
    width, height, fps = 128, 72, 25
    frame = b"FRAME\n" + bytes([48]) * (width * height) + bytes([128]) * (width * height // 2)
    with path.open("wb") as output:
        output.write(f"YUV4MPEG2 W{width} H{height} F{fps}:1 Ip A1:1 C420jpeg\n".encode())
        for _ in range(fps * 90):
            output.write(frame)


def run_session(package, work, mode, clip, next_clip, baseline):
    log = work / (mode + ".log")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen([
        str(package / "vlc.exe"), "--no-one-instance", "--no-one-instance-when-started-from-file",
        "--no-qt-privacy-ask", "--no-qt-pause-minimized", "--no-video-title-show",
        "--aout=dummy", "--extraintf=rc", f"--rc-host=127.0.0.1:{port}",
        "--verbose=2", "--file-logging", f"--logfile={log}",
        "--qt-minimal-view" if mode == "minimal" else "--no-qt-minimal-view", str(clip),
    ])
    connection = None
    remote = None
    hwnd = None
    try:
        def connect():
            nonlocal connection
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=.2)
            except OSError:
                return False
            return True

        wait_for(connect, "RC did not open", 30)
        remote = Remote(connection)
        wait_for(lambda: log.exists() and "Received first picture" in log.read_text(
            encoding="utf-8-sig", errors="replace"), "Video did not render", 30)
        wait_for(lambda: window_for(process.pid), "No visible VLC window", 15)
        hwnd = window_for(process.pid)
        activate(hwnd)
        point_at_video(hwnd)
        wait_for(lambda: remote.state() == "play", "Video did not enter playing state")
        # Let startup overlays and the focus change finish before sending keys.
        time.sleep(1.0)

        space = lambda up=False: key(0x20, up)
        tap(space)
        time.sleep(.8)
        if baseline and mode == "minimal":
            print(f"Unchanged upstream Hide UI after a Space tap: {remote.state()}", flush=True)
            return
        wait_for(lambda: remote.state() == "pause", f"{mode}: Space tap did not pause")
        tap(space)
        wait_for(lambda: remote.state() == "play", f"{mode}: Space tap did not resume")
        print(f"{mode}: Space tap pauses and resumes", flush=True)
        if baseline:
            return

        wait_for(lambda: re.search(r"(?m)^\| \d+ - ", remote.run("strack")),
                 f"{mode}: local subtitle was not available")
        print(f"{mode}: local subtitle still available after the folder scan", flush=True)

        # K is a second Play/Pause binding. It exercises the Hide UI QML
        # route without using the new Space gesture in the window handler.
        play_pause = lambda up=False: key(0x4B, up)
        tap(play_pause)
        wait_for(lambda: remote.state() == "pause", f"{mode}: remapped tap did not pause")
        tap(play_pause)
        wait_for(lambda: remote.state() == "play", f"{mode}: remapped tap did not resume")
        print(f"{mode}: custom Play/Pause binding toggles once", flush=True)

        remote.run("rate 1.25")
        remote.expect_rate(1.25)
        space()
        remote.expect_rate(2)
        space()  # held-key repeat
        remote.expect_rate(2)
        assert remote.state() == "play", "Space repeat changed the play state"
        space(True)
        remote.expect_rate(1.25)
        print(f"{mode}: Space hold, repeat, and restoration passed", flush=True)

        tap(mouse)
        wait_for(lambda: remote.state() == "pause", f"{mode}: mouse tap did not pause")
        time.sleep(user32.GetDoubleClickTime() / 1000 + .1)
        tap(mouse)
        wait_for(lambda: remote.state() == "play", f"{mode}: mouse tap did not resume")
        time.sleep(user32.GetDoubleClickTime() / 1000 + .1)
        mouse()
        remote.expect_rate(2)
        space()
        time.sleep(.55)
        mouse(True)
        remote.expect_rate(2)
        space(True)
        remote.expect_rate(1.25)
        assert remote.state() == "play", "Mouse hold changed the play state"
        print(f"{mode}: mouse click, hold, and overlapping inputs passed", flush=True)

        space()
        remote.expect_rate(2)
        user32.ShowWindow(hwnd, 6)  # minimize, which deactivates the window
        remote.expect_rate(1.25)
        space(True)
        user32.ShowWindow(hwnd, 9)
        activate(hwnd)
        print(f"{mode}: window deactivation restores rate", flush=True)

        space()
        remote.expect_rate(2)
        ctrl_key(0x50)  # Ctrl+P opens Settings
        remote.expect_rate(1.25)
        space(True)
        tap(space)
        time.sleep(.35)
        assert remote.state() == "play", "Space reached the video while Settings was open"
        tap(lambda up=False: key(0x1B, up))
        time.sleep(.5)
        print(f"{mode}: Settings cancels the hold and takes playback focus", flush=True)

        space()
        remote.expect_rate(2)
        remote.run(f"add {next_clip.as_uri()}")
        wait_for(lambda: next_clip.name in remote.run("status"), "New media did not start")
        wait_for(lambda: remote.state() == "play", "New media did not enter playing state")
        remote.expect_rate(1.25)
        space(True)
        assert remote.state() == "play", "Release after media change paused the new video"
        print(f"{mode}: media switch keeps the normal rate", flush=True)
    finally:
        # Avoid leaving any synthetic button held if an assertion failed.
        try:
            key(0x20, True)
            mouse(True)
        except RuntimeError:
            pass
        if connection:
            connection.sendall(b"quit\n")
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise AssertionError("VLC did not quit cleanly")
        finally:
            if connection:
                connection.close()
        assert process.returncode == 0, f"VLC exited with {process.returncode}"
        if log.exists():
            messages = log.read_text(encoding="utf-8-sig", errors="replace")
            assert not re.search(r"qt(?: generic)? (?:warning|error|critical).*qrc:", messages), \
                f"{mode}: QML warnings appeared while checking playback controls"


parser = argparse.ArgumentParser()
parser.add_argument("package", type=Path)
parser.add_argument("work", type=Path)
parser.add_argument("--baseline", action="store_true")
args = parser.parse_args()
package, work = args.package.resolve(), args.work.resolve()
work.mkdir(parents=True, exist_ok=True)
profile = package / "portable"
(profile / "vlc").mkdir(parents=True, exist_ok=True)
(profile / "standalone").touch()
(profile / "vlcrc").write_text(
    "qt-privacy-ask=0\nredesign-update-check=0\nmetadata-network-access=0\nkey-play-pause=Space\tk\n")
(profile / "vlc" / "vlc-qt-interface.ini").write_text(
    "[redesign]\nremember-window-geometry=false\nsubtitle-auto-search=false\nwhats-new-seen=0.5.5\n")
clip, next_clip = work / "first.y4m", work / "second.y4m"
make_clip(clip)
make_clip(next_clip)
subtitle = "1\n00:00:00,000 --> 00:01:10,000\nPlayback input test\n"
clip.with_suffix(".en.srt").write_text(subtitle)
for index in range(32):
    (work / f"unrelated-{index}.srt").write_text(subtitle)
for mode in ("player", "minimal"):
    run_session(package, work, mode, clip, next_clip, args.baseline)
