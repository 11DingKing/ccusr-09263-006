"""共享工坊维护窗口：发布后只拒绝新预约，既有/进行中任务保持可查询且不被删除。

存储约定：维护排期与任务状态分开存储（内存实现为两个独立 store，
SQLite 实现为 booking.db / maintenance.db 两个独立数据库文件）。
"""
from __future__ import annotations

import tempfile
import unittest

from service_09252_008.application.booking_service import BookingService
from service_09252_008.application.catalog_service import CatalogService
from service_09252_008.application.maintenance_service import (
    COLLECTION_MAINTENANCE,
    MaintenanceService,
)
from service_09252_008.application.ports import ManualClock, UuidIdGenerator
from service_09252_008.domain.errors import MaintenanceBlockedError
from service_09252_008.persistence.sqlite_store import SQLiteStore
from tests.helpers import (
    NOW,
    apply_payload,
    make_services_with_maintenance,
    seed_catalog,
)

# 维护窗口覆盖标准课程时段（02:00-04:00 UTC）：01:00-05:00 UTC
MAINT_START = "2026-10-01T01:00:00+00:00"
MAINT_END = "2026-10-01T05:00:00+00:00"


class MaintenanceWindowServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.maintenance, self.clock, self.bstore, self.mstore = (
            make_services_with_maintenance()
        )
        self.ids = seed_catalog(self.catalog)

    def _publish_covering(self) -> dict:
        return self.maintenance.publish(
            {"title": "共享工坊维护窗口", "start": MAINT_START, "end": MAINT_END, "reason": "设备年检"}
        )

    def test_publish_persists_in_separate_store(self) -> None:
        window = self._publish_covering()
        # 维护窗口只落在维护库，不出现在任务库
        self.assertEqual(len(self.mstore.query(COLLECTION_MAINTENANCE)), 1)
        self.assertEqual(len(self.bstore.query(COLLECTION_MAINTENANCE)), 0)
        self.assertEqual(window["title"], "共享工坊维护窗口")
        self.assertEqual(window["maintenance_id"][:4], "mnt_")

    def test_new_overlapping_booking_is_rejected(self) -> None:
        self._publish_covering()
        before = len(self.bookings.list_bookings())
        with self.assertRaises(MaintenanceBlockedError) as ctx:
            self.bookings.apply(apply_payload(self.ids, "k-maint-block"))
        self.assertEqual(ctx.exception.code, "maintenance_blocked")
        # 拒绝后不产生任何预约（也不进入候补）
        self.assertEqual(len(self.bookings.list_bookings()), before)

    def test_inprogress_task_stays_queryable_after_window_published(self) -> None:
        # 先发预约、报价、锁定，任务进入进行中（LOCKED）
        applied = self.bookings.apply(apply_payload(self.ids, "k-maint-live"))
        booking_id = applied["booking_id"]
        self.bookings.quote(booking_id)
        self.bookings.lock(booking_id, {"idempotency_key": "k-maint-live-lock"})

        # 之后发布覆盖同一时段的维护窗口
        self._publish_covering()

        # 既有任务不被删除、状态不变，仍可查询
        view = self.bookings.get_booking(booking_id)
        self.assertEqual(view["status"], "LOCKED")
        self.assertEqual(view["booking_id"], booking_id)
        self.assertEqual(len(view["reservations"]), 2)
        # 列表中依然存在
        self.assertIn(booking_id, [b["booking_id"] for b in self.bookings.list_bookings()])

    def test_inprogress_task_can_advance_after_window_published(self) -> None:
        # 维护窗口只拦截“新预约”，不影响既有任务推进
        applied = self.bookings.apply(apply_payload(self.ids, "k-maint-flow"))
        booking_id = applied["booking_id"]
        self.bookings.quote(booking_id)
        self.bookings.lock(booking_id, {"idempotency_key": "k-maint-flow-lock"})
        self._publish_covering()

        shipped = self.bookings.ship(booking_id, {"idempotency_key": "k-maint-flow-ship"})
        self.assertEqual(shipped["status"], "SHIPPED")
        self.assertEqual(self.bookings.get_booking(booking_id)["status"], "SHIPPED")

    def test_adjacent_slot_not_blocked(self) -> None:
        # 半开区间：维护窗口 01:00-05:00 与 05:00 开始的课程不重叠。
        # 接待窗口默认 09:00-17:00+08:00 = 01:00-09:00 UTC，可容纳 05:00-07:00。
        self._publish_covering()
        applied = self.bookings.apply(
            apply_payload(
                self.ids,
                "k-maint-adj",
                slot_start="2026-10-01T05:00:00+00:00",
                slot_end="2026-10-01T07:00:00+00:00",
            )
        )
        self.assertEqual(applied["status"], "REQUESTED")

    def test_window_after_slot_not_blocked(self) -> None:
        # 维护窗口完全在课程之后（06:00-08:00），标准课程 02:00-04:00 不受影响。
        self.maintenance.publish(
            {"title": "深夜巡检", "start": "2026-10-01T06:00:00+00:00", "end": "2026-10-01T08:00:00+00:00"}
        )
        applied = self.bookings.apply(apply_payload(self.ids, "k-maint-before"))
        self.assertEqual(applied["status"], "REQUESTED")

    def test_validation_on_publish(self) -> None:
        with self.assertRaises(Exception):
            self.maintenance.publish({"title": "x", "start": MAINT_END, "end": MAINT_START})


