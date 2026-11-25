<template>
  <section class="page batch-drill-page" data-module="batch_drill">
    <header class="page-head">
      <div>
        <h2>批量下钻数据集</h2>
        <p class="page-desc">
          一次归并多条下钻路径的来源明细：交叠父路径读取同一子来源时按贡献身份只计一次，
          各路径实际读取区间逐条可复查；分群对比先用批次数据核对覆盖，缺失来源不按 0 计入。
        </p>
      </div>
    </header>

    <form class="batch-form" data-testid="batch-form" @submit.prevent="submitBatch">
      <fieldset class="path-picker">
        <legend>选择父路径（同一次批量下钻）</legend>
        <label v-for="path in pathOptions" :key="path.id" class="path-option">
          <input
            v-model="selectedPaths"
            type="checkbox"
            :value="path.id"
            :data-testid="`path-option-${path.id}`"
          />
          <span>路径 {{ path.id }} · {{ path.路径标识 }}</span>
        </label>
        <span v-if="!pathOptions.length" class="empty-state">暂无可选下钻路径</span>
      </fieldset>
      <label class="batch-field">
        <span>统计期</span>
        <input v-model="period" data-testid="batch-period" placeholder="YYYY-MM-DD~YYYY-MM-DD" />
      </label>
      <label class="batch-field">
        <span>指标</span>
        <input v-model="metric" data-testid="batch-metric" placeholder="支付金额" />
      </label>
      <button class="btn primary" data-testid="create-batch" type="submit" :disabled="creating">
        {{ creating ? '归并中…' : '发起批量下钻' }}
      </button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="batch-error">{{ errorMessage }}</p>
    <p v-if="notice" class="notice-text" data-testid="batch-notice">{{ notice }}</p>

    <template v-if="view">
      <div class="stat-row" data-testid="batch-summary">
        <article class="stat-card">
          <span class="stat-label">批次实算金额（去重后）</span>
          <strong class="stat-value" data-testid="actual-total">{{ view.summary.actual_total }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">交叠金额（重复部分）</span>
          <strong class="stat-value" data-testid="overlap-total">{{ view.summary.overlap_amount }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">各路径原始累加</span>
          <strong class="stat-value" data-testid="raw-total">{{ view.summary.raw_received_total }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">实际覆盖</span>
          <strong class="stat-value" data-testid="coverage">
            {{ view.summary.groups_covered }}/{{ view.summary.groups_total }} 组 ·
            {{ view.summary.coverage_pct }}%
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">缺失来源</span>
          <strong class="stat-value" data-testid="missing-count">{{ view.summary.missing_source_count }}</strong>
        </article>
      </div>

      <p class="dedup-note" data-testid="dedup-rule">
        归并规则：{{ view.summary.dedup_rule }}；批次 {{ view.batch.batch_name }}
        · 统计期 {{ view.batch.period }} · 状态 {{ view.batch.status }}
      </p>

      <h3 class="section-title">各父路径实际读取范围</h3>
      <table class="data-table" data-testid="group-table">
        <thead>
          <tr>
            <th>分组</th><th>路径</th><th>来源数</th><th>应收</th><th>实收</th>
            <th>覆盖</th><th>缺失来源</th><th>实际读取范围</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="group in view.groups" :key="group.group" :data-testid="`group-${group.group}`">
            <td>{{ group.group }}</td>
            <td>路径 {{ group.path_id }} · {{ group.path_name }}</td>
            <td>{{ group.source_count }}</td>
            <td>{{ group.expected_amount }}</td>
            <td>{{ group.received_amount ?? '—' }}</td>
            <td>{{ group.coverage_pct }}%<span v-if="!group.complete" class="warn-tag">未齐</span></td>
            <td>{{ group.missing_sources.join('、') || '—' }}</td>
            <td>
              <span v-if="group.actual_read_range">{{ group.actual_read_range }}</span>
              <span v-else class="warn-text">统计期内未读取</span>
              <ul class="range-list">
                <li v-for="range in group.read_ranges" :key="range.source_id">
                  {{ range.source_id }}：{{ range.read_start ?? '—' }} ~ {{ range.read_end ?? '—' }}
                  <em>{{ range.read_note || range.status }}</em>
                </li>
              </ul>
            </td>
          </tr>
        </tbody>
      </table>

      <h3 class="section-title">来源明细（按贡献身份去重）</h3>
      <table class="data-table" data-testid="source-table">
        <thead>
          <tr>
            <th>来源</th><th>贡献身份</th><th>读取路径</th><th>应收</th><th>实收</th>
            <th>计一次</th><th>交叠金额</th><th>状态</th><th>读取区间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="source in view.sources" :key="source.contribution_identity" :data-testid="`source-${source.source_id}`">
            <td>{{ source.source_name }}<small>({{ source.source_id }})</small></td>
            <td>{{ source.contribution_identity }}</td>
            <td>{{ source.groups.join('、') }}</td>
            <td>{{ source.expected_amount }}</td>
            <td>{{ source.received_amount ?? '—' }}</td>
            <td>{{ source.counted_once ? `是（${source.duplicate_group_count} 条路径）` : '—' }}</td>
            <td>{{ source.overlap_amount }}</td>
            <td>{{ source.status }}</td>
            <td>{{ source.read_start ?? '—' }} ~ {{ source.read_end ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h3 class="section-title">补证记录（可续做）</h3>
      <table class="data-table" data-testid="evidence-table">
        <thead>
          <tr><th>补证号</th><th>来源</th><th>区间</th><th>原因</th><th>状态</th><th>回执</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in view.evidence" :key="item.evidence_id" :data-testid="`evidence-${item.evidence_id}`">
            <td>EV-{{ item.evidence_id }}</td>
            <td>{{ item.source_name }}<small>({{ item.source_id }})</small></td>
            <td>{{ item.interval }}</td>
            <td>{{ item.reason }}</td>
            <td>{{ item.status }}</td>
            <td>
              <span v-if="item.receipt_id">{{ receiptLabel(item.receipt_id) }}</span>
              <span v-else>—</span>
            </td>
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`claim-${item.source_id}`"
                @click="claimFor(item)"
              >
                领取补证
              </button>
            </td>
          </tr>
          <tr v-if="!view.evidence.length">
            <td colspan="7" class="empty-state">当前批次没有缺失来源，无需补证</td>
          </tr>
        </tbody>
      </table>

      <h3 class="section-title">领取回执与补数据</h3>
      <table class="data-table" data-testid="receipt-table">
        <thead>
          <tr>
            <th>回执</th><th>来源</th><th>区间</th><th>状态</th><th>申请次数</th>
            <th>送入次数</th><th>补证金额</th><th>读取区间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="receipt in view.receipts" :key="receipt.receipt_id" :data-testid="`receipt-${receipt.receipt_no}`">
            <td>{{ receipt.receipt_no }}</td>
            <td>{{ receipt.source_id }}</td>
            <td>{{ receipt.interval }}</td>
            <td>{{ receipt.status }}</td>
            <td>{{ receipt.request_count }}</td>
            <td>{{ receipt.submission_count }}</td>
            <td>{{ receipt.amount ?? '—' }}</td>
            <td>{{ receipt.read_start ?? '—' }} ~ {{ receipt.read_end ?? '—' }}</td>
            <td>
              <form class="inline-form" @submit.prevent="submitFor(receipt)">
                <input
                  v-model.number="submitAmounts[receipt.receipt_id]"
                  type="number"
                  step="0.01"
                  min="0"
                  :data-testid="`submit-amount-${receipt.receipt_id}`"
                  placeholder="金额"
                />
                <button class="link" type="submit" :data-testid="`submit-${receipt.receipt_id}`">送入数据</button>
              </form>
            </td>
          </tr>
          <tr v-if="!view.receipts.length">
            <td colspan="9" class="empty-state">尚未领取补证</td>
          </tr>
        </tbody>
      </table>

      <h3 class="section-title">分群对比（先核对覆盖，再出统计）</h3>
      <form class="compare-form" data-testid="compare-form" @submit.prevent="runCompare">
        <label>
          <span>分组 A</span>
          <select v-model="segA" data-testid="seg-a">
            <option v-for="group in view.groups" :key="group.group" :value="group.group">
              {{ group.group }} · 路径 {{ group.path_id }}
            </option>
          </select>
        </label>
        <label>
          <span>分组 B</span>
          <select v-model="segB" data-testid="seg-b">
            <option v-for="group in view.groups" :key="group.group" :value="group.group">
              {{ group.group }} · 路径 {{ group.path_id }}
            </option>
          </select>
        </label>
        <button class="btn primary" data-testid="run-compare" type="submit" :disabled="comparing">
          {{ comparing ? '核对中…' : '发起分群对比' }}
        </button>
      </form>

      <div v-if="compare" class="coverage-box" data-testid="coverage-box">
        <p :class="compare.coverage.complete ? 'ok-text' : 'warn-text'" data-testid="coverage-message">
          {{ compare.message }}
        </p>
        <p class="coverage-rule">{{ compare.coverage.coverage_rule }}；缺失按 0 计入：{{ compare.coverage.zero_filled ? '是' : '否' }}</p>
        <ul>
          <li>
            参与对比：{{ compare.seg_a }} vs {{ compare.seg_b }} · 统计期 {{ compare.period }}
          </li>
          <li v-for="group in compare.coverage.all_groups_coverage" :key="group.group">
            {{ group.group }} 覆盖 {{ group.coverage_pct }}%
            <template v-if="group.missing_sources.length">（缺失 {{ group.missing_sources.join('、') }}）</template>
          </li>
        </ul>
        <div v-if="compare.report" class="compare-report" data-testid="compare-report">
          <span>结果：<strong>{{ compare.result }}</strong></span>
          <span>方向：{{ compare.direction }}</span>
          <span>均值：A {{ compare.report.mean_a }} / B {{ compare.report.mean_b }}</span>
          <span>P 值：{{ compare.report.p_value }}</span>
          <span>数据点：A {{ compare.report.data_points_a }} / B {{ compare.report.data_points_b }}</span>
          <span>方案 ID：{{ compare.plan_id }}<em v-if="compare.duplicate">（同一请求复用方案）</em></span>
        </div>
        <p v-else class="warn-text" data-testid="compare-pending">
          覆盖未齐，未出统计；补齐缺失来源后可直接复算。
        </p>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  claimEvidence,
  compareBatch,
  createBatch,
  listDrillPaths,
  submitEvidence,
} from '@/api/batch-drill'
import type { BatchView, CompareResult, EvidenceRecord, Receipt } from '@/api/batch-drill'

const pathOptions = ref<{ id: number; 路径标识: string; 起始指标: string }[]>([])
const selectedPaths = ref<number[]>([1, 2, 3])
const period = ref('2026-10-01~2026-10-31')
const metric = ref('支付金额')
const segA = ref('P1')
const segB = ref('P3')
const batchName = ref('')

const view = ref<BatchView | null>(null)
const compare = ref<CompareResult | null>(null)
const submitAmounts = ref<Record<number, number | undefined>>({})

const creating = ref(false)
const comparing = ref(false)
const errorMessage = ref('')
const notice = ref('')

function receiptLabel(receiptId: number): string {
  const receipt = view.value?.receipts.find((item) => item.receipt_id === receiptId)
  return receipt ? receipt.receipt_no : `#${receiptId}`
}

async function loadPaths() {
  try {
    const items = await listDrillPaths()
    pathOptions.value = items.map((item) => ({
      id: Number(item.id),
      路径标识: String(item.路径标识 ?? `路径${item.id}`),
      起始指标: String(item.起始指标 ?? ''),
    }))
  } catch (error) {
    pathOptions.value = [1, 2, 3].map((id) => ({ id, 路径标识: `下钻路径 ${id}`, 起始指标: '' }))
  }
}

async function submitBatch() {
  creating.value = true
  errorMessage.value = ''
  notice.value = ''
  compare.value = null
  try {
    if (!selectedPaths.value.length) throw new Error('至少选择一条父路径')
    const result = await createBatch({
      path_ids: selectedPaths.value,
      period: period.value,
      metric: metric.value,
      batch_name: batchName.value || undefined,
    })
    view.value = result.batch
    const groups = result.batch.groups.map((group) => group.group)
    segA.value = groups[0] ?? ''
    segB.value = groups[groups.length - 1] ?? ''
    notice.value = `批次 ${result.batch.batch.batch_id} 已归并，${result.batch.summary.missing_source_count} 个来源待补证`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量下钻归并失败'
  } finally {
    creating.value = false
  }
}

async function claimFor(item: EvidenceRecord) {
  if (!view.value) return
  errorMessage.value = ''
  notice.value = ''
  try {
    const result = await claimEvidence(view.value.batch.batch_id, item.source_id)
    view.value = result.batch
    compare.value = null
    notice.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领取补证失败'
  }
}

async function submitFor(receipt: Receipt) {
  errorMessage.value = ''
  notice.value = ''
  try {
    const amount = submitAmounts.value[receipt.receipt_id]
    if (amount === undefined || amount === null || Number.isNaN(amount)) {
      throw new Error('请先填写补证金额')
    }
    const result = await submitEvidence(receipt.receipt_id, { amount })
    view.value = result.batch
    compare.value = null
    notice.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补证数据送入失败'
  }
}

async function runCompare() {
  if (!view.value) return
  comparing.value = true
  errorMessage.value = ''
  notice.value = ''
  try {
    compare.value = await compareBatch(view.value.batch.batch_id, {
      seg_a: segA.value,
      seg_b: segB.value,
    })
    view.value = compare.value.batch
    notice.value = compare.value.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分群对比失败'
  } finally {
    comparing.value = false
  }
}

onMounted(loadPaths)
</script>

<style scoped>
.batch-form,
.compare-form {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: flex-end;
  padding: 12px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 12px;
}
.path-picker {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  flex-basis: 100%;
}
.path-picker legend {
  font-size: 12px;
  color: var(--muted);
  padding: 0 4px;
}
.path-option {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.batch-field,
.compare-form label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}
.batch-field input,
.compare-form select {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
}
.section-title {
  font-size: 14px;
  margin: 18px 0 8px;
}
.dedup-note {
  font-size: 12px;
  color: var(--muted);
  margin: 0 0 6px;
}
.range-list {
  margin: 4px 0 0;
  padding-left: 16px;
  font-size: 11px;
  color: var(--muted);
}
.range-list em {
  font-style: normal;
  margin-left: 4px;
}
.inline-form {
  display: flex;
  gap: 6px;
  align-items: center;
}
.inline-form input {
  width: 80px;
  padding: 4px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
}
.warn-tag {
  display: inline-block;
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 4px;
  background: #fef3c7;
  color: #b45309;
  font-size: 11px;
}
.warn-text {
  color: #b45309;
}
.ok-text {
  color: #15803d;
}
.notice-text {
  color: #1d4ed8;
  font-size: 13px;
}
.coverage-box {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  font-size: 13px;
}
.coverage-box ul {
  margin: 8px 0;
  padding-left: 18px;
  color: var(--muted);
}
.coverage-rule {
  font-size: 12px;
  color: var(--muted);
  margin: 4px 0;
}
.compare-report {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  padding-top: 8px;
  border-top: 1px solid #f0f2f5;
}
.compare-report em {
  font-style: normal;
  color: var(--muted);
}
</style>
