"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS
from app.seed_demo import DEMO_ROWS


def _seed_rows() -> dict[str, list[dict[str, Any]]]:
    """通用示例数据叠加演示链路；同编号的记录以演示数据为准，避免重复。"""
    merged: dict[str, list[dict[str, Any]]] = {
        name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
    }
    for name, demo_rows in DEMO_ROWS.items():
        base = merged.setdefault(name, [])
        existing = {str(row.get("id")) for row in base}
        base.extend(dict(row) for row in demo_rows if str(row.get("id")) not in existing)
    return merged


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = _seed_rows()
        # 聚合口径异常记录：键为 (视图编号, 批次号)，同批异常聚在一起做优先级排序。
        self._aggregation_anomalies: list[dict[str, Any]] = []
        # 聚合视图历史结论：按批次留档，修复后的新批次不覆盖旧批次。
        self._aggregation_batches: dict[tuple[str, str], dict[str, Any]] = {}
        # 跨模块质量告警台账：与质量监控模块共用同一份数据，级别冲突以台账为准。
        self._quality_alerts: list[dict[str, Any]] = []
        self._batch_counter: int = 0
        # 上游校验失败事件：按视图编号记一次校验失败，修复重算时重新校验并清除。
        self._upstream_validation_failures: dict[str, dict[str, Any]] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    # ---- 聚合口径异常与历史批次 -------------------------------------------
    def aggregation_anomalies(self) -> list[dict[str, Any]]:
        return self._aggregation_anomalies

    def aggregation_batches(self) -> dict[tuple[str, str], dict[str, Any]]:
        return self._aggregation_batches

    def quality_alerts(self) -> list[dict[str, Any]]:
        return self._quality_alerts

    def upstream_validation_failures(self) -> dict[str, dict[str, Any]]:
        return self._upstream_validation_failures

    def find_quality_alert(self, alert_id: str) -> dict[str, Any] | None:
        for alert in self._quality_alerts:
            if alert.get("告警编号") == alert_id:
                return alert
        return None

    def next_batch_id(self) -> str:
        self._batch_counter += 1
        return f"BATCH-{self._batch_counter:04d}"

    def reset(self) -> None:
        """测试与演示复位：回到种子数据，同时清空跨模块的异常与台账。"""
        self._tables = _seed_rows()
        self._aggregation_anomalies = []
        self._aggregation_batches = {}
        self._quality_alerts = []
        self._batch_counter = 0
        self._upstream_validation_failures = {}
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
