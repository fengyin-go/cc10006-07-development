"""聚合层管理接口：维护聚合视图，覆盖异常检测、口径修复、重新计算与历史留档。

口径异常隔离遵循三条硬约束：待修复期间禁止启用；异常未解决不生成结论；
修复后的新结论写入新批次，历史批次保留当时的异常原因与字段取值。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.gold import AggregationError, GoldService

router = APIRouter(prefix="/api/gold", tags=["聚合层管理"])

service = GoldService()

LIST_FIELDS = ["视图编号", "上游清洗表", "聚合维度", "度量字段", "聚合函数", "物化策略", "更新频率", "数据新鲜度"]
STATUSES = ["草稿", "待校验", "已启用", "待修复", "已停用"]


def _fail(message: str) -> ActionResult:
    return ActionResult(ok=False, message=message)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按视图编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、待修复、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按视图编号与状态过滤聚合层管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/alerts", response_model=dict)
def list_alerts(
    view: str | None = Query(default=None, description="按视图编号过滤"),
    status: str | None = Query(default=None, description="待处理、已关闭"),
) -> dict[str, Any]:
    """质量告警台账：聚合层与质量监控共享同一份数据，级别冲突以台账为准。"""
    items = service.alerts(view_no=view, status=status)
    return {"items": items, "total": len(items)}


@router.get("/anomalies", response_model=dict)
def list_anomalies(
    view: str | None = Query(default=None, description="按视图编号过滤"),
    batch: str | None = Query(default=None, description="按异常批次过滤"),
    status: str | None = Query(default=None, description="未处置、已修复"),
) -> dict[str, Any]:
    """异常台账：按严重程度排序，历史异常保留当时的字段取值与发生时间。"""
    items = service.list_anomalies(view_no=view, batch_id=batch, status=status)
    return {"items": items, "total": len(items)}


@router.get("/demo/caliber-anomalies")
def demo_caliber_anomalies() -> dict[str, Any]:
    """演示构造：在销售额聚合视图上同时制造三类口径异常，并把视图置为待修复。"""
    try:
        result = service.seed_caliber_anomalies("GOLD-SALES-0001")
    except AggregationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"ok": True, "message": "已构造三类口径异常并隔离聚合视图", **result}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出聚合层管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "gold", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条聚合视图明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"聚合视图 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/upstream-validation", response_model=dict)
def upstream_validation(entry_id: int) -> dict[str, Any]:
    """读取上游校验结论：校验失败与数据新鲜度不一致都会阻止聚合视图继续生成。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"聚合视图 {entry_id} 不存在或已归档")
    return service.validate_upstream(str(entry.get("视图编号")))


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条聚合视图，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="聚合视图已登记", entry=entry)


@router.post("/{entry_id}/inspect", response_model=ActionResult)
def inspect_caliber(entry_id: int) -> ActionResult:
    """检测三类口径异常；有异常时视图进入待修复，阻塞原因按严重程度排序返回。"""
    try:
        result = service.inspect_caliber(entry_id)
    except AggregationError as error:
        return _fail(str(error))
    count = len(result["异常"])
    if not count:
        return ActionResult(ok=True, message="口径检测通过，未发现异常", entry=result["entry"])
    return ActionResult(
        ok=True,
        message=f"检测到 {count} 项口径异常，聚合视图已置为待修复",
        entry=result["entry"],
    )


@router.post("/{entry_id}/compute", response_model=ActionResult)
def compute(entry_id: int) -> ActionResult:
    """生成/重算聚合结论；存在口径异常时拒绝生成并返回阻塞原因。"""
    try:
        result = service.compute(entry_id)
    except AggregationError as error:
        return _fail(str(error))
    return ActionResult(
        ok=True,
        message=f"聚合结论已生成（批次 {result['批次']['批次号']}）",
        entry=result["entry"],
    )


@router.post("/{entry_id}/result-invalid", response_model=ActionResult)
def mark_result_invalid(entry_id: int, payload: EntryPayload) -> ActionResult:
    """运行期报错/不生效/指标丢失：旧结论立即失效，视图回到待修复并同步台账。"""
    kind = str(payload.values.get("失效类型") or "").strip()
    detail = str(payload.values.get("失效说明") or "").strip()
    try:
        result = service.mark_result_invalid(entry_id, kind, detail)
    except AggregationError as error:
        return _fail(str(error))
    return ActionResult(
        ok=True,
        message=f"聚合视图结论已失效并进入待修复（{kind}），批次 {result['批次号']}",
        entry=result["entry"],
    )


@router.post("/{entry_id}/repair", response_model=ActionResult)
def repair(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修复口径字段并重新计算；只处置已解决的异常，未解决项继续阻塞。"""
    try:
        result = service.repair(entry_id, payload.values)
    except AggregationError as error:
        # 未完全修复时不带新结论，但把当前阻塞原因回给调用方，便于继续处置。
        return ActionResult(ok=False, message=str(error), entry=service.get_entry(entry_id))
    return ActionResult(
        ok=True,
        message=(
            f"已修复 {len(result['已修复异常'])} 项口径异常并重新计算"
            f"（批次 {result['批次']['批次号']}），历史批次已保留"
        ),
        entry=result["entry"],
    )


@router.get("/{entry_id}/history", response_model=dict)
def history(entry_id: int) -> dict[str, Any]:
    """按批次返回历史结论；修复后的新批次不会覆盖旧批次里的异常原因。"""
    try:
        items = service.history(entry_id)
    except AggregationError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"items": items, "total": len(items)}


@router.post("/alerts/{alert_id}/level", response_model=ActionResult)
def set_alert_level(alert_id: str, payload: EntryPayload) -> ActionResult:
    """跨模块同步质量告警：冲突时以质量告警台账级别为准。"""
    level = str(payload.values.get("告警级别") or "").strip()
    try:
        alert = service.override_alert_level(alert_id, level)
    except AggregationError as error:
        return _fail(str(error))
    return ActionResult(ok=True, message=f"告警级别已按质量告警台账更新为{level}", entry=alert)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条聚合视图执行提交校验、启用聚合、停用聚合；待修复视图禁止启用。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
