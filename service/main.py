"""HTTP 服务入口。

除原有 /health 外，挂载森林经营方案年历接口于 /calendar/*。
服务默认使用文件型 SQLite（CALENDAR_DB 环境变量指定路径），
因此重启后事件、未决审批与提醒状态完整保留。
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from service.calendar import (
    CalendarError,
    CalendarService,
    ConflictError,
    NotFoundError,
    PrerequisiteError,
    SystemClock,
    ValidationError,
    VersionError,
)
from service.calendar.store import EventStore

_ERROR_STATUS = {
    NotFoundError: 404,
    ValidationError: 400,
    ConflictError: 409,
    VersionError: 409,
    PrerequisiteError: 412,
}


class CalendarRouter:
    """把 HTTP 请求映射到年历应用服务。"""

    def __init__(self, service: CalendarService) -> None:
        self.service = service

    def handle(self, method: str, path: str, query: dict, body: dict, headers) -> tuple[int, dict]:
        actor = headers.get("x-actor", "client")
        command_key = headers.get("idempotency-key")
        svc = self.service
        today = query.get("today")

        def p(name: str, default=None):
            return body.get(name, default)

        routes: list[tuple[str, str, object]] = [
            ("POST", r"^/calendar/plans$", lambda m: (
                201, {"plan_id": svc.register_plan(
                    p("code"), p("name"), plan_id=p("plan_id"),
                    actor=actor, command_key=command_key)})),
            ("POST", r"^/calendar/plans/([^/]+)/versions$", lambda m: (
                201, _ok(svc.approve_version(
                    m.group(1), int(p("version_no")), remark=p("remark", ""),
                    actor=actor, command_key=command_key)))),
            ("POST", r"^/calendar/plans/([^/]+)/supersede$", lambda m: (
                201, _ok(svc.supersede_version(
                    m.group(1), int(p("new_version_no")),
                    actor=actor, command_key=command_key)))),
            ("POST", r"^/calendar/parcels$", lambda m: (
                201, {"parcel_id": svc.register_parcel(
                    p("code"), p("name"), parcel_id=p("parcel_id"),
                    area=p("area"), actor=actor, command_key=command_key)})),
            ("POST", r"^/calendar/parties$", lambda m: (
                201, {"party_id": svc.register_party(
                    p("name"), p("role"), party_id=p("party_id"),
                    contact=p("contact", ""), actor=actor, command_key=command_key)})),
            ("POST", r"^/calendar/restrictions$", lambda m: (
                201, {"restriction_id": svc.declare_restriction(
                    p("kind"), p("reason"), p("source_ref"), p("start_date"),
                    end_date=p("end_date"), plan_id=p("plan_id"),
                    parcel_ids=p("parcel_ids"), task_kinds=p("task_kinds"),
                    supersedes=p("supersedes"), actor=actor, command_key=command_key)})),
            ("POST", r"^/calendar/restrictions/([^/]+)/revoke$", lambda m: (
                201, _ok(svc.revoke_restriction(
                    m.group(1), reason=p("reason", ""),
                    actor=actor, command_key=command_key)))),
            ("POST", r"^/calendar/tasks$", lambda m: (
                201, {"task_id": svc.schedule_task(
                    p("plan_id"), int(p("plan_year")), p("code"), p("kind"), p("title"),
                    p("window_start"), p("window_end"),
                    version_no=p("version_no"), parcel_ids=p("parcel_ids"),
                    quantity=float(p("quantity", 1.0)), unit=p("unit", ""),
                    prerequisites=p("prerequisites"), assignee_id=p("assignee_id"),
                    source_task_id=p("source_task_id"),
                    actor=actor, command_key=command_key)})),
            ("POST", r"^/calendar/tasks/([^/]+)/split$", lambda m: (
                201, {"child_task_ids": svc.split_task(
                    m.group(1), p("splits"), actor=actor, command_key=command_key)})),
            ("POST", r"^/calendar/tasks/([^/]+)/deferrals$", lambda m: (
                201, {"request_id": svc.request_deferral(
                    m.group(1), p("new_window_end"), p("reason"),
                    actor=actor, command_key=command_key)})),
            ("POST", r"^/calendar/tasks/([^/]+)/deferrals/([^/]+)/decision$", lambda m: (
                201, _ok(svc.decide_deferral(
                    m.group(1), m.group(2), p("decision"),
                    actor=actor, command_key=command_key)))),
            ("POST", r"^/calendar/tasks/([^/]+)/progress$", lambda m: (
                201, _ok(svc.report_progress(
                    m.group(1), float(p("quantity")), p("receipt_id"),
                    actor=actor, command_key=command_key)))),
            ("POST", r"^/calendar/tasks/([^/]+)/complete$", lambda m: (
                201, _ok(svc.complete_task(
                    m.group(1), p("receipt_id"), actor=actor, command_key=command_key)))),
            ("POST", r"^/calendar/tasks/([^/]+)/cancel$", lambda m: (
                201, _ok(svc.cancel_task(
                    m.group(1), p("reason"), actor=actor, command_key=command_key)))),
            ("POST", r"^/calendar/tasks/([^/]+)/assignee$", lambda m: (
                201, _ok(svc.assign_responsibility(
                    m.group(1), p("party_id"), reason=p("reason", ""),
                    actor=actor, command_key=command_key)))),
            ("POST", r"^/calendar/reminders/scan$", lambda m: (
                201, {"reminder_ids": svc.scan_reminders(
                    today, actor=actor, command_key=command_key)})),
            ("POST", r"^/calendar/reminders/([^/]+)/ack$", lambda m: (
                201, _ok(svc.acknowledge_reminder(
                    m.group(1), reason=p("reason", ""),
                    new_status=p("new_status", "acknowledged"),
                    actor=actor, command_key=command_key)))),
            ("GET", r"^/calendar/board$", lambda m: (200, svc.board(today))),
            ("GET", r"^/calendar/overdue$", lambda m: (200, {"items": svc.overdue_basis(today)})),
            ("GET", r"^/calendar/history$", lambda m: (200, svc.history())),
            ("GET", r"^/calendar/events$", lambda m: (
                200, {"events": svc.events(int(query.get("after_seq", 0) or 0))})),
            ("GET", r"^/calendar/tasks/([^/]+)$", lambda m: (200, svc.task(m.group(1)))),
        ]
        for route_method, pattern, handler in routes:
            if route_method != method:
                continue
            match = re.match(pattern, path)
            if match is None:
                continue
            try:
                return handler(match)
            except CalendarError as exc:
                status = _ERROR_STATUS.get(type(exc), 500)
                return status, {"error": exc.code, "message": exc.message, "details": exc.details}
            except (TypeError, ValueError) as exc:
                return 400, {"error": "validation", "message": f"请求参数不合法: {exc}"}
        return 404, {"error": "not_found", "message": f"无此接口: {method} {path}"}


def _ok(result=None) -> dict:
    return {"status": "ok"} if result is None else result


class Handler(BaseHTTPRequestHandler):
    """处理 HTTP 请求（健康检查 + 年历接口）。"""

    router: CalendarRouter | None = None

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if not parts.path.startswith("/calendar/"):
            self._write_json(404, {"error": "not_found"})
            return
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        body: dict = {}
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    if not isinstance(parsed, dict):
                        raise ValueError
                    body = parsed
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    self._write_json(400, {"error": "validation", "message": "请求体必须是 JSON 对象"})
                    return
        assert self.router is not None
        status, payload = self.router.handle(self.command, parts.path, query, body, self.headers)
        self._write_json(status, payload)

    def _write_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    host: str = "0.0.0.0",
    port: int = 0,
    *,
    store: EventStore | None = None,
    clock=None,
) -> ThreadingHTTPServer:
    """创建可由应用与测试共同使用的服务实例。"""

    own_store = False
    if store is None:
        db_path = os.environ.get("CALENDAR_DB", os.path.join("data", "calendar.db"))
        store = EventStore.open(db_path)
        own_store = True
    service = CalendarService(store, clock or SystemClock())
    Handler.router = CalendarRouter(service)
    server = ThreadingHTTPServer((host, port), Handler)
    server.calendar_store = store  # type: ignore[attr-defined]
    server.calendar_owns_store = own_store  # type: ignore[attr-defined]
    return server


def main() -> None:
    """启动服务。"""

    port = int(os.environ.get("PORT", "3000"))
    server = create_server(port=port)
    print(f"服务已启动：http://0.0.0.0:{port}（事件库：{server.calendar_store.path}）")  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