class MaintenanceSeparateSqliteTests(unittest.TestCase):
    """SQLite 双库：维护排期与任务状态分文件，重启后各自恢复且互不影响。"""

    def test_separate_db_files_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            booking_db = f"{tmp}/booking.db"
            maintenance_db = f"{tmp}/maintenance.db"
            clock = ManualClock(NOW)

            bstore = SQLiteStore(booking_db)
            mstore = SQLiteStore(maintenance_db)
            catalog = CatalogService(bstore, clock, UuidIdGenerator())
            maintenance = MaintenanceService(mstore, clock, UuidIdGenerator())
            bookings = BookingService(bstore, clock, UuidIdGenerator(), maintenance=maintenance)
            ids = seed_catalog(catalog)

            # 一个进行中任务
            applied = bookings.apply(apply_payload(ids, "k-sql-live"))
            booking_id = applied["booking_id"]
            bookings.quote(booking_id)
            bookings.lock(booking_id, {"idempotency_key": "k-sql-live-lock"})
            # 发布覆盖维护窗口
            maintenance.publish(
                {"title": "共享工坊维护窗口", "start": MAINT_START, "end": MAINT_END}
            )
            bstore.close()
            mstore.close()

            # 两个数据库文件彼此独立
            import os

            self.assertTrue(os.path.exists(booking_db))
            self.assertTrue(os.path.exists(maintenance_db))

            # “重启”：各自挂载自己的库文件
            bstore2 = SQLiteStore(booking_db)
            mstore2 = SQLiteStore(maintenance_db)
            clock2 = ManualClock(NOW)
            catalog2 = CatalogService(bstore2, clock2, UuidIdGenerator())
            maintenance2 = MaintenanceService(mstore2, clock2, UuidIdGenerator())
            bookings2 = BookingService(bstore2, clock2, UuidIdGenerator(), maintenance=maintenance2)

            # 维护窗口跨重启仍生效：新预约被拒
            with self.assertRaises(MaintenanceBlockedError):
                bookings2.apply(apply_payload(ids, "k-sql-block"))
            # 进行中任务跨重启仍可查询、未被删除、状态不变
            view = bookings2.get_booking(booking_id)
            self.assertEqual(view["status"], "LOCKED")
            self.assertEqual(len(view["reservations"]), 2)
            # 维护库里确实有一条，任务库里没有维护集合
            self.assertEqual(len(mstore2.query(COLLECTION_MAINTENANCE)), 1)
            self.assertEqual(len(bstore2.query(COLLECTION_MAINTENANCE)), 0)
            bstore2.close()
            mstore2.close()


if __name__ == "__main__":
    unittest.main()
