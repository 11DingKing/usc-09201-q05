"""年历 HTTP 接口端到端测试，包含服务重启一致性。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from service.main import create_server


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.data_path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.server = create_server("127.0.0.1", 0, self.data_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://%s:%d" % self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        os.unlink(self.data_path)

    def request(self, method: str, path: str, payload: dict | None = None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_full_annual_scenario_over_http_with_restart(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))

        _, body = self.request("POST", "/plans", {
            "name": "云岭林场经营方案", "enterprise": "云岭林场",
            "plots": [{"code": "A-01"}, {"code": "B-02"}],
        })
        plan_id = body["plan_id"]

        _, body = self.request("POST", f"/plans/{plan_id}/versions", {
            "version_no": "2025版", "title": "初版",
            "approved_at": "2025-01-10", "approver": "县林业局",
        })
        version_id = body["version_id"]

        _, body = self.request("POST", f"/plans/{plan_id}/tasks", {
            "version_no": "2025版", "year": 2025, "action": "replant",
            "title": "春季补植", "plot_ids": self._plot_ids(plan_id),
            "qty": 100, "unit": "株",
            "planned_start": "2025-03-01", "planned_end": "2025-03-31",
            "responsible_org": "营林一队", "responsible_person": "张三",
        })
        task_id = body["task_id"]

        # 提前发布政策禁作期
        _, body = self.request("POST", "/restrictions", {
            "plan_id": plan_id, "code": "JG-01", "title": "禁作",
            "kind": "policy", "actions": ["replant"],
            "start": "2025-03-05", "end": "2025-03-15",
            "source": "县林业局", "issued_at": "2025-02-25",
        })
        restriction_id = body["restriction_id"]

        # 禁作期内：任务暂停，不在可执行清单
        _, body = self.request("GET", f"/plans/{plan_id}/checklist?as_of=2025-03-10")
        row = body["rows"][0]
        self.assertEqual(row["state"], "suspended")
        self.assertEqual(body["executable_task_ids"], [])

        # 部分完成回执
        status, _ = self.request("POST", f"/tasks/{task_id}/receipts", {
            "receipt_id": "RC-1", "qty": 60, "at": "2025-03-20", "by": "张三",
        })
        self.assertEqual(status, 200)
        # 重复回执被拒
        status, body = self.request("POST", f"/tasks/{task_id}/receipts", {
            "receipt_id": "RC-1", "qty": 10, "at": "2025-03-21", "by": "张三",
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "duplicate_receipt")

        # 跨年度延期：申请待决
        status, body = self.request("POST", f"/tasks/{task_id}/deferrals", {
            "new_start": "2025-04-10", "new_end": "2026-02-28",
            "reason": "跨年度补植", "requested_by": "张三",
            "requested_at": "2025-04-02",
        })
        self.assertEqual(status, 201)
        approval_id = body["approval_id"]
        _, body = self.request("GET", "/approvals/pending")
        self.assertEqual([a["approval_id"] for a in body["pending"]], [approval_id])

        # 重启服务（同一数据文件）：待决审批仍在、禁作仍生效、部分完成量保留
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server = create_server("127.0.0.1", 0, self.data_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://%s:%d" % self.server.server_address

        _, body = self.request("GET", "/approvals/pending")
        self.assertEqual([a["approval_id"] for a in body["pending"]], [approval_id])
        _, body = self.request("GET", f"/plans/{plan_id}/checklist?as_of=2025-03-10")
        self.assertEqual(body["rows"][0]["state"], "suspended")
        # 时点推演：3 月 10 日回执（3 月 20 日）尚未发生，完成量为 0
        self.assertEqual(body["rows"][0]["completed_qty"], 0)
        _, body = self.request("GET", f"/plans/{plan_id}/checklist?as_of=2025-03-21")
        self.assertEqual(body["rows"][0]["completed_qty"], 60)

        # 批准延期后换版：原版本成为历史，任务经批准延期跨入新版年度
        status, _ = self.request(
            "POST", f"/approvals/{approval_id}/decision",
            {"decision": "approved", "decided_by": "县林业局",
             "decided_at": "2025-04-05"},
        )
        self.assertEqual(status, 200)
        self.request("POST", f"/plans/{plan_id}/versions", {
            "version_no": "2026版", "title": "修订版",
            "approved_at": "2026-01-15", "approver": "县林业局",
        })
        _, body = self.request("GET", f"/plans/{plan_id}/checklist?as_of=2026-01-20")
        self.assertEqual(body["version_no"], "2026版")
        self.assertEqual(body["rows"][0]["kind"], "carry_over")
        self.assertEqual(body["rows"][0]["state"], "executable")

        # 历史计划保留原始窗口
        _, history = self.request("GET", f"/plans/{plan_id}/history")
        v1 = next(v for v in history["versions"] if v["version_no"] == "2025版")
        self.assertEqual(v1["status"], "superseded")
        self.assertEqual(v1["tasks"][0]["original_planned_end"], "2025-03-31")

        # 解除禁作接口可用
        status, _ = self.request(
            "POST", f"/restrictions/{restriction_id}/lift",
            {"lifted_at": "2025-03-12", "by": "县林业局"},
        )
        self.assertEqual(status, 200)

        # 未知路由返回 404
        status, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def _plot_ids(self, plan_id: str) -> list[str]:
        _, body = self.request("GET", f"/plans/{plan_id}")
        return [p["plot_id"] for p in body["plots"]]


if __name__ == "__main__":
    unittest.main()
