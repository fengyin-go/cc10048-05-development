"""批量下钻数据集：归并多条下钻路径的来源明细，按贡献身份去重并承接补证。

一次批量下钻会选中多个父分组（P1、P2、P3…），每个父分组读取若干子来源。
同一个子来源可能被多条父路径同时读取，批量汇总必须按“贡献身份”只计一次，
同时把每条路径实际读取到的区间保留下来，运营才能核对交叠金额和覆盖情况。

分群对比直接消费这里的批次数据：先核对覆盖，缺失来源不按 0 计入；
缺失来源会生成可续做的补证记录，领取回执按“同一来源 + 同一区间”幂等，
补齐数据后重复提交同一请求不会产生第二份回执，也不会重复累加金额。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from app.compute import segment_diff_report
from app.store import store

DEFAULT_PERIOD = "2026-10-01~2026-10-31"
DEFAULT_METRIC = "支付金额"

# 演示场景：P1、P2 命中同一条 30 元子来源，P3 的子来源 Q 迟到。
SHARED_SOURCE_ID = "SRC-SHARED-30"
SHARED_SOURCE_NAME = "共享子来源 S"
LATE_SOURCE_ID = "SRC-Q"
LATE_SOURCE_NAME = "子来源 Q"


class BatchDrillService:
    """批量下钻、来源归并、补证回执与批次分群对比。"""

    def __init__(self) -> None:
        self._batches: dict[int, dict[str, Any]] = {}
        self._evidence: dict[int, dict[str, Any]] = {}
        self._receipts: dict[int, dict[str, Any]] = {}
        self._source_data: dict[tuple[str, str], dict[str, Any]] = {}
        self._evidence_index: dict[tuple[int, str, str], int] = {}
        self._receipt_index: dict[tuple[str, str], int] = {}
        self._compare_index: dict[tuple[int, str, str, str], int] = {}
        self._seq = {"batch": 0, "evidence": 0, "receipt": 0}

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def create_batch(
        self,
        path_ids: list[int],
        period: str | None = None,
        metric: str | None = None,
        batch_name: str | None = None,
    ) -> dict[str, Any]:
        period = (period or DEFAULT_PERIOD).strip()
        metric = (metric or DEFAULT_METRIC).strip() or DEFAULT_METRIC
        try:
            start, end = self._parse_period(period)
        except ValueError as exc:
            return {"error": str(exc)}

        unique_paths: list[int] = []
        for raw_id in path_ids or []:
            try:
                path_id = int(raw_id)
            except (TypeError, ValueError):
                return {"error": f"下钻路径标识 {raw_id} 不是有效整数"}
            if store.find("drill_down", path_id) is None:
                return {"error": f"下钻路径 {path_id} 不存在"}
            if path_id not in unique_paths:
                unique_paths.append(path_id)
        if not unique_paths:
            return {"error": "至少选择一条下钻路径"}

        self._seq["batch"] += 1
        batch_id = self._seq["batch"]
        lines: list[dict[str, Any]] = []
        groups: list[dict[str, Any]] = []
        for index, path_id in enumerate(unique_paths, start=1):
            group = f"P{index}"
            path_row = store.find("drill_down", path_id) or {}
            groups.append({
                "group": group,
                "path_id": path_id,
                "path_name": str(path_row.get("路径标识", f"路径{path_id}")),
            })
            for source in self._catalog(path_id, start, end):
                line = {
                    "group": group,
                    "path_id": path_id,
                    "path_name": str(path_row.get("路径标识", f"路径{path_id}")),
                    "source_id": source["source_id"],
                    "source_name": source["source_name"],
                    "contribution_identity": f"{source['source_id']}|{metric}",
                    "expected_amount": source["expected_amount"],
                    "received_amount": None,
                    "status": source["status"],
                    "records": source["records"],
                    "read_start": source["read_start"],
                    "read_end": source["read_end"],
                    "read_note": source["read_note"],
                }
                submitted = self._source_data.get((source["source_id"], period))
                if submitted:
                    self._apply_submission(line, submitted)
                elif source["status"] == "已到":
                    line["received_amount"] = source["expected_amount"]
                lines.append(line)

        now = self._now()
        batch = {
            "batch_id": batch_id,
            "batch_name": (batch_name or f"批量下钻-{metric}").strip(),
            "period": period,
            "metric": metric,
            "path_ids": unique_paths,
            "groups": groups,
            "lines": lines,
            "created_at": now,
            "updated_at": now,
        }
        self._batches[batch_id] = batch
        return {"ok": True, "duplicate": False, "batch": self.view_batch(batch_id)}

    def list_batches(self) -> dict[str, Any]:
        items = []
        for batch in self._batches.values():
            summary = self._summary(batch)
            items.append({
                "batch_id": batch["batch_id"],
                "batch_name": batch["batch_name"],
                "period": batch["period"],
                "metric": batch["metric"],
                "path_ids": list(batch["path_ids"]),
                "status": "待补证" if summary["missing_source_count"] else "已齐备",
                "actual_total": summary["actual_total"],
                "overlap_amount": summary["overlap_amount"],
                "coverage_pct": summary["coverage_pct"],
            })
        return {"items": items, "total": len(items)}

    def view_batch(self, batch_id: int) -> dict[str, Any] | None:
        batch = self._batches.get(int(batch_id))
        if batch is None:
            return None
        summary = self._summary(batch)
        self._ensure_evidence(batch)
        return {
            "batch": {
                "batch_id": batch["batch_id"],
                "batch_name": batch["batch_name"],
                "period": batch["period"],
                "metric": batch["metric"],
                "path_ids": list(batch["path_ids"]),
                "status": "待补证" if summary["missing_source_count"] else "已齐备",
                "created_at": batch["created_at"],
                "updated_at": batch["updated_at"],
            },
            "summary": summary,
            "groups": self._group_rows(batch),
            "sources": self._source_rows(batch),
            "evidence": self._ensure_evidence(batch),
            "receipts": self._receipts_for_batch(batch),
        }

    # ------------------------------------------------------------------
    # 补证：记录生成、领取回执、数据提交
    # ------------------------------------------------------------------
    def ensure_evidence(self, batch_id: int) -> list[dict[str, Any]]:
        batch = self._batches.get(int(batch_id))
        if batch is None:
            return []
        return self._ensure_evidence(batch)

    def claim_evidence(
        self,
        batch_id: int,
        source_id: str | None = None,
        evidence_id: int | None = None,
        remark: str | None = None,
    ) -> dict[str, Any]:
        batch = self._batches.get(int(batch_id))
        if batch is None:
            return {"error": f"批量下钻数据集 {batch_id} 不存在"}

        evidence = None
        if evidence_id is not None:
            evidence = self._evidence.get(int(evidence_id))
            if evidence is not None and evidence["batch_id"] != batch["batch_id"]:
                evidence = None
        elif source_id:
            evidence = self._find_evidence(batch, str(source_id))

        if evidence is None:
            if not source_id:
                return {"error": "请指定需要领取补证的来源或补证记录"}
            self._ensure_evidence(batch)
            evidence = self._find_evidence(batch, str(source_id))
        if evidence is None:
            arrived = next(
                (source for source in self._source_rows(batch) if source["source_id"] == str(source_id)),
                None,
            )
            if arrived is not None:
                return {
                    "ok": True,
                    "duplicate": True,
                    "message": f"来源 {source_id} 已到数，无需补证",
                    "evidence": None,
                    "receipt": None,
                    "batch": self.view_batch(batch["batch_id"]),
                }
            return {"error": f"来源 {source_id or evidence_id} 不在本批次，无需补证"}
        if evidence["status"] == "已补证":
            receipt = self._receipts.get(int(evidence.get("receipt_id") or 0))
            return {
                "ok": True,
                "duplicate": True,
                "message": f"来源 {evidence['source_id']} 已补证，无需重复领取",
                "evidence": dict(evidence),
                "receipt": dict(receipt) if receipt else None,
                "batch": self.view_batch(batch["batch_id"]),
            }

        key = (evidence["source_id"], evidence["interval"])
        existing_id = self._receipt_index.get(key)
        if existing_id is not None:
            receipt = self._receipts[existing_id]
            receipt["request_count"] = int(receipt.get("request_count", 1)) + 1
            receipt["last_requested_at"] = self._now()
            if remark:
                receipt["remark"] = remark
            evidence["receipt_id"] = receipt["receipt_id"]
            if evidence["status"] == "待领取":
                evidence["status"] = "已领取"
            return {
                "ok": True,
                "duplicate": True,
                "message": (
                    f"来源 {evidence['source_id']} 区间 {evidence['interval']} 已有领取回执，"
                    "保留原回执，未重复创建"
                ),
                "evidence": dict(evidence),
                "receipt": dict(receipt),
                "batch": self.view_batch(batch["batch_id"]),
            }

        self._seq["receipt"] += 1
        receipt_id = self._seq["receipt"]
        receipt = {
            "receipt_id": receipt_id,
            "receipt_no": f"RCPT-{receipt_id:04d}",
            "source_id": evidence["source_id"],
            "source_name": evidence["source_name"],
            "interval": evidence["interval"],
            "batch_id": batch["batch_id"],
            "evidence_id": evidence["evidence_id"],
            "status": "已领取",
            "claimed_at": self._now(),
            "last_requested_at": self._now(),
            "request_count": 1,
            "submission_count": 0,
            "remark": remark or "",
        }
        self._receipts[receipt_id] = receipt
        self._receipt_index[key] = receipt_id
        evidence["receipt_id"] = receipt_id
        evidence["status"] = "已领取"
        evidence["updated_at"] = self._now()
        return {
            "ok": True,
            "duplicate": False,
            "message": f"来源 {evidence['source_id']} 补证已领取，回执 {receipt['receipt_no']}",
            "evidence": dict(evidence),
            "receipt": dict(receipt),
            "batch": self.view_batch(batch["batch_id"]),
        }

    def submit_evidence_data(
        self,
        receipt_id: int,
        amount: float | None = None,
        records: list[dict[str, Any]] | None = None,
        read_start: str | None = None,
        read_end: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        receipt = self._receipts.get(int(receipt_id))
        if receipt is None:
            return {"error": f"领取回执 {receipt_id} 不存在，请先领取补证"}

        batch = self._batches.get(int(receipt["batch_id"]))
        if batch is None:
            return {"error": f"回执关联的批量下钻数据集 {receipt['batch_id']} 不存在"}

        normalized_records = self._normalize_records(records)
        if amount is None and normalized_records:
            amount = sum(item["amount"] for item in normalized_records)
        if amount is None:
            return {"error": "补证数据缺少金额或记录明细"}
        try:
            amount = round(float(amount), 2)
        except (TypeError, ValueError):
            return {"error": "补证金额必须是数值"}
        if amount < 0:
            return {"error": "补证金额不能为负数"}

        if normalized_records:
            read_start = read_start or normalized_records[0]["date"]
            read_end = read_end or normalized_records[-1]["date"]
        elif not read_start or not read_end:
            start, _ = self._parse_period(batch["period"])
            normalized_records = self._records(
                amount, start, self._parse_period(batch["period"])[1], [4, 5, 6, 7, 6, 5])
            read_start = normalized_records[0]["date"]
            read_end = normalized_records[-1]["date"]

        payload = {
            "source_id": receipt["source_id"],
            "interval": receipt["interval"],
            "amount": amount,
            "records": normalized_records,
            "read_start": read_start,
            "read_end": read_end,
            "note": note or "",
        }
        key = (receipt["source_id"], receipt["interval"])
        previous = self._source_data.get(key)
        duplicate = previous == payload
        if not duplicate:
            self._source_data[key] = payload
            receipt["status"] = "已补证"
            receipt["submitted_at"] = self._now()
            receipt["submission_count"] = int(receipt.get("submission_count", 0)) + 1
            receipt["amount"] = amount
            receipt["read_start"] = read_start
            receipt["read_end"] = read_end
            if note:
                receipt["note"] = note
            self._apply_to_batches(receipt["source_id"], receipt["interval"], payload)
            evidence = self._find_evidence(batch, receipt["source_id"])
            if evidence:
                evidence["status"] = "已补证"
                evidence["receipt_id"] = receipt["receipt_id"]
                evidence["updated_at"] = self._now()
        else:
            receipt["last_requested_at"] = self._now()
            receipt["duplicate_submissions"] = int(receipt.get("duplicate_submissions", 0)) + 1

        return {
            "ok": True,
            "duplicate": duplicate,
            "message": (
                f"来源 {receipt['source_id']} 已补证，重复提交未重复累加"
                if duplicate else f"来源 {receipt['source_id']} 数据已送入批次"
            ),
            "receipt": dict(receipt),
            "batch": self.view_batch(batch["batch_id"]),
            "comparison_ready": self._summary(batch)["missing_source_count"] == 0,
        }

    # ------------------------------------------------------------------
    # 分群对比：先用批次数据核对覆盖，再出统计
    # ------------------------------------------------------------------
    def compare_from_batch(
        self,
        batch_id: int,
        seg_a: str | int | None = None,
        seg_b: str | int | None = None,
    ) -> dict[str, Any]:
        batch = self._batches.get(int(batch_id))
        if batch is None:
            return {"error": f"批量下钻数据集 {batch_id} 不存在"}

        group_names = [group["group"] for group in batch["groups"]]
        resolved_a = self._resolve_group(batch, seg_a) if seg_a is not None else group_names[0]
        resolved_b = self._resolve_group(batch, seg_b) if seg_b is not None else group_names[-1]
        if resolved_a is None or resolved_b is None:
            return {
                "error": f"对比分组不在本批次中：{seg_a} / {seg_b}",
                "available_groups": group_names,
            }

        evidence = self._ensure_evidence(batch)
        coverage = self._coverage(batch, [resolved_a, resolved_b])
        plan_key = (batch["batch_id"], resolved_a, resolved_b, batch["period"])
        plan_id = self._compare_index.get(plan_key)
        duplicate = plan_id is not None

        empty_report = None
        if not coverage["complete"]:
            result_text = "待补证"
            direction = "未计算"
            status = "待计算"
            message = coverage["message"]
            report = empty_report
        else:
            values_a = self._group_values(batch, resolved_a)
            values_b = self._group_values(batch, resolved_b)
            if not values_a or not values_b or len(values_a) < 2 or len(values_b) < 2:
                result_text = "待补证"
                direction = "未计算"
                status = "待计算"
                message = "同一统计期内可用明细不足，缺失数据不按 0 计入，暂不出统计"
                report = None
                coverage["complete"] = False
                coverage["message"] = message
            else:
                report = segment_diff_report(values_a, values_b)
                direction = (
                    "A高于B" if report.get("diff_pct", 0) > 0
                    else ("B高于A" if report.get("diff_pct", 0) < 0 else "无差异")
                )
                result_text = "显著差异" if report.get("significant") else "差异不显著"
                status = "已完成" if report.get("significant") else "差异不显著"
                message = (
                    f"覆盖核对通过，按统计期 {batch['period']} 实算 {len(values_a)} 个数据点"
                )
                report["period"] = batch["period"]
                report["data_points_a"] = len(values_a)
                report["data_points_b"] = len(values_b)

        plan = self._upsert_plan(
            plan_id, batch, resolved_a, resolved_b,
            result_text, direction, status, coverage, report,
        )
        return {
            "ok": True,
            "duplicate": duplicate,
            "plan_id": plan["id"],
            "plan": plan,
            "batch_id": batch["batch_id"],
            "period": batch["period"],
            "metric": batch["metric"],
            "seg_a": resolved_a,
            "seg_b": resolved_b,
            "coverage": coverage,
            "report": report,
            "result": result_text,
            "direction": direction,
            "message": message,
            "evidence": evidence,
            "receipts": self._receipts_for_batch(batch),
            "batch": self.view_batch(batch["batch_id"]),
        }

    # ------------------------------------------------------------------
    # 构造批次明细
    # ------------------------------------------------------------------
    def _catalog(self, path_id: int, start, end) -> list[dict[str, Any]]:
        """演示路径 1、2 共用一条 30 元子来源；路径 3 的子来源 Q 迟到。"""
        if path_id == 1:
            return [self._source_line(
                SHARED_SOURCE_ID, SHARED_SOURCE_NAME, 30.0, "已到",
                self._records(30.0, start, end, [4, 5, 6, 5, 4, 6]),
            )]
        if path_id == 2:
            return [self._source_line(
                SHARED_SOURCE_ID, SHARED_SOURCE_NAME, 30.0, "已到",
                self._records(30.0, start, end, [4, 5, 6, 5, 4, 6]),
            )]
        if path_id == 3:
            return [self._source_line(
                LATE_SOURCE_ID, LATE_SOURCE_NAME, 45.0, "迟到",
                [], read_note="来源 Q 迟到，统计期内未读取",
            )]
        rng = random.Random(9000 + path_id)
        amount = round(rng.uniform(20, 80), 2)
        weights = [rng.randint(2, 9) for _ in range(6)]
        return [self._source_line(
            f"SRC-P{path_id}", f"路径{path_id}子来源", amount, "已到",
            self._records(amount, start, end, weights),
        )]

    @staticmethod
    def _source_line(
        source_id: str,
        source_name: str,
        expected_amount: float,
        status: str,
        records: list[dict[str, Any]],
        read_note: str = "",
    ) -> dict[str, Any]:
        return {
            "source_id": source_id,
            "source_name": source_name,
            "expected_amount": round(expected_amount, 2),
            "status": status,
            "records": records,
            "read_start": records[0]["date"] if records else None,
            "read_end": records[-1]["date"] if records else None,
            "read_note": read_note,
        }

    def _records(self, amount: float, start, end, weights: list[int]) -> list[dict[str, Any]]:
        available_days = (end - start).days + 1
        count = max(1, min(len(weights), available_days))
        used = weights[:count]
        total = sum(used)
        values = [round(amount * weight / total, 2) for weight in used]
        values[-1] = round(amount - sum(values[:-1]), 2)
        return [
            {"date": (start + timedelta(days=index)).isoformat(), "amount": value}
            for index, value in enumerate(values)
        ]

    @staticmethod
    def _normalize_records(records: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        normalized = []
        for record in records or []:
            try:
                normalized.append({
                    "date": str(record.get("date", "")).strip(),
                    "amount": round(float(record.get("amount", 0)), 2),
                })
            except (AttributeError, TypeError, ValueError):
                continue
        return sorted(
            [item for item in normalized if item["date"]],
            key=lambda item: item["date"],
        )

    # ------------------------------------------------------------------
    # 数据提交与归并计算
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_submission(line: dict[str, Any], payload: dict[str, Any]) -> None:
        line["received_amount"] = round(float(payload["amount"]), 2)
        line["records"] = list(payload.get("records") or [])
        line["status"] = "已到"
        line["read_start"] = payload.get("read_start")
        line["read_end"] = payload.get("read_end")
        line["read_note"] = "补证数据已送入"

    def _apply_to_batches(self, source_id: str, interval: str, payload: dict[str, Any]) -> None:
        for batch in self._batches.values():
            if batch["period"] != interval:
                continue
            for line in batch["lines"]:
                if line["source_id"] == source_id:
                    self._apply_submission(line, payload)
            batch["updated_at"] = self._now()

    def _summary(self, batch: dict[str, Any]) -> dict[str, Any]:
        sources = self._source_rows(batch)
        received = [source for source in sources if source["received_amount"] is not None]
        # 各路径各自累加的原始合计：交叠来源会被重复计入，只能用来看重复程度。
        raw_total = round(sum(
            line["received_amount"] for line in batch["lines"]
            if line["status"] == "已到" and line["received_amount"] is not None
        ), 2)
        actual_total = round(sum(source["received_amount"] for source in sources if source["counted"]), 2)
        overlap_total = round(sum(source["overlap_amount"] for source in sources), 2)
        expected_total = round(sum(source["expected_amount"] for source in sources), 2)
        groups = self._group_rows(batch)
        missing = [source for source in sources if source["status"] != "已到"]
        coverage_pct = round(actual_total / expected_total * 100, 2) if expected_total else 100.0
        return {
            "raw_received_total": raw_total,
            "actual_total": actual_total,
            "overlap_amount": overlap_total,
            "expected_unique_total": expected_total,
            "unique_source_count": len(sources),
            "received_source_count": len(received),
            "missing_source_count": len(missing),
            "groups_total": len(groups),
            "groups_covered": sum(1 for group in groups if group["complete"]),
            "coverage_pct": coverage_pct,
            "dedup_rule": "同一贡献身份（子来源+指标）只计一次",
        }

    def _source_rows(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        order: list[str] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for line in batch["lines"]:
            identity = line["contribution_identity"]
            if identity not in grouped:
                grouped[identity] = []
                order.append(identity)
            grouped[identity].append(line)

        rows = []
        for identity in order:
            lines = grouped[identity]
            received = [line for line in lines if line["status"] == "已到" and line["received_amount"] is not None]
            first = lines[0]
            status = "已到" if len(received) == len(lines) else ("部分到" if received else "缺失")
            base_amount = received[0]["received_amount"] if received else None
            rows.append({
                "source_id": first["source_id"],
                "source_name": first["source_name"],
                "contribution_identity": identity,
                "expected_amount": first["expected_amount"],
                "received_amount": base_amount,
                "status": status,
                "counted": base_amount is not None,
                "counted_once": len(lines) > 1,
                "duplicate_group_count": len(lines),
                "overlap_amount": (
                    round(base_amount * (len(received) - 1), 2)
                    if base_amount is not None and len(received) > 1 else 0.0
                ),
                "groups": [line["group"] for line in lines],
                "read_start": received[0]["read_start"] if received else None,
                "read_end": received[0]["read_end"] if received else None,
                "records": list(received[0]["records"]) if received else [],
                "paths": [
                    {
                        "group": line["group"],
                        "path_id": line["path_id"],
                        "path_name": line["path_name"],
                        "status": line["status"],
                        "received_amount": line["received_amount"],
                        "read_start": line["read_start"],
                        "read_end": line["read_end"],
                        "read_note": line["read_note"],
                    }
                    for line in lines
                ],
            })
        return rows

    def _group_rows(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for group in batch["groups"]:
            lines = [line for line in batch["lines"] if line["group"] == group["group"]]
            expected = round(sum(line["expected_amount"] for line in lines), 2)
            received = round(sum(
                line["received_amount"] for line in lines
                if line["status"] == "已到" and line["received_amount"] is not None
            ), 2)
            complete = all(
                line["status"] == "已到" and line["received_amount"] is not None
                for line in lines
            )
            missing_sources = [
                line["source_id"] for line in lines if line["status"] != "已到"
            ]
            read_start = min(
                (line["read_start"] for line in lines if line["read_start"]),
                default=None,
            )
            read_end = max(
                (line["read_end"] for line in lines if line["read_end"]),
                default=None,
            )
            rows.append({
                "group": group["group"],
                "path_id": group["path_id"],
                "path_name": group["path_name"],
                "source_count": len(lines),
                "expected_amount": expected,
                "received_amount": received if complete else None,
                "coverage_pct": round(received / expected * 100, 2) if expected else 100.0,
                "complete": complete,
                "missing_sources": missing_sources,
                "actual_read_range": (
                    f"{read_start}~{read_end}" if read_start and read_end else None
                ),
                "read_ranges": [
                    {
                        "source_id": line["source_id"],
                        "source_name": line["source_name"],
                        "read_start": line["read_start"],
                        "read_end": line["read_end"],
                        "status": line["status"],
                        "read_note": line["read_note"],
                    }
                    for line in lines
                ],
            })
        return rows

    # ------------------------------------------------------------------
    # 覆盖核对与统计实算
    # ------------------------------------------------------------------
    def _coverage(self, batch: dict[str, Any], group_names: list[str]) -> dict[str, Any]:
        groups = {group["group"]: group for group in self._group_rows(batch)}
        selected = [groups[name] for name in group_names if name in groups]
        missing_sources = []
        for source in self._source_rows(batch):
            if source["status"] == "已到":
                continue
            affected = [group for group in source["groups"] if group in group_names]
            if not affected:
                continue
            missing_sources.append({
                "source_id": source["source_id"],
                "source_name": source["source_name"],
                "groups": affected,
                "expected_amount": source["expected_amount"],
                "reason": "来源迟到，统计期内未读取" if source["status"] == "缺失" else "部分路径未读取",
            })
        complete = all(group["complete"] for group in selected) and not missing_sources
        message = "覆盖核对通过，可出统计"
        if not complete:
            names = "、".join(
                f"{item['source_name']}({item['source_id']})" for item in missing_sources
            ) or "部分分组"
            message = f"{names} 缺失，缺失数据不按 0 计入，补齐后再出统计"
        return {
            "checked": True,
            "complete": complete,
            "zero_filled": False,
            "coverage_rule": "缺失来源先补证，不按 0 参与统计",
            "compared_groups": group_names,
            "groups": selected,
            "all_groups_coverage": [
                {
                    "group": group["group"],
                    "coverage_pct": group["coverage_pct"],
                    "complete": group["complete"],
                    "missing_sources": group["missing_sources"],
                }
                for group in groups.values()
            ],
            "missing_sources": missing_sources,
            "batch_missing_sources": [
                {
                    "source_id": source["source_id"],
                    "source_name": source["source_name"],
                    "groups": source["groups"],
                    "status": source["status"],
                }
                for source in self._source_rows(batch)
                if source["status"] != "已到"
            ],
            "message": message,
        }

    def _group_values(self, batch: dict[str, Any], group: str) -> list[float] | None:
        lines = [line for line in batch["lines"] if line["group"] == group]
        if not lines or any(
            line["status"] != "已到" or not line["records"] for line in lines
        ):
            return None
        start, end = self._parse_period(batch["period"])
        buckets: dict[str, float] = {}
        for line in lines:
            for record in line["records"]:
                day = str(record.get("date", ""))
                if not day or not (start.isoformat() <= day <= end.isoformat()):
                    continue
                buckets[day] = round(buckets.get(day, 0.0) + float(record.get("amount", 0)), 2)
        if len(buckets) < 2:
            return None
        return [buckets[day] for day in sorted(buckets)]

    # ------------------------------------------------------------------
    # 补证记录与回执
    # ------------------------------------------------------------------
    def _ensure_evidence(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        items = []
        for source in self._source_rows(batch):
            if source["status"] == "已到":
                continue
            key = (batch["batch_id"], source["source_id"], batch["period"])
            evidence_id = self._evidence_index.get(key)
            if evidence_id is None:
                self._seq["evidence"] += 1
                evidence_id = self._seq["evidence"]
                record = {
                    "evidence_id": evidence_id,
                    "batch_id": batch["batch_id"],
                    "source_id": source["source_id"],
                    "source_name": source["source_name"],
                    "interval": batch["period"],
                    "reason": (
                        "来源迟到，统计期内未读取"
                        if source["status"] == "缺失" else "部分路径未读取"
                    ),
                    "status": "待领取",
                    "receipt_id": None,
                    "resumable": True,
                    "resume_hint": "领取回执后提交该来源同一区间的数据即可续做",
                    "created_at": self._now(),
                    "updated_at": self._now(),
                }
                self._evidence[evidence_id] = record
                self._evidence_index[key] = evidence_id
            else:
                record = self._evidence[evidence_id]
                if record["status"] == "待领取":
                    record["reason"] = source.get("status") or record["reason"]
            items.append(dict(record))
        return items

    def _find_evidence(self, batch: dict[str, Any], source_id: str) -> dict[str, Any] | None:
        evidence_id = self._evidence_index.get((batch["batch_id"], source_id, batch["period"]))
        if evidence_id is None:
            return None
        return self._evidence.get(evidence_id)

    def _evidence_for_batch(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            dict(record) for record in self._evidence.values()
            if record["batch_id"] == batch["batch_id"]
        ]

    def _receipts_for_batch(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        source_ids = {line["source_id"] for line in batch["lines"]}
        return [
            dict(receipt) for receipt in self._receipts.values()
            if receipt["interval"] == batch["period"] and receipt["source_id"] in source_ids
        ]

    # ------------------------------------------------------------------
    # 对比方案落库
    # ------------------------------------------------------------------
    def _upsert_plan(
        self,
        plan_id: int | None,
        batch: dict[str, Any],
        seg_a: str,
        seg_b: str,
        result_text: str,
        direction: str,
        status: str,
        coverage: dict[str, Any],
        report: dict[str, Any] | None,
    ) -> dict[str, Any]:
        rows = store.rows("segment_compare")
        if plan_id is None:
            plan_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            plan = {"id": plan_id, "abnormal": False}
            rows.append(plan)
            self._compare_index[(batch["batch_id"], seg_a, seg_b, batch["period"])] = plan_id
        else:
            plan = store.find("segment_compare", plan_id) or {"id": plan_id}
        plan.update({
            "status": status,
            "pending": status == "待计算",
            "方案标识": f"批量下钻对比-{batch['batch_id']}-{seg_a}-{seg_b}",
            "分群A": seg_a,
            "分群B": seg_b,
            "对比指标": batch["metric"],
            "对比维度": "批量下钻来源",
            "对比周期": batch["period"],
            "对比结果": result_text,
            "差异方向": direction,
            "_batch_id": batch["batch_id"],
            "_seg_a": seg_a,
            "_seg_b": seg_b,
            "coverage": coverage,
            "report": report,
        })
        batch["updated_at"] = self._now()
        return dict(plan)

    def _resolve_group(self, batch: dict[str, Any], value: str | int | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        for group in batch["groups"]:
            if group["group"].casefold() == text.casefold():
                return group["group"]
        for group in batch["groups"]:
            if str(group["path_id"]) == text:
                return group["group"]
        for group in batch["groups"]:
            if str(group["path_name"]).casefold() == text.casefold():
                return group["group"]
        return None

    def reset(self) -> None:
        """测试用：清空批次、补证和对比索引。"""
        self._batches.clear()
        self._evidence.clear()
        self._receipts.clear()
        self._source_data.clear()
        self._evidence_index.clear()
        self._receipt_index.clear()
        self._compare_index.clear()
        self._seq = {"batch": 0, "evidence": 0, "receipt": 0}

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_period(period: str):
        text = (period or "").strip()
        separator = "~" if "~" in text else ("至" if "至" in text else None)
        if separator is None:
            raise ValueError("统计期格式应为 开始日期~结束日期")
        start_text, end_text = text.split(separator, 1)
        try:
            start = datetime.strptime(start_text.strip(), "%Y-%m-%d").date()
            end = datetime.strptime(end_text.strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("统计期日期格式应为 YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("统计期开始日期不能晚于结束日期")
        return start, end

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


batch_drill_service = BatchDrillService()
