"""森林经营方案年历领域规则测试。

以一次跨年度年度检查的视角，模拟禁作期提前发布、任务部分完成、跨年度延期、
责任人交接、方案换版与任务拆分，验证可执行清单、逾期依据和历史计划同时准确，
并验证服务重启后提醒时钟与待决审批不漂移。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

from service.domain import (
    DISASTER,
    ECOLOGY,
    HARVEST,
    POLICY,
    REPLANT,
    TEND,
    DomainError,
    ForestCalendar,
)
from service.store import EventStore


class CalendarFixture:
    """搭建多年方案：2025 版含补植、采收、管护三个年度任务。"""

    def __init__(self, path: str | None = None) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.cal = ForestCalendar(EventStore(self.path))
        self.plan_id = self.cal.create_plan(
            name="云岭林场森林经营方案",
            enterprise="云岭林场",
            plots=[
                {"code": "A-01", "name": "1号林班"},
                {"code": "B-02", "name": "2号林班"},
            ],
        )
        plots = self.cal.plan_detail(self.plan_id)["plots"]
        self.plot_a = plots[0]["plot_id"]
        self.plot_b = plots[1]["plot_id"]
        self.ver1 = self.cal.add_version(
            self.plan_id, "2025版", "十五五前期经营方案",
            approved_at=date(2025, 1, 10), approver="县林业局",
        )
        self.t_replant = self.cal.create_task(
            self.plan_id, "2025版", 2025, REPLANT, "春季补植",
            [self.plot_a], 100, "株",
            date(2025, 3, 1), date(2025, 3, 31),
            "营林一队", "张三",
        )
        self.t_harvest = self.cal.create_task(
            self.plan_id, "2025版", 2025, HARVEST, "抚育采收",
            [self.plot_b], 200, "立方米",
            date(2025, 5, 1), date(2025, 6, 30),
            "采收二队", "李四",
            prerequisite_ids=[self.t_replant],
        )
        self.t_tend = self.cal.create_task(
            self.plan_id, "2025版", 2026, TEND, "冬季管护",
            [self.plot_a], 50, "亩",
            date(2026, 1, 5), date(2026, 1, 31),
            "营林一队", "王五",
        )

    def reload(self) -> ForestCalendar:
        return ForestCalendar(EventStore(self.path))

    def row(self, cal: ForestCalendar, task_id: str, as_of: date):
        result = cal.checklist(self.plan_id, as_of)
        rows = {r["task_id"]: r for r in result["rows"]}
        return rows.get(task_id), result

    def cleanup(self) -> None:
        os.unlink(self.path)


class AnnualInspectionTest(unittest.TestCase):
    """年度检查逐项推演。"""

    def setUp(self) -> None:
        self.fx = CalendarFixture()
        self.cal = self.fx.cal
        self.plan_id = self.fx.plan_id
        self.t_replant = self.fx.t_replant
        self.t_harvest = self.fx.t_harvest
        self.t_tend = self.fx.t_tend

    def tearDown(self) -> None:
        self.fx.cleanup()

    # ---------------------------------------------------------- 禁作期提前

    def test_policy_restriction_issued_early_suspends_task(self) -> None:
        # 政策边界在作业季开始前提前发布：3 月 1 日起补植禁作
        self.cal.issue_restriction(
            self.plan_id, "JG-2025-01", "春季候鸟栖息禁作",
            POLICY, [REPLANT],
            date(2025, 3, 1), date(2025, 3, 20),
            source="县林业局公告", issued_at=date(2025, 2, 25),
        )
        # 禁作期发布之前（2 月 24 日）清单不受影响
        row, _ = self.fx.row(self.cal, self.fx.t_replant, date(2025, 2, 24))
        self.assertEqual(row["state"], "not_started")
        self.assertEqual(row["blocking_restrictions"], [])

        # 作业窗口内遭遇禁作：可执行清单排除该任务
        row, result = self.fx.row(self.cal, self.fx.t_replant, date(2025, 3, 10))
        self.assertEqual(row["state"], "suspended")
        self.assertFalse(row["executable"])
        self.assertNotIn(self.fx.t_replant, result["executable_task_ids"])
        self.assertEqual(row["blocking_restrictions"][0]["code"], "JG-2025-01")
        self.assertEqual(row["blocking_restrictions"][0]["kind_label"], "政策边界")

        # 提前解除禁作后，任务恢复可执行，原禁作记录仍保留
        self.cal.lift_restriction(
            self._restriction_id("JG-2025-01"),
            lifted_at=date(2025, 3, 15), by="县林业局",
        )
        row, _ = self.fx.row(self.cal, self.fx.t_replant, date(2025, 3, 18))
        self.assertEqual(row["state"], "executable")

    def test_disaster_during_window_then_overdue_basis(self) -> None:
        # 补植先完成，采收进行到一半
        self.cal.record_receipt(
            self.fx.t_replant, "RC-1", 100, date(2025, 3, 20), by="张三",
        )
        self.cal.record_receipt(
            self.fx.t_harvest, "RC-2", 120, date(2025, 6, 5), by="李四",
        )
        # 灾害风险在作业期内临时发布
        self.cal.issue_restriction(
            self.plan_id, "ZH-2025-07", "暴雨滑坡风险",
            DISASTER, [HARVEST],
            date(2025, 6, 10), date(2025, 6, 20),
            source="县应急局预警", issued_at=date(2025, 6, 10),
        )
        row, _ = self.fx.row(self.cal, self.fx.t_harvest, date(2025, 6, 12))
        self.assertEqual(row["state"], "suspended")
        self.assertEqual(row["completed_qty"], 120)
        self.assertEqual(row["remaining"], 80)

        # 禁作窗口过去而任务未完成：逾期，且依据保留灾害记录与部分完成量
        row, _ = self.fx.row(self.cal, self.fx.t_harvest, date(2025, 7, 2))
        self.assertEqual(row["state"], "overdue")
        basis = row["overdue_basis"]
        self.assertEqual(basis["original_due"], date(2025, 6, 30))
        self.assertEqual(basis["effective_due"], date(2025, 6, 30))
        self.assertEqual(basis["completed_qty"], 120)
        self.assertEqual(basis["remaining"], 80)
        self.assertEqual(basis["last_receipt"]["receipt_id"], "RC-2")
        self.assertEqual(basis["restrictions"][0]["code"], "ZH-2025-07")

    def test_ecology_restriction_all_plots_blocks_matching_action_only(self) -> None:
        # 不指定地块=全方案生效；生态限制只拦截采收，不拦截管护
        rid = self.cal.issue_restriction(
            self.plan_id, "ST-2026-01", "天然林保育",
            ECOLOGY, [HARVEST],
            date(2025, 12, 1), date(2026, 3, 31),
            source="生态保护红线划定", issued_at=date(2025, 11, 1),
        )
        self.cal.record_receipt(
            self.fx.t_replant, "RC-1", 100, date(2025, 3, 20), by="张三",
        )
        row_h, _ = self.fx.row(self.cal, self.fx.t_harvest, date(2026, 1, 20))
        # 采收任务原窗口 2025 年内已结束，禁作延续到次年 → 逾期且仍被暂停
        self.assertEqual(row_h["state"], "overdue_suspended")
        row_t, _ = self.fx.row(self.cal, self.fx.t_tend, date(2026, 1, 20))
        self.assertEqual(row_t["state"], "executable")
        # 解除后状态变为普通逾期
        self.cal.lift_restriction(rid, date(2026, 1, 25), by="县林业局")
        row_h, _ = self.fx.row(self.cal, self.fx.t_harvest, date(2026, 1, 26))
        self.assertEqual(row_h["state"], "overdue")

    # ---------------------------------------------------------- 重复/超量回执

    def test_duplicate_and_over_quantity_receipts_rejected(self) -> None:
        self.cal.record_receipt(
            self.fx.t_replant, "RC-X", 60, date(2025, 3, 20), by="张三",
        )
        with self.assertRaises(DomainError) as ctx:
            self.cal.record_receipt(
                self.fx.t_replant, "RC-X", 10, date(2025, 3, 21), by="张三",
            )
        self.assertEqual(ctx.exception.code, "duplicate_receipt")
        with self.assertRaises(DomainError) as ctx:
            self.cal.record_receipt(
                self.fx.t_replant, "RC-Y", 50, date(2025, 3, 22), by="张三",
            )
        self.assertEqual(ctx.exception.code, "over_qty")
        # 被拒绝的回执不得影响完成量
        row, _ = self.fx.row(self.cal, self.fx.t_replant, date(2025, 3, 23))
        self.assertEqual(row["completed_qty"], 60)
        self.assertEqual(row["remaining"], 40)

    # ---------------------------------------------------------- 前置关系

    def test_prerequisite_gates_executability(self) -> None:
        # 补植未完成，采收即使进入窗口也不可执行
        row, result = self.fx.row(self.cal, self.fx.t_harvest, date(2025, 5, 10))
        self.assertEqual(row["state"], "waiting_prerequisite")
        self.assertFalse(row["prerequisites_met"])
        self.assertNotIn(self.fx.t_harvest, result["executable_task_ids"])

        self.cal.record_receipt(
            self.fx.t_replant, "RC-1", 100, date(2025, 3, 20), by="张三",
        )
        row, _ = self.fx.row(self.cal, self.fx.t_harvest, date(2025, 5, 10))
        self.assertTrue(row["prerequisites_met"])
        self.assertEqual(row["state"], "executable")

    # ----------------------------------------------- 跨年度延期与待决审批

    def test_deferral_requires_approval_and_keeps_overdue_basis(self) -> None:
        self.cal.record_receipt(
            self.fx.t_replant, "RC-1", 100, date(2025, 3, 20), by="张三",
        )
        self.cal.record_receipt(
            self.fx.t_harvest, "RC-2", 120, date(2025, 6, 5), by="李四",
        )
        approval_id = self.cal.request_deferral(
            self.fx.t_harvest,
            date(2025, 8, 1), date(2026, 2, 28),
            reason="剩余山场道路水毁，需跨年度作业",
            requested_by="李四", requested_at=date(2025, 7, 5),
        )
        # 待决期间：仍按原计划逾期，不能出现第二个待决审批
        row, _ = self.fx.row(self.cal, self.fx.t_harvest, date(2025, 7, 6))
        self.assertEqual(row["effective_window"]["end"], date(2025, 6, 30))
        self.assertEqual(row["state"], "overdue")
        pending = self.cal.pending_approvals(self.fx.plan_id)
        self.assertEqual([a["approval_id"] for a in pending], [approval_id])
        with self.assertRaises(DomainError) as ctx:
            self.cal.request_deferral(
                self.fx.t_harvest,
                date(2025, 9, 1), date(2026, 3, 31),
                reason="再次申请", requested_by="李四",
            )
        self.assertEqual(ctx.exception.code, "approval_pending")

        # 驳回不改变任何窗口
        self.cal.decide_approval(
            approval_id, "rejected", decided_by="县林业局",
            decided_at=date(2025, 7, 8), note="材料不全",
        )
        with self.assertRaises(DomainError) as ctx:
            self.cal.decide_approval(
                approval_id, "approved", decided_by="县林业局",
            )
        self.assertEqual(ctx.exception.code, "approval_decided")
        row, _ = self.fx.row(self.cal, self.fx.t_harvest, date(2025, 7, 9))
        self.assertEqual(row["effective_window"]["end"], date(2025, 6, 30))

        # 重新申请并批准跨年度窗口后，原计划窗口仍保留为逾期依据
        approval2 = self.cal.request_deferral(
            self.fx.t_harvest,
            date(2025, 8, 1), date(2026, 2, 28),
            reason="补充材料后重新申报", requested_by="李四",
            requested_at=date(2025, 7, 10),
        )
        self.cal.decide_approval(
            approval2, "approved", decided_by="县林业局",
            decided_at=date(2025, 7, 12),
        )
        row, _ = self.fx.row(self.cal, self.fx.t_harvest, date(2025, 7, 20))
        self.assertEqual(row["state"], "not_started")
        self.assertEqual(row["effective_window"],
                         {"start": date(2025, 8, 1), "end": date(2026, 2, 28)})
        self.assertEqual(row["original_window"]["end"], date(2025, 6, 30))
        self.assertEqual(self.cal.pending_approvals(), [])

    def test_restart_keeps_pending_approvals_and_clocks(self) -> None:
        """系统重启：待决审批与提醒时钟逐字节一致，不漂移。"""

        self.cal.request_deferral(
            self.fx.t_harvest,
            date(2025, 8, 1), date(2026, 2, 28),
            reason="跨年度", requested_by="李四",
            requested_at=date(2025, 7, 5),
        )
        before = self.cal.reminders("2025-02-23T10:00:00")
        pending_before = self.cal.pending_approvals()
        # 补植的开工提醒（计划 3 月 1 日，提前 7 天即 2 月 22 日 9 时）应已到点
        start_reminder = next(
            r for r in before
            if r["task_id"] == self.fx.t_replant and r["kind"] == "start"
        )
        self.assertEqual(start_reminder["status"], "due")
        self.cal.mark_reminder_delivered(
            start_reminder["key"], at="2025-02-23T10:05:00",
        )
        # 发送留痕后的快照作为重启一致性基准
        before = self.cal.reminders("2025-02-23T10:00:00")

        reloaded = self.fx.reload()
        self.assertEqual(
            [a["approval_id"] for a in reloaded.pending_approvals()],
            [a["approval_id"] for a in pending_before],
        )
        after = reloaded.reminders("2025-02-23T10:00:00")
        self.assertEqual(
            [(r["key"], r["fire_at"].isoformat(), r["status"]) for r in before],
            [(r["key"], r["fire_at"].isoformat(), r["status"]) for r in after],
        )
        delivered = next(r for r in after if r["key"] == start_reminder["key"])
        self.assertEqual(delivered["status"], "delivered")
        # 重启后重复标记同一提醒仍被拒绝
        with self.assertRaises(DomainError) as ctx:
            reloaded.mark_reminder_delivered(start_reminder["key"])
        self.assertEqual(ctx.exception.code, "already_delivered")

    # ---------------------------------------------------------- 方案换版

    def test_version_switch_preserves_history_and_carry_over(self) -> None:
        self.cal.record_receipt(
            self.fx.t_replant, "RC-1", 100, date(2025, 3, 20), by="张三",
        )
        self.cal.record_receipt(
            self.fx.t_harvest, "RC-2", 120, date(2025, 6, 5), by="李四",
        )
        approval = self.cal.request_deferral(
            self.fx.t_harvest,
            date(2025, 8, 1), date(2026, 2, 28),
            reason="道路水毁跨年度", requested_by="李四",
            requested_at=date(2025, 7, 5),
        )
        self.cal.decide_approval(
            approval, "approved", decided_by="县林业局",
            decided_at=date(2025, 7, 12),
        )
        # 换版：2026 版 1 月 15 日批准
        ver2 = self.cal.add_version(
            self.fx.plan_id, "2026版", "2026 年度修订方案",
            approved_at=date(2026, 1, 15), approver="县林业局",
        )
        t_new = self.cal.create_task(
            self.fx.plan_id, "2026版", 2026, TEND, "春季管护",
            [self.fx.plot_b], 30, "亩",
            date(2026, 3, 1), date(2026, 3, 20),
            "营林一队", "赵六",
        )

        result = self.cal.checklist(self.fx.plan_id, date(2026, 1, 20))
        self.assertEqual(result["version_no"], "2026版")
        rows = {r["task_id"]: r for r in result["rows"]}
        # 旧版采收经批准延期跨入新年 → 保留为结转任务
        self.assertIn(self.fx.t_harvest, rows)
        self.assertEqual(rows[self.fx.t_harvest]["kind"], "carry_over")
        self.assertEqual(rows[self.fx.t_harvest]["state"], "executable")
        # 旧版管护未获延期结转 → 不出现在新版清单
        self.assertNotIn(self.fx.t_tend, rows)
        # 已完成的旧任务也不再出现
        self.assertNotIn(self.fx.t_replant, rows)
        # 新版任务在列
        self.assertEqual(rows[t_new]["kind"], "current")
        self.assertEqual(rows[t_new]["state"], "not_started")

        # 历史计划：两个版本及各自原始计划窗口完整保留，延期不改历史
        history = self.cal.plan_history(self.fx.plan_id)
        by_no = {v["version_no"]: v for v in history["versions"]}
        self.assertEqual(by_no["2025版"]["status"], "superseded")
        self.assertEqual(by_no["2026版"]["status"], "active")
        old_tasks = {t["title"]: t for t in by_no["2025版"]["tasks"]}
        self.assertEqual(
            old_tasks["抚育采收"]["original_planned_end"], date(2025, 6, 30)
        )
        self.assertEqual(
            old_tasks["冬季管护"]["original_planned_start"], date(2026, 1, 5)
        )
        self.assertEqual(
            [t["title"] for t in by_no["2026版"]["tasks"]], ["春季管护"]
        )
        self.assertTrue(ver2)

    # ---------------------------------------------------------- 拆分与交接

    def test_split_keeps_parent_history_and_children_take_remaining(self) -> None:
        self.cal.record_receipt(
            self.fx.t_harvest, "RC-2", 120, date(2025, 6, 5), by="李四",
        )
        approval = self.cal.request_deferral(
            self.fx.t_harvest,
            date(2025, 8, 1), date(2026, 2, 28),
            reason="道路水毁跨年度", requested_by="李四",
            requested_at=date(2025, 7, 5),
        )
        self.cal.decide_approval(
            approval, "approved", decided_by="县林业局",
            decided_at=date(2025, 7, 12),
        )
        # 余量 80 必须与拆分合计一致，否则拒绝
        with self.assertRaises(DomainError) as ctx:
            self.cal.split_task(
                self.fx.t_harvest,
                [{"qty": 50, "title": "剩余采收-东段"}],
                requested_by="李四", split_at=date(2026, 2, 10),
            )
        self.assertEqual(ctx.exception.code, "bad_split")

        child_ids = self.cal.split_task(
            self.fx.t_harvest,
            [
                {"qty": 50, "title": "剩余采收-东段", "plot_ids": [self.fx.plot_b]},
                {"qty": 30, "title": "剩余采收-西段", "plot_ids": [self.fx.plot_b]},
            ],
            requested_by="李四", split_at=date(2026, 2, 10),
        )
        # 拆分后不能对父任务再回执或再拆分
        with self.assertRaises(DomainError):
            self.cal.record_receipt(
                self.fx.t_harvest, "RC-3", 10, date(2026, 2, 12), by="李四",
            )

        # 拆分时点之前：仍是父任务
        rows_before, result_before = self.fx.row(
            self.cal, self.fx.t_harvest, date(2026, 2, 9)
        )
        self.assertIsNotNone(rows_before)
        for cid in child_ids:
            self.assertNotIn(cid, [r["task_id"] for r in result_before["rows"]])

        # 拆分时点之后：父任务退场，两个子任务承担余量
        result = self.cal.checklist(self.fx.plan_id, date(2026, 3, 2))
        rows = {r["task_id"]: r for r in result["rows"]}
        self.assertNotIn(self.fx.t_harvest, rows)
        for cid, qty in zip(child_ids, (50, 30)):
            self.assertEqual(rows[cid]["state"], "overdue")
            self.assertEqual(rows[cid]["qty"], qty)
            self.assertEqual(rows[cid]["original_window"]["end"], date(2026, 2, 28))
        # 父任务详情仍保留原计划、120 方回执与子任务索引
        detail = self.cal.task_detail(self.fx.t_harvest)
        self.assertEqual(detail["original_window"]["end"], date(2025, 6, 30))
        self.assertEqual(detail["completed_qty"], 120)
        self.assertEqual([r["receipt_id"] for r in detail["receipts"]], ["RC-2"])
        self.assertEqual(detail["children"], child_ids)

        # 责任人交接给东段负责人，历史责任人保留
        self.cal.handover(
            child_ids[0], "采收三队", "周七",
            by="李四", at=date(2026, 3, 5), note="队伍调整",
        )
        detail_child = self.cal.task_detail(child_ids[0])
        history_resp = detail_child["responsible_history"]
        self.assertEqual(history_resp[0]["person"], "李四")
        self.assertEqual(detail_child["current_responsible"]["person"], "周七")
        # 时点推演：交接前是李四，交接后是周七
        result_before_h = self.cal.checklist(self.fx.plan_id, date(2026, 3, 4))
        row_before = next(r for r in result_before_h["rows"]
                          if r["task_id"] == child_ids[0])
        self.assertEqual(row_before["responsible"]["person"], "李四")
        result_after = self.cal.checklist(self.fx.plan_id, date(2026, 3, 6))
        row_after = next(r for r in result_after["rows"]
                         if r["task_id"] == child_ids[0])
        self.assertEqual(row_after["responsible"]["person"], "周七")

    # ---------------------------------------------------------- 重启总验

    def test_full_state_identical_after_restart(self) -> None:
        self.cal.issue_restriction(
            self.fx.plan_id, "JG-X", "禁作", POLICY, [REPLANT],
            date(2025, 3, 5), date(2025, 3, 15),
            source="县林业局", issued_at=date(2025, 3, 1),
        )
        self.cal.record_receipt(
            self.fx.t_harvest, "RC-9", 30, date(2025, 6, 1), by="李四",
        )
        snapshots = [
            date(2025, 3, 10),
            date(2025, 7, 1),
            date(2026, 1, 20),
        ]
        before = [self.cal.checklist(self.fx.plan_id, d) for d in snapshots]
        reloaded = self.fx.reload()
        after = [reloaded.checklist(self.fx.plan_id, d) for d in snapshots]
        self.assertEqual(
            [(r["task_id"], r["state"]) for s in before for r in s["rows"]],
            [(r["task_id"], r["state"]) for s in after for r in s["rows"]],
        )
        self.assertEqual(
            self.cal.plan_history(self.fx.plan_id),
            reloaded.plan_history(self.fx.plan_id),
        )

    def _restriction_id(self, code: str) -> str:
        for r in self.cal.restrictions.values():
            if r["code"] == code:
                return r["restriction_id"]
        raise KeyError(code)


class RuleValidationTest(unittest.TestCase):
    """边界规则校验。"""

    def setUp(self) -> None:
        self.fx = CalendarFixture()
        self.cal = self.fx.cal
        self.plan_id = self.fx.plan_id
        self.t_replant = self.fx.t_replant
        self.t_harvest = self.fx.t_harvest
        self.t_tend = self.fx.t_tend

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_unknown_plan_task_version_rejected(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.cal.checklist("plan_missing", date(2025, 3, 1))
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(DomainError):
            self.cal.create_task(
                self.fx.plan_id, "2099版", 2025, TEND, "x",
                [self.fx.plot_a], 1, "亩",
                date(2025, 3, 1), date(2025, 3, 2),
                "队", "人",
            )
        with self.assertRaises(DomainError):
            self.cal.create_task(
                self.fx.plan_id, "2025版", 2025, "采伐", "x",
                [self.fx.plot_a], 1, "亩",
                date(2025, 3, 1), date(2025, 3, 2),
                "队", "人",
            )

    def test_duplicate_version_no_rejected(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.cal.add_version(
                self.fx.plan_id, "2025版", "重复版本",
                approved_at=date(2026, 1, 1), approver="县林业局",
            )
        self.assertEqual(ctx.exception.status, 409)

    def test_deferral_must_extend_effective_end(self) -> None:
        with self.assertRaises(DomainError):
            self.cal.request_deferral(
                self.fx.t_harvest,
                date(2025, 5, 1), date(2025, 6, 15),
                reason="比原计划还早", requested_by="李四",
            )

    def test_checklist_before_first_version(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.cal.checklist(self.fx.plan_id, date(2024, 12, 1))
        self.assertEqual(ctx.exception.code, "no_version")


if __name__ == "__main__":
    unittest.main()
