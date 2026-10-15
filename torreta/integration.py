"""Integração com o sistema: instância única e associação de magnet:/.torrent.

A associação é feita em HKCU (não precisa de administrador) e registra o Torreta
em "Aplicativos padrão" do Windows. Em outros sistemas, só a instância única vale.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

APP_NAME = "Torreta"
PROGID_TORRENT = "Torreta.Torrent"
PROGID_MAGNET = "Torreta.Magnet"
INSTANCE_FILE = "instance.json"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PROJECT_ROOT / "torreta_launcher.pyw"


# ----------------------------------------------------------------- instância única

def write_instance(data_dir: Path, port: int, token: str) -> None:
    path = data_dir / INSTANCE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "token": token, "pid": os.getpid()}), "utf-8")
    os.replace(tmp, path)


def remove_instance(data_dir: Path) -> None:
    try:
        (data_dir / INSTANCE_FILE).unlink()
    except FileNotFoundError:
        pass


def _post(url: str, token: str, body: bytes, content_type: str, timeout: float = 10) -> bool:
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("X-Torreta-Token", token)
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def forward_to_running(data_dir: Path, items: list[str]) -> bool:
    """Se já há um Torreta aberto, entrega os itens a ele e devolve True."""
    path = data_dir / INSTANCE_FILE
    try:
        info = json.loads(path.read_text("utf-8"))
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    base = f"http://127.0.0.1:{port}"
    req = urllib.request.Request(f"{base}/api/state")
    req.add_header("X-Torreta-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=1.5):
            pass
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False  # arquivo velho de uma execução que morreu

    for item in items:
        if item.lower().startswith(("magnet:", "http://", "https://")):
            _post(f"{base}/api/torrents", token, json.dumps({"uri": item}).encode(), "application/json")
        else:
            try:
                data = Path(item).read_bytes()
            except OSError:
                continue
            boundary = uuid.uuid4().hex
            name = Path(item).name.replace('"', "")
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                "Content-Type: application/x-bittorrent\r\n\r\n"
            ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
            _post(f"{base}/api/torrents/file", token, body, f"multipart/form-data; boundary={boundary}")
    _post(f"{base}/api/focus", token, b"{}", "application/json", timeout=2)
    return True


# ------------------------------------------------------------ associação (Windows)

def launcher_command() -> str:
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{LAUNCHER}" "%1"'


def is_registered() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROGID_MAGNET}\shell\open\command") as k:
            cmd, _ = winreg.QueryValueEx(k, "")
        return str(LAUNCHER) in cmd
    except OSError:
        return False


def register_windows() -> str:
    if sys.platform != "win32":
        raise RuntimeError("Associação automática só está disponível no Windows.")
    import winreg

    cmd = launcher_command()
    icon = f'"{Path(sys.executable).with_name("pythonw.exe")}",0'
    root = winreg.HKEY_CURRENT_USER

    def put(path: str, value: str = "", name: str = ""):
        with winreg.CreateKey(root, path) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    # ProgIDs
    put(rf"Software\Classes\{PROGID_TORRENT}", "Arquivo torrent (Torreta)")
    put(rf"Software\Classes\{PROGID_TORRENT}\DefaultIcon", icon)
    put(rf"Software\Classes\{PROGID_TORRENT}\shell\open\command", cmd)
    put(rf"Software\Classes\{PROGID_MAGNET}", "Link magnet (Torreta)")
    put(rf"Software\Classes\{PROGID_MAGNET}\DefaultIcon", icon)
    put(rf"Software\Classes\{PROGID_MAGNET}\shell\open\command", cmd)
    # extensão e protocolo
    put(r"Software\Classes\.torrent", PROGID_TORRENT)
    put(r"Software\Classes\.torrent", "application/x-bittorrent", "Content Type")
    put(rf"Software\Classes\.torrent\OpenWithProgids", "", PROGID_TORRENT)
    put(r"Software\Classes\magnet", "URL:Magnet link")
    put(r"Software\Classes\magnet", "", "URL Protocol")
    put(r"Software\Classes\magnet\DefaultIcon", icon)
    put(r"Software\Classes\magnet\shell\open\command", cmd)
    # aparece em Configurações > Aplicativos padrão
    cap = rf"Software\{APP_NAME}\Capabilities"
    put(cap, APP_NAME, "ApplicationName")
    put(cap, "Cliente torrent simples, sem anúncios.", "ApplicationDescription")
    put(rf"{cap}\FileAssociations", PROGID_TORRENT, ".torrent")
    put(rf"{cap}\URLAssociations", PROGID_MAGNET, "magnet")
    put(r"Software\RegisteredApplications", cap, APP_NAME)
    _notify_shell()
    return "Torreta associado a links magnet e arquivos .torrent."


def unregister_windows() -> str:
    if sys.platform != "win32":
        raise RuntimeError("Associação automática só está disponível no Windows.")
    import winreg

    root = winreg.HKEY_CURRENT_USER

    def delete_tree(path: str):
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as k:
                while True:
                    try:
                        sub = winreg.EnumKey(k, 0)
                    except OSError:
                        break
                    delete_tree(f"{path}\\{sub}")
            winreg.DeleteKey(root, path)
        except FileNotFoundError:
            pass

    for p in (rf"Software\Classes\{PROGID_TORRENT}", rf"Software\Classes\{PROGID_MAGNET}", rf"Software\{APP_NAME}"):
        delete_tree(p)
    try:
        with winreg.OpenKey(root, r"Software\RegisteredApplications", 0, winreg.KEY_ALL_ACCESS) as k:
            winreg.DeleteValue(k, APP_NAME)
    except FileNotFoundError:
        pass
    # só desfaz .torrent/magnet se ainda apontam para nós
    for path, ours in ((r"Software\Classes\.torrent", PROGID_TORRENT), (r"Software\Classes\magnet", None)):
        try:
            with winreg.OpenKey(root, path) as k:
                cur, _ = winreg.QueryValueEx(k, "")
        except OSError:
            continue
        if ours and cur != ours:
            continue
        if ours is None:
            try:
                with winreg.OpenKey(root, path + r"\shell\open\command") as k:
                    cmd, _ = winreg.QueryValueEx(k, "")
                if str(LAUNCHER) not in cmd:
                    continue
            except OSError:
                continue
        delete_tree(path)
    _notify_shell()
    return "Associação removida."


def _notify_shell() -> None:
    try:
        import ctypes

        SHCNE_ASSOCCHANGED, SHCNF_IDLIST = 0x08000000, 0
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)  # type: ignore[attr-defined]
    except Exception:
        pass
