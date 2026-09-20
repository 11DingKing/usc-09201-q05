"""追加式事件存储。

所有状态变更先以 JSONL 追加落盘，再应用到内存；服务重启后重放日志即可恢复，
提醒时钟和待决审批因此不会漂移。日志文件按序号追加，写入与 fsync 均在锁内完成。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any, Iterator


class EventStore:
    """基于单个 JSONL 文件的事件日志。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._seq = 0
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        # 确定下一个序号（不预先读全部事件到业务状态，只扫描头部）
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    self._seq = max(self._seq, event["seq"])

    def append(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "type": event_type,
                "ts": datetime.now().isoformat(timespec="seconds"),
                "data": data,
            }
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def events(self) -> Iterator[dict[str, Any]]:
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)
