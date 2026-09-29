"""共享工坊维护窗口：发布后拒绝新预约，进行中任务保持可查询且不被删除。

覆盖：
- 发布维护窗口（默认命名“共享工坊维护窗口”），重叠新预约被拒绝；
- 发布前已在进行（已发运）的任务仍可查询、状态不变、不被强制删除；
- 非重叠时段与不相关资源不受影响；
- 维护日历与任务状态分库存放（独立 SQLite 文件与专用表），重启后仍生效。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from service_09252_008.application.booking_service import BookingService
from service_09252_008.application.catalog_service import CatalogService
from service_09252_008.application.maintenance_service import MaintenanceService
from service_09252_008.application.ports import ManualClock, SequentialIdGenerator
from service_09252_008.domain.errors import MaintenanceBlockedError
from service_09252_008.domain.models import MAINTENANCE_TITLE
from service_09252_008.persistence.maintenance_store import SQLiteMaintenanceSchedule
from service_09252_008.persistence.sqlite_store import SQLiteStore
from tests.helpers import (
    NOW,
    apply_payload,
    make_services_with_maintenance,
    seed_catalog,
)

# 与标准时段 02:00-04:00 UTC 重叠
OVERLAPPING_WINDOW = {
    "start": "2026-10-01T03:00:00+00:00",
    "end": "2026-10-01T05:00:00+00:00",
}
# 与标准时段不重叠（窗口起点恰为时段终点，半开区间不相交）
ADJACENT_WINDOW = {
    "start": "2026-10-01T04:00:00+00:00",
    "end": "2026-10-01T05:00:00+00:00",
}


def drive_to_shipped(bookings: BookingService, ids: dict, key: str) -> str:
    applied = bookings.apply(apply_payload(ids, f"{key}-apply"))
    booking_id = applied["booking_id"]
    bookings.quote(booking_id)
    bookings.lock(booking_id, {"idempotency_key": f"{key}-lock"})
    bookings.ship(booking_id, {"idempotency_key": f"{key}-ship"})
    return booking_id


class MaintenanceWindowServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.maintenance, self.schedule, self.clock, self.store = (
            make_services_with_maintenance()
        )
        self.ids = seed_catalog(self.catalog)

    def test_publish_uses_shared_workshop_title_by_default(self) -> None:
        window = self.maintenance.publish_window(OVERLAPPING_WINDOW)
        self.assertEqual(window["title"], MAINTENANCE_TITLE)
        self.assertEqual(window["title"], "共享工坊维护窗口")
        self.assertIsNone(window["resource_id"])
        self.assertEqual(len(self.maintenance.list_windows()), 1)

    def test_overlapping_new_booking_is_rejected_after_publication(self) -> None:
        # 发布前可以预约
        before = self.bookings.apply(apply_payload(self.ids, "k-before"))
        self.assertEqual(before["status"], "REQUESTED")

        self.maintenance.publish_window(OVERLAPPING_WINDOW)

        with self.assertRaises(MaintenanceBlockedError) as ctx:
            self.bookings.apply(apply_payload(self.ids, "k-after"))
        self.assertEqual(ctx.exception.code, "maintenance_window_blocked")
        self.assertEqual(ctx.exception.details["maintenance_window_id"], "mnt_0001")

    def test_in_progress_task_remains_queryable_and_is_not_deleted(self) -> None:
        booking_id = drive_to_shipped(self.bookings, self.ids, "k-prog")
        bookings_before = len(self.store.query("bookings"))

        self.maintenance.publish_window(OVERLAPPING_WINDOW)

        # 预约记录仍在，状态不变，可查询（含发运单视图）
        self.assertEqual(len(self.store.query("bookings")), bookings_before)
        view = self.bookings.get_booking(booking_id)
        self.assertEqual(view["status"], "SHIPPED")
        self.assertEqual(len(view["shipments"]), 2)

        # 维护窗口只存在于维护日历，未混入任务状态集合
        self.assertEqual(self.store.query("maintenance_windows"), [])
        self.assertEqual(len(self.schedule.list_windows()), 1)

        # 窗口内的新预约仍被拒绝
        with self.assertRaises(MaintenanceBlockedError):
            self.bookings.apply(apply_payload(self.ids, "k-blocked"))

    def test_non_overlapping_slot_is_still_accepted(self) -> None:
        self.maintenance.publish_window(ADJACENT_WINDOW)
        applied = self.bookings.apply(apply_payload(self.ids, "k-adjacent"))
        self.assertEqual(applied["status"], "REQUESTED")

    def test_resource_scoped_window_does_not_block_other_resources(self) -> None:
        other = self.catalog.create_resource(
            {
                "name": "染整工坊B",
                "capacity": 30,
                "safety_rating": 2,
                "tz": "Asia/Shanghai",
                "hourly_fee_cents": 5000,
            }
        )
        self.maintenance.publish_window({**OVERLAPPING_WINDOW, "resource_id": other["resource_id"]})
        # 标准预约用的是 seed 的染整工坊A，不受针对工坊B的窗口影响
        applied = self.bookings.apply(apply_payload(self.ids, "k-other-res"))
        self.assertEqual(applied["status"], "REQUESTED")

        # 全工坊范围窗口则阻断所有资源
        self.maintenance.publish_window(
            {"start": "2026-10-01T06:00:00+00:00", "end": "2026-10-01T08:00:00+00:00"}
        )
        payload = apply_payload(
            self.ids,
            "k-global",
            slot_start="2026-10-01T06:00:00+00:00",
            slot_end="2026-10-01T08:00:00+00:00",
        )
        with self.assertRaises(MaintenanceBlockedError):
            self.bookings.apply(payload)


class MaintenanceWindowSQLiteSeparationTests(unittest.TestCase):
    def test_windows_and_bookings_live_in_separate_databases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            booking_db = tmp_path / "booking.db"
            maintenance_db = tmp_path / "maintenance.db"
            store = SQLiteStore(booking_db)
            schedule = SQLiteMaintenanceSchedule(maintenance_db)
            clock = ManualClock(NOW)
            ids_gen = SequentialIdGenerator()
            catalog = CatalogService(store, clock, ids_gen)
            bookings = BookingService(store, clock, ids_gen, maintenance_schedule=schedule)
            maintenance = MaintenanceService(schedule, clock, ids_gen)
            ids = seed_catalog(catalog)

            booking_id = drive_to_shipped(bookings, ids, "k-sql")
            window = maintenance.publish_window(OVERLAPPING_WINDOW)

            # 两个库文件各自独立存在
            self.assertTrue(booking_db.exists())
            self.assertTrue(maintenance_db.exists())

            def tables(path: Path) -> set[str]:
                conn = sqlite3.connect(str(path))
                try:
                    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                finally:
                    conn.close()
                return {name for (name,) in rows}

            booking_tables = tables(booking_db)
            maintenance_tables = tables(maintenance_db)
            self.assertIn("records", booking_tables)
            self.assertNotIn("maintenance_windows", booking_tables)
            self.assertIn("maintenance_windows", maintenance_tables)
            self.assertNotIn("records", maintenance_tables)

            # 新预约被拒绝，进行中任务仍可查询
            with self.assertRaises(MaintenanceBlockedError):
                bookings.apply(apply_payload(ids, "k-sql-blocked"))
            self.assertEqual(bookings.get_booking(booking_id)["status"], "SHIPPED")

            store.close()
            schedule.close()

            # 重开两个库：维护日历与任务状态各自持久化，互不依赖
            store2 = SQLiteStore(booking_db)
            schedule2 = SQLiteMaintenanceSchedule(maintenance_db)
            bookings2 = BookingService(store2, clock, ids_gen, maintenance_schedule=schedule2)
            self.assertEqual(bookings2.get_booking(booking_id)["status"], "SHIPPED")
            reopened = schedule2.list_windows()
            self.assertEqual(len(reopened), 1)
            self.assertEqual(reopened[0].window_id, window["window_id"])
            self.assertEqual(reopened[0].title, "共享工坊维护窗口")
            with self.assertRaises(MaintenanceBlockedError):
                bookings2.apply(apply_payload(ids, "k-sql-blocked-2"))
            store2.close()
            schedule2.close()


if __name__ == "__main__":
    unittest.main()
