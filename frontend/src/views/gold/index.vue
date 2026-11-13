<template>
  <section class="page" data-module="gold">
    <header class="page-head">
      <div>
        <h2>聚合层管理</h2>
        <p class="page-desc">
          维护聚合视图的输入、执行约束与结果记录；口径异常会隔离视图并阻止生成，修复后重算并同步目录、血缘与质量告警。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="constructDemoAnomalies">演示：构造三类口径异常</button>
        <button class="btn" type="button" @click="exportRows">导出聚合层管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>视图编号</span>
        <input v-model="filters.keyword" placeholder="按视图编号检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <div v-if="selected" class="detail-panel">
      <header class="detail-head">
        <div>
          <strong>{{ selected['视图编号'] }}</strong>
          <span class="badge" :class="statusClass(selected.status)">{{ selected.status }}</span>
          <span v-if="selected['结论状态']" class="badge">结论：{{ selected['结论状态'] }}</span>
        </div>
        <button class="btn ghost" type="button" @click="selected = null">收起明细</button>
      </header>

      <p v-if="selected['结失效原因']" class="error-text">
        失效原因：{{ selected['结失效原因'] }}（{{ selected['失效时间'] }}）
      </p>

      <section>
        <h4>阻塞原因（按严重程度排序）</h4>
        <table class="data-table">
          <thead>
            <tr><th>优先级</th><th>异常类型</th><th>告警级别</th><th>发生时间</th><th>详情</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in blockers" :key="item['异常编号']">
              <td>{{ item['严重等级'] }}</td>
              <td>{{ item['异常类型'] }}</td>
              <td>{{ item['告警级别'] }}</td>
              <td>{{ item['发生时间'] }}</td>
              <td>{{ item['详情'] }}</td>
            </tr>
            <tr v-if="!blockers.length">
              <td colspan="5" class="empty-state">当前没有阻塞原因</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section>
        <h4>口径修复</h4>
        <form class="filter-bar" @submit.prevent="repair">
          <label class="filter-item">
            <span>度量字段</span>
            <input v-model="repairForm['度量字段']" placeholder="例如：销售额" />
          </label>
          <label class="filter-item">
            <span>聚合函数</span>
            <input v-model="repairForm['聚合函数']" placeholder="例如：SUM" />
          </label>
          <label class="filter-item">
            <span>上游清洗表</span>
            <input v-model="repairForm['上游清洗表']" placeholder="例如：SILV-SALES-0001" />
          </label>
          <label class="filter-item">
            <span>数据新鲜度</span>
            <input v-model="repairForm['数据新鲜度']" placeholder="例如：T+1" />
          </label>
          <button class="btn primary" type="submit">修复并重新计算</button>
        </form>
      </section>

      <section>
        <h4>历史批次（修复后的新结果不会覆盖旧批次）</h4>
        <table class="data-table">
          <thead>
            <tr><th>批次号</th><th>触发方式</th><th>计算时间</th><th>结论状态</th><th>度量字段</th><th>聚合函数</th><th>异常原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="batch in historyBatches" :key="batch['批次号']">
              <td>{{ batch['批次号'] }}</td>
              <td>{{ batch['触发方式'] }}</td>
              <td>{{ batch['计算时间'] }}</td>
              <td>{{ batch['结论状态'] }}</td>
              <td>{{ batch['度量字段'] || '—' }}</td>
              <td>{{ batch['聚合函数'] || '—' }}</td>
              <td>
                <span v-for="reason in batch['异常原因'] || []" :key="reason['异常编号']" class="reason-item">
                  {{ reason['严重等级'] }} {{ reason['异常类型'] }}（{{ reason['发生时间'] }}）
                </span>
                <span v-if="!(batch['异常原因'] || []).length">—</span>
              </td>
            </tr>
            <tr v-if="!historyBatches.length">
              <td colspan="7" class="empty-state">暂无历史批次</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section>
        <h4>质量告警台账（级别冲突以质量告警为准）</h4>
        <table class="data-table">
          <thead>
            <tr><th>告警编号</th><th>异常类型</th><th>告警级别</th><th>级别来源</th><th>状态</th><th>登记时间</th><th>关闭时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="alert in viewAlerts" :key="alert['告警编号']">
              <td>{{ alert['告警编号'] }}</td>
              <td>{{ alert['异常类型'] }}</td>
              <td>{{ alert['告警级别'] }}</td>
              <td>{{ alert['级别来源'] }}</td>
              <td>{{ alert['处置状态'] }}</td>
              <td>{{ alert['登记时间'] }}</td>
              <td>{{ alert['关闭时间'] || '—' }}</td>
            </tr>
            <tr v-if="!viewAlerts.length">
              <td colspan="7" class="empty-state">暂无质量告警</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>异常 / 阻塞</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-blocked': row.status === '待修复' }">
          <td v-for="column in columns" :key="column">
            <span v-if="column === '状态'">
              <span class="badge" :class="statusClass(row.status)">{{ row.status }}</span>
            </span>
            <span v-else>{{ row[column] ?? '—' }}</span>
          </td>
          <td>
            <span v-if="row['异常数量']">{{ row['异常数量'] }} 项：{{ blockerSummary(row) }}</span>
            <span v-else>—</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="inspect(row)">口径检测</button>
            <button class="link" type="button" @click="compute(row)">重新计算</button>
            <button class="link" type="button" @click="runAction('启用聚合', row)">启用聚合</button>
            <button class="link" type="button" @click="runAction('停用聚合', row)">停用聚合</button>
            <button class="link" type="button" @click="openDetail(row)">明细</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无聚合层管理数据，可先登记聚合视图</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条聚合层管理记录</span>
      <span v-if="message" class="ok-text">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
