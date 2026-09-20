"""年度检查综合场景测试。

模拟年度检查的三类情形——禁作期提前、任务部分完成、方案换版——
同时核对可执行清单、逾期依据与历史计划；并验证重启后提醒时钟与
未决审批不漂移、重复回执不产生二次影响。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from service.calendar import (
    CalendarService,
    ConflictError,
    FixedClock,
    NotFoundError,
    PrerequisiteError,
    ValidationError,
    VersionError,
)
from service.calendar.store import EventStore

# 场景固定在 2026 年度
T0 = datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc)


class InspectionScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(T0)
        self.store = EventStore(":memory:")
        self.svc = CalendarService(self.store, self.clock)
        self._build_base_data()

    def _build_base_data(self) -> None:
        svc = self.svc
        # 方案与 v1
        self.plan = svc.register_plan("FMP-2026", "青山林场五年经营方案")
        svc.approve_version(self.plan, 1, remark="v1 初版")
        # 地块
        self.p1 = svc.register_parcel("P-01", "一号坡")
        self.p2 = svc.register_parcel("P-02", "二号沟")
        # 责任主体
        self.team_a = svc.register_party("甲组", "harvest_team", contact="13800000001")
        self.team_b = svc.register_party("乙组", "tending_team")
        # 年度任务：
        # T1 春季采伐（一号坡），窗口 2026-03-01~03-31
        # T2 补植（二号沟），窗口 2026-04-01~04-30，前置 T1
        # T3 秋季管护，窗口 2026-10-01~10-31
        self.t1 = svc.schedule_task(
            self.plan, 2026, "T1", "harvest", "春季采伐",
            "2026-03-01", "2026-03-31",
            parcel_ids=[self.p1], quantity=100, unit="m3",
            assignee_id=self.team_a,
        )
        self.t2 = svc.schedule_task(
            self.plan, 2026, "T2", "replant", "迹地补植",
            "2026-04-01", "2026-04-30",
            parcel_ids=[self.p2], quantity=2000, unit="株",
            prerequisites=[self.t1], assignee_id=self.team_b,
        )
        self.t3 = svc.schedule_task(
            self.plan, 2026, "T3", "tending", "秋季管护",
            "2026-10-01", "2026-10-31",
            parcel_ids=[self.p1, self.p2], quantity=50, unit="亩",
        )

    # ------------------------------------------------------------------

    def test_initial_board_during_window(self) -> None:
        self.clock.set(datetime(2026, 3, 15, tzinfo=timezone.utc))
        board = self.svc.board("2026-03-15")
        ids = {row["task_id"] for row in board["executable"]}
        self.assertEqual(ids, {self.t1})
        # T2 前置未满足，T3 未到窗口
        self.assertEqual(
            {row["task_id"] for row in board["waiting_prerequisites"]}, {self.t2}
        )
        self.assertEqual(board["paused"], [])
        self.assertEqual(board["overdue"], [])

    def test_prerequisite_gates_replant(self) -> None:
        self.clock.set(datetime(2026, 4, 10, tzinfo=timezone.utc))
        with self.assertRaises(PrerequisiteError):
            self.svc.report_progress(self.t2, 100, "R-T2-1")
        # T1 完工后 T2 可执行
        self.svc.complete_task(self.t1, "R-T1-1")
        board = self.svc.board("2026-04-10")
        self.assertIn(self.t2, {r["task_id"] for r in board["executable"]})

    # ---- 禁作期提前 ---------------------------------------------------

    def test_moratorium_brought_forward_pauses_and_extends_due(self) -> None:
        # 原通知：防火禁作期 2026-03-20 ~ 2026-03-25（6 天）
        r_orig = self.svc.declare_restriction(
            "fire_control", "春季高火险禁作", "应急〔2026〕3号",
            "2026-03-20", end_date="2026-03-25",
            task_kinds=["harvest"], parcel_ids=[self.p1],
        )
        # 政策变化：禁作期提前至 03-15，原通知废止
        r_new = self.svc.declare_restriction(
            "fire_control", "高火险提前，禁作期前移", "应急〔2026〕5号",
            "2026-03-15", end_date="2026-03-25",
            task_kinds=["harvest"], parcel_ids=[self.p1],
            supersedes=r_orig,
        )
        # 03-16 当日：T1 必须暂停
        board = self.svc.board("2026-03-16")
        paused = {row["task_id"]: row for row in board["paused"]}
        self.assertIn(self.t1, paused)
        self.assertEqual(paused[self.t1]["restriction_ids"], [r_new])
        with self.assertRaises(ConflictError):
            self.svc.report_progress(self.t1, 10, "R-T1-X")

        # 禁作期内 T1 不得判逾期（04-02 仍在 r_new 窗口外，但 toll 已固定）
        board_apr = self.svc.board("2026-04-02")
        overdue = {row["task_id"]: row for row in board_apr["overdue"]}
        # 阻断区间并集 03-15..03-25 = 11 天 → 有效到期 04-11
        self.assertNotIn(self.t1, overdue)
        basis = self.svc.overdue_basis("2026-04-12")[0]
        self.assertEqual(basis["task_id"], self.t1)
        self.assertEqual(basis["effective_due"], "2026-04-11")
        self.assertEqual(basis["restriction_toll"]["toll_days"], 11)
        refs = {d["source_ref"] for d in basis["restriction_toll"]["details"]}
        self.assertEqual(refs, {"应急〔2026〕3号", "应急〔2026〕5号"})
        self.assertEqual(basis["days_overdue"], 1)

    def test_open_ended_restriction_never_overdue_until_released(self) -> None:
        self.svc.declare_restriction(
            "disaster", "泥石流风险，暂停一切作业", "应急〔2026〕9号",
            "2026-03-10", task_kinds=["harvest"],
        )
        # 04-20：窗口早过，但禁作期未解除，不算逾期
        self.assertEqual(self.svc.overdue_basis("2026-04-20"), [])
        board = self.svc.board("2026-04-20")
        self.assertIn(self.t1, {r["task_id"] for r in board["paused"]})

    def test_revoked_restriction_keeps_historical_toll(self) -> None:
        rid = self.svc.declare_restriction(
            "ecology", "候鸟停歇地临时管控", "林保〔2026〕2号",
            "2026-03-20", end_date="2026-04-10",
            task_kinds=["harvest"],
        )
        # 03-25 解除 → 实际阻断 03-20..03-25 共 6 天
        self.clock.set(datetime(2026, 3, 25, tzinfo=timezone.utc))
        self.svc.revoke_restriction(rid, reason="风险解除")
        basis = self.svc.overdue_basis("2026-04-10")
        t1 = next(b for b in basis if b["task_id"] == self.t1)
        self.assertEqual(t1["restriction_toll"]["toll_days"], 6)
        self.assertEqual(t1["effective_due"], "2026-04-06")

    # ---- 部分完成与重复回执 ------------------------------------------

    def test_partial_completion_and_idempotent_receipt(self) -> None:
        self.clock.set(datetime(2026, 3, 5, tzinfo=timezone.utc))
        self.svc.report_progress(self.t1, 60, "R-1", command_key="cmd-1")
        # 网络重试：同一 command_key 与同一回执号都不产生二次影响
        self.svc.report_progress(self.t1, 60, "R-1", command_key="cmd-1")
        view = self.svc.task(self.t1)
        self.assertEqual(view["completed_quantity"], 60)
        self.assertEqual(len(view["receipts"]), 1)
        # 换一个回执号继续报
        self.svc.report_progress(self.t1, 30, "R-2")
        view = self.svc.task(self.t1)
        self.assertEqual(view["completed_quantity"], 90)
        self.assertEqual(view["remaining"], 10)
        # 超量拒绝
        with self.assertRaises(ConflictError):
            self.svc.report_progress(self.t1, 20, "R-3")
        # 收方收到错误任务上的同名回执：拒绝而不是覆盖
        with self.assertRaises(ConflictError):
            self.svc.report_progress(self.t2, 60, "R-1")
        # 全额完工
        self.svc.complete_task(self.t1, "R-4")
        self.assertEqual(self.svc.task(self.t1)["status"], "completed")
        # 完工后冻结
        with self.assertRaises(ConflictError):
            self.svc.cancel_task(self.t1, "想取消")
        with self.assertRaises(ConflictError):
            self.svc.report_progress(self.t1, 1, "R-5")

    # ---- 跨年度延期与未决审批 ----------------------------------------

    def test_cross_year_deferral_pending_survives_restart(self) -> None:
        self.clock.set(datetime(2026, 3, 28, tzinfo=timezone.utc))
        req = self.svc.request_deferral(
            self.t1, "2027-01-31", "冬季木材市场冻结，申请跨年度延期"
        )
        board = self.svc.board("2026-04-15")
        pending = {r["request_id"]: r for r in board["pending_deferral_decisions"]}
        self.assertIn(req, pending)
        # 有未决审批时不计逾期、不可拆分
        self.assertEqual(self.svc.overdue_basis("2026-05-01"), [])
        with self.assertRaises(ConflictError):
            self.svc.split_task(self.t1, [
                {"code": "T1A", "window_start": "2026-03-01",
                 "window_end": "2026-03-15", "quantity": 50},
                {"code": "T1B", "window_start": "2026-03-16",
                 "window_end": "2026-03-31", "quantity": 50},
            ])
        # 审批通过后有效到期日变为 2027-01-31
        self.svc.decide_deferral(self.t1, req, "approved")
        self.assertEqual(
            self.svc.overdue_basis("2027-02-01")[0]["effective_due"], "2027-01-31"
        )

    def test_rejected_deferral_leaves_original_due(self) -> None:
        req = self.svc.request_deferral(self.t1, "2026-06-30", "理由不充分的延期")
        self.svc.decide_deferral(self.t1, req, "rejected")
        basis = self.svc.overdue_basis("2026-04-05")[0]
        self.assertEqual(basis["effective_due"], "2026-03-31")
        self.assertEqual(basis["deferrals"][0]["status"], "rejected")

    # ---- 计划拆分 -----------------------------------------------------

    def test_split_preserves_original_and_children_inherit(self) -> None:
        self.clock.set(datetime(2026, 3, 5, tzinfo=timezone.utc))
        self.svc.report_progress(self.t1, 40, "R-1")
        ids = self.svc.split_task(self.t1, [
            {"code": "T1-A", "title": "采伐（东坡）",
             "window_start": "2026-03-10", "window_end": "2026-03-20",
             "quantity": 30, "assignee_id": self.team_a},
            {"code": "T1-B", "title": "采伐（西坡）",
             "window_start": "2026-03-21", "window_end": "2027-02-28",
             "quantity": 30, "plan_year": 2027, "assignee_id": self.team_b},
        ])
        # 原任务保留且冻结，历史里仍是 100m3 / 已完成 40
        parent = self.svc.task(self.t1)
        self.assertEqual(parent["status"], "split")
        self.assertEqual(parent["quantity"], 100)
        self.assertEqual(parent["replaced_by"], ids)
        with self.assertRaises(ConflictError):
            self.svc.report_progress(self.t1, 10, "R-X")
        # 拆分量必须等于剩余量
        with self.assertRaises(ValidationError):
            self.svc.split_task(self.t3, [
                {"code": "X1", "window_start": "2026-10-01",
                 "window_end": "2026-10-15", "quantity": 20},
                {"code": "X2", "window_start": "2026-10-16",
                 "window_end": "2026-10-31", "quantity": 20},
            ])

    def test_split_children_satisfy_prerequisite_when_all_done(self) -> None:
        children = self.svc.split_task(self.t1, [
            {"code": "T1-A", "window_start": "2026-03-01",
             "window_end": "2026-03-15", "quantity": 60},
            {"code": "T1-B", "window_start": "2026-03-16",
             "window_end": "2026-03-31", "quantity": 40},
        ])
        self.clock.set(datetime(2026, 4, 5, tzinfo=timezone.utc))
        with self.assertRaises(PrerequisiteError):
            self.svc.report_progress(self.t2, 10, "R-T2")
        self.svc.complete_task(children[0], "RC-1")
        with self.assertRaises(PrerequisiteError):
            self.svc.report_progress(self.t2, 10, "R-T2")
        self.svc.complete_task(children[1], "RC-2")
        self.svc.report_progress(self.t2, 10, "R-T2")

    # ---- 责任人交接 ---------------------------------------------------

    def test_handover_keeps_trail_and_overdue_follows_new_party(self) -> None:
        self.clock.set(datetime(2026, 3, 5, tzinfo=timezone.utc))
        self.svc.assign_responsibility(
            self.t1, self.team_b, reason="甲组设备故障，交接乙组"
        )
        self.assertEqual(self.svc.task(self.t1)["assignee_id"], self.team_b)
        basis = self.svc.overdue_basis("2026-04-05")[0]
        parties = [h["party_id"] for h in basis["assignee_handover_trail"]]
        self.assertEqual(parties, [self.team_a, self.team_b])
        self.assertEqual(basis["assignee"], self.team_b)
        # 重复交接同一人拒绝
        with self.assertRaises(ConflictError):
            self.svc.assign_responsibility(self.t1, self.team_b, reason="再次交接")

    # ---- 方案换版 -----------------------------------------------------

    def test_plan_revision_freezes_old_tasks_and_requires_carryover(self) -> None:
        self.clock.set(datetime(2026, 3, 10, tzinfo=timezone.utc))
        self.svc.report_progress(self.t1, 50, "R-1")
        # 批准并切换 v2
        self.svc.approve_version(self.plan, 2, remark="根据二类调查修订采伐量")
        self.svc.supersede_version(self.plan, 2)
        # 旧任务立即退出执行看板，进入待承接
        board = self.svc.board("2026-03-11")
        rebase = {r["task_id"] for r in board["pending_rebaseline"]}
        self.assertIn(self.t1, rebase)
        # 旧任务禁止任何修改与回执
        with self.assertRaises(VersionError):
            self.svc.report_progress(self.t1, 10, "R-2")
        with self.assertRaises(VersionError):
            self.svc.request_deferral(self.t1, "2026-05-31", "想延期旧任务")
        # 新版本承接 T1（保留来源），承接量按剩余量 50
        self.clock.set(datetime(2026, 3, 12, tzinfo=timezone.utc))
        t1_v2 = self.svc.schedule_task(
            self.plan, 2026, "T1R", "harvest", "春季采伐(v2承接)",
            "2026-03-12", "2026-04-15",
            parcel_ids=[self.p1], quantity=50, unit="m3",
            assignee_id=self.team_a, source_task_id=self.t1,
        )
        self.svc.report_progress(t1_v2, 30, "R-V2-1")
        self.svc.complete_task(t1_v2, "R-V2-2")
        # 历史：v1 原任务原样（100/50），v2 承接链可溯
        history = self.svc.history()
        plan_hist = next(p for p in history["plans"] if p["plan_id"] == self.plan)
        v1 = next(v for v in plan_hist["versions"] if v["version_no"] == 1)
        old = next(t for t in v1["tasks"] if t["task_id"] == self.t1)
        self.assertEqual(old["quantity"], 100)
        self.assertEqual(old["completed_quantity"], 50)
        self.assertIsNotNone(v1["superseded_at"])
        v2 = next(v for v in plan_hist["versions"] if v["version_no"] == 2)
        carried = next(t for t in v2["tasks"] if t["task_id"] == t1_v2)
        self.assertEqual(carried["source_task_id"], self.t1)

    def test_cannot_schedule_on_unapproved_or_rollback_version(self) -> None:
        with self.assertRaises(VersionError):
            self.svc.schedule_task(
                self.plan, 2026, "NX", "tending", "未批准版本任务",
                "2026-09-01", "2026-09-30", version_no=9,
            )
        self.svc.approve_version(self.plan, 2)
        self.svc.supersede_version(self.plan, 2)
        with self.assertRaises(VersionError):
            self.svc.supersede_version(self.plan, 1)

    # ---- 提醒时钟重启不漂移 ------------------------------------------

    def test_reminder_clock_stable_across_restart(self) -> None:
        self.clock.set(datetime(2026, 3, 31, tzinfo=timezone.utc))
        ids = self.svc.scan_reminders("2026-03-31")
        self.assertEqual(len(ids), 1)
        reminder_id = ids[0]
        # 重复扫描（同日重跑/重启）幂等返回同一结果，不再产生提醒
        self.assertEqual(self.svc.scan_reminders("2026-03-31"), ids)
        # “系统重启”：新建服务实例，从同一事件存储重放
        restarted = CalendarService(self.store, FixedClock(
            datetime(2026, 4, 2, tzinfo=timezone.utc)))
        self.assertEqual(restarted.scan_reminders("2026-04-01"), [])
        # 4-1 已逾期，但同一活动提醒仍只此一条；确认后才允许再提醒
        restarted.acknowledge_reminder(reminder_id, reason="已电话督促")
        new_ids = restarted.scan_reminders("2026-04-02")
        self.assertEqual(len(new_ids), 1)
        self.assertNotEqual(new_ids[0], reminder_id)
        # 未决审批在重启后仍在
        restarted.request_deferral(self.t3, "2026-12-31", "跨年度申请")
        again = CalendarService(self.store, self.clock)
        board = again.board("2026-11-01")
        self.assertEqual(
            {r["task_id"] for r in board["pending_deferral_decisions"]}, {self.t3}
        )

    # ---- 历史不可变 ---------------------------------------------------

    def test_history_is_append_only(self) -> None:
        self.clock.set(datetime(2026, 3, 5, tzinfo=timezone.utc))
        self.svc.report_progress(self.t1, 10, "H-1")
        before = self.svc.history()
        before_events = self.svc.events()
        self.svc.report_progress(self.t1, 10, "H-2")
        after = self.svc.history()
        self.assertEqual(before["replayed_through_seq"] + 1, after["replayed_through_seq"])
        # 早先事件的序号、时间、内容永不改变
        self.assertEqual(self.svc.events()[: len(before_events)], before_events)

    def test_unknown_entities_raise_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.task("task_nope")
        with self.assertRaises(NotFoundError):
            self.svc.assign_responsibility(self.t1, "party_nope")


if __name__ == "__main__":
    unittest.main()
