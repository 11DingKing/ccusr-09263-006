"""维护窗口的 HTTP 边界：发布、409 拒绝重叠新预约、进行中任务仍可查询。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from service_09252_008.interfaces.http_api import create_server
from tests.helpers import apply_payload, make_services_with_maintenance, seed_catalog


class MaintenanceHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog, cls.bookings, cls.maintenance, cls.schedule, cls.clock, cls.store = (
            make_services_with_maintenance()
        )
        cls.ids = seed_catalog(cls.catalog)
        cls.server = create_server("127.0.0.1", 0, cls.catalog, cls.bookings, cls.maintenance)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _request(
        self, method: str, path: str, body: dict | None = None, headers: dict | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers or {}
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_publish_blocks_overlapping_booking_but_keeps_in_progress_queryable(self) -> None:
        payload = apply_payload(self.ids, "k-http-prog")
        payload.pop("idempotency_key")
        status, applied = self._request(
            "POST", "/bookings", payload, headers={"Idempotency-Key": "k-http-prog"}
        )
        self.assertEqual(status, 201)
        booking_id = applied["booking_id"]

        status, _ = self._request("POST", f"/bookings/{booking_id}/quote", {})
        self.assertEqual(status, 200)
        status, _ = self._request(
            "POST", f"/bookings/{booking_id}/lock", {}, headers={"Idempotency-Key": "k-http-lock"}
        )
        self.assertEqual(status, 200)
        status, shipped = self._request(
            "POST", f"/bookings/{booking_id}/ship", {}, headers={"Idempotency-Key": "k-http-ship"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(shipped["status"], "SHIPPED")

        # 发布共享工坊维护窗口（命名固定）
        status, window = self._request(
            "POST",
            "/maintenance-windows",
            {"start": "2026-10-01T03:00:00+00:00", "end": "2026-10-01T05:00:00+00:00"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(window["title"], "共享工坊维护窗口")

        # 窗口内新预约被拒绝：409 + 专门错误码
        blocked = {**payload}
        blocked["institution"] = "港城理工学院"
        status, body = self._request(
            "POST", "/bookings", blocked, headers={"Idempotency-Key": "k-http-blocked"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "maintenance_window_blocked")

        # 已在进行的任务仍可查询
        status, fetched = self._request("GET", f"/bookings/{booking_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["status"], "SHIPPED")
        self.assertEqual(len(fetched["shipments"]), 2)

        # 维护日历可列出
        status, listed = self._request("GET", "/maintenance-windows")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["items"]), 1)

    def test_publish_validation_error(self) -> None:
        status, body = self._request(
            "POST",
            "/maintenance-windows",
            {"start": "2026-10-01T05:00:00+00:00", "end": "2026-10-01T03:00:00+00:00"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation_error")


if __name__ == "__main__":
    unittest.main()
