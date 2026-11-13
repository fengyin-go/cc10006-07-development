# 数据湖仓一体管理平台

面向数据湖仓一体架构下的多源异构接入、原始文件编目、湖内分区治理、流批一体转换、版本回溯与数据编目检索的管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 聚合口径异常隔离与修复闭环

聚合视图在生成结论前先做口径检测，三类异常任一命中就进入「待修复」并阻止启用：

| 异常类型 | 严重等级 | 触发条件 |
| --- | --- | --- |
| 上游校验失败 | P0 | 上游清洗表不存在、未启用、校验未通过，或数据新鲜度与上游调度周期不一致 |
| 度量字段缺失 | P1 | 视图未维护度量字段 |
| 聚合函数不兼容 | P2 | 聚合函数为空、不在支持范围，或数值聚合缺少度量字段 |

- 多个异常同时出现时，阻塞原因按 P0 → P1 → P2 排序；同级按发生时间与异常编号稳定排序。
- 待修复期间 `启用聚合` 被拒绝，`compute` 被拒绝，错误指标不会流入下游。
- 视图报错、结论不生效或指标丢失时，当前结论标记「已失效」并写明原因，最近有效批次同步
  标记失效，不残留旧结论。
- 修复只处置复检已通过的异常，未解决项继续阻塞；全部解决后才允许重新计算。
- 每次计算独立批次，历史批次保留当时的字段取值与异常发生时间，新结果只写入新批次。
- 目录、血缘节点、质量告警台账随视图状态同步；同一异常级别冲突时以质量告警台账为准。

演示接口（后端启动后执行）：

```bash
# 构造上游校验失败 + 度量字段缺失 + 聚合函数不支持
curl http://127.0.0.1:8000/api/gold/demo/caliber-anomalies
# 查看阻塞原因、历史批次
curl http://127.0.0.1:8000/api/gold/4
# 修复度量字段后重算
curl -X POST http://127.0.0.1:8000/api/gold/4/repair \
  -H 'Content-Type: application/json' \
  -d '{"values": {"度量字段": "销售额", "聚合函数": "SUM"}}'
```

回归测试：`cd backend && PYTHONPATH=. .venv/bin/python -m pytest tests -q`
（测试依赖 `pytest` 与 `httpx`，如未安装可用 `.venv/bin/pip install pytest httpx` 补齐）。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据编目 | `catalog` | 数据目录 | 编目编号、数据集名称、存储路径 |
| 多源接入 | `ingest` | 接入通道 | 通道编号、源端类型、连接参数 |
| 原始层管理 | `bronze` | 原始表 | 表编号、来源通道、目标路径 |
| 清洗层管理 | `silver` | 清洗表 | 表编号、上游原始表、清洗脚本 |
| 聚合层管理 | `gold` | 聚合视图 | 视图编号、上游清洗表、聚合维度 |
| 湖区分区 | `lakezone` | 分区方案 | 方案编号、目标表名、分区字段 |
| 流式处理 | `stream` | 流处理作业 | 作业编号、输入Topic、处理逻辑 |
| 批量转换 | `batch` | 批处理任务 | 任务编号、输入路径、处理脚本 |
| 版本快照 | `snapshot` | 快照记录 | 快照编号、目标表名、快照时间 |
| 湖仓治理 | `govern` | 治理规则 | 规则编号、治理对象、治理类型 |
| 元数据中心 | `meta` | 元数据条目 | 条目编号、对象类型、对象名称 |
| 血缘图谱 | `lineage` | 血缘节点 | 节点编号、节点类型、节点名称 |
| 质量监控 | `quality` | 质量指标 | 指标编号、监控对象、指标类型 |
| 编排调度 | `schedule` | 编排计划 | 计划编号、任务列表、依赖图定义 |
| 历史回填 | `backfill` | 回填作业 | 作业编号、目标表名、回填起始 |
| 数据检索 | `search` | 检索索引 | 索引编号、索引名称、覆盖表名 |
| 资源计量 | `resource` | 计量记录 | 记录编号、计算引擎、作业名称 |
| 操作审计 | `audit` | 审计日志 | 日志编号、操作类型、操作对象 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
