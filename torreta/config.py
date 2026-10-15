"""Configuração persistida em %APPDATA%\\Torreta\\config.json."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

# trackers públicos estáveis, adicionados a torrents NÃO privados para achar mais peers
# (mesma prática do qBittorrent e do FDM; torrents privados nunca recebem trackers extras)
PUBLIC_TRACKERS = [  # verificados respondendo em 2026-10
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://explodie.org:6969/announce",
]

ENCRYPTION_MODES = ("none", "prefer", "require")


def default_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Torreta"


def default_download_dir() -> str:
    return str(Path.home() / "Downloads" / "Torreta")


def _ensure_dir(value: str, label: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError(f"{label} não pode ficar vazia.")
    try:
        Path(value).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"Não foi possível usar a pasta '{value}': {e.strerror}") from e
    return value


@dataclass
class Config:
    download_dir: str = field(default_factory=default_download_dir)
    listen_port: int = 6881
    download_limit_kib: int = 0  # 0 = ilimitado
    upload_limit_kib: int = 0  # 0 = ilimitado
    sequential_default: bool = False
    extra_trackers: bool = True  # trackers públicos em torrents não privados
    tracker_list: list[str] = field(default_factory=lambda: list(PUBLIC_TRACKERS))
    encryption: str = "prefer"  # none | prefer | require
    seed_ratio_limit: float = 0.0  # 0 = semeia para sempre
    slow_detect: bool = True
    slow_speed_kib: int = 10
    slow_interval_s: int = 300
    watch_dir: str = ""  # pasta vigiada por .torrent novos ("" = desligado)
    prevent_sleep: bool = True
    data_dir: Path = field(default_factory=default_data_dir)

    # campos que não vão para o JSON
    _TRANSIENT = ("data_dir",)

    @property
    def path(self) -> Path:
        return self.data_dir / "config.json"

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "Config":
        cfg = cls()
        if data_dir:
            cfg.data_dir = data_dir
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if cfg.path.exists():
            try:
                raw = json.loads(cfg.path.read_text("utf-8"))
                cfg.update(**raw)
            except (OSError, ValueError, TypeError):
                pass
        return cfg

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in self._TRANSIENT:
            d.pop(k, None)
        return d

    def update(self, **changes) -> None:
        """Aplica mudanças validadas. Levanta ValueError com mensagem em pt-BR."""
        known = {f.name for f in fields(self)} - set(self._TRANSIENT)
        for key, value in changes.items():
            if key not in known:
                continue
            if key == "download_dir":
                value = _ensure_dir(value, "A pasta de downloads")
            elif key == "watch_dir":
                value = _ensure_dir(value, "A pasta vigiada") if str(value).strip() else ""
            elif key == "listen_port":
                value = int(value)
                if not 1024 <= value <= 65535:
                    raise ValueError("A porta precisa estar entre 1024 e 65535.")
            elif key in ("download_limit_kib", "upload_limit_kib"):
                value = max(0, int(value))
            elif key in ("sequential_default", "extra_trackers", "slow_detect", "prevent_sleep"):
                value = bool(value)
            elif key == "encryption":
                value = str(value).lower()
                if value not in ENCRYPTION_MODES:
                    raise ValueError("Criptografia precisa ser 'none', 'prefer' ou 'require'.")
            elif key == "tracker_list":
                if isinstance(value, str):
                    value = value.splitlines()
                cleaned: list[str] = []
                for url in value:
                    url = str(url).strip()
                    if not url:
                        continue
                    if not url.lower().startswith(("udp://", "http://", "https://", "ws://", "wss://")):
                        raise ValueError(f"Tracker inválido: '{url}' (use udp://, http:// ou https://).")
                    if url not in cleaned:
                        cleaned.append(url)
                value = cleaned
            elif key == "seed_ratio_limit":
                value = max(0.0, float(value))
            elif key == "slow_speed_kib":
                value = max(1, int(value))
            elif key == "slow_interval_s":
                value = max(30, int(value))
            setattr(self, key, value)

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)
