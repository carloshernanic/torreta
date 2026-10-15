"""Núcleo do Torreta: um invólucro fino e cuidadoso em volta da sessão do libtorrent.

Decisões tomadas a partir da análise do uTorrent Web e do Free Download Manager
(ambos usam libtorrent por baixo):
- checa espaço em disco assim que os metadados chegam e pausa com mensagem clara,
  em vez de gerar milhares de erros "disco cheio" no log;
- grava o resume data de cada torrent num arquivo próprio, atomicamente, a cada
  minuto e no encerramento, para nunca perder o estado da sessão;
- trackers públicos só em torrents não privados, e em magnets só depois dos metadados;
- nenhum contato com servidores além de trackers/DHT/peers do próprio torrent.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import libtorrent as lt

from .config import Config

log = logging.getLogger("torreta.engine")

ESTADOS = {
    "checking_files": "verificando",
    "checking_resume_data": "verificando",
    "downloading_metadata": "buscando metadados",
    "downloading": "baixando",
    "finished": "concluído",
    "seeding": "semeando",
    "allocating": "alocando",
}

DHT_BOOTSTRAP = ",".join(
    [
        "router.bittorrent.com:6881",
        "router.utorrent.com:6881",
        "dht.transmissionbt.com:6881",
        "dht.libtorrent.org:25401",
    ]
)

PUBLIC_TRACKER_TIER = 10

# códigos de "disco cheio": Windows ERROR_DISK_FULL / HANDLE_DISK_FULL, POSIX ENOSPC
DISK_FULL_CODES = {112, 39, 28}

# SetThreadExecutionState (Windows): impede a suspensão enquanto houver download ativo
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _hash_of(h: lt.torrent_handle) -> str:
    """Identificador estável do torrent (v2 quando houver, senão v1)."""
    try:
        ih = h.info_hashes()
        best = ih.get_best() if hasattr(ih, "get_best") else (ih.v2 if ih.has_v2() else ih.v1)
        return str(best)
    except AttributeError:
        return str(h.info_hash())


def _port_free(port: int) -> bool:
    """TCP e UDP em 0.0.0.0 precisam estar livres; outro cliente (ex.: uTorrent Web) costuma segurar os dois."""
    for typ in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        s = socket.socket(socket.AF_INET, typ)
        try:
            # no Windows, sem isto o bind em 0.0.0.0 passa mesmo com outro
            # processo escutando em endereços específicos nessa porta
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def pick_listen_port(preferred: int, attempts: int = 50) -> int:
    for port in range(preferred, min(preferred + attempts, 65536)):
        if _port_free(port):
            return port
    return 0  # deixa o sistema escolher


def _is_pad_file(fs, index: int) -> bool:
    try:
        return bool(fs.file_flags(index) & lt.file_flags_t.flag_pad_file)
    except AttributeError:
        return "/.pad/" in fs.file_path(index).replace("\\", "/")


def _save_info_dict_flag():
    try:
        return lt.save_resume_flags_t.save_info_dict
    except AttributeError:
        return lt.torrent_handle.save_info_dict


def _delete_files_flag():
    try:
        return lt.options_t.delete_files
    except AttributeError:
        return lt.session.delete_files


def _enc_settings(mode: str) -> dict:
    pol = lt.enc_policy
    if mode == "none":
        return {"out_enc_policy": pol.disabled, "in_enc_policy": pol.disabled}
    if mode == "require":
        return {
            "out_enc_policy": pol.forced,
            "in_enc_policy": pol.forced,
            "allowed_enc_level": lt.enc_level.rc4,
        }
    return {"out_enc_policy": pol.enabled, "in_enc_policy": pol.enabled, "allowed_enc_level": lt.enc_level.both}


class EngineError(Exception):
    """Erro com mensagem pronta para mostrar ao usuário."""


class Engine:
    RESUME_INTERVAL = 60.0
    WATCH_INTERVAL = 5.0
    MAX_TORRENT_FILE = 20 * 1024 * 1024

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.resume_dir = cfg.data_dir / "resume"
        self.resume_dir.mkdir(parents=True, exist_ok=True)
        self.issues_path = cfg.data_dir / "issues.json"

        self._lock = threading.RLock()
        self._issues: dict[str, dict[str, str]] = self._load_issues()  # problemas (persistidos)
        self._notes: dict[str, tuple[str, str]] = {}  # avisos brandos: (tipo, mensagem)
        self._slow_since: dict[str, float] = {}
        self._awake = False
        self._snapshot: list[dict[str, Any]] = []
        self._stop = threading.Event()

        self.listen_port = pick_listen_port(cfg.listen_port)
        if self.listen_port != cfg.listen_port:
            log.warning("porta %d ocupada; usando %d", cfg.listen_port, self.listen_port)
        self.ses = lt.session(self._settings())
        self._load_resume()

        self._thread = threading.Thread(target=self._loop, name="torreta-engine", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ sessão

    def _settings(self) -> dict:
        port = self.listen_port
        s = {
            "user_agent": f"Torreta/0.1.0 libtorrent/{lt.__version__}",
            "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "dht_bootstrap_nodes": DHT_BOOTSTRAP,
            # sem isto o libtorrent anuncia só no primeiro tier que responde e os
            # trackers públicos extras nunca seriam consultados
            "announce_to_all_tiers": True,
            "announce_to_all_trackers": True,
            "download_rate_limit": self.cfg.download_limit_kib * 1024,
            "upload_rate_limit": self.cfg.upload_limit_kib * 1024,
            "alert_mask": (
                lt.alert.category_t.error_notification
                | lt.alert.category_t.status_notification
                | lt.alert.category_t.storage_notification
            ),
        }
        s.update(_enc_settings(self.cfg.encryption))
        return s

    def apply_config(self) -> None:
        """Reaplica configurações que podem mudar em tempo de execução."""
        if self.cfg.listen_port != self.listen_port and _port_free(self.cfg.listen_port):
            self.listen_port = self.cfg.listen_port
        port = self.listen_port
        s = {
            "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
            "download_rate_limit": self.cfg.download_limit_kib * 1024,
            "upload_rate_limit": self.cfg.upload_limit_kib * 1024,
        }
        s.update(_enc_settings(self.cfg.encryption))
        self.ses.apply_settings(s)

    def _load_resume(self) -> None:
        for path in sorted(self.resume_dir.glob("*.fastresume")):
            try:
                params = lt.read_resume_data(path.read_bytes())
                self.ses.async_add_torrent(params)
            except Exception as e:  # arquivo corrompido não pode derrubar o app
                log.warning("resume inválido %s: %s", path.name, e)

    def _load_issues(self) -> dict:
        try:
            return json.loads(self.issues_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _persist_issues(self) -> None:
        tmp = self.issues_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._issues, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.issues_path)

    # --------------------------------------------------------------- operações

    def _handle(self, tid: str) -> lt.torrent_handle:
        for h in self.ses.get_torrents():
            if h.is_valid() and _hash_of(h) == tid:
                return h
        raise EngineError("Torrent não encontrado.")

    def add_uri(self, uri: str, sequential: bool | None = None) -> str:
        """Aceita link magnet ou URL http(s) de um arquivo .torrent."""
        uri = uri.strip()
        low = uri.lower()
        if low.startswith("magnet:"):
            try:
                params = lt.parse_magnet_uri(uri)
            except RuntimeError as e:
                raise EngineError(f"Link magnet inválido: {e}") from e
            return self._add(params, sequential)
        if low.startswith(("http://", "https://")):
            return self.add_torrent_bytes(self._fetch(uri), sequential)
        raise EngineError("Isso não parece um link magnet nem uma URL de .torrent.")

    def _fetch(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "Torreta/0.1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read(self.MAX_TORRENT_FILE + 1)
        except urllib.error.HTTPError as e:
            raise EngineError(f"O servidor respondeu {e.code} ao baixar o .torrent.") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise EngineError(f"Não consegui baixar o .torrent: {getattr(e, 'reason', e)}") from e
        if len(data) > self.MAX_TORRENT_FILE:
            raise EngineError("Esse arquivo é grande demais para ser um .torrent.")
        return data

    def add_torrent_bytes(self, data: bytes, sequential: bool | None = None) -> str:
        try:
            if hasattr(lt, "load_torrent_buffer"):
                params = lt.load_torrent_buffer(data)
            else:
                params = lt.add_torrent_params()
                params.ti = lt.torrent_info(lt.bdecode(data))
        except RuntimeError as e:
            raise EngineError("Isso não é um arquivo .torrent válido.") from e
        return self._add(params, sequential)

    def _add(self, params, sequential: bool | None) -> str:
        if sequential is None:
            sequential = self.cfg.sequential_default
        params.save_path = self.cfg.download_dir
        Path(params.save_path).mkdir(parents=True, exist_ok=True)
        params.flags |= lt.torrent_flags.auto_managed | lt.torrent_flags.duplicate_is_error
        if sequential:
            params.flags |= lt.torrent_flags.sequential_download
        try:
            h = self.ses.add_torrent(params)
        except RuntimeError as e:
            msg = str(e)
            if "duplicate" in msg.lower() or "already exists" in msg.lower():
                raise EngineError("Esse torrent já está na lista.") from e
            raise EngineError(f"Não foi possível adicionar: {msg}") from e
        tid = _hash_of(h)
        if h.status().has_metadata:
            self._check_space(h, tid)
            self._add_public_trackers(h)
        h.save_resume_data(_save_info_dict_flag())
        self._refresh_snapshot()
        return tid

    def _add_public_trackers(self, h: lt.torrent_handle) -> int:
        """Só com metadados em mãos (para saber se é privado). Idempotente: o libtorrent ignora URL repetida."""
        if not self.cfg.extra_trackers:
            return 0
        ti = h.torrent_file()
        if ti is None or ti.priv():
            return 0
        for url in self.cfg.tracker_list:
            h.add_tracker({"url": url, "tier": PUBLIC_TRACKER_TIER})
        return len(self.cfg.tracker_list)

    def apply_extra_trackers_to_all(self) -> None:
        for h in self.ses.get_torrents():
            if h.is_valid():
                self._add_public_trackers(h)

    def pause(self, tid: str) -> None:
        h = self._handle(tid)
        h.unset_flags(lt.torrent_flags.auto_managed)
        h.pause()
        self._refresh_snapshot()

    def resume(self, tid: str) -> None:
        h = self._handle(tid)
        with self._lock:
            self._issues.pop(tid, None)
            self._notes.pop(tid, None)
            self._persist_issues()
        h.clear_error()
        if not self._check_space(h, tid):
            self._refresh_snapshot()
            raise EngineError(self._issues[tid]["message"])
        h.set_flags(lt.torrent_flags.auto_managed)
        h.resume()
        self._refresh_snapshot()

    def recheck(self, tid: str) -> None:
        self._handle(tid).force_recheck()

    def reannounce(self, tid: str) -> None:
        """Pede aos trackers e ao DHT uma nova lista de peers agora, sem esperar o intervalo."""
        h = self._handle(tid)
        h.force_reannounce()
        h.force_dht_announce()

    def set_sequential(self, tid: str, on: bool) -> None:
        h = self._handle(tid)
        if on:
            h.set_flags(lt.torrent_flags.sequential_download)
        else:
            h.unset_flags(lt.torrent_flags.sequential_download)
        self._refresh_snapshot()

    def remove(self, tid: str, delete_files: bool = False) -> None:
        h = self._handle(tid)
        self.ses.remove_torrent(h, _delete_files_flag() if delete_files else 0)
        with self._lock:
            self._issues.pop(tid, None)
            self._notes.pop(tid, None)
            self._slow_since.pop(tid, None)
            self._persist_issues()
        for p in (self.resume_dir / f"{tid}.fastresume", self.resume_dir / f"{tid}.fastresume.tmp"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self._refresh_snapshot()

    def open_folder(self, tid: str) -> None:
        st = self._handle(tid).status()
        target = Path(st.save_path) / st.name if st.name else Path(st.save_path)
        if not target.exists():
            target = Path(st.save_path)
        if sys.platform == "win32":
            if target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target if target.is_dir() else target.parent)])
        else:
            subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])

    def files(self, tid: str) -> list[dict[str, Any]]:
        h = self._handle(tid)
        ti = h.torrent_file()
        if ti is None:
            return []
        fs = ti.files()
        progress = h.file_progress()
        prios = h.get_file_priorities()
        out = []
        for i in range(fs.num_files()):
            if _is_pad_file(fs, i):  # preenchimento BEP 47: não é um arquivo do usuário
                continue
            size = fs.file_size(i)
            out.append(
                {
                    "index": i,
                    "path": fs.file_path(i),
                    "size": size,
                    "done": progress[i] if i < len(progress) else 0,
                    "priority": int(prios[i]) if i < len(prios) else 4,
                }
            )
        return out

    def set_file_priorities(self, tid: str, priorities: dict[int, int]) -> None:
        h = self._handle(tid)
        ti = h.torrent_file()
        if ti is None:
            raise EngineError("Ainda não há metadados para escolher arquivos.")
        fs = ti.files()
        n = fs.num_files()
        current = list(h.get_file_priorities())
        for idx, prio in priorities.items():
            if 0 <= idx < n and not _is_pad_file(fs, idx):
                current[idx] = max(0, min(7, int(prio)))
        if not any(p for i, p in enumerate(current) if not _is_pad_file(fs, i)):
            raise EngineError("Pelo menos um arquivo precisa ficar selecionado.")
        h.prioritize_files(current)
        # a quantidade de dados desejada mudou: refaz a checagem de espaço
        time.sleep(0.05)
        self._check_space(h, tid)
        self._refresh_snapshot()

    # ----------------------------------------------------------------- leitura

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._snapshot)

    def _status_dict(self, h: lt.torrent_handle) -> dict[str, Any]:
        st = h.status()
        tid = _hash_of(h)
        remaining = max(0, st.total_wanted - st.total_wanted_done)
        paused = bool(st.flags & lt.torrent_flags.paused)
        # a taxa do libtorrent é uma média que decai; pausado é zero de verdade
        rate = 0 if paused else st.download_payload_rate
        up_rate = 0 if paused else st.upload_payload_rate
        eta = int(remaining / rate) if rate > 0 and remaining > 0 else None
        state = ESTADOS.get(st.state.name, st.state.name)
        error = st.errc.message() if st.errc.value() else None
        issue = self._issues.get(tid)
        note = self._notes.get(tid)
        if paused:
            state = "pausado"
        if issue or error:
            state = "problema"
        return {
            "id": tid,
            "name": st.name or f"magnet {tid[:12]}…",
            "state": state,
            "progress": round(st.progress, 4),
            "size": st.total_wanted,
            "done": st.total_wanted_done,
            "down_rate": rate,
            "up_rate": up_rate,
            "peers": st.num_peers,
            "seeds": st.num_seeds,
            "eta": eta,
            "paused": paused,
            "finished": st.is_finished,
            "sequential": bool(st.flags & lt.torrent_flags.sequential_download),
            "has_metadata": st.has_metadata,
            "save_path": st.save_path,
            "uploaded": st.all_time_upload,
            "downloaded": st.all_time_download,
            "trackers": len(h.trackers()),
            "added_on": st.added_time,
            "issue": (issue or {}).get("message") or error,
            "note": note[1] if note else None,
        }

    def _refresh_snapshot(self) -> None:
        rows = []
        for h in self.ses.get_torrents():
            if not h.is_valid():
                continue
            try:
                rows.append(self._status_dict(h))
            except RuntimeError:  # handle ficou inválido no meio do caminho
                continue
        rows.sort(key=lambda r: r["added_on"], reverse=True)
        with self._lock:
            self._snapshot = rows

    # ------------------------------------------------------------- espaço/erros

    def _set_issue(self, tid: str, kind: str, message: str) -> None:
        with self._lock:
            self._issues[tid] = {"kind": kind, "message": message}
            self._persist_issues()

    def _set_note(self, tid: str, kind: str, message: str | None) -> None:
        with self._lock:
            if message is None:
                if self._notes.get(tid, ("",))[0] == kind:
                    self._notes.pop(tid, None)
            else:
                self._notes[tid] = (kind, message)

    def _check_space(self, h: lt.torrent_handle, tid: str) -> bool:
        """Garante que cabe no disco; se não couber, pausa com explicação. True = ok."""
        st = h.status()
        if not st.has_metadata:
            return True
        remaining = max(0, st.total_wanted - st.total_wanted_done)
        try:
            free = shutil.disk_usage(st.save_path).free
        except OSError:
            return True
        if remaining > free:
            drive = os.path.splitdrive(st.save_path)[0] or st.save_path
            self._set_issue(
                tid,
                "space",
                f"Espaço insuficiente em {drive}: faltam {fmt_bytes(remaining - free)} "
                f"(precisa de {fmt_bytes(remaining)}, livre {fmt_bytes(free)}). "
                "Libere espaço ou desmarque arquivos e retome.",
            )
            h.unset_flags(lt.torrent_flags.auto_managed)
            h.pause()
            return False
        with self._lock:
            if self._issues.get(tid, {}).get("kind") == "space":
                self._issues.pop(tid)
                self._persist_issues()
        return True

    # ------------------------------------------------------------------- loop

    def _loop(self) -> None:
        last_snapshot = 0.0
        last_resume = time.monotonic()
        last_watch = 0.0
        while not self._stop.is_set():
            self.ses.wait_for_alert(500)
            self._handle_alerts()
            now = time.monotonic()
            if now - last_snapshot >= 1.0:
                try:
                    self._housekeeping()
                except Exception as e:  # nunca derrubar o loop por um aviso
                    log.debug("housekeeping: %s", e)
                self._refresh_snapshot()
                last_snapshot = now
            if now - last_resume >= self.RESUME_INTERVAL:
                self._request_resume_all(only_dirty=True)
                last_resume = now
            if now - last_watch >= self.WATCH_INTERVAL:
                self._scan_watch_dir()
                last_watch = now
        self._set_keep_awake(False)

    def _housekeeping(self) -> None:
        """Limite de proporção, aviso de lentidão e bloqueio de suspensão — uma passada por segundo."""
        downloading = False
        now = time.monotonic()
        for h in self.ses.get_torrents():
            if not h.is_valid():
                continue
            st = h.status()
            tid = _hash_of(h)
            paused = bool(st.flags & lt.torrent_flags.paused)

            # para de semear ao atingir a proporção configurada
            limit = self.cfg.seed_ratio_limit
            if limit > 0 and st.is_finished and not paused and st.all_time_download > 0:
                ratio = st.all_time_upload / st.all_time_download
                if ratio >= limit:
                    h.unset_flags(lt.torrent_flags.auto_managed)
                    h.pause()
                    self._set_note(tid, "ratio", f"Parou de semear: proporção {ratio:.2f} atingiu o limite de {limit:g}.")
                    continue

            active = st.has_metadata and not paused and not st.is_finished and st.state.name == "downloading"
            if active:
                downloading = True

            # aviso (sem pausar) de download lento ou parado
            if self.cfg.slow_detect and active:
                if st.download_payload_rate < self.cfg.slow_speed_kib * 1024:
                    since = self._slow_since.setdefault(tid, now)
                    elapsed = now - since
                    if elapsed >= self.cfg.slow_interval_s:
                        why = "nenhum seed conectado" if st.num_seeds == 0 else f"só {st.num_seeds} seed(s)"
                        self._set_note(
                            tid, "slow",
                            f"Lento: abaixo de {self.cfg.slow_speed_kib} KiB/s há {int(elapsed // 60)} min ({why}). "
                            "Tente Reanunciar ou aguarde mais peers.",
                        )
                else:
                    self._slow_since.pop(tid, None)
                    self._set_note(tid, "slow", None)
            else:
                self._slow_since.pop(tid, None)
                self._set_note(tid, "slow", None)

        self._set_keep_awake(self.cfg.prevent_sleep and downloading)

    def _set_keep_awake(self, on: bool) -> None:
        if sys.platform != "win32" or on == self._awake:
            return
        try:
            import ctypes

            flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
            ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]
            self._awake = on
        except Exception as e:
            log.debug("SetThreadExecutionState: %s", e)

    def _scan_watch_dir(self) -> None:
        d = self.cfg.watch_dir
        if not d:
            return
        folder = Path(d)
        if not folder.is_dir():
            return
        done = folder / "adicionados"
        for f in list(folder.glob("*.torrent")):
            try:
                data = f.read_bytes()
            except OSError:
                continue  # provavelmente ainda sendo gravado pelo navegador
            try:
                self.add_torrent_bytes(data)
                log.info("pasta vigiada: adicionado %s", f.name)
            except EngineError as e:
                log.warning("pasta vigiada: %s ignorado (%s)", f.name, e)
            try:
                done.mkdir(exist_ok=True)
                f.replace(done / f.name)
            except OSError as e:
                log.warning("pasta vigiada: não consegui mover %s: %s", f.name, e)

    def _handle_alerts(self) -> int:
        saved = 0
        for a in self.ses.pop_alerts():
            try:
                if isinstance(a, lt.save_resume_data_alert):
                    self._write_resume(a)
                    saved += 1
                elif isinstance(a, lt.save_resume_data_failed_alert):
                    saved += 1
                    if "not modified" not in a.message().lower():
                        log.warning("resume falhou: %s", a.message())
                elif isinstance(a, lt.metadata_received_alert):
                    tid = _hash_of(a.handle)
                    self._check_space(a.handle, tid)
                    self._add_public_trackers(a.handle)
                    a.handle.save_resume_data(_save_info_dict_flag())
                elif isinstance(a, lt.file_error_alert):
                    self._on_file_error(a)
                elif isinstance(a, lt.torrent_finished_alert):
                    a.handle.save_resume_data(_save_info_dict_flag())
                elif isinstance(a, lt.add_torrent_alert):
                    if a.error.value():
                        log.warning("falha ao adicionar: %s", a.error.message())
                    else:  # torrents restaurados do resume data na abertura
                        self._add_public_trackers(a.handle)
                elif isinstance(a, lt.listen_failed_alert):
                    log.warning("escuta falhou: %s", a.message())
            except RuntimeError as e:
                log.debug("alerta ignorado: %s", e)
        return saved

    def _on_file_error(self, a) -> None:
        tid = _hash_of(a.handle)
        if self._issues.get(tid):  # já avisamos; não repetir
            return
        code = a.error.value()
        name = os.path.basename(a.filename()) if hasattr(a, "filename") else ""
        if code in DISK_FULL_CODES:
            msg = f"Disco cheio ao gravar '{name}'. Libere espaço e retome o download."
        else:
            msg = f"Erro ao acessar '{name}': {a.error.message()}. Verifique a pasta e retome."
        self._set_issue(tid, "file", msg)
        a.handle.unset_flags(lt.torrent_flags.auto_managed)
        a.handle.pause()

    def _write_resume(self, a) -> None:
        tid = _hash_of(a.handle)
        data = lt.write_resume_data_buf(a.params)
        final = self.resume_dir / f"{tid}.fastresume"
        tmp = final.with_suffix(".fastresume.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, final)

    def _request_resume_all(self, only_dirty: bool) -> int:
        n = 0
        for h in self.ses.get_torrents():
            if not h.is_valid():
                continue
            if only_dirty and hasattr(h, "need_save_resume_data") and not h.need_save_resume_data():
                continue
            h.save_resume_data(_save_info_dict_flag())
            n += 1
        return n

    def shutdown(self, timeout: float = 10.0) -> None:
        """Para o loop, grava o resume data de tudo e só então encerra a sessão."""
        self._stop.set()
        self._thread.join(3)
        self.ses.pause()
        pending = self._request_resume_all(only_dirty=False)
        deadline = time.monotonic() + timeout
        while pending > 0 and time.monotonic() < deadline:
            self.ses.wait_for_alert(500)
            pending -= self._handle_alerts()
        log.info("sessão encerrada (%d resume pendentes)", max(0, pending))
