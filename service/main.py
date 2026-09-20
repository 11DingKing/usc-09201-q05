"""森林经营方案年历 HTTP 服务。

除健康检查外，所有接口均围绕“方案版本—年度任务—限制窗口—审批/回执”的
追加式记录提供读写。状态由事件日志重放得到，重启不漂移。

数据文件通过环境变量 CALENDAR_DATA 指定（默认 data/calendar.jsonl）。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import domain
from .domain import ForestCalendar
from .store import EventStore


class CalendarServer(ThreadingHTTPServer):
    """持有领域应用实例的 HTTP 服务器。"""

    def __init__(self, address: tuple[str, int], calendar: ForestCalendar) -> None:
        super().__init__(address, Handler)
        self.calendar = calendar


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class Handler(BaseHTTPRequestHandler):
    """处理年历 HTTP 请求。"""

    server: CalendarServer

    @property
    def calendar(self) -> ForestCalendar:
        return self.server.calendar

    # -------------------------------------------------------------- 基础方法

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(_jsonable(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: str, message: str, status: int) -> None:
        self._send_json({"error": code, "message": message}, status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise domain.DomainError("bad_json", f"请求体不是合法 JSON：{exc}", 400)
        if not isinstance(payload, dict):
            raise domain.DomainError("bad_json", "请求体必须是 JSON 对象", 400)
        return payload

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            handler = self._match(method, path, query)
            if handler is None:
                self._send_error("not_found", f"未找到 {method} {path}", 404)
                return
            handler(query)
        except domain.DomainError as exc:
            self._send_error(exc.code, str(exc), exc.status)
        except (KeyError, TypeError) as exc:
            self._send_error("bad_request", f"请求参数有误：{exc}", 400)

    def _match(self, method: str, path: str, query: dict[str, list[str]]):
        body = self._read_json if method == "POST" else None
        routes: list[tuple[str, str, Any]] = [
            ("GET", r"^/health$", lambda q: self._send_json({"status": "ok"})),
            ("POST", r"^/plans$", self._create_plan),
            ("GET", r"^/plans/(?P<id>[^/]+)$", self._get_plan),
            ("GET", r"^/plans/(?P<id>[^/]+)/history$", self._get_history),
            ("GET", r"^/plans/(?P<id>[^/]+)/checklist$", self._get_checklist),
            ("POST", r"^/plans/(?P<id>[^/]+)/versions$", self._add_version),
            ("POST", r"^/plans/(?P<id>[^/]+)/tasks$", self._create_task),
            ("POST", r"^/restrictions$", self._issue_restriction),
            ("POST", r"^/restrictions/(?P<id>[^/]+)/lift$", self._lift_restriction),
            ("POST", r"^/tasks/(?P<id>[^/]+)/deferrals$", self._request_deferral),
            ("POST", r"^/tasks/(?P<id>[^/]+)/split$", self._split_task),
            ("POST", r"^/tasks/(?P<id>[^/]+)/receipts$", self._record_receipt),
            ("POST", r"^/tasks/(?P<id>[^/]+)/handover$", self._handover),
            ("GET", r"^/tasks/(?P<id>[^/]+)$", self._get_task),
            ("POST", r"^/approvals/(?P<id>[^/]+)/decision$", self._decide_approval),
            ("GET", r"^/approvals/pending$", self._pending_approvals),
            ("GET", r"^/reminders$", self._reminders),
            ("POST", r"^/reminders/delivered$", self._mark_delivered),
        ]
        for http_method, pattern, fn in routes:
            match = re.match(pattern, path)
            if http_method == method and match:
                kwargs = match.groupdict()
                if method == "POST":
                    return lambda q, fn=fn, kwargs=kwargs: fn(body(), **kwargs)
                return lambda q, fn=fn, kwargs=kwargs: fn(q, **kwargs)
        return None

    # -------------------------------------------------------------- 路由处理

    def _create_plan(self, payload: dict[str, Any]) -> None:
        plan_id = self.calendar.create_plan(
            name=payload["name"],
            enterprise=payload["enterprise"],
            plots=payload.get("plots"),
        )
        self._send_json({"plan_id": plan_id}, 201)

    def _get_plan(self, query: dict[str, list[str]], id: str) -> None:
        self._send_json(self.calendar.plan_detail(id))

    def _get_history(self, query: dict[str, list[str]], id: str) -> None:
        self._send_json(self.calendar.plan_history(id))

    def _get_checklist(self, query: dict[str, list[str]], id: str) -> None:
        as_of = query.get("as_of", [date.today().isoformat()])[0]
        self._send_json(self.calendar.checklist(id, as_of))

    def _add_version(self, payload: dict[str, Any], id: str) -> None:
        version_id = self.calendar.add_version(
            plan_id=id,
            version_no=payload["version_no"],
            title=payload["title"],
            approved_at=payload["approved_at"],
            approver=payload["approver"],
            note=payload.get("note", ""),
        )
        self._send_json({"version_id": version_id}, 201)

    def _create_task(self, payload: dict[str, Any], id: str) -> None:
        task_id = self.calendar.create_task(
            plan_id=id,
            version_no=payload["version_no"],
            year=payload["year"],
            action=payload["action"],
            title=payload["title"],
            plot_ids=payload["plot_ids"],
            qty=payload["qty"],
            unit=payload["unit"],
            planned_start=payload["planned_start"],
            planned_end=payload["planned_end"],
            responsible_org=payload["responsible_org"],
            responsible_person=payload["responsible_person"],
            prerequisite_ids=payload.get("prerequisite_ids"),
            created_by=payload.get("created_by", "system"),
        )
        self._send_json({"task_id": task_id}, 201)

    def _issue_restriction(self, payload: dict[str, Any]) -> None:
        restriction_id = self.calendar.issue_restriction(
            plan_id=payload["plan_id"],
            code=payload["code"],
            title=payload["title"],
            kind=payload["kind"],
            actions=payload["actions"],
            start=payload["start"],
            end=payload["end"],
            source=payload["source"],
            plot_ids=payload.get("plot_ids"),
            issued_at=payload.get("issued_at"),
        )
        self._send_json({"restriction_id": restriction_id}, 201)

    def _lift_restriction(self, payload: dict[str, Any], id: str) -> None:
        self.calendar.lift_restriction(
            restriction_id=id,
            lifted_at=payload["lifted_at"],
            by=payload["by"],
            note=payload.get("note", ""),
        )
        self._send_json({"status": "lifted"})

    def _request_deferral(self, payload: dict[str, Any], id: str) -> None:
        approval_id = self.calendar.request_deferral(
            task_id=id,
            new_start=payload["new_start"],
            new_end=payload["new_end"],
            reason=payload["reason"],
            requested_by=payload["requested_by"],
            requested_at=payload.get("requested_at"),
        )
        self._send_json({"approval_id": approval_id}, 201)

    def _decide_approval(self, payload: dict[str, Any], id: str) -> None:
        self.calendar.decide_approval(
            approval_id=id,
            decision=payload["decision"],
            decided_by=payload["decided_by"],
            decided_at=payload.get("decided_at"),
            note=payload.get("note", ""),
        )
        self._send_json({"status": payload["decision"]})

    def _split_task(self, payload: dict[str, Any], id: str) -> None:
        child_ids = self.calendar.split_task(
            task_id=id,
            splits=payload["splits"],
            requested_by=payload["requested_by"],
            split_at=payload["split_at"],
        )
        self._send_json({"child_task_ids": child_ids}, 201)

    def _record_receipt(self, payload: dict[str, Any], id: str) -> None:
        self.calendar.record_receipt(
            task_id=id,
            receipt_id=payload["receipt_id"],
            qty=payload["qty"],
            at=payload["at"],
            by=payload["by"],
            note=payload.get("note", ""),
        )
        self._send_json({"status": "recorded"})

    def _handover(self, payload: dict[str, Any], id: str) -> None:
        self.calendar.handover(
            task_id=id,
            to_org=payload["to_org"],
            to_person=payload["to_person"],
            by=payload["by"],
            at=payload["at"],
            note=payload.get("note", ""),
        )
        self._send_json({"status": "transferred"})

    def _get_task(self, query: dict[str, list[str]], id: str) -> None:
        self._send_json(self.calendar.task_detail(id))

    def _pending_approvals(self, query: dict[str, list[str]]) -> None:
        self._send_json({"pending": self.calendar.pending_approvals()})

    def _reminders(self, query: dict[str, list[str]]) -> None:
        now = query.get("now", [None])[0]
        self._send_json({"reminders": self.calendar.reminders(now)})

    def _mark_delivered(self, payload: dict[str, Any]) -> None:
        self.calendar.mark_reminder_delivered(
            key=payload["key"], at=payload.get("at")
        )
        self._send_json({"status": "delivered"})

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    host: str = "0.0.0.0",
    port: int = 0,
    data_path: str | None = None,
) -> CalendarServer:
    """创建服务实例；data_path 为 None 时使用 CALENDAR_DATA 环境变量。"""

    path = data_path or os.environ.get("CALENDAR_DATA", "data/calendar.jsonl")
    calendar = ForestCalendar(EventStore(path))
    return CalendarServer((host, port), calendar)


def main() -> None:
    """启动服务。"""

    port = int(os.environ.get("PORT", "3000"))
    server = create_server(port=port)
    print(f"服务已启动：http://0.0.0.0:{port}（数据：{server.calendar.store.path}）")
    server.serve_forever()


if __name__ == "__main__":
    main()
