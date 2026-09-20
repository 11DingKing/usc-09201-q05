"""HTTP 接口与基于文件事件库的重启持久化测试。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from service.calendar import FixedClock
from service.calendar.store import EventStore
from service.main import create_server

from datetime import datetime, timezone


class HttpFixture:
    def __init__(self, store=None, clock=None) -> None:
        self.server = create_server("127.0.0.1", 0, store=store, clock=clock)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def get(self, path: str):
        try:
            with urllib.request.urlopen(f"{self.base}{path}") as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def post(self, path: str, body: dict, headers: dict | None = None):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(datetime(2026, 3, 10, tzinfo=timezone.utc))
        self.fx = HttpFixture(store=EventStore(":memory:"), clock=self.clock)

    def tearDown(self) -> None:
        self.fx.close()

    def test_full_flow_over_http(self) -> None:
        fx = self.fx
        status, plan = fx.post("/calendar/plans", {"code": "F1", "name": "方案甲"})
        self.assertEqual(status, 201)
        plan_id = plan["plan_id"]
        self.assertEqual(
            fx.post(f"/calendar/plans/{plan_id}/versions", {"version_no": 1})[0], 201
        )
        _, parcel = fx.post("/calendar/parcels", {"code": "D1", "name": "一号地"})
        _, party = fx.post("/calendar/parties", {"name": "老张", "role": "forester"})
        _, task = fx.post("/calendar/tasks", {
            "plan_id": plan_id, "plan_year": 2026, "code": "A1",
            "kind": "tending", "title": "割灌除草",
            "window_start": "2026-03-01", "window_end": "2026-03-31",
            "parcel_ids": [parcel["parcel_id"]], "quantity": 10, "unit": "亩",
            "assignee_id": party["party_id"],
        })
        task_id = task["task_id"]
        # 可执行清单
        _, board = fx.get("/calendar/board?today=2026-03-10")
        self.assertIn(task_id, {r["task_id"] for r in board["executable"]})
        # 完工回执
        self.assertEqual(
            fx.post(f"/calendar/tasks/{task_id}/complete", {"receipt_id": "RC-1"})[0],
            201,
        )
        # 重复回执 → 409 冲突，但不产生第二次影响
        status, err = fx.post(
            f"/calendar/tasks/{task_id}/complete", {"receipt_id": "RC-1"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "conflict")
        # 逾期依据在逾期后可查
        _, overdue = fx.get("/calendar/overdue?today=2026-05-01")
        # 已完工任务不在逾期清单
        self.assertEqual(overdue["items"], [])
        # 历史始终可查
        _, history = fx.get("/calendar/history")
        self.assertEqual(history["plans"][0]["code"], "F1")

    def test_restriction_blocks_progress_over_http(self) -> None:
        fx = self.fx
        _, plan = fx.post("/calendar/plans", {"code": "F2", "name": "方案乙"})
        fx.post(f"/calendar/plans/{plan['plan_id']}/versions", {"version_no": 1})
        _, parcel = fx.post("/calendar/parcels", {"code": "D2", "name": "二号地"})
        _, task = fx.post("/calendar/tasks", {
            "plan_id": plan["plan_id"], "plan_year": 2026, "code": "B1",
            "kind": "harvest", "title": "采伐",
            "window_start": "2026-03-01", "window_end": "2026-03-31",
            "parcel_ids": [parcel["parcel_id"]], "quantity": 50,
        })
        task_id = task["task_id"]
        _, restriction = fx.post("/calendar/restrictions", {
            "kind": "fire_control", "reason": "高火险", "source_ref": "应急1号",
            "start_date": "2026-03-09", "end_date": "2026-03-20",
            "parcel_ids": [parcel["parcel_id"]], "task_kinds": ["harvest"],
        })
        _, board = fx.get("/calendar/board?today=2026-03-10")
        self.assertIn(task_id, {r["task_id"] for r in board["paused"]})
        status, err = fx.post(
            f"/calendar/tasks/{task_id}/progress",
            {"quantity": 5, "receipt_id": "P-1"},
        )
        self.assertEqual(status, 409)
        self.assertIn("禁作期", err["message"])
        # 提前修订禁作期
        status, _ = fx.post("/calendar/restrictions", {
            "kind": "fire_control", "reason": "火险提前", "source_ref": "应急2号",
            "start_date": "2026-03-05", "end_date": "2026-03-20",
            "parcel_ids": [parcel["parcel_id"]], "task_kinds": ["harvest"],
            "supersedes": restriction["restriction_id"],
        })
        self.assertEqual(status, 201)

    def test_health_still_ok(self) -> None:
        status, body = self.fx.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_unknown_route_and_bad_json(self) -> None:
        self.assertEqual(self.fx.get("/nope")[0], 404)
        # 非法 JSON
        data = b"{not json"
        req = urllib.request.Request(
            f"{self.fx.base}/calendar/plans", data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


class FileStoreRestartTest(unittest.TestCase):
    """用真实文件事件库验证：关闭进程再打开，状态、未决审批、提醒不漂移。"""

    def test_state_survives_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "calendar.db")
            clock = FixedClock(datetime(2026, 3, 20, tzinfo=timezone.utc))

            fx = HttpFixture(store=EventStore.open(db_path), clock=clock)
            _, plan = fx.post("/calendar/plans", {"code": "F-R", "name": "重启方案"})
            plan_id = plan["plan_id"]
            fx.post(f"/calendar/plans/{plan_id}/versions", {"version_no": 1})
            _, task = fx.post("/calendar/tasks", {
                "plan_id": plan_id, "plan_year": 2026, "code": "R1",
                "kind": "tending", "title": "管护",
                "window_start": "2026-03-01", "window_end": "2026-03-31",
                "quantity": 10,
            })
            task_id = task["task_id"]
            # 留下未决延期申请
            _, deferral = fx.post(
                f"/calendar/tasks/{task_id}/deferrals",
                {"new_window_end": "2026-05-31", "reason": "连续降雨"},
            )
            fx.close()

            # —— 模拟进程重启：新存储、新服务、同一文件 ——
            clock2 = FixedClock(datetime(2026, 4, 15, tzinfo=timezone.utc))
            fx2 = HttpFixture(store=EventStore.open(db_path), clock=clock2)
            _, board = fx2.get("/calendar/board?today=2026-04-15")
            pending = {r["task_id"] for r in board["pending_deferral_decisions"]}
            self.assertIn(task_id, pending)
            # 未决期间不判逾期
            _, overdue = fx2.get("/calendar/overdue?today=2026-04-15")
            self.assertEqual(overdue["items"], [])
            # 审批后立刻按新到期日判定
            self.assertEqual(fx2.post(
                f"/calendar/tasks/{task_id}/deferrals/{deferral['request_id']}/decision",
                {"decision": "approved"},
            )[0], 201)
            _, overdue_after = fx2.get("/calendar/overdue?today=2026-06-02")
            self.assertEqual(len(overdue_after["items"]), 1)
            self.assertEqual(overdue_after["items"][0]["effective_due"], "2026-05-31")
            # 历史完整
            _, history = fx2.get("/calendar/history")
            self.assertEqual(history["plans"][0]["code"], "F-R")
            self.assertGreaterEqual(history["replayed_through_seq"], 5)
            fx2.close()


if __name__ == "__main__":
    unittest.main()
