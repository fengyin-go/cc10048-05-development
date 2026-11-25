"""批量下钻数据集：多路径来源归并、交叠去重、补证回执与批次分群对比。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.drill_batch import batch_drill_service

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_batches():
    batch_drill_service.reset()
    yield
    batch_drill_service.reset()


def _create_batch(path_ids=(1, 2, 3)):
    result = batch_drill_service.create_batch(list(path_ids))
    assert "error" not in result
    return result["batch"]


class TestBatchAggregation:
    def test_shared_source_counted_once(self):
        view = _create_batch()
        summary = view["summary"]
        # P1、P2 都读到同一条 30 元来源，两台路径原始累加是 60，去重后只计 30。
        assert summary["raw_received_total"] == 60.0
        assert summary["actual_total"] == 30.0
        assert summary["overlap_amount"] == 30.0
        assert summary["unique_source_count"] == 2

    def test_overlap_visible_per_source(self):
        view = _create_batch()
        shared = next(item for item in view["sources"] if item["source_id"] == "SRC-SHARED-30")
        assert shared["duplicate_group_count"] == 2
        assert shared["counted_once"] is True
        assert shared["overlap_amount"] == 30.0
        assert shared["groups"] == ["P1", "P2"]

    def test_each_path_read_range_reviewable(self):
        view = _create_batch()
        groups = {group["group"]: group for group in view["groups"]}
        assert groups["P1"]["read_ranges"][0]["read_start"] == "2026-10-01"
        assert groups["P1"]["read_ranges"][0]["read_end"] == "2026-10-06"
        assert groups["P3"]["read_ranges"][0]["status"] == "迟到"
        assert groups["P3"]["actual_read_range"] is None

    def test_missing_source_creates_evidence(self):
        batch = _create_batch()
        detail = batch_drill_service.view_batch(batch["batch"]["batch_id"])
        assert detail["summary"]["missing_source_count"] == 1
        assert any(item["source_id"] == "SRC-Q" for item in detail["evidence"])
        evidence = detail["evidence"][0]
        assert evidence["status"] == "待领取"
        assert evidence["resumable"] is True
        assert evidence["interval"] == "2026-10-01~2026-10-31"

    def test_nonexistent_path_rejected(self):
        result = batch_drill_service.create_batch([1, 999])
        assert "error" in result


class TestCompareCoverage:
    def test_compare_blocked_when_source_missing(self):
        batch = _create_batch()
        batch_id = batch["batch"]["batch_id"]
        result = batch_drill_service.compare_from_batch(batch_id)
        assert result["result"] == "待补证"
        assert result["report"] is None
        assert result["coverage"]["complete"] is False
        assert result["coverage"]["zero_filled"] is False
        assert result["coverage"]["missing_sources"][0]["source_id"] == "SRC-Q"
        assert "不按 0" in result["message"]

    def test_compare_passes_after_evidence(self):
        batch = _create_batch()
        batch_id = batch["batch"]["batch_id"]
        claim = batch_drill_service.claim_evidence(batch_id, source_id="SRC-Q")
        receipt_id = claim["receipt"]["receipt_id"]
        batch_drill_service.submit_evidence_data(receipt_id, amount=45.0)

        result = batch_drill_service.compare_from_batch(batch_id)
        assert result["coverage"]["complete"] is True
        assert result["report"] is not None
        assert result["report"]["period"] == batch["batch"]["period"]
        assert result["report"]["data_points_a"] == 6
        assert result["result"] in ("显著差异", "差异不显著")

    def test_compare_actual_total_after_evidence(self):
        batch = _create_batch()
        batch_id = batch["batch"]["batch_id"]
        receipt = batch_drill_service.claim_evidence(batch_id, source_id="SRC-Q")["receipt"]
        submitted = batch_drill_service.submit_evidence_data(receipt["receipt_id"], amount=45.0)
        summary = submitted["batch"]["summary"]
        assert summary["actual_total"] == 75.0
        # 各路径原始累加把共享来源算了两次：30 + 30 + 45 = 105。
        assert summary["raw_received_total"] == 105.0
        assert summary["missing_source_count"] == 0
        assert summary["groups_covered"] == 3


class TestEvidenceIdempotency:
    def test_claim_arrived_source_is_noop(self):
        batch = _create_batch()
        result = batch_drill_service.claim_evidence(
            batch["batch"]["batch_id"], source_id="SRC-SHARED-30")
        assert result["ok"] is True
        assert result["duplicate"] is True
        assert result["receipt"] is None
        assert batch_drill_service.view_batch(batch["batch"]["batch_id"])["receipts"] == []

    def test_duplicate_claim_keeps_one_receipt(self):
        batch = _create_batch()
        batch_id = batch["batch"]["batch_id"]
        first = batch_drill_service.claim_evidence(batch_id, source_id="SRC-Q")
        second = batch_drill_service.claim_evidence(batch_id, source_id="SRC-Q")
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert first["receipt"]["receipt_id"] == second["receipt"]["receipt_id"]
        receipts = batch_drill_service.view_batch(batch_id)["receipts"]
        assert len(receipts) == 1
        assert receipts[0]["request_count"] == 2

    def test_duplicate_submission_not_accumulated(self):
        batch = _create_batch()
        batch_id = batch["batch"]["batch_id"]
        receipt = batch_drill_service.claim_evidence(batch_id, source_id="SRC-Q")["receipt"]
        first = batch_drill_service.submit_evidence_data(receipt["receipt_id"], amount=45.0)
        second = batch_drill_service.submit_evidence_data(receipt["receipt_id"], amount=45.0)
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert second["batch"]["summary"]["actual_total"] == 75.0
        assert second["receipt"]["submission_count"] == 1
        assert second["receipt"]["duplicate_submissions"] == 1

    def test_duplicate_compare_keeps_one_plan(self):
        batch = _create_batch()
        batch_id = batch["batch"]["batch_id"]
        batch_drill_service.claim_evidence(batch_id, source_id="SRC-Q")
        receipt = batch_drill_service.view_batch(batch_id)["receipts"][0]
        batch_drill_service.submit_evidence_data(receipt["receipt_id"], amount=45.0)

        first = batch_drill_service.compare_from_batch(batch_id)
        second = batch_drill_service.compare_from_batch(batch_id)
        assert second["duplicate"] is True
        assert second["plan_id"] == first["plan_id"]


class TestBatchDrillApi:
    def test_batch_execute_via_drill_down_domain(self):
        resp = client.post(
            "/api/domain/drill_down/batch-execute",
            json={"path_ids": [1, 2, 3], "metric": "支付金额"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["batch"]["summary"]["actual_total"] == 30.0
        assert body["batch"]["summary"]["overlap_amount"] == 30.0
    def test_create_and_view(self):
        resp = client.post("/api/batch-drill/batches", json={"path_ids": [1, 2, 3]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        batch_id = body["batch"]["batch"]["batch_id"]

        resp = client.get(f"/api/batch-drill/batches/{batch_id}")
        assert resp.status_code == 200
        assert resp.json()["summary"]["actual_total"] == 30.0

        resp = client.get(f"/api/batch-drill/batches/{batch_id}/evidence")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_claim_submit_compare_api(self):
        resp = client.post("/api/batch-drill/batches", json={"path_ids": [1, 2, 3]})
        batch_id = resp.json()["batch"]["batch"]["batch_id"]

        resp = client.post(
            f"/api/batch-drill/batches/{batch_id}/evidence/claim",
            json={"source_id": "SRC-Q"},
        )
        assert resp.status_code == 200
        receipt_id = resp.json()["receipt"]["receipt_id"]

        resp = client.post(
            f"/api/batch-drill/receipts/{receipt_id}/submit",
            json={"amount": 45.0},
        )
        assert resp.status_code == 200
        assert resp.json()["comparison_ready"] is True

        resp = client.post(f"/api/batch-drill/batches/{batch_id}/compare", json={})
        assert resp.status_code == 200
        assert resp.json()["coverage"]["complete"] is True
        plan_id = resp.json()["plan_id"]
        assert client.get(f"/api/segment_compare/{plan_id}").status_code == 200

    def test_missing_batch_404(self):
        assert client.get("/api/batch-drill/batches/99999").status_code == 404
        assert client.post(
            "/api/batch-drill/batches/99999/compare", json={}).status_code == 404

    def test_bad_path_400(self):
        resp = client.post("/api/batch-drill/batches", json={"path_ids": [99999]})
        assert resp.status_code == 400
