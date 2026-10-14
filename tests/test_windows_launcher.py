from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_launcher_opens_visible_powershell_logs():
    launcher = (ROOT / "run-bot-with-logs.cmd").read_text(encoding="utf-8")

    assert "powershell -NoProfile" in launcher
    assert "python -X utf8 -u -m aviasales_collector.telegram_bot" in launcher
    assert "chcp 65001" in launcher
    # pythonw must not be USED as the executable (invisible window = lost logs);
    # a troubleshooting mention in the help prose is fine.
    executable_lines = [line for line in launcher.splitlines()
                        if "python" in line and "echo" not in line]
    assert all("pythonw" not in line for line in executable_lines)
