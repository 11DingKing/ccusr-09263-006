"""维护日历服务：发布共享工坊维护窗口。

发布只写入维护日历存储，不触碰预约任务状态库；
是否拒绝新预约由 :class:`BookingService` 在申请路径上判定。
"""
from __future__ import annotations

from typing import Any

from ..domain.errors import NotFoundError, ValidationError
from ..domain.models import MAINTENANCE_TITLE, MaintenanceWindow
from ..persistence.maintenance_store import (
    InMemoryMaintenanceSchedule,
    SQLiteMaintenanceSchedule,
)
from .ports import Clock, IdGenerator


class MaintenanceService:
    """发布与查询共享工坊维护窗口。"""

    def __init__(self, schedule: Any, clock: Clock, ids: IdGenerator) -> None:
        self._schedule = schedule
        self._clock = clock
        self._ids = ids

    def publish_window(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            from ..domain.models import dt_from_str

            start = dt_from_str(request.get("start"))
            end = dt_from_str(request.get("end"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"invalid maintenance window: {exc}") from exc
        if end <= start:
            raise ValidationError("maintenance window end must be after start")
        resource_id = request.get("resource_id")
        if resource_id is not None and (not isinstance(resource_id, str) or not resource_id.strip()):
            raise ValidationError("field resource_id must be null or a non-empty string")
        resource_id = resource_id.strip() if isinstance(resource_id, str) else None
        title = request.get("title", MAINTENANCE_TITLE)
        if not isinstance(title, str) or not title.strip():
            raise ValidationError("field title must be a non-empty string")
        reason = request.get("reason")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValidationError("field reason must be null or a non-empty string")
        window = MaintenanceWindow(
            window_id=self._ids.new_id("mnt"),
            resource_id=resource_id,
            title=title.strip(),
            start=start,
            end=end,
            reason=reason.strip() if isinstance(reason, str) else None,
            published_at=self._clock.now(),
        )
        self._schedule.add(window)
        return window.to_dict()

    def get_window(self, window_id: str) -> dict[str, Any]:
        window = self._schedule.get(window_id)
        if window is None:
            raise NotFoundError(
                f"maintenance window not found: {window_id}",
                details={"window_id": window_id},
            )
        return window.to_dict()

    def list_windows(self) -> list[dict[str, Any]]:
        return [w.to_dict() for w in self._schedule.list_windows()]


__all__ = [
    "MaintenanceService",
    "InMemoryMaintenanceSchedule",
    "SQLiteMaintenanceSchedule",
]
