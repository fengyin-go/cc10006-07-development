"""聚合口径异常隔离与修复闭环的演示数据。

在通用示例数据之外补一条「销售额聚合视图」，并把目录、血缘、质量指标串成同一条链路：
- 上游清洗表 SILV-SALES-0001 已启用，调度周期 T+1；
- 聚合视图 GOLD-SALES-0001 初始健康，后续由演示接口把三种口径异常一次性构造出来；
- 目录 CATA-SALES-0001（已发布）、血缘节点 LINE-SALES-0001（已启用）、
  质量指标 QUAL-SALES-0001（告警级别：严重）都按视图编号关联。
"""
from __future__ import annotations

from typing import Any

DEMO_VIEW_NO = "GOLD-SALES-0001"
DEMO_UPSTREAM_NO = "SILV-SALES-0001"

DEMO_ROWS: dict[str, list[dict[str, Any]]] = {
    "silver": [
        {
            "id": 4,
            "status": "已启用",
            "pending": False,
            "abnormal": False,
            "表编号": DEMO_UPSTREAM_NO,
            "上游原始表": "BRON-SALES-0001",
            "清洗脚本": "sales_clean.sql",
            "格式校验": "通过",
            "异常过滤": "剔除金额为负的记录",
            "去重字段": "订单号",
            "输出路径": "silver/sales",
            "调度周期": "T+1",
        }
    ],
    "gold": [
        {
            "id": 4,
            "status": "已启用",
            "pending": False,
            "abnormal": False,
            "视图编号": DEMO_VIEW_NO,
            "上游清洗表": DEMO_UPSTREAM_NO,
            "聚合维度": "区域",
            "度量字段": "销售额",
            "聚合函数": "SUM",
            "物化策略": "全量物化",
            "更新频率": "每日",
            "数据新鲜度": "T+1",
            "结论状态": "未生成",
            "当前结论": None,
            "最近批次号": None,
            "异常数量": 0,
            "阻塞原因": [],
            "同步状态": "正常",
        }
    ],
    "catalog": [
        {
            "id": 4,
            "status": "已发布",
            "pending": False,
            "abnormal": False,
            "编目编号": "CATA-SALES-0001",
            "数据集名称": DEMO_VIEW_NO,
            "存储路径": "gold/sales",
            "文件格式": "Parquet",
            "所属分层": "聚合层",
            "负责人": "值班管理员",
            "业务域标签": "销售分析",
            "编目版本": "v1",
            "关联视图": DEMO_VIEW_NO,
        }
    ],
    "lineage": [
        {
            "id": 4,
            "status": "已启用",
            "pending": False,
            "abnormal": False,
            "节点编号": "LINE-SALES-0001",
            "节点类型": "聚合视图",
            "节点名称": DEMO_VIEW_NO,
            "上游节点列表": DEMO_UPSTREAM_NO,
            "下游节点列表": "销售看板",
            "转换说明": "按区域聚合销售额",
            "采集时间": "2026-09-04",
            "图谱版本": "v1",
            "关联视图": DEMO_VIEW_NO,
        }
    ],
    "quality": [
        {
            "id": 4,
            "status": "已启用",
            "pending": False,
            "abnormal": False,
            "指标编号": "QUAL-SALES-0001",
            "监控对象": DEMO_VIEW_NO,
            "指标类型": "聚合口径校验",
            "阈值范围": "阻塞原因=0",
            "检测周期": "每次重算",
            "告警级别": "严重",
            # 聚合层同类异常默认级别是「中」，这里故意配置成「严重」，
            # 演示跨模块冲突时以质量告警台账级别为准。
            "聚合层告警级别": "中",
            "通知渠道": "站内",
            "恢复策略": "修复并重算后自动关闭",
            "关联视图": DEMO_VIEW_NO,
        }
    ],
}
