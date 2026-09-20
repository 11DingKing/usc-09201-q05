# 森林经营方案年历

面向“一次编制、多年管用”的森林经营方案，本服务把多年目标拆解为**有前置关系的年度任务**，
关联责任主体、适用地块、生态限制与**批准版本**，为基层提供可独立部署的年历系统。

- 运行检查：`python3 -m unittest`（22 个测试，含年度检查综合场景）
- 启动服务：`CALENDAR_DB=data/calendar.db PORT=3000 python3 -m service.main`
- 健康检查：`GET /health`；年历接口挂载于 `/calendar/*`

仅依赖 Python 3.11 标准库，事件存储使用 SQLite（默认 `data/calendar.db`，
可由 `CALENDAR_DB` 覆盖），可独立部署。

## 核心原则：只追加，不改旧账

所有状态变化都是**不可变事件**（SQLite 追加表），读取模型每次从事件重放得到。
因此旧计划永远不会被直接改掉，任何修订都留有来源与事件序号：

| 业务情形 | 处理方式 |
| --- | --- |
| 跨年度延期 | 延期申请→林业部门审批；批准后有效到期日=窗口末日+批准延期天数+禁作期顺延 |
| 计划拆分 | 原任务标记 `split` 并原样保留，按**剩余量**生成承接子任务（可跨年度、可分责任人） |
| 临时禁作期 | 生态限制匹配方案/地块/任务类型；当日命中则任务**暂停**，阻断天数顺延到期日 |
| 禁作期提前 | 新通知以 `supersedes` 废止原通知；原通知保留，阻断区间按两者并集计算 |
| 责任人交接 | 交接全程留痕；逾期依据中的责任人为当前责任人，并附完整交接链 |
| 方案换版 | 旧版本任务立即冻结（禁止任何修改/回执），须在新版本以 `source_task_id` 承接 |
| 部分完成 | 回执上报完成量，可累计；超量拒绝；达量须走完工回执 |
| 重复回执 | 回执号全局唯一，重复提交返回冲突且不产生第二次影响 |

**重启不漂移**：提醒扫描与未决审批完全由持久化事件与当日日期推导。每个未结任务
至多一条活动提醒，同一天重复扫描（含进程重启）幂等返回同一批提醒；确认后才会再次提醒。

## 年度检查的三个读取模型

- `GET /calendar/board?today=YYYY-MM-DD` —— **可执行清单**
  `executable / paused / waiting_prerequisites / pending_deferral_decisions / pending_rebaseline / overdue`
- `GET /calendar/overdue?today=...` —— **逾期依据**：原始窗口、历次延期审批、
  禁作期抵扣明细（文号与事件序号）、完成量、责任人交接链、回执、任务血缘
- `GET /calendar/history` —— **历史计划**：方案版本链（批准/被替代时间）、
  各版本任务原计划与承接关系、全部回执与事件序号

逾期判定：`今日 > 窗口末日 + 批准延期天数 + 禁作期顺延天数`，且当日不处于禁作期、
前置已满足、无未决延期审批、所属版本为当前版本。

## 写入接口（均为 POST，JSON）

```
/calendar/plans                      登记方案
/calendar/plans/{id}/versions        批准版本
/calendar/plans/{id}/supersede       换版
/calendar/parcels                    登记地块
/calendar/parties                    登记责任主体
/calendar/restrictions               声明生态限制（supersedes 表示提前修订）
/calendar/restrictions/{id}/revoke   解除限制
/calendar/tasks                      排定年度任务（prerequisites 前置、source_task_id 承接）
/calendar/tasks/{id}/split           拆分
/calendar/tasks/{id}/deferrals       申请延期（可跨年度）
/calendar/tasks/{id}/deferrals/{rid}/decision  审批 approved/rejected
/calendar/tasks/{id}/progress        部分完成回执
/calendar/tasks/{id}/complete        完工回执
/calendar/tasks/{id}/cancel          取消
/calendar/tasks/{id}/assignee        责任人交接
/calendar/reminders/scan             提醒扫描（可带 ?today=）
/calendar/reminders/{id}/ack         确认提醒
```

写接口支持请求头 `Idempotency-Key` 与 `X-Actor`；同一幂等键的重试直接返回首次结果，
不再执行任何校验或写入。

## 代码结构

```
service/calendar/
  clock.py    可注入时钟（SystemClock / FixedClock），判定只依赖业务时间
  events.py   追加式领域事件定义
  store.py    SQLite 事件存储与命令幂等表
  model.py    事件重放状态与三个读取模型
  service.py  命令与校验（应用服务）
service/main.py  HTTP 路由与服务启动
tests/        健康检查、年度检查综合场景、HTTP 与文件库重启持久化
```
