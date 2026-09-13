"""读取命令工作台的独立 HTML 静态资源。"""

from __future__ import annotations

from pathlib import Path


STATIC_FILE = Path(__file__).with_name("static") / "index.html"


def get_index_html() -> str:
    """读取命令工作台单页 HTML。"""

    return STATIC_FILE.read_text(encoding="utf-8")


__all__ = ["STATIC_FILE", "get_index_html"]
