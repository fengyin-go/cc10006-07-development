"""聚合口径异常隔离与修复闭环的回归测试。

覆盖：
- 三类口径异常同时出现时的优先级排序；
- 待修复期间禁止启用、禁止生成结论；
- 修复后重算，目录/血缘/质量告警同步恢复；
- 历史批次保留最初缺失字段与发生时间，修复结果不覆盖旧批次；
- 报错/不生效/指标丢失时旧结论不残留；
- 质量告警级别冲突以质量告警台账为准。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store

DEMO_VIEW_NO = "GOLD-SALES-0001"
DEMO_VIEW_ID = 4


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()
    yield
    store.reset()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _entry(client: TestClient) -> dict:
    return client.get(f"/api/gold/{DEMO_VIEW_ID}").json()


def _construct_three_anomalies(client: TestClient) -> dict:
    response = client.get("/api/gold/demo/caliber-anomalies")
    assert response.status_code == 200
    return response.json()


def test_three_anomalies_isolate_view_and_order_blockers(client: TestClient) -> None:
    client.post(f"/api/gold/{DEMO_VIEW_ID}/compute")
    _construct_three_anomalies(client)

    entry = _entry(client)
    assert entry["status"] == "待修复"
    assert entry["abnormal"] is True
    assert entry["结论状态"] == "已失效"
    assert entry["当前结论"] is None

    blockers = entry["阻塞原因"]
    assert [item["异常类型"] for item in blockers] == [
        "上游校验失败",
        "度量字段缺失",
        "聚合函数不兼容",
    ]
    assert [item["严重等级"] for item in blockers] == ["P0", "P1", "P2"]
    assert entry["异常数量"] == 3
    # 历史批次留下三类异常原因
    blocked_batches = [batch for batch in entry["历史批次"] if batch["结论状态"] == "已阻断"]
    assert len(blocked_batches) == 1
    assert [item["异常类型"] for item in blocked_batches[0]["异常原因"]] == [
        "上游校验失败",
        "度量字段缺失",
        "聚合函数不兼容",
    ]
    assert blocked_batches[0]["异常原因"][0]["当时的字段取值"]["上游清洗表"] == "SILV-SALES-0001"


def test_repair_pending_view_cannot_be_enabled_or_computed(client: TestClient) -> None:
    _construct_three_anomalies(client)

    enable = client.post(
        f"/api/gold/{DEMO_VIEW_ID}/actions", json={"values": {"action": "启用聚合"}}
    ).json()
    assert enable["ok"] is False
    assert "禁止启用" in enable["message"]
    assert "P0 上游校验失败" in enable["message"]

    compute = client.post(f"/api/gold/{DEMO_VIEW_ID}/compute").json()
    assert compute["ok"] is False
    assert "待修复" in compute["message"]


def test_partial_repair_keeps_isolation_until_all_resolved(client: TestClient) -> None:
    _construct_three_anomalies(client)
    response = client.post(
        f"/api/gold/{DEMO_VIEW_ID}/repair", json={"values": {"度量字段": "销售额"}}
    )
    assert response.json()["ok"] is False
    entry = _entry(client)
    assert entry["status"] == "待修复"
    assert [item["异常类型"] for item in entry["阻塞原因"]] == ["聚合函数不兼容"]
    assert entry["当前结论"] is None


def test_repair_recomputes_and_syncs_catalog_lineage_quality(client: TestClient) -> None:
    _construct_three_anomalies(client)
    catalog_before = [
        row for row in client.get("/api/catalog").json()["items"] if row.get("关联视图") == DEMO_VIEW_NO
    ][0]
    assert catalog_before["status"] == "待修复"
    lineage_before = [
        row for row in client.get("/api/lineage").json()["items"] if row.get("关联视图") == DEMO_VIEW_NO
    ][0]
    assert lineage_before["status"] == "待修复"

    response = client.post(
        f"/api/gold/{DEMO_VIEW_ID}/repair",
        json={"values": {"度量字段": "销售额", "聚合函数": "SUM"}},
    )
    payload = response.json()
    assert payload["ok"] is True
    entry = payload["entry"]
    assert entry["status"] == "已启用"
    assert entry["结论状态"] == "有效"
    assert entry["阻塞原因"] == []
    assert entry["当前结论"]["指标值"] == 3000.0

    catalog_after = [
        row for row in client.get("/api/catalog").json()["items"] if row.get("关联视图") == DEMO_VIEW_NO
    ][0]
    assert catalog_after["status"] == "已发布"
    assert catalog_after["abnormal"] is False
    assert catalog_after["阻塞原因"] == []

    lineage_after = [
        row for row in client.get("/api/lineage").json()["items"] if row.get("关联视图") == DEMO_VIEW_NO
    ][0]
    assert lineage_after["status"] == "已启用"
    assert lineage_after["同步状态"] == "已同步"

    open_alerts = client.get("/api/gold/alerts?status=待处理").json()["items"]
    assert open_alerts == []
    closed_alerts = client.get("/api/gold/alerts?status=已关闭").json()["items"]
    assert len(closed_alerts) == 3
    assert all(alert["关闭批次号"] == entry["最近批次号"] for alert in closed_alerts)


def test_history_keeps_original_missing_field_and_time(client: TestClient) -> None:
    _construct_three_anomalies(client)
    before = _entry(client)
    blocked = [batch for batch in before["历史批次"] if batch["结论状态"] == "已阻断"][0]
    original_metric_reason = next(
        item for item in blocked["异常原因"] if item["异常类型"] == "度量字段缺失"
    )
    original_time = original_metric_reason["发生时间"]
    original_evidence = dict(original_metric_reason["当时的字段取值"])

    repaired = client.post(
        f"/api/gold/{DEMO_VIEW_ID}/repair",
        json={"values": {"度量字段": "销售额", "聚合函数": "SUM"}},
    ).json()
    history = client.get(f"/api/gold/{DEMO_VIEW_ID}/history").json()["items"]
    old_batch = next(batch for batch in history if batch["批次号"] == blocked["批次号"])
    old_metric_reason = next(
        item for item in old_batch["异常原因"] if item["异常类型"] == "度量字段缺失"
    )
    # 旧批次不被新结果覆盖：字段取值与发生时间都保持最初记录
    assert old_metric_reason["当时的字段取值"] == original_evidence
    assert old_metric_reason["发生时间"] == original_time
    assert old_batch["结论"] is None
    # 新批次写的是修复后的口径，且与旧批次分开
    new_batch = next(batch for batch in history if batch["批次号"] == repaired["entry"]["最近批次号"])
    assert new_batch["结论状态"] == "有效"
    assert new_batch["度量字段"] == "销售额"
    assert new_batch["口径快照"]["度量字段"] == "销售额"


def test_runtime_failure_invalidates_old_conclusion(client: TestClient) -> None:
    client.post(f"/api/gold/{DEMO_VIEW_ID}/compute")
    entry = _entry(client)
    assert entry["结论状态"] == "有效"

    response = client.post(
        f"/api/gold/{DEMO_VIEW_ID}/result-invalid",
        json={"values": {"失效类型": "指标丢失", "失效说明": "下游看板指标为空"}},
    )
    payload = response.json()
    assert payload["ok"] is True
    entry = payload["entry"]
    assert entry["status"] == "待修复"
    assert entry["结论状态"] == "已失效"
    assert entry["当前结论"] is None
    assert "下游看板指标为空" in entry["结失效原因"]
    history = client.get(f"/api/gold/{DEMO_VIEW_ID}/history").json()["items"]
    invalid_batches = [batch for batch in history if batch["结论状态"] == "已失效"]
    assert invalid_batches
    assert invalid_batches[0]["失效原因"] == entry["结失效原因"]


def test_quality_alert_level_wins_over_aggregation_level(client: TestClient) -> None:
    _construct_three_anomalies(client)
    alerts = client.get("/api/gold/alerts").json()["items"]
    assert {alert["告警级别"] for alert in alerts} == {"严重"}
    assert {alert["级别来源"] for alert in alerts} == {"质量监控台账"}
    metric_alert = next(alert for alert in alerts if alert["异常类型"] == "度量字段缺失")
    assert metric_alert["聚合层告警级别"] == "高"

    level = client.post(
        f"/api/gold/alerts/{alerts[0]['告警编号']}/level",
        json={"values": {"告警级别": "严重"}},
    ).json()
    assert level["ok"] is True
    assert level["entry"]["告警级别"] == "严重"
    assert level["entry"]["冲突判定"] == "以质量告警级别为准"


def test_quality_module_reads_same_alert_ledger(client: TestClient) -> None:
    _construct_three_anomalies(client)
    gold_alerts = client.get("/api/gold/alerts").json()["items"]
    quality_alerts = client.get("/api/quality/alerts").json()["items"]
    assert [item["告警编号"] for item in quality_alerts] == [item["告警编号"] for item in gold_alerts]
