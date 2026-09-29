"""维护日历存储：与预约任务状态物理分离。

预约任务状态落在 ``booking.db`` 的文档集合中；维护窗口落在独立的
``maintenance.db``（独立连接、独立专用表）。发布维护窗口不会触碰
预约库，既有任务不被删除或强制改状态。

预约服务在“新预约”路径上只读本端口，两者之间不存在跨库写事务。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ..domain.models import MaintenanceWindow
from ..domain.rules import maintenance_blocks

_SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_windows (
    window_id    TEXT PRIMARY KEY,
    resource_id  TEXT,
    title        TEXT NOT NULL,
    start_ts     TEXT NOT NULL,
    end_ts       TEXT NOT NULL,
    reason       TEXT,
    published_at TEXT NOT NULL,
    data         TEXT NOT NULL
);
"""


class MaintenanceSchedule(Protocol):
    """维护日历只读端口：预约服务仅依赖这一个查询方法。"""

    def find_overlap(
        self, slot_start: datetime, slot_end: datetime, resource_id: str
    ) -> MaintenanceWindow | None:
        """返回阻断该候选时段的维护窗口（全工坊或同资源且时段重叠）。"""
        ...

    def list_windows(self) -> list[MaintenanceWindow]:
        ...


class InMemoryMaintenanceSchedule:
    """进程内维护日历：测试与演示用。"""

    def __init__(self) -> None:
        self._windows: dict[str, MaintenanceWindow] = {}
        self._lock = threading.RLock()

    def add(self, window: MaintenanceWindow) -> None:
        with self._lock:
            self._windows[window.window_id] = window

    def get(self, window_id: str) -> MaintenanceWindow | None:
        with self._lock:
            return self._windows.get(window_id)

    def list_windows(self) -> list[MaintenanceWindow]:
        with self._lock:
            return list(self._windows.values())

    def find_overlap(
        self, slot_start: datetime, slot_end: datetime, resource_id: str
    ) -> MaintenanceWindow | None:
        with self._lock:
            windows = list(self._windows.values())
        for window in windows:
            if maintenance_blocks(window, slot_start, slot_end, resource_id):
                return window
        return None


class SQLiteMaintenanceSchedule:
    """SQLite 维护日历：独立库文件与专用表，与任务状态库分开存放。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if self._path != Path(":memory:"):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()

    def add(self, window: MaintenanceWindow) -> None:
        payload = json.dumps(window.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._conn.execute(
                "INSERT INTO maintenance_windows "
                "(window_id, resource_id, title, start_ts, end_ts, reason, published_at, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(window_id) DO UPDATE SET "
                "resource_id=excluded.resource_id, title=excluded.title, start_ts=excluded.start_ts, "
                "end_ts=excluded.end_ts, reason=excluded.reason, published_at=excluded.published_at, "
                "data=excluded.data",
                (
                    window.window_id,
                    window.resource_id,
                    window.title,
                    window.start.isoformat(),
                    window.end.isoformat(),
                    window.reason,
                    window.published_at.isoformat(),
                    payload,
                ),
            )
            self._conn.commit()

    def get(self, window_id: str) -> MaintenanceWindow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM maintenance_windows WHERE window_id = ?", (window_id,)
            ).fetchone()
        return MaintenanceWindow.from_dict(json.loads(row["data"])) if row else None

    def list_windows(self) -> list[MaintenanceWindow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM maintenance_windows ORDER BY start_ts, window_id"
            ).fetchall()
        return [MaintenanceWindow.from_dict(json.loads(row["data"])) for row in rows]

    def find_overlap(
        self, slot_start: datetime, slot_end: datetime, resource_id: str
    ) -> MaintenanceWindow | None:
        # 半开区间重叠下推到 SQL：全工坊窗口（resource_id IS NULL）或同资源窗口。
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM maintenance_windows "
                "WHERE (resource_id IS NULL OR resource_id = ?) "
                "AND start_ts < ? AND end_ts > ? "
                "ORDER BY start_ts, window_id",
                (
                    resource_id,
                    slot_end.isoformat(),
                    slot_start.isoformat(),
                ),
            ).fetchall()
        windows = [MaintenanceWindow.from_dict(json.loads(row["data"])) for row in rows]
        return windows[0] if windows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
