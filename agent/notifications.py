"""Windows toast submission; acceptance is not a read receipt."""
import base64
import os
import subprocess
from xml.sax.saxutils import escape


def notify(title, body):
    if os.name != "nt":
        raise OSError("Windows notifications require Windows")
    # Register a per-user desktop notification identity; this is not autostart.
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId\PersonalAIHelper") as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Personal AI Helper")
    xml = '<toast><visual><binding template="ToastGeneric"><text>' + escape(title) + '</text><text>' + escape(body) + '</text></binding></visual></toast>'
    encoded = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    script = '''$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')))
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PersonalAIHelper').Show($toast)
''' % encoded
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(script.encode("utf-16le")).decode("ascii")],
                   check=True, capture_output=True, timeout=30,
                   creationflags=subprocess.CREATE_NO_WINDOW)
