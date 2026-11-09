"""Desktop notifications, using what each operating system already has."""

from __future__ import annotations
from . import processes

import shutil
import subprocess

from .paths import os_name


def notify(title: str, message: str) -> bool:
    message = message[:240]
    try:
        system = os_name()
        if system == "macos":
            script = f"display notification {_applescript(message)} with title {_applescript(title)}"
            processes.run(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            return True
        if system == "windows":
            ps = shutil.which("powershell") or shutil.which("pwsh")
            if not ps:
                return False
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$x = $t.GetElementsByTagName('text');"
                f"$x.Item(0).AppendChild($t.CreateTextNode({_ps(title)})) > $null;"
                f"$x.Item(1).AppendChild($t.CreateTextNode({_ps(message)})) > $null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('agent-kit-sync').Show([Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            processes.run([ps, "-NoProfile", "-NonInteractive", "-Command", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            return True
        if shutil.which("notify-send"):
            processes.run(["notify-send", title, message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    return False


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ps(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"
