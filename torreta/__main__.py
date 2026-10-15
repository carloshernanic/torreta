"""Ponto de entrada: `python -m torreta [--no-window] [magnet|arquivo.torrent|URL ...]`."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from . import integration
from .config import Config
from .engine import Engine, EngineError
from .server import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _setup_logging(verbose: bool, data_dir: Path) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:  # com pythonw não há console
        handlers.append(logging.StreamHandler())
    handlers.append(logging.handlers.RotatingFileHandler(data_dir / "torreta.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


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
    parser.add_argument("items", nargs="*", help="links magnet, URLs ou arquivos .torrent para adicionar")
    parser.add_argument("--no-window", action="store_true", help="abre no navegador em vez da janela nativa")
    parser.add_argument("--port", type=int, default=0, help="porta da interface local (padrão: aleatória)")
    parser.add_argument("--no-browser", action="store_true", help="com --no-window, não abre o navegador (só imprime a URL)")
    parser.add_argument("--token", default=None, help="token fixo da interface (padrão: aleatório)")
    parser.add_argument("--register", action="store_true", help="associa magnet: e .torrent ao Torreta (Windows) e sai")
    parser.add_argument("--unregister", action="store_true", help="remove a associação e sai")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    cfg = Config.load()
    _setup_logging(args.verbose, cfg.data_dir)
    log = logging.getLogger("torreta")

    if args.register or args.unregister:
        try:
            print(integration.register_windows() if args.register else integration.unregister_windows())
            return 0
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 1

    # instância única: se já há um Torreta aberto, entrega os itens a ele
    if integration.forward_to_running(cfg.data_dir, args.items):
        log.info("Torreta já aberto; itens encaminhados")
        return 0

    engine = Engine(cfg)
    _add_cli_items(engine, args.items)

    token = args.token or secrets.token_urlsafe(24)
    port = args.port or _free_port()
    url = f"http://127.0.0.1:{port}/?token={token}"

    window = None
    pick_folder = None
    focus = None
    if not args.no_window:
        try:
            import webview  # importado aqui para o modo --no-window não exigir pywebview

            def pick_folder():  # noqa: F811 - só existe quando há janela
                res = window.create_file_dialog(webview.FOLDER_DIALOG, directory=cfg.download_dir)
                return res[0] if res else None

            def focus():  # noqa: F811
                if window is None:
                    return
                try:
                    window.restore()
                    window.show()
                    window.on_top = True
                    window.on_top = False
                except Exception as e:  # trazer à frente é cortesia, não requisito
                    log.debug("focus: %s", e)

        except ImportError:
            log.warning("pywebview não instalado; abrindo no navegador")
            args.no_window = True

    app = create_app(engine, token, pick_folder if not args.no_window else None, focus if not args.no_window else None)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, name="torreta-http", daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    integration.write_instance(cfg.data_dir, port, token)
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
        integration.remove_instance(cfg.data_dir)
        server.should_exit = True
        engine.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
