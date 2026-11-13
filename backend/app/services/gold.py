"""聚合口径异常隔离与修复闭环的业务规则。

核心约束：
1. 度量字段缺失、聚合函数不兼容、上游校验失败都属于「聚合口径异常」。
   只要存在未处置异常，视图进入「待修复」，启用动作被阻止，不允许继续生成结论。
2. 多个异常同时出现时按严重程度排序：上游校验失败 > 度量字段缺失 > 聚合函数不兼容；
   同级按发现时间先后，再按异常编号，保证顺序稳定可复现。
3. 每次计算（首次计算或修复后重算）落在独立批次里；异常批次按当时的字段取值与发生时间留档，
   修复后的新结果只写入新批次，不回填旧批次。
4. 视图报错、不生效或指标丢失时，当前结论标记为「已失效」并写明失效原因，不残留旧结论。
5. 目录状态、血缘节点、质量告警台账随批次同步；同一异常在质量监控与聚合层给出不同
   告警级别时，以质量告警台账级别为准。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from app.store import store

MODULE = "gold"
REQUIRED_FIELDS = ["视图编号", "上游清洗表", "聚合维度"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
REPAIR_STATUS = "待修复"
ACTION_RULES = {"提交校验": "待校验", "启用聚合": "已启用", "停用聚合": "已停用"}
NEGATIVE_ACTIONS = ["停用聚合"]
# 允许落地聚合结论的状态；待修复不在其中，调度器也不应读取待修复视图的结论。
COMPUTABLE_STATUSES = {"已启用"}

ANOMALY_TYPES = {
    # 排序权重越小优先级越高；告警级别仅在质量台账没有配置级别时兜底使用。
    "上游校验失败": {"等级": "P0", "告警级别": "严重", "排序": 0},
    "聚合视图报错": {"等级": "P0", "告警级别": "严重", "排序": 0},
    "度量字段缺失": {"等级": "P1", "告警级别": "高", "排序": 1},
    "指标丢失": {"等级": "P1", "告警级别": "高", "排序": 1},
    "聚合函数不兼容": {"等级": "P2", "告警级别": "中", "排序": 2},
    "结论不生效": {"等级": "P1", "告警级别": "高", "排序": 1},
}
SUPPORTED_AGGREGATIONS = ("SUM", "COUNT", "AVG", "MAX", "MIN")
# 数值类聚合在没有度量字段时无法验证口径，按「聚合函数不兼容」拦截。
NUMERIC_ONLY_AGGREGATIONS = ("SUM", "AVG", "MAX", "MIN")
ALERT_STATUS_OPEN = "待处理"
ALERT_STATUS_CLOSED = "已关闭"
BATCH_STATUS_VALID = "有效"
BATCH_STATUS_BLOCKED = "已阻断"
BATCH_INVALID = "已失效"
# 结论失效原因：报错、不生效、指标丢失都必须显式标注，不能残留旧结论。
RESULT_INVALID_KINDS = ("聚合视图报错", "结论不生效", "指标丢失")


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _blocking_summary(items: list[dict[str, Any]]) -> str:
    return "；".join(f"{item['严重等级']} {item['异常类型']}" for item in items)


class AggregationError(Exception):
    """业务侧可读的拒绝原因：路由层把它转成 ok=false 的返回。"""


class GoldService:
    # ---- 列表与登记 ------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("视图编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._view(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._view(row) if row is not None else None

    def find_by_view_no(self, view_no: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("视图编号") == view_no:
                return row
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not _norm(values.get(field))]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["度量字段"] = values.get("度量字段")
        entry["聚合函数"] = values.get("聚合函数")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["结论状态"] = "未生成"
        entry["当前结论"] = None
        entry["结失效原因"] = None
        entry["失效时间"] = None
        entry["最近批次号"] = None
        entry["待修复批次号"] = None
        entry["异常数量"] = 0
        entry["阻塞原因"] = []
        entry["同步状态"] = "正常"
        rows.append(entry)
        return self._view(entry), []

    # ---- 异常检测与隔离 ---------------------------------------------------
    def seed_caliber_anomalies(self, view_no: str) -> dict[str, Any]:
        """演示构造：在指定视图上同时制造上游校验失败、度量字段缺失、聚合函数不支持。"""
        row = self.find_by_view_no(view_no)
        if row is None:
            raise AggregationError(f"聚合视图 {view_no} 不存在或已归档")
        upstream = self._find_upstream(_norm(row.get("上游清洗表")))
        if upstream is not None:
            # 上游校验失败：登记一次校验失败事件，模拟上游数据校验未通过。
            store.upstream_validation_failures()[view_no] = {
                "上游清洗表": _norm(row.get("上游清洗表")),
                "校验结论": "金额字段出现空值，上游校验未通过",
                "校验时间": _now(),
            }
            upstream["abnormal"] = True
        row["度量字段"] = None
        row["聚合函数"] = "MEDIAN"
        return self.inspect_caliber(int(row["id"]))

    def inspect_caliber(self, entry_id: int, *, persist: bool = True) -> dict[str, Any]:
        """检测一条聚合视图的口径异常；persist=True 时登记异常并把视图置为待修复。

        每个隔离周期复用一个异常批次，重复检测不会不停制造新历史；修复后才开启新批次。
        """
        row = store.find(MODULE, entry_id)
        if row is None:
            raise AggregationError(f"聚合视图 {entry_id} 不存在或已归档")
        view_no = str(row.get("视图编号") or f"GOLD-{entry_id}")
        cycle = _norm(row.get("待修复批次号"))
        if not cycle:
            cycle = store.next_batch_id()
            row["待修复批次号"] = cycle
        detected = self._detect(row, batch_id=cycle)
        if persist:
            self._persist_anomalies(row, detected)
            if detected:
                self._isolate(row, detected, batch_id=cycle, trigger="口径检测")
            elif _norm(row.get("status")) == REPAIR_STATUS:
                # 已无未处置异常：结束隔离周期，视图回到停用/原状态等待重新计算。
                self._release_isolation(row, batch_id=cycle)
        return {
            "entry": self._view(row),
            "视图编号": view_no,
            "批次号": cycle,
            "异常": self._sorted(detected),
            "阻塞原因": self.blockers(view_no, batch_id=cycle),
        }

    def _detect(self, row: dict[str, Any], *, batch_id: str) -> list[dict[str, Any]]:
        """按三类口径逐项检测；每条异常都带发生时间与当时的字段取值。"""
        at = _now()
        view_no = str(row.get("视图编号") or "")
        upstream_no = _norm(row.get("上游清洗表"))
        upstream = self._find_upstream(upstream_no)
        failure_event = store.upstream_validation_failures().get(view_no)
        anomalies: list[dict[str, Any]] = []

        if not upstream_no:
            anomalies.append(self._anomaly(
                view_no, batch_id, "上游校验失败", at=at,
                detail="未登记上游清洗表，无法读取上游校验结果",
                evidence={"上游清洗表": row.get("上游清洗表")},
            ))
        elif upstream is None:
            anomalies.append(self._anomaly(
                view_no, batch_id, "上游校验失败", at=at,
                detail=f"上游清洗表 {upstream_no} 不存在或已归档",
                evidence={"上游清洗表": upstream_no},
            ))
        elif not self._upstream_ok(upstream) or failure_event is not None:
            failure = failure_event or {}
            anomalies.append(self._anomaly(
                view_no, batch_id, "上游校验失败", at=failure.get("校验时间") or at,
                detail=(
                    f"上游清洗表 {upstream_no} 校验未通过"
                    f"（状态={_norm(upstream.get('status')) or '未知'}，"
                    f"格式校验={_norm(upstream.get('格式校验')) or '未记录'}）"
                    + (f"；校验结论：{failure['校验结论']}" if failure.get("校验结论") else "")
                ),
                evidence={
                    "上游清洗表": upstream_no,
                    "上游状态": _norm(upstream.get("status")),
                    "格式校验": _norm(upstream.get("格式校验")),
                    "校验结论": failure.get("校验结论"),
                },
            ))
        else:
            freshness = _norm(row.get("数据新鲜度"))
            upstream_cycle = _norm(upstream.get("调度周期"))
            if freshness and upstream_cycle and freshness != upstream_cycle:
                # 数据新鲜度与上游清洗状态不一致时按上游校验失败处理，不能继续生成。
                anomalies.append(self._anomaly(
                    view_no, batch_id, "上游校验失败", at=at,
                    detail=(
                        f"数据新鲜度 {freshness} 与上游清洗表 {upstream_no} "
                        f"调度周期 {upstream_cycle} 不一致"
                    ),
                    evidence={
                        "数据新鲜度": freshness,
                        "上游调度周期": upstream_cycle,
                        "上游清洗表": upstream_no,
                    },
                ))

        if not _norm(row.get("度量字段")):
            anomalies.append(self._anomaly(
                view_no, batch_id, "度量字段缺失", at=at,
                detail="聚合视图未维护度量字段，无法计算指标",
                evidence={"度量字段": row.get("度量字段"), "聚合维度": _norm(row.get("聚合维度"))},
            ))

        agg = _norm(row.get("聚合函数")).upper()
        metric = _norm(row.get("度量字段"))
        if not agg:
            anomalies.append(self._anomaly(
                view_no, batch_id, "聚合函数不兼容", at=at,
                detail="聚合函数为空，按不兼容口径拦截",
                evidence={"聚合函数": row.get("聚合函数"), "支持函数": list(SUPPORTED_AGGREGATIONS)},
            ))
        elif agg not in SUPPORTED_AGGREGATIONS:
            anomalies.append(self._anomaly(
                view_no, batch_id, "聚合函数不兼容", at=at,
                detail=f"聚合函数 {agg} 不在支持范围 {', '.join(SUPPORTED_AGGREGATIONS)}",
                evidence={"聚合函数": agg, "支持函数": list(SUPPORTED_AGGREGATIONS)},
            ))
        elif agg in NUMERIC_ONLY_AGGREGATIONS and not metric:
            # 数值类聚合在没有度量字段时无法验证口径，按不兼容拦截；补齐度量字段后复检通过。
            anomalies.append(self._anomaly(
                view_no, batch_id, "聚合函数不兼容", at=at,
                detail=f"聚合函数 {agg} 需要数值度量字段，当前度量字段为空，无法验证口径",
                evidence={"聚合函数": agg, "度量字段": row.get("度量字段")},
            ))
        if self._indicator_state(row) == "指标丢失":
            anomalies.append(self._anomaly(
                view_no, batch_id, "指标丢失", at=at,
                detail="最近批次的结论缺少指标值或明细，下游无法读取聚合指标",
                evidence={"最近批次号": row.get("最近批次号")},
            ))
        return anomalies

    @staticmethod
    def _indicator_state(row: dict[str, Any]) -> str | None:
        """已生成过结论的视图，若最近结论缺指标值/明细，按「指标丢失」处理。"""
        result = row.get("当前结论")
        if not isinstance(result, dict):
            return None
        if result.get("指标值") is None or not result.get("明细"):
            return "指标丢失"
        return None

    @staticmethod
    def _anomaly(
        view_no: str,
        batch_id: str,
        anomaly_type: str,
        *,
        at: str,
        detail: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        meta = ANOMALY_TYPES[anomaly_type]
        return {
            "异常编号": None,
            "视图编号": view_no,
            "批次号": batch_id,
            "异常类型": anomaly_type,
            "严重等级": meta["等级"],
            "排序权重": meta["排序"],
            "告警级别": meta["告警级别"],
            "发生时间": at,
            "详情": detail,
            "检测证据": dict(evidence),
            "处置状态": "未处置",
            "处置时间": None,
        }

    @staticmethod
    def _find_upstream(upstream_no: str) -> dict[str, Any] | None:
        if not upstream_no:
            return None
        for row in store.rows("silver"):
            if _norm(row.get("表编号")) == upstream_no:
                return row
        return None

    @staticmethod
    def _upstream_ok(upstream: dict[str, Any]) -> bool:
        status = _norm(upstream.get("status"))
        check = _norm(upstream.get("格式校验"))
        # 种子数据里格式校验是自由文本；只有「已启用」且校验明确通过（或未记录）才算通过。
        return status == "已启用" and check in {"通过", "已启用", ""}

    def validate_upstream(self, view_no: str, *, revalidate: bool = False) -> dict[str, Any]:
        """上游校验：默认读取已有校验失败事件；revalidate=True 时重新跑一次上游校验并清除失败事件。"""
        failures = store.upstream_validation_failures()
        if revalidate and view_no in failures:
            failure = failures[view_no]
            failure["复检时间"] = _now()
            # 重算前复检通过，失败事件关闭；旧异常仍留在历史批次里。
            failure["处置状态"] = "已通过"
            del failures[view_no]
            return {"视图编号": view_no, "复检": True, "结论": "通过", "复检时间": failure["复检时间"]}
        if view_no in failures:
            return {"视图编号": view_no, "复检": False, "结论": "未通过", **failures[view_no]}
        return {"视图编号": view_no, "复检": False, "结论": "通过"}

    def _persist_anomalies(self, row: dict[str, Any], anomalies: list[dict[str, Any]]) -> None:
        """登记异常到全局台账；同视图同类型同批次的未处置异常只保留一条。"""
        for anomaly in anomalies:
            duplicate = next(
                (
                    item for item in store.aggregation_anomalies()
                    if item["视图编号"] == anomaly["视图编号"]
                    and item["批次号"] == anomaly["批次号"]
                    and item["异常类型"] == anomaly["异常类型"]
                    and item["处置状态"] == "未处置"
                ),
                None,
            )
            if duplicate is not None:
                continue
            anomaly["异常编号"] = f"ANOM-{len(store.aggregation_anomalies()) + 1:04d}"
            store.aggregation_anomalies().append(anomaly)

    def _invalidate_latest_batch(self, row: dict[str, Any], *, reason: str, at: str) -> None:
        """把最近一个已生成结果的批次标记失效；异常/阻挡批次本来就无结论，跳过。"""
        latest = _norm(row.get("最近批次号"))
        if not latest:
            return
        batch = store.aggregation_batches().get((str(row.get("视图编号")), latest))
        if batch is None or batch.get("结论状态") != BATCH_STATUS_VALID:
            return
        batch["结论状态"] = BATCH_INVALID
        batch["失效原因"] = reason
        batch["失效时间"] = at

    def _record_blocked_batch(self, row: dict[str, Any], anomalies: list[dict[str, Any]], batch_id: str, trigger: str) -> None:
        """把异常批次留档；旧批次（含此前的有效结论）保持原样，不被覆盖。"""
        view_no = str(row.get("视图编号"))
        batches = store.aggregation_batches()
        if (view_no, batch_id) in batches:
            return
        batches[(view_no, batch_id)] = {
            "批次号": batch_id,
            "视图编号": view_no,
            "触发方式": trigger,
            "计算时间": _now(),
            "上游清洗表": _norm(row.get("上游清洗表")),
            "上游状态": self._upstream_state(row),
            "度量字段": _norm(row.get("度量字段")),
            "聚合函数": _norm(row.get("聚合函数")).upper(),
            "聚合维度": _norm(row.get("聚合维度")),
            "结论": None,
            "结论状态": BATCH_STATUS_BLOCKED,
            "异常原因": [
                {
                    "异常编号": item["异常编号"],
                    "异常类型": item["异常类型"],
                    "严重等级": item["严重等级"],
                    "发生时间": item["发生时间"],
                    "详情": item["详情"],
                    "当时的字段取值": dict(item["检测证据"]),
                }
                for item in self._sorted(anomalies)
            ],
            "口径快照": {
                "度量字段": row.get("度量字段"),
                "聚合函数": row.get("聚合函数"),
                "上游清洗表": row.get("上游清洗表"),
            },
        }

    def _isolate(self, row: dict[str, Any], anomalies: list[dict[str, Any]], *, batch_id: str, trigger: str) -> None:
        """把视图置为待修复：不允许启用，当前结论不残留，并同步质量告警台账。"""
        previous_status = _norm(row.get("status"))
        view_no = str(row.get("视图编号"))
        row["status"] = REPAIR_STATUS
        row["pending"] = True
        row["abnormal"] = True
        row["原状态"] = previous_status
        row["待修复批次号"] = batch_id
        row["异常数量"] = len(self._open_anomalies(view_no))
        row["阻塞原因"] = self.blockers(view_no, batch_id=batch_id)
        row["同步状态"] = "待修复"
        # 出现报错/不生效/指标丢失时不得残留旧结论：当前结论立即失效并写明原因。
        if row.get("当前结论") is not None or row.get("结论状态") in {"有效", "计算中"}:
            reasons = "；".join(f"{item['异常类型']}：{item['详情']}" for item in self._sorted(anomalies))
            row["当前结论"] = None
            row["结论状态"] = "已失效"
            row["结失效原因"] = f"口径异常触发隔离（{reasons}）"
            row["失效时间"] = _now()
            # 历史批次内容保留，但结论标记失效，下游不能再把它当成有效结论读取。
            self._invalidate_latest_batch(row, reason=row["结失效原因"], at=row["失效时间"])
        else:
            row["结论状态"] = "未生成"
        self._record_blocked_batch(row, anomalies, batch_id, trigger)
        self._sync_view(row, pending_batch=batch_id)

    def _release_isolation(self, row: dict[str, Any], *, batch_id: str) -> None:
        """异常已全部消除但走的是复检而不是修复：视图解除隔离，回到待校验等待重新计算。"""
        view_no = str(row.get("视图编号"))
        row["status"] = "待校验"
        row["pending"] = True
        row["abnormal"] = False
        row["原状态"] = None
        row["待修复批次号"] = None
        row["异常数量"] = 0
        row["阻塞原因"] = []
        row["同步状态"] = "待生成"
        row["结论状态"] = "未生成" if row.get("当前结论") is None else row.get("结论状态")
        self._sync_view(row, pending_batch=batch_id)

    def blockers(self, view_no: str, *, batch_id: str | None = None) -> list[dict[str, Any]]:
        """按严重程度排序的阻塞原因；指定批次时只看该批次，便于页面直接展示。"""
        items = [
            item for item in store.aggregation_anomalies()
            if item["视图编号"] == view_no
            and item["处置状态"] == "未处置"
            and (batch_id is None or item["批次号"] == batch_id)
        ]
        return [
            {
                "异常编号": item["异常编号"],
                "异常类型": item["异常类型"],
                "严重等级": item["严重等级"],
                "告警级别": item["告警级别"],
                "发生时间": item["发生时间"],
                "详情": item["详情"],
                "批次号": item.get("批次号"),
            }
            for item in self._sorted(items)
        ]

    @staticmethod
    def _sorted(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items,
            key=lambda item: (item.get("排序权重", 9), item.get("发生时间", ""), str(item.get("异常编号") or "")),
        )

    def _open_anomalies(self, view_no: str) -> list[dict[str, Any]]:
        return [
            item for item in store.aggregation_anomalies()
            if item["视图编号"] == view_no and item["处置状态"] == "未处置"
        ]

    def mark_result_invalid(self, entry_id: int, kind: str, detail: str) -> dict[str, Any]:
        """运行期报错/结论不生效/指标丢失：当前结论立即失效，视图回到待修复并同步台账。"""
        if kind not in RESULT_INVALID_KINDS:
            raise AggregationError(f"不支持的失效类型「{kind}」，可选：{'、'.join(RESULT_INVALID_KINDS)}")
        if not _norm(detail):
            raise AggregationError("失效说明不能为空，下游需要知道旧结论为什么不能再使用")
        row = store.find(MODULE, entry_id)
        if row is None:
            raise AggregationError(f"聚合视图 {entry_id} 不存在或已归档")
        view_no = str(row.get("视图编号") or f"GOLD-{entry_id}")
        batch_id = store.next_batch_id()
        anomaly = self._anomaly(
            view_no, batch_id, kind, at=_now(), detail=_norm(detail),
            evidence={"失效类型": kind, "原结论状态": row.get("结论状态")},
        )
        self._persist_anomalies(row, [anomaly])
        self._isolate(row, [anomaly], batch_id=batch_id, trigger="运行期失效")
        return {"entry": self._view(row), "批次号": batch_id, "失效原因": _norm(detail)}

    # ---- 计算与修复 -------------------------------------------------------
    def compute(self, entry_id: int) -> dict[str, Any]:
        """为视图生成结论：只允许已启用且无未处置异常；每次生成独立批次。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            raise AggregationError(f"聚合视图 {entry_id} 不存在或已归档")
        view_no = str(row.get("视图编号") or f"GOLD-{entry_id}")
        if _norm(row.get("status")) == REPAIR_STATUS:
            raise AggregationError(
                f"聚合视图 {view_no} 处于待修复状态，请先修复阻塞原因再重新计算"
            )
        if _norm(row.get("status")) not in COMPUTABLE_STATUSES:
            raise AggregationError(
                f"聚合视图 {view_no} 当前状态为 {_norm(row.get('status')) or '未知'}，不允许生成结论"
            )

        batch_id = store.next_batch_id()
        detected = self._detect(row, batch_id=batch_id)
        if detected:
            # 检测到异常：立即隔离，拒绝生成，错误指标不流入下游。
            self._persist_anomalies(row, detected)
            self._isolate(row, detected, batch_id=batch_id, trigger="计算前校验")
            raise AggregationError(
                f"检测到 {len(detected)} 项聚合口径异常，聚合视图已置为待修复并阻止生成"
            )

        result = self._calculate(row)
        batch = {
            "批次号": batch_id,
            "视图编号": view_no,
            "触发方式": "重新计算",
            "计算时间": _now(),
            "上游清洗表": _norm(row.get("上游清洗表")),
            "上游状态": self._upstream_state(row),
            "度量字段": _norm(row.get("度量字段")),
            "聚合函数": _norm(row.get("聚合函数")).upper(),
            "聚合维度": _norm(row.get("聚合维度")),
            "结论": result,
            "结论状态": BATCH_STATUS_VALID,
            "异常原因": [],
            "口径快照": {
                "度量字段": row.get("度量字段"),
                "聚合函数": row.get("聚合函数"),
                "上游清洗表": row.get("上游清洗表"),
            },
        }
        store.aggregation_batches()[(view_no, batch_id)] = batch
        row["最近批次号"] = batch_id
        row["待修复批次号"] = None
        row["当前结论"] = result
        row["结论状态"] = "有效"
        row["结失效原因"] = None
        row["失效时间"] = None
        row["同步状态"] = "正常"
        self._sync_view(row, pending_batch=batch_id)
        return {"entry": self._view(row), "批次": dict(batch)}

    def _upstream_state(self, row: dict[str, Any]) -> str:
        upstream = self._find_upstream(_norm(row.get("上游清洗表")))
        return _norm(upstream.get("status")) if upstream else "未知"

    def _calculate(self, row: dict[str, Any]) -> dict[str, Any]:
        """示例计算：按聚合维度对度量字段做聚合，给出可核对的结果结构。"""
        agg = _norm(row.get("聚合函数")).upper()
        metric = _norm(row.get("度量字段"))
        dimension = _norm(row.get("聚合维度"))
        samples = [
            {"区域": "华东", "销售额": 1200.0},
            {"区域": "华南", "销售额": 860.0},
            {"区域": "华北", "销售额": 940.0},
        ]
        values = [item[metric] for item in samples if metric in item]
        total: Any
        if agg == "SUM":
            total = round(sum(values), 2)
        elif agg == "COUNT":
            total = len(values)
        elif agg == "AVG":
            total = round(sum(values) / len(values), 2) if values else 0
        elif agg == "MAX":
            total = max(values) if values else None
        elif agg == "MIN":
            total = min(values) if values else None
        else:
            total = None
        return {
            "度量字段": metric,
            "聚合函数": agg,
            "聚合维度": dimension,
            "指标值": total,
            "明细": [
                {"维度值": item.get(dimension) or dimension, "指标值": item.get(metric)}
                for item in samples
            ],
        }

    def _rollback_aggregation(self, row: dict[str, Any], fixed_fields: list[str]) -> dict[str, Any] | None:
        """修复前回滚受影响批次：被修复字段参与过的结论标记失效，避免下游继续读到错误指标。"""
        view_no = str(row.get("视图编号"))
        affected = [
            batch for batch in self._history_for(view_no)
            if batch.get("结论状态") == BATCH_STATUS_VALID
            and any(
                _norm(batch.get("口径快照", {}).get(field)) != _norm(row.get(field))
                for field in fixed_fields
            )
        ]
        if not affected:
            return None
        reverted_at = _now()
        for batch in affected:
            batch["结论状态"] = "已回滚"
            batch["失效原因"] = "口径字段修复，该批次结论已回滚"
            batch["失效时间"] = reverted_at
        if row.get("当前结论") is not None:
            row["当前结论"] = None
            row["结论状态"] = "已失效"
            row["结失效原因"] = "口径字段修复导致旧结论回滚，需要重新计算"
            row["失效时间"] = reverted_at
        return {
            "字段": fixed_fields[0],
            "回滚批次": [batch["批次号"] for batch in affected],
            "回滚时间": reverted_at,
        }

    def repair(self, entry_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """修复口径字段后重算：只处置已解决的异常，未解决项继续阻塞。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            raise AggregationError(f"聚合视图 {entry_id} 不存在或已归档")
        view_no = str(row.get("视图编号") or f"GOLD-{entry_id}")
        fixed_fields = [field for field in ("上游清洗表", "度量字段", "聚合函数", "数据新鲜度") if field in values]
        if not fixed_fields:
            raise AggregationError("修复至少需要提交上游清洗表、度量字段、聚合函数、数据新鲜度中的一项")
        for field in fixed_fields:
            if values.get(field) is not None:
                row[field] = values.get(field)

        rolled_back = self._rollback_aggregation(row, fixed_fields)

        batch_id = _norm(row.get("待修复批次号")) or store.next_batch_id()
        # 修复动作会触发上游复检：复检通过则清除校验失败事件，复检不通过则继续阻塞。
        self.validate_upstream(view_no, revalidate=True)
        remaining = self._detect(row, batch_id=batch_id)
        remaining_types = {item["异常类型"] for item in remaining}
        resolved: list[dict[str, Any]] = []
        for item in self._open_anomalies(view_no):
            if item["异常类型"] in remaining_types:
                continue
            item["处置状态"] = "已修复"
            item["处置时间"] = _now()
            item["修复字段"] = fixed_fields + ([rolled_back["字段"]] if rolled_back else [])
            # 历史异常保留当时的字段取值；修复后的值只写在新批次里。
            resolved.append(dict(item))

        # 已经通过复检的异常若在本次检测里再次出现，登记为新异常（旧异常仍保留历史原因）。
        self._persist_anomalies(row, remaining)
        if remaining_types:
            if remaining:
                self._isolate(row, remaining, batch_id=batch_id, trigger="修复复检")
            raise AggregationError(
                f"已修复 {len(resolved)} 项，但仍剩 {len(remaining)} 项聚合口径异常，视图保持待修复"
            )

        row["status"] = "已启用"
        row["pending"] = False
        row["abnormal"] = False
        row["异常数量"] = 0
        row["阻塞原因"] = []
        row["原状态"] = None
        row["待修复批次号"] = None
        computed = self.compute(entry_id)
        self._close_alerts(view_no, batch_id=computed["批次"]["批次号"], fixed_fields=fixed_fields)
        computed["已修复异常"] = resolved
        computed["历史批次"] = self._history_for(view_no)
        return computed

    # ---- 历史与跨模块同步 -------------------------------------------------
    def history(self, entry_id: int) -> list[dict[str, Any]]:
        row = store.find(MODULE, entry_id)
        if row is None:
            raise AggregationError(f"聚合视图 {entry_id} 不存在或已归档")
        return self._history_for(str(row.get("视图编号")))

    def _history_for(self, view_no: str) -> list[dict[str, Any]]:
        batch_rows = [
            dict(batch) for key, batch in store.aggregation_batches().items() if key[0] == view_no
        ]
        batch_rows.sort(key=lambda batch: batch["批次号"])
        return batch_rows

    def _sync_view(self, row: dict[str, Any], *, pending_batch: str) -> None:
        """把视图状态同步到目录、血缘节点与质量告警台账。

        三种同步口径：
        - 已隔离：存在阻塞原因或状态为待修复 → 目录/血缘置待修复，登记质量告警；
        - 有效：已启用且结论有效 → 目录已发布、血缘已启用，告警由修复流程关闭；
        - 待生成：草稿/待校验/已停用或结论尚未生成 → 目录草稿、血缘待校验，不产生告警。
        """
        view_no = str(row.get("视图编号"))
        blockers = self.blockers(view_no)
        if blockers or _norm(row.get("status")) == REPAIR_STATUS:
            mode = "isolated"
        elif _norm(row.get("status")) == "已启用" and row.get("结论状态") == "有效":
            mode = "valid"
        else:
            mode = "pending"
        block_types = [item["异常类型"] for item in blockers]

        catalog = self._ensure_catalog(view_no, row)
        if catalog is not None:
            catalog["最近批次号"] = pending_batch
            catalog["阻塞原因"] = block_types if mode == "isolated" else []
            if mode == "isolated":
                catalog["status"] = "待修复"
                catalog["abnormal"] = True
                catalog["pending"] = True
                catalog["目录状态"] = "已隔离"
                catalog["同步状态"] = "待修复"
            elif mode == "valid":
                catalog["status"] = "已发布"
                catalog["abnormal"] = False
                catalog["pending"] = False
                catalog["目录状态"] = "已恢复"
                catalog["同步状态"] = "正常"
            else:
                catalog["status"] = "草稿"
                catalog["abnormal"] = False
                catalog["pending"] = True
                catalog["目录状态"] = "待生成"
                catalog["同步状态"] = "待生成"

        lineage = self._ensure_lineage(view_no, row)
        if lineage is not None:
            lineage["最近批次号"] = pending_batch
            lineage["阻塞原因"] = block_types if mode == "isolated" else []
            if mode == "isolated":
                lineage["status"] = "待修复"
                lineage["abnormal"] = True
                lineage["pending"] = True
                lineage["同步状态"] = "已隔离"
            elif mode == "valid":
                lineage["status"] = "已启用"
                lineage["abnormal"] = False
                lineage["pending"] = False
                lineage["同步状态"] = "已同步"
            else:
                lineage["status"] = "待校验"
                lineage["abnormal"] = False
                lineage["pending"] = True
                lineage["同步状态"] = "待同步"
        row["血缘同步"] = "已隔离" if mode == "isolated" else "已同步"

        if mode == "isolated":
            self._raise_alerts(row, view_no=view_no, batch_id=pending_batch)
            row["质量告警"] = "已登记告警（{}）".format(pending_batch)
        else:
            row["质量告警"] = "无未处置告警"

    def _ensure_catalog(self, view_no: str, row: dict[str, Any]) -> dict[str, Any] | None:
        for item in store.rows("catalog"):
            if _norm(item.get("关联视图")) == view_no or _norm(item.get("数据集名称")) == view_no:
                return item
        entry = {
            "id": max((int(item.get("id", 0)) for item in store.rows("catalog")), default=0) + 1,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "编目编号": f"CATA-{view_no}",
            "数据集名称": view_no,
            "存储路径": f"gold/{_norm(row.get('聚合维度')) or 'default'}",
            "文件格式": "Parquet",
            "所属分层": "聚合层",
            "负责人": "值班管理员",
            "业务域标签": "销售分析",
            "编目版本": "v1",
            "关联视图": view_no,
        }
        store.rows("catalog").append(entry)
        return entry

    def _ensure_lineage(self, view_no: str, row: dict[str, Any]) -> dict[str, Any] | None:
        for item in store.rows("lineage"):
            if _norm(item.get("关联视图")) == view_no or _norm(item.get("节点名称")) == view_no:
                return item
        entry = {
            "id": max((int(item.get("id", 0)) for item in store.rows("lineage")), default=0) + 1,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "节点编号": f"LINE-{view_no}",
            "节点类型": "聚合视图",
            "节点名称": view_no,
            "上游节点列表": _norm(row.get("上游清洗表")),
            "下游节点列表": "销售看板",
            "转换说明": "按{}聚合{}".format(
                _norm(row.get("聚合维度")) or "维度", _norm(row.get("度量字段")) or "度量"
            ),
            "采集时间": _now()[:10],
            "图谱版本": "v1",
            "关联视图": view_no,
        }
        store.rows("lineage").append(entry)
        return entry

    def _raise_alerts(self, row: dict[str, Any], *, view_no: str, batch_id: str) -> None:
        """把口径异常写进质量告警台账；同一异常已有关闭记录时新开一条，避免覆盖历史。"""
        quality = self._ensure_quality_indicator(view_no, row)
        quality_level = _norm(quality.get("告警级别")) if quality else ""
        open_blockers = self.blockers(view_no)
        block_reason = _blocking_summary(open_blockers) or f"批次 {batch_id} 存在未处置口径异常"
        for anomaly in open_blockers:
            existing = next(
                (
                    alert for alert in store.quality_alerts()
                    if alert["视图编号"] == view_no
                    and alert["异常编号"] == anomaly["异常编号"]
                ),
                None,
            )
            if existing is not None and existing["处置状态"] == ALERT_STATUS_OPEN:
                existing["阻塞原因"] = block_reason
                continue
            self._append_alert(
                anomaly, view_no, anomaly["批次号"], quality_level, block_reason,
                reopened=existing is not None,
            )

    def _append_alert(
        self,
        anomaly: dict[str, Any],
        view_no: str,
        batch_id: str,
        quality_level: str,
        block_reason: str,
        *,
        reopened: bool = False,
    ) -> None:
        # 级别冲突以质量告警台账（质量监控模块）为准；台账未配置级别时才用口径默认级别。
        level = quality_level or anomaly["告警级别"]
        source = "质量监控台账" if quality_level else "聚合口径默认"
        alert = {
            "告警编号": f"QALT-{len(store.quality_alerts()) + 1:04d}",
            "指标编号": f"QUAL-{view_no}",
            "视图编号": view_no,
            "批次号": batch_id,
            "异常编号": anomaly["异常编号"],
            "异常类型": anomaly["异常类型"],
            "严重等级": anomaly["严重等级"],
            "告警级别": level,
            "级别来源": source,
            "聚合层告警级别": anomaly["告警级别"],
            "冲突判定": "以质量告警级别为准" if level != anomaly["告警级别"] else "一致",
            "监控对象": view_no,
            "详情": anomaly["详情"],
            "阻塞原因": block_reason,
            "状态": ALERT_STATUS_OPEN,
            "处置状态": ALERT_STATUS_OPEN,
            "发生时间": anomaly["发生时间"],
            "登记时间": _now(),
            "关闭时间": None,
            "重开": reopened,
        }
        store.quality_alerts().append(alert)

    def _ensure_quality_indicator(self, view_no: str, row: dict[str, Any]) -> dict[str, Any] | None:
        for item in store.rows("quality"):
            if _norm(item.get("关联视图")) == view_no or _norm(item.get("监控对象")) == view_no:
                return item
        entry = {
            "id": max((int(item.get("id", 0)) for item in store.rows("quality")), default=0) + 1,
            "status": "已启用",
            "pending": False,
            "abnormal": False,
            "指标编号": f"QUAL-{view_no}",
            "监控对象": view_no,
            "指标类型": "聚合口径校验",
            "阈值范围": "阻塞原因=0",
            "检测周期": "每次重算",
            "告警级别": "严重",
            "通知渠道": "站内",
            "恢复策略": "修复并重算后自动关闭",
            "关联视图": view_no,
        }
        store.rows("quality").append(entry)
        return entry

    def _close_alerts(self, view_no: str, *, batch_id: str, fixed_fields: list[str]) -> None:
        for alert in store.quality_alerts():
            if alert["视图编号"] != view_no or alert["处置状态"] != ALERT_STATUS_OPEN:
                continue
            alert["处置状态"] = ALERT_STATUS_CLOSED
            alert["状态"] = ALERT_STATUS_CLOSED
            alert["关闭时间"] = _now()
            alert["关闭批次号"] = batch_id
            alert["修复字段"] = fixed_fields

    def list_anomalies(
        self,
        *,
        view_no: str | None = None,
        batch_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        items = [
            dict(item) for item in store.aggregation_anomalies()
            if (not view_no or item["视图编号"] == view_no)
            and (not batch_id or item["批次号"] == batch_id)
            and (not status or item["处置状态"] == status)
        ]
        return self._sorted(items)

    def alerts(
        self,
        *,
        view_no: str | None = None,
        status: str | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        items = list(store.quality_alerts())
        if view_no:
            items = [item for item in items if item["视图编号"] == view_no]
        if status:
            items = [item for item in items if item["处置状态"] == status]
        if level:
            items = [item for item in items if item["告警级别"] == level]
        # 严重等级优先、发生时间其次，保证台账可读且稳定。
        items.sort(
            key=lambda item: (
                ANOMALY_TYPES.get(item["异常类型"], {}).get("排序", 9),
                item.get("发生时间", ""),
                item.get("告警编号", ""),
            )
        )
        return items

    def override_alert_level(self, alert_id: str, level: str) -> dict[str, Any]:
        """跨模块冲突处置：质量告警台账级别优先，覆盖聚合层默认级别并留痕。"""
        alert = store.find_quality_alert(alert_id)
        if alert is None:
            raise AggregationError(f"质量告警 {alert_id} 不存在或已归档")
        if not level:
            raise AggregationError("告警级别不能为空")
        alert["原告警级别"] = alert.get("告警级别")
        alert["告警级别"] = level
        alert["级别来源"] = "质量监控台账"
        alert["冲突判定"] = "以质量告警级别为准"
        alert["级别调整时间"] = _now()
        return dict(alert)

    # ---- 动作 ------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合视图 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于聚合层管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "启用聚合":
            open_items = self._open_anomalies(str(entry.get("视图编号")))
            if open_items:
                ordered = self.blockers(str(entry.get("视图编号")))
                batch_id = _norm(entry.get("待修复批次号")) or store.next_batch_id()
                self._isolate(entry, self._sorted(open_items), batch_id=batch_id, trigger="启用拦截")
                return None, (
                    f"聚合视图处于待修复，禁止启用；阻塞原因：{_blocking_summary(ordered)}"
                )
            entry["status"] = target
            entry["pending"] = False
            entry["abnormal"] = False
            self._sync_view(entry, pending_batch=_norm(entry.get("最近批次号")) or store.next_batch_id())
            return self._view(entry), "聚合视图已启用"
        if action == "提交校验":
            # 待修复视图也允许提交校验：重新检测口径，未解决则继续隔离并返回最新阻塞原因。
            result = self.inspect_caliber(entry_id)
            if result["异常"]:
                return self._view(entry), (
                    f"口径复检仍有 {len(result['异常'])} 项异常：{_blocking_summary(self._sorted(result['异常']))}"
                )
            if _norm(entry.get("结论状态")) == "有效":
                return self._view(entry), "口径复检通过，当前结论仍然有效"
            return self._view(entry), "口径复检通过，可重新计算生成结论"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "停用聚合":
            # 停用即不再生成结论；当前结论必须失效，不能继续被下游读取。
            if entry.get("当前结论") is not None:
                entry["当前结论"] = None
                entry["结论状态"] = "已失效"
                entry["结失效原因"] = "聚合视图已停用，旧结论不再生效"
                entry["失效时间"] = _now()
                self._invalidate_latest_batch(
                    entry, reason=entry["结失效原因"], at=entry["失效时间"]
                )
            self._sync_view(entry, pending_batch=_norm(entry.get("最近批次号")) or store.next_batch_id())
        return self._view(entry), f"聚合视图已{action}"

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        """返回给前端/接口的视图结构：阻塞原因与异常按严重程度排序。"""
        view_no = str(row.get("视图编号"))
        data = dict(row)
        # 种子数据里没有口径闭环字段，这里补齐默认值，页面不必到处判空。
        data.setdefault("结论状态", "未生成")
        data.setdefault("当前结论", None)
        data.setdefault("结失效原因", None)
        data.setdefault("失效时间", None)
        data.setdefault("最近批次号", None)
        data.setdefault("待修复批次号", None)
        data.setdefault("同步状态", "正常")
        data.setdefault("血缘同步", "已同步")
        data.setdefault("质量告警", "无未处置告警")
        data["阻塞原因"] = self.blockers(view_no)
        data["异常数量"] = len(self._open_anomalies(view_no))
        data["历史批次"] = self._history_for(view_no)
        return data
