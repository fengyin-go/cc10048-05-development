/** 批量下钻数据集接口：多路径归并、补证回执、批次分群对比。 */
import { request } from './client'

export type BatchGroup = {
  group: string
  path_id: number
  path_name: string
  source_count: number
  expected_amount: number
  received_amount: number | null
  coverage_pct: number
  complete: boolean
  missing_sources: string[]
  actual_read_range: string | null
  read_ranges: {
    source_id: string
    source_name: string
    read_start: string | null
    read_end: string | null
    status: string
    read_note: string
  }[]
}

export type BatchSource = {
  source_id: string
  source_name: string
  contribution_identity: string
  expected_amount: number
  received_amount: number | null
  status: string
  counted: boolean
  counted_once: boolean
  duplicate_group_count: number
  overlap_amount: number
  groups: string[]
  read_start: string | null
  read_end: string | null
  paths: {
    group: string
    path_id: number
    path_name: string
    status: string
    received_amount: number | null
    read_start: string | null
    read_end: string | null
    read_note: string
  }[]
}

export type EvidenceRecord = {
  evidence_id: number
  batch_id: number
  source_id: string
  source_name: string
  interval: string
  reason: string
  status: string
  receipt_id: number | null
  resumable: boolean
  resume_hint: string
}

export type Receipt = {
  receipt_id: number
  receipt_no: string
  source_id: string
  source_name: string
  interval: string
  batch_id: number
  evidence_id: number
  status: string
  request_count: number
  submission_count: number
  duplicate_submissions?: number
  amount?: number
  read_start?: string
  read_end?: string
}

export type BatchSummary = {
  raw_received_total: number
  actual_total: number
  overlap_amount: number
  expected_unique_total: number
  unique_source_count: number
  received_source_count: number
  missing_source_count: number
  groups_total: number
  groups_covered: number
  coverage_pct: number
  dedup_rule: string
}

export type BatchView = {
  batch: {
    batch_id: number
    batch_name: string
    period: string
    metric: string
    path_ids: number[]
    status: string
    created_at: string
    updated_at: string
  }
  summary: BatchSummary
  groups: BatchGroup[]
  sources: BatchSource[]
  evidence: EvidenceRecord[]
  receipts: Receipt[]
}

export type Coverage = {
  checked: boolean
  complete: boolean
  zero_filled: boolean
  coverage_rule: string
  compared_groups: string[]
  groups: BatchGroup[]
  all_groups_coverage: {
    group: string
    coverage_pct: number
    complete: boolean
    missing_sources: string[]
  }[]
  missing_sources: {
    source_id: string
    source_name: string
    groups: string[]
    expected_amount: number
    reason: string
  }[]
  batch_missing_sources: {
    source_id: string
    source_name: string
    groups: string[]
    status: string
  }[]
  message: string
}

export type CompareResult = {
  ok: boolean
  duplicate: boolean
  plan_id: number
  plan: Record<string, unknown>
  batch_id: number
  period: string
  metric: string
  seg_a: string
  seg_b: string
  coverage: Coverage
  report: Record<string, number | boolean | string> | null
  result: string
  direction: string
  message: string
  evidence: EvidenceRecord[]
  receipts: Receipt[]
  batch: BatchView
}

export type ClaimResult = {
  ok: boolean
  duplicate: boolean
  message: string
  evidence: EvidenceRecord
  receipt: Receipt | null
  batch: BatchView
}

export type SubmitResult = {
  ok: boolean
  duplicate: boolean
  message: string
  receipt: Receipt
  batch: BatchView
  comparison_ready: boolean
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = (await response.json()) as T & { detail?: string }
  if (!response.ok) {
    throw new Error(payload?.detail || `接口返回 ${response.status}，操作未完成`)
  }
  return payload
}

export async function listDrillPaths(): Promise<{ id: number; 路径标识: string; 起始指标: string }[]> {
  const response = await request('/api/drill_down?size=50')
  if (!response.ok) throw new Error('下钻路径读取失败')
  const payload = (await response.json()) as { items: { id: number; 路径标识: string; 起始指标: string }[] }
  return payload.items
}

export function createBatch(payload: {
  path_ids: number[]
  period?: string
  metric?: string
  batch_name?: string
}): Promise<{ ok: boolean; batch: BatchView }> {
  return postJson('/api/batch-drill/batches', payload)
}

export async function getBatch(batchId: number): Promise<BatchView> {
  const response = await request(`/api/batch-drill/batches/${batchId}`)
  if (!response.ok) throw new Error('批量下钻数据集读取失败')
  return await response.json()
}

export function claimEvidence(batchId: number, sourceId: string): Promise<ClaimResult> {
  return postJson(`/api/batch-drill/batches/${batchId}/evidence/claim`, { source_id: sourceId })
}

export function submitEvidence(
  receiptId: number,
  payload: { amount?: number; read_start?: string; read_end?: string; note?: string },
): Promise<SubmitResult> {
  return postJson(`/api/batch-drill/receipts/${receiptId}/submit`, payload)
}

export function compareBatch(
  batchId: number,
  payload: { seg_a?: string; seg_b?: string },
): Promise<CompareResult> {
  return postJson(`/api/batch-drill/batches/${batchId}/compare`, payload)
}
