# 森林经营方案年历

把“一次编制、多年管用”的森林经营方案拆成有前置关系的年度任务，并关联责任主体、
适用地块、生态限制与批准版本。系统以**追加式事件日志**为唯一事实来源：跨年度延期、
计划拆分、临时禁作期、责任人交接与完成回执都只追加记录、不改原计划；重启服务后
重放日志即可恢复，提醒时钟和待决审批不漂移。

## 运行与测试

```bash
python3 -m unittest            # 全部测试（领域规则 + HTTP 端到端）
python3 -m service.main        # 启动服务，默认 http://0.0.0.0:3000
```

环境变量：

- `PORT`：监听端口（默认 3000）
- `CALENDAR_DATA`：事件日志路径（默认 `data/calendar.jsonl`）；独立部署时指向受控存储目录

仅使用 Python 3.11+ 标准库，无外部依赖，可独立部署。

## 核心概念

| 概念 | 说明 |
| --- | --- |
| 方案 plan | 一个经营主体（企业/林场）的多年方案，挂接适用地块 |
| 版本 version | 已批准的方案版本；换版只追加新版本，旧版本自动标为 superseded，其下任务原样保留 |
| 年度任务 task | 采收 harvest / 补植 replant / 管护 tend，带原计划窗口、数量、地块、责任人、前置任务 |
| 限制 restriction | 政策边界 policy / 灾害风险 disaster / 生态限制 ecology 的禁作窗口，命中动作与地块即暂停任务，可提前解除 |
| 延期 deferral | 跨年度延期须走审批；批准后形成“有效窗口”，**原计划窗口永久保留**为逾期依据 |
| 拆分 split | 原任务保留并退场，子任务按余量承担后续作业；原回执仍挂在原任务 |
| 回执 receipt | 完成量凭证，编号去重、禁止超量 |
| 交接 handover | 责任人变更追加留痕，按时点推演当时责任人 |
| 提醒 reminder | 开工前 7 天 9:00 与到期日 9:00 两个时钟，由有效窗口派生；发送留痕防止重复提醒 |

## 年度检查怎么看

所有读模型都是“状态 + 时点”的纯函数，年度检查可任意指定 `as_of` 模拟：

- **禁作期提前**：`issued_at <= as_of` 的限制才生效，发布前的清单不受影响；
- **任务部分完成**：回执按 `at <= as_of` 计入，逾期依据含已完成量、余量和最后回执；
- **方案换版**：清单只展示当前版本任务，以及经批准延期跨入新年度的 `carry_over` 任务；
  旧版本完整计划通过 `/history` 查阅，原始窗口不被延期改写；
- **重启**：重放 JSONL 日志后，待决审批、可执行清单、提醒时刻与已发送状态完全一致。

任务状态：`executable`（可执行）、`suspended`（禁作暂停）、`overdue_suspended`
（逾期且仍被禁作）、`overdue`（逾期，附依据）、`waiting_prerequisite`（前置未完成）、
`not_started`、`completed`。

## HTTP 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/plans` | 建立方案并登记地块 |
| GET | `/plans/{id}` | 方案与当前版本 |
| POST | `/plans/{id}/versions` | 登记已批准版本（换版） |
| POST | `/plans/{id}/tasks` | 建立年度任务（可带前置） |
| GET | `/plans/{id}/checklist?as_of=YYYY-MM-DD` | 时点可执行清单与逾期依据 |
| GET | `/plans/{id}/history` | 版本沿革与各版本原始计划 |
| POST | `/restrictions` | 发布禁作/限制窗口 |
| POST | `/restrictions/{id}/lift` | 提前解除限制（留痕） |
| POST | `/tasks/{id}/deferrals` | 发起延期审批（可跨年度） |
| POST | `/approvals/{id}/decision` | 批准/驳回 |
| GET | `/approvals/pending` | 待决审批 |
| POST | `/tasks/{id}/split` | 按余量拆分任务 |
| POST | `/tasks/{id}/receipts` | 记录完成回执（去重、防超量） |
| POST | `/tasks/{id}/handover` | 责任人交接 |
| GET | `/tasks/{id}` | 任务详情（原窗口/有效窗口/回执/交接史） |
| GET | `/reminders?now=YYYY-MM-DDTHH:MM:SS` | 提醒时钟与到点状态 |
| POST | `/reminders/delivered` | 标记提醒已发送（幂等保护） |

业务冲突返回 4xx，形如 `{"error": "duplicate_receipt", "message": "..."}`。
