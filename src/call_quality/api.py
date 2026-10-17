"""FastAPI contract for the local customer-call-quality MVP."""
from __future__ import annotations

import csv
import io
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from call_quality import service, store

router = APIRouter(tags=["customer-call-quality"])


class ReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    verdict: str = Field(pattern="^(correct|incorrect|needs_review)$")
    note: str | None = Field(default=None, max_length=2000)


class RuleRequest(BaseModel):
    id: str
    name: str
    version: str = "v1"
    speaker_scope: str = "either"
    pattern: str
    severity: str = "warning"
    active: bool = True


class TaxonomyRequest(BaseModel):
    id: str
    parent_id: str | None = None
    label: str
    synonyms: list[str] = []
    active: bool = True
    version: str = "v1"


class SpeakerRoleRequest(BaseModel):
    role: str = Field(pattern="^(agent|customer|neutral)$")
    confidence: float = Field(default=1.0, ge=0, le=1)


class IssueReviewRequest(ReviewRequest):
    pass


def _not_found(entity: str) -> None:
    raise HTTPException(404, f"{entity} was not found")


@router.get("/quality/health")
def health() -> dict:
    store.init_db()
    from app.config import settings
    return {"service": "Customer Call Quality Analyzer", "mode": "customer-call", "external_llm": settings.CALL_QUALITY_LLM_ENABLED, "llm_model": settings.LLM_MODEL if settings.CALL_QUALITY_LLM_ENABLED else None, "diarization_enabled": settings.CALL_QUALITY_DIARIZATION_ENABLED, "database": str(store.DATABASE_PATH)}


@router.post("/calls", status_code=201)
async def upload_call(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    call_date: str | None = Form(default=None),
    agent_id: str | None = Form(default=None),
    channel_mapping: str | None = Form(default=None),
    caller_id: str | None = Form(default=None),
    team: str | None = Form(default=None),
    product: str | None = Form(default=None),
    source_system: str | None = Form(default=None),
    process_immediately: bool = Form(default=True),
) -> dict:
    suffix = Path(file.filename or "upload.wav").suffix
    staging = Path(tempfile.gettempdir()) / f"call-upload-{uuid.uuid4()}{suffix}"
    try:
        with staging.open("wb") as target:
            shutil.copyfileobj(file.file, target)
        call_id, job_id = service.register_call(staging, call_date, agent_id, channel_mapping, caller_id, team, product, source_system)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        staging.unlink(missing_ok=True)
    if process_immediately:
        background.add_task(service.process_call, job_id)
    return {"call_id": call_id, "job_id": job_id, "state": "queued", "local_only": True}


@router.post("/calls/batch", status_code=201)
async def upload_batch(
    background: BackgroundTasks, files: list[UploadFile] = File(...),
    call_date: str | None = Form(default=None), agent_id: str | None = Form(default=None),
    channel_mapping: str | None = Form(default=None), process_immediately: bool = Form(default=True),
    caller_id: str | None = Form(default=None), team: str | None = Form(default=None),
    product: str | None = Form(default=None), source_system: str | None = Form(default=None),
) -> dict:
    """Register a batch. Jobs are submitted in order; a caller can process them one at a time."""
    if not files:
        raise HTTPException(400, "At least one call recording is required")
    items = []
    for file in files:
        suffix = Path(file.filename or "upload.wav").suffix
        staging = Path(tempfile.gettempdir()) / f"call-upload-{uuid.uuid4()}{suffix}"
        try:
            with staging.open("wb") as target:
                shutil.copyfileobj(file.file, target)
            call_id, job_id = service.register_call(staging, call_date, agent_id, channel_mapping, caller_id, team, product, source_system)
            items.append({"filename": file.filename, "call_id": call_id, "job_id": job_id, "state": "queued"})
        except ValueError as exc:
            items.append({"filename": file.filename, "error": str(exc)})
        finally:
            staging.unlink(missing_ok=True)
    # BackgroundTasks run sequentially in the order they are added for this request.
    if process_immediately:
        for item in items:
            if item.get("job_id"):
                background.add_task(service.process_call, item["job_id"])
    return {"items": items, "local_only": True}


