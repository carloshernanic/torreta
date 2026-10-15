"""Lançador sem console, usado pela associação de magnet:/.torrent no Windows.

Equivale a `python -m torreta <args>` com a pasta do projeto no sys.path.
"""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
runpy.run_module("torreta", run_name="__main__", alter_sys=True)
