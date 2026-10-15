"""Ponto de entrada: `python -m torreta [--no-window] [magnet|arquivo.torrent ...]`."""
from __future__ import annotations

import argparse
import logging
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from .config import Config
from .engine import Engine, EngineError
from .server import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _add_cli_items(engine: Engine, items: list[str]) -> None:
    for item in items:
        try:
            if item.lower().startswith(("magnet:", "http://", "https://")):
                engine.add_uri(item)
            else:
                engine.add_torrent_bytes(Path(item).read_bytes())
        except (EngineError, OSError) as e:
            logging.getLogger("torreta").warning("não adicionado '%s': %s", item, e)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="torreta", description="Cliente torrent simples, sem anúncios.")
    parser.add_argument("items", nargs="*", help="links magnet ou arquivos .torrent para adicionar")
    parser.add_argument("--no-window", action="store_true", help="abre no navegador em vez da janela nativa")
    parser.add_argument("--port", type=int, default=0, help="porta da interface local (padrão: aleatória)")
    parser.add_argument("--no-browser", action="store_true", help="com --no-window, não abre o navegador (só imprime a URL)")
    parser.add_argument("--token", default=None, help="token fixo da interface (padrão: aleatório)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("torreta")

    cfg = Config.load()
    engine = Engine(cfg)
    _add_cli_items(engine, args.items)

    token = args.token or secrets.token_urlsafe(24)
    port = args.port or _free_port()
    url = f"http://127.0.0.1:{port}/?token={token}"

    window = None
    pick_folder = None
    if not args.no_window:
        try:
            import webview  # importado aqui para o modo --no-window não exigir pywebview

            def pick_folder():  # noqa: F811 - só existe quando há janela
                res = window.create_file_dialog(webview.FOLDER_DIALOG, directory=cfg.download_dir)
                return res[0] if res else None

        except ImportError:
            log.warning("pywebview não instalado; abrindo no navegador")
            args.no_window = True

    app = create_app(engine, token, pick_folder if not args.no_window else None)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, name="torreta-http", daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    log.info("interface em %s", url)

    try:
        if args.no_window:
            if not args.no_browser:
                webbrowser.open(url)
            while True:
                time.sleep(1)
        else:
            import webview

            window = webview.create_window(
                "Torreta", url, width=1120, height=740, min_size=(820, 520), text_select=True
            )
            webview.start()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("encerrando…")
        server.should_exit = True
        engine.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
