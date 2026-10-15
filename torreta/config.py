"""Configuração persistida em %APPDATA%\\Torreta\\config.json."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def default_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Torreta"


def default_download_dir() -> str:
    return str(Path.home() / "Downloads" / "Torreta")


@dataclass
class Config:
    download_dir: str = field(default_factory=default_download_dir)
    listen_port: int = 6881
    download_limit_kib: int = 0  # 0 = ilimitado
    upload_limit_kib: int = 0  # 0 = ilimitado
    sequential_default: bool = False
    extra_trackers: bool = True  # trackers públicos em torrents não privados
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
                value = str(value).strip()
                if not value:
                    raise ValueError("A pasta de downloads não pode ficar vazia.")
                try:
                    Path(value).mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    raise ValueError(f"Não foi possível usar a pasta '{value}': {e.strerror}") from e
            elif key == "listen_port":
                value = int(value)
                if not 1024 <= value <= 65535:
                    raise ValueError("A porta precisa estar entre 1024 e 65535.")
            elif key in ("download_limit_kib", "upload_limit_kib"):
                value = max(0, int(value))
            elif key in ("sequential_default", "extra_trackers"):
                value = bool(value)
            setattr(self, key, value)

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)