@router.post("/calls/{call_id}/process")
def queue_processing(call_id: str, background: BackgroundTasks) -> dict:
    call = store.one("SELECT id FROM calls WHERE id=?", (call_id,))
    if not call: _not_found("call")
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with store.connection() as db:
        db.execute("""INSERT INTO jobs (id,call_id,state,stage,progress,timings_json,warnings_json,error,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (job_id, call_id, "queued", "queued", 0, "{}", "[]", None, now, now))
    background.add_task(service.process_call, job_id)
    return {"call_id": call_id, "job_id": job_id, "state": "queued"}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job: _not_found("job")
    job["timings"] = store.loads(job.pop("timings_json"), {})
    job["warnings"] = store.loads(job.pop("warnings_json"), [])
    return job


@router.get("/calls/{call_id}")
def get_call(call_id: str) -> dict:
    result = service.report(call_id)
    if not result: _not_found("call")
    return result


@router.get("/calls")
def list_calls(state: str | None = None, agent_id: str | None = None, caller_id: str | None = None, team: str | None = None, product: str | None = None, date_from: str | None = None, date_to: str | None = None, limit: int = 100) -> dict:
    clauses, values = [], []
    for column, value in [("state", state), ("agent_id", agent_id), ("caller_id", caller_id), ("team", team), ("product", product)]:
        if value: clauses.append(f"{column}=?"); values.append(value)
    if date_from: clauses.append("COALESCE(call_date,created_at)>=?"); values.append(date_from)
    if date_to: clauses.append("COALESCE(call_date,created_at)<=?"); values.append(date_to)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    limit = max(1, min(limit, 500))
    items = store.rows("SELECT id,source_filename,call_date,agent_id,caller_id,team,product,source_system,duration_seconds,state,error,created_at,completed_at,warnings_json FROM calls" + where + " ORDER BY created_at DESC LIMIT ?", tuple(values + [limit]))
    for item in items: item["warnings"] = store.loads(item.pop("warnings_json"), [])
    return {"items": items, "count": len(items), "filters": {"state": state, "agent_id": agent_id, "caller_id": caller_id, "team": team, "product": product}}


@router.get("/calls/{call_id}/analysis")
def get_call_analysis(call_id: str) -> dict:
    result = service.report(call_id)
    if not result:
        _not_found("call")
    return {"call_id": call_id, "analysis": result.get("analysis", {}), "quality_metrics": result.get("quality_metrics", [])}


@router.get("/calls/{call_id}/audio")
def get_audio(call_id: str):
    call = store.one("SELECT source_path,source_filename FROM calls WHERE id=?", (call_id,))
    if not call: _not_found("call")
    path = Path(call["source_path"])
    if not path.is_file(): _not_found("call audio")
    return FileResponse(path, filename=call["source_filename"], content_disposition_type="inline")


@router.post("/calls/{call_id}/speakers/{speaker}/role")
def assign_speaker_role(call_id: str, speaker: str, payload: SpeakerRoleRequest) -> dict:
    if not store.one("SELECT id FROM calls WHERE id=?", (call_id,)): _not_found("call")
    role = None if payload.role == "neutral" else payload.role
    with store.connection() as db:
        changed = db.execute("UPDATE transcript_segments SET role=?, role_confidence=? WHERE call_id=? AND speaker=?", (role, payload.confidence if role else None, call_id, speaker)).rowcount
        db.execute("INSERT INTO audit_log VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), "speaker_role_assigned", "call", call_id, store.dumps({"speaker": speaker, "role": payload.role}), datetime.now(timezone.utc).isoformat()))
    return {"call_id": call_id, "speaker": speaker, "role": payload.role, "segments_updated": changed}


@router.get("/dashboard/overview")
def dashboard_overview(date_from: str | None = None, date_to: str | None = None, agent_id: str | None = None) -> dict:
    clauses, values = ["state='completed'"], []
    if date_from: clauses.append("COALESCE(call_date,created_at)>=?"); values.append(date_from)
    if date_to: clauses.append("COALESCE(call_date,created_at)<=?"); values.append(date_to)
    if agent_id: clauses.append("agent_id=?"); values.append(agent_id)
    where = " WHERE " + " AND ".join(clauses)
    calls = store.rows("SELECT id FROM calls" + where, tuple(values)); ids = [row["id"] for row in calls]
    if not ids:
        return {"denominator_completed_calls": 0, "calls_needing_review": 0, "angry_customer_rate_percent": 0, "refund_cancellation_rate_percent": 0, "escalation_rate_percent": 0, "compliance_alert_count": 0, "average_silence_hold_seconds": None}
    placeholders = ",".join("?" * len(ids))
    def count(sql: str, params: tuple = ()) -> int: return store.one(sql, tuple(ids) + params)["value"]
    return {
        "denominator_completed_calls": len(ids),
        "calls_needing_review": count(f"SELECT COUNT(DISTINCT call_id) AS value FROM detections WHERE call_id IN ({placeholders}) AND review_status='needs_review'"),
        "angry_customer_rate_percent": 0,  # emotion model is deliberately not configured, so this value is unknown rather than inferred.
        "refund_cancellation_rate_percent": round(100 * count(f"SELECT COUNT(DISTINCT call_id) AS value FROM issue_records WHERE call_id IN ({placeholders}) AND taxonomy_id IN ('refund','cancellation')") / len(ids), 2),
        "escalation_rate_percent": round(100 * count(f"SELECT COUNT(DISTINCT call_id) AS value FROM issue_records WHERE call_id IN ({placeholders}) AND taxonomy_id='escalation'") / len(ids), 2),
        "compliance_alert_count": count(f"SELECT COUNT(*) AS value FROM detections WHERE call_id IN ({placeholders}) AND kind='compliance'"),
        "average_silence_hold_seconds": None,
        "unknown_metrics": ["angry_customer_rate_percent", "average_silence_hold_seconds"],
    }


@router.get("/issues")
def get_issues(state: str | None = None, call_id: str | None = None, severity: str | None = None,
               review_status: str | None = None, include_normal_queries: bool = False) -> dict:
    clauses, values = [], []
    if state: clauses.append("state=?"); values.append(state)
    if call_id: clauses.append("call_id=?"); values.append(call_id)
    if severity: clauses.append("severity=?"); values.append(severity)
    if review_status: clauses.append("review_status=?"); values.append(review_status)
    if not include_normal_queries: clauses.append("state != 'normal_query'")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return {"items": store.rows("SELECT * FROM issue_records" + where + " ORDER BY rowid DESC", tuple(values))}


@router.get("/issues/trends")
def issue_trends(date_from: str | None = None, date_to: str | None = None, agent_id: str | None = None) -> dict:
    filters, values = ["i.state != 'normal_query'", "c.state='completed'"], []
    if date_from: filters.append("COALESCE(c.call_date,c.created_at) >= ?"); values.append(date_from)
    if date_to: filters.append("COALESCE(c.call_date,c.created_at) <= ?"); values.append(date_to)
    if agent_id: filters.append("c.agent_id=?"); values.append(agent_id)
    where = " WHERE " + " AND ".join(filters)
    rows = store.rows("""SELECT i.taxonomy_id, substr(COALESCE(c.call_date,c.created_at),1,10) AS date_bucket,
        COUNT(*) AS issue_count, COUNT(DISTINCT i.call_id) AS call_count,
        json_group_array(DISTINCT i.call_id) AS call_ids
        FROM issue_records i JOIN calls c ON c.id=i.call_id""" + where + " GROUP BY i.taxonomy_id,date_bucket ORDER BY date_bucket DESC,issue_count DESC", tuple(values))
    call_filters = ["state='completed'"] + [x.replace("i.", "").replace("c.", "") for x in filters[2:]]
    denominator = store.one("SELECT COUNT(*) AS value FROM calls WHERE " + " AND ".join(call_filters), tuple(values))["value"]
    for row in rows: row["rate_percent"] = round(100 * row["call_count"] / denominator, 2) if denominator else 0
    return {"denominator_completed_calls": denominator, "items": rows}


@router.get("/agents/{agent_id}/quality")
def agent_quality(agent_id: str) -> dict:
    calls = store.rows("SELECT id FROM calls WHERE agent_id=?", (agent_id,))
    ids = [item["id"] for item in calls]
    metrics = [service.report(call_id)["quality_metrics"] for call_id in ids]
    return {"agent_id": agent_id, "call_count": len(ids), "metrics_by_call": metrics, "review_required": True}


@router.get("/agents/{agent_id}/conduct")
def agent_conduct(agent_id: str) -> dict:
    return {"agent_id": agent_id, "items": store.rows("SELECT * FROM conduct_signals WHERE agent_id=?", (agent_id,)), "review_required": True}


@router.post("/detections/{detection_id}/review")
def review_detection(detection_id: str, payload: ReviewRequest) -> dict:
    if not store.one("SELECT id FROM detections WHERE id=?", (detection_id,)): _not_found("detection")
    with store.connection() as db:
        db.execute("UPDATE detections SET review_status=? WHERE id=?", (payload.verdict, detection_id))
        db.execute("UPDATE conduct_signals SET review_status=? WHERE call_id=(SELECT call_id FROM detections WHERE id=?) AND category=(SELECT category FROM detections WHERE id=?)", (payload.verdict, detection_id, detection_id))
        db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), detection_id, payload.reviewer, payload.verdict, payload.note, datetime.now(timezone.utc).isoformat()))
    return {"detection_id": detection_id, "review_status": payload.verdict}


@router.post("/issues/{issue_id}/review")
def review_issue(issue_id: str, payload: IssueReviewRequest) -> dict:
    if not store.one("SELECT id FROM issue_records WHERE id=?", (issue_id,)):
        _not_found("issue")
    with store.connection() as db:
        db.execute("UPDATE issue_records SET review_status=? WHERE id=?", (payload.verdict, issue_id))
        db.execute("INSERT INTO audit_log VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), "issue_reviewed", "issue", issue_id, store.dumps(payload.model_dump()), datetime.now(timezone.utc).isoformat()))
    return {"issue_id": issue_id, "review_status": payload.verdict}


@router.get("/compliance-rules")
def list_rules() -> dict: return {"items": store.rows("SELECT * FROM compliance_rules ORDER BY id")}

@router.post("/compliance-rules", status_code=201)
def create_rule(payload: RuleRequest) -> dict:
    with store.connection() as db:
        try: db.execute("INSERT INTO compliance_rules VALUES (?,?,?,?,?,?,?)", (payload.id, payload.name, payload.version, payload.speaker_scope, payload.pattern, payload.severity, int(payload.active)))
        except Exception as exc: raise HTTPException(409, "Rule ID already exists") from exc
    return payload.model_dump()


@router.put("/compliance-rules/{rule_id}")
def update_rule(rule_id: str, payload: RuleRequest) -> dict:
    if rule_id != payload.id: raise HTTPException(400, "Path and payload rule IDs must match")
    with store.connection() as db:
        cursor = db.execute("UPDATE compliance_rules SET name=?,version=?,speaker_scope=?,pattern=?,severity=?,active=? WHERE id=?", (payload.name, payload.version, payload.speaker_scope, payload.pattern, payload.severity, int(payload.active), rule_id))
        if not cursor.rowcount: _not_found("rule")
    return payload.model_dump()


@router.get("/taxonomy")
def list_taxonomy() -> dict: return {"items": store.rows("SELECT * FROM taxonomy ORDER BY id")}


@router.post("/taxonomy", status_code=201)
def create_taxonomy(payload: TaxonomyRequest) -> dict:
    with store.connection() as db:
        try:
            db.execute("INSERT INTO taxonomy (id,parent_id,label,synonyms_json,active,version) VALUES (?,?,?,?,?,?)", (payload.id, payload.parent_id, payload.label, store.dumps(payload.synonyms), int(payload.active), payload.version))
        except Exception as exc: raise HTTPException(409, "Taxonomy ID already exists") from exc
    return payload.model_dump()


@router.put("/taxonomy/{taxonomy_id}")
def update_taxonomy(taxonomy_id: str, payload: TaxonomyRequest) -> dict:
    if taxonomy_id != payload.id: raise HTTPException(400, "Path and payload taxonomy IDs must match")
    with store.connection() as db:
        result = db.execute("UPDATE taxonomy SET parent_id=?,label=?,synonyms_json=?,active=?,version=? WHERE id=?", (payload.parent_id, payload.label, store.dumps(payload.synonyms), int(payload.active), payload.version, taxonomy_id))
        if not result.rowcount: _not_found("taxonomy entry")
    return payload.model_dump()


@router.get("/exports/issues")
def export_issues(format: str = "json"):
    items = store.rows("SELECT * FROM issue_records WHERE state != 'normal_query'")
    if format == "json": return {"items": items, "redacted": True}
    if format != "csv": raise HTTPException(400, "format must be json or csv")
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=list(items[0]) if items else ["id"]); writer.writeheader(); writer.writerows(items)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=issues.csv"})


@router.get("/exports/quality")
def export_quality(format: str = "json"):
    items = store.rows("SELECT qm.*, c.agent_id, c.call_date FROM quality_metrics qm JOIN calls c ON c.id=qm.call_id")
    if format == "json": return {"items": items, "redacted": True}
    if format != "csv": raise HTTPException(400, "format must be json or csv")
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=list(items[0]) if items else ["id"]); writer.writeheader(); writer.writerows(items)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=quality.csv"})
