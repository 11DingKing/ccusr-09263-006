"""共享工坊维护窗口服务：发布维护排期并判定新预约是否被拒。

维护窗口与预约任务状态分开存储：本服务持有独立的存储后端（独立 SQLite
文件），不读写预约集合，因此维护排期的发布/重启恢复与任务状态互不影响。

语义约定：
- 窗口一经发布即生效，只用于拒绝与其时段重叠的“新预约”；
- 不取消、不删除、不强制改期任何既有任务，进行中的任务始终保持可查询。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..domain.errors import NotFoundError, ValidationError
from ..domain.models import MaintenanceWindow, dt_from_str
from ..persistence.store import Store
from .ports import Clock, IdGenerator

COLLECTION_MAINTENANCE = "maintenance_windows"


def _require_str(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"field {field} must be a non-empty string", details={"field": field})
    return value.strip()


class MaintenanceService:
    """维护排期的发布、查询与新预约准入判定。"""

    def __init__(self, store: Store, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    def publish(self, request: dict[str, Any]) -> dict[str, Any]:
        """发布一个共享工坊维护窗口。"""
        title = _require_str(request, "title")
        try:
            start = dt_from_str(request.get("start"))
            end = dt_from_str(request.get("end"))
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"invalid maintenance window: {exc}") from exc
        if end <= start:
            raise ValidationError("maintenance window end must be after start")
        reason = request.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValidationError("field reason must be a string or null")
        window = MaintenanceWindow(
            maintenance_id=self._ids.new_id("mnt"),
            title=title,
            start=start,
            end=end,
            created_at=self._clock.now(),
            reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
        )
        with self._store.transaction():
            self._store.put(COLLECTION_MAINTENANCE, window.maintenance_id, window.to_dict())
        return window.to_dict()

    def list_windows(self) -> list[dict[str, Any]]:
        records = self._store.query(COLLECTION_MAINTENANCE)
        records.sort(key=lambda r: (r["start"], r["maintenance_id"]))
        return records

    def get_window(self, maintenance_id: str) -> dict[str, Any]:
        record = self._store.get(COLLECTION_MAINTENANCE, maintenance_id)
        if record is None:
            raise NotFoundError(
                f"maintenance window not found: {maintenance_id}",
                details={"maintenance_id": maintenance_id},
            )
        return record

    def find_blocking_window(
        self, slot_start: datetime, slot_end: datetime, *, at: datetime | None = None
    ) -> MaintenanceWindow | None:
        """返回与给定课程时段重叠的、在 ``at`` 之前已发布的维护窗口；无则 ``None``。

        传入 ``at``（通常是新预约的申请时刻）时，仅统计发布时间不晚于该时刻的窗口，
        从而准确表达“窗口发布之后的新预约才被拒”；为 ``None`` 时视为全部已发布。
        """
        for record in self._store.query(COLLECTION_MAINTENANCE):
            window = MaintenanceWindow.from_dict(record)
            if at is not None and window.created_at > at:
                continue
            if window.overlaps(slot_start, slot_end):
                return window
        return None

    def is_blocked(self, slot_start: datetime, slot_end: datetime, *, at: datetime | None = None) -> bool:
        return self.find_blocking_window(slot_start, slot_end, at=at) is not None
