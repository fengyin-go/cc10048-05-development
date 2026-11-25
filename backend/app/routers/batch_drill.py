"""批量下钻数据集接口：多路径来源归并、补证回执、批次分群对比。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.drill_batch import batch_drill_service

router = APIRouter(prefix="/api/batch-drill", tags=["批量下钻"])


class BatchCreatePayload(BaseModel):
    path_ids: list[int] = Field(default_factory=list)
    period: str | None = None
    metric: str | None = None
    batch_name: str | None = None


class EvidenceClaimPayload(BaseModel):
    source_id: str | None = None
    evidence_id: int | None = None
    remark: str | None = None


class EvidenceSubmitPayload(BaseModel):
    amount: float | None = None
    records: list[dict[str, Any]] = Field(default_factory=list)
    read_start: str | None = None
    read_end: str | None = None
    note: str | None = None


class ComparePayload(BaseModel):
    seg_a: str | int | None = None
    seg_b: str | int | None = None


@router.post("/batches")
def create_batch(payload: BatchCreatePayload) -> dict[str, Any]:
    result = batch_drill_service.create_batch(
        payload.path_ids, payload.period, payload.metric, payload.batch_name)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    return batch_drill_service.list_batches()


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    batch = batch_drill_service.view_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批量下钻数据集 {batch_id} 不存在")
    return batch


@router.get("/batches/{batch_id}/evidence")
def list_evidence(batch_id: int) -> dict[str, Any]:
    if batch_drill_service.view_batch(batch_id) is None:
        raise HTTPException(status_code=404, detail=f"批量下钻数据集 {batch_id} 不存在")
    items = batch_drill_service.ensure_evidence(batch_id)
    return {"items": items, "total": len(items)}


@router.post("/batches/{batch_id}/evidence/claim")
def claim_evidence(batch_id: int, payload: EvidenceClaimPayload) -> dict[str, Any]:
    result = batch_drill_service.claim_evidence(
        batch_id, payload.source_id, payload.evidence_id, payload.remark)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/receipts/{receipt_id}/submit")
def submit_evidence(receipt_id: int, payload: EvidenceSubmitPayload) -> dict[str, Any]:
    result = batch_drill_service.submit_evidence_data(
        receipt_id, payload.amount, payload.records,
        payload.read_start, payload.read_end, payload.note)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/batches/{batch_id}/compare")
def compare_from_batch(batch_id: int, payload: ComparePayload) -> dict[str, Any]:
    if batch_drill_service.view_batch(batch_id) is None:
        raise HTTPException(status_code=404, detail=f"批量下钻数据集 {batch_id} 不存在")
    result = batch_drill_service.compare_from_batch(batch_id, payload.seg_a, payload.seg_b)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