type FilterState = { keyword: string; status: string }

const ENDPOINT = '/api/gold'
const columns = ["视图编号", "上游清洗表", "聚合维度", "度量字段", "聚合函数", "物化策略", "更新频率", "数据新鲜度", "状态"]
const statuses = ["草稿", "待校验", "已启用", "待修复", "已停用"]

const rows = ref<Row[]>([])
const total = ref(0)
const message = ref('')
const errorMessage = ref('')
const filters = ref<FilterState>({ keyword: '', status: '' })
const selected = ref<Row | null>(null)
const viewAlerts = ref<Row[]>([])
const repairForm = ref<Record<string, string>>({
  '度量字段': '',
  '聚合函数': '',
  '上游清洗表': '',
  '数据新鲜度': '',
})

const blockers = computed<Row[]>(() => selected.value?.['阻塞原因'] ?? [])
const historyBatches = computed<Row[]>(() => selected.value?.['历史批次'] ?? [])

const statCards = computed(() => [
  { label: '聚合视图总数', value: total.value },
  { label: '已启用视图', value: rows.value.filter((row) => row.status === '已启用').length },
  { label: '待修复视图', value: rows.value.filter((row) => row.status === '待修复').length },
  { label: '未处置异常', value: rows.value.reduce((sum, row) => sum + Number(row['异常数量'] || 0), 0) },
])

function statusClass(status: string | undefined) {
  if (status === '待修复') return 'badge-danger'
  if (status === '已启用') return 'badge-ok'
  return 'badge-muted'
}

function blockerSummary(row: Row) {
  return (row['阻塞原因'] ?? [])
    .map((item: Row) => `${item['严重等级']} ${item['异常类型']}`)
    .join('；')
}

function resetFilters() {
  filters.value = { keyword: '', status: '' }
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function call(path: string, body?: unknown) {
  const response = await request(path, body === undefined
    ? {}
    : { method: 'POST', body: JSON.stringify(body) })
  return response
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (filters.value.keyword) query.set('keyword', filters.value.keyword)
  if (filters.value.status) query.set('status', filters.value.status)
  try {
    const response = await call(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) throw new Error('聚合视图列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (selected.value) {
      const current = rows.value.find((row) => row.id === selected.value?.id)
      if (current) await openDetail(current)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '聚合层管理列表读取失败'
  }
}

async function openDetail(row: Row) {
  try {
    const response = await call(`${ENDPOINT}/${row.id}`)
    if (!response.ok) throw new Error('聚合视图明细读取失败')
    selected.value = await response.json()
    repairForm.value['度量字段'] = String(selected.value?.['度量字段'] ?? '')
    repairForm.value['聚合函数'] = String(selected.value?.['聚合函数'] ?? '')
    repairForm.value['上游清洗表'] = String(selected.value?.['上游清洗表'] ?? '')
    repairForm.value['数据新鲜度'] = String(selected.value?.['数据新鲜度'] ?? '')
    const alerts = await call(`${ENDPOINT}/alerts?view=${encodeURIComponent(String(selected.value?.['视图编号'] ?? ''))}`)
    viewAlerts.value = alerts.ok ? (await alerts.json()).items ?? [] : []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '聚合视图明细读取失败'
  }
}

async function inspect(row: Row) {
  await operate(`${ENDPOINT}/${row.id}/inspect`)
}

async function compute(row: Row) {
  await operate(`${ENDPOINT}/${row.id}/compute`)
}

async function runAction(action: string, row: Row) {
  await operate(`${ENDPOINT}/${row.id}/actions`, { action })
}

async function repair() {
  if (!selected.value) return
  const values: Record<string, string> = {}
  for (const [key, value] of Object.entries(repairForm.value)) {
    if (value) values[key] = value
  }
  await operate(`${ENDPOINT}/${selected.value.id}/repair`, values)
}

async function constructDemoAnomalies() {
  await operate(`${ENDPOINT}/demo/caliber-anomalies`)
}

async function operate(path: string, values?: Record<string, unknown>) {
  message.value = ''
  errorMessage.value = ''
  try {
    const response = await call(path, values === undefined ? undefined : { values })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '操作未生效'
    } else {
      message.value = payload.message ?? '操作已生效'
    }
    await reload()
    if (payload.entry) selected.value = payload.entry
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '聚合层管理操作失败'
  }
}

onMounted(reload)
</script>
