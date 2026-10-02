"""JSON 文件持久化：原子写入，重启后完整恢复。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


COLLECTIONS = (
    "institutions",
    "instructors",
    "officers",
    "data_sources",
    "courses",
    "material_versions",
    "course_versions",
    "consents",
    "sessions",
    "enrollments",
    "certificates",
    "review_tasks",
    "remediation_tasks",
    "reminders",
)


def empty_state() -> dict[str, Any]:
    """空状态：每个集合一个映射，外加审计日志与编号计数器。"""
    state: dict[str, Any] = {name: {} for name in COLLECTIONS}
    state["audit"] = []
    state["counters"] = {}
    return state


class JsonStore:
    """把服务状态整体保存为一个 JSON 文件，写入时先落临时文件再替换。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_state()
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("状态文件内容无效")
        template = empty_state()
        missing = [key for key in template if key not in state]
        if missing:
            raise ValueError(f"状态文件缺少集合: {missing}")
        return state

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
