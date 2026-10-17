"""Evidence-first customer-call processing with optional diarization and LLM analysis."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from call_quality import store
from app.config import settings

CALLS_DIR = Path("data") / "call_quality_calls"
OUTPUTS_DIR = Path("outputs") / "call_quality"
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".mp4"}

ISSUE_RULES = {
    "refund": ("financial_dispute", "high", ["refund", "money back", "charged twice"]),
    "cancellation": ("request", "medium", ["cancel", "cancellation"]),
    "escalation": ("escalation", "high", ["manager", "supervisor", "escalate", "complaint"]),
    "login_access": ("incident", "high", ["login", "log in", "sign in", "password", "access"]),
    "billing": ("financial_dispute", "high", ["billing", "charged", "payment", "invoice"]),
    "travel_disruption": ("incident", "high", ["missed flight", "delayed flight", "connection was delayed", "stranded"]),
}
CONDUCT_RULES = {
    "rude_or_dismissive": ["not my problem", "calm down", "whatever", "i don't care"],
    "abusive_or_profane": ["idiot", "stupid", "shut up", "damn"],
    "hostile_or_threatening": ["i will terminate", "stop calling", "threat"],
    "refusal_to_assist": ["cannot help", "won't help", "not helping"],
}
_TRANSCRIBERS: dict[tuple[str, str], Any] = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_is_fresh(job: dict[str, Any]) -> bool:
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(job["updated_at"])
        return age.total_seconds() < 900
    except (KeyError, TypeError, ValueError):
        return False


def _id() -> str:
    return str(uuid.uuid4())


def redact_pii(text: str) -> str:
    """Conservative local display redaction. Raw text remains restricted in SQLite."""
    patterns = [
        (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL REDACTED]"),
        (r"\b(?:\+?\d[\d .()-]{7,}\d)\b", "[PHONE/ID REDACTED]"),
        (r"\b(?:\d[ -]?){13,19}\b", "[CARD/ID REDACTED]"),
    ]
    redacted = text
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted, flags=re.I)
    return redacted


def register_call(source: Path, call_date: str | None, agent_id: str | None, channel_mapping: str | None, caller_id: str | None = None, team: str | None = None, product: str | None = None, source_system: str | None = None) -> tuple[str, str]:
    if source.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("Supported formats: WAV, MP3, M4A, FLAC, MP4.")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    existing = store.one("SELECT id FROM calls WHERE sha256=?", (digest,))
    if existing:
        existing_job = store.one("SELECT id,state,updated_at FROM jobs WHERE call_id=? ORDER BY created_at DESC", (existing["id"],))
        if existing_job and existing_job["state"] in {"queued", "normalising", "transcribing", "diarizing", "analysing", "indexing"} and _job_is_fresh(existing_job):
            return existing["id"], existing_job["id"]
        retry_job_id = _id()
        with store.connection() as db:
            db.execute("INSERT INTO jobs (id,call_id,state,stage,progress,timings_json,warnings_json,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (retry_job_id, existing["id"], "queued", "queued", 0, "{}", "[]", None, now(), now()))
            db.execute("UPDATE calls SET state='queued',error=NULL,completed_at=NULL WHERE id=?", (existing["id"],))
        return existing["id"], retry_job_id
    call_id, job_id = _id(), _id()
    target = CALLS_DIR / call_id
    target.mkdir(parents=True, exist_ok=True)
    raw_path = target / f"source{source.suffix.lower()}"
    shutil.copy2(source, raw_path)
    with store.connection() as db:
        db.execute("""INSERT INTO calls (id,source_filename,source_path,sha256,call_date,agent_id,caller_id,team,product,source_system,channel_mapping,
                duration_seconds,state,error,created_at,completed_at,warnings_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            call_id, source.name, str(raw_path), digest, call_date, agent_id, caller_id, team, product, source_system, channel_mapping,
            None, "queued", None, now(), None, "[]"))
        db.execute("""INSERT INTO jobs (id,call_id,state,stage,progress,timings_json,warnings_json,error,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            job_id, call_id, "queued", "queued", 0, "{}", "[]", None, now(), now()))
    return call_id, job_id


def _update_job(job_id: str, state: str, stage: str, progress: int, error: str | None = None) -> None:
    with store.connection() as db:
        db.execute("UPDATE jobs SET state=?, stage=?, progress=?, error=?, updated_at=? WHERE id=?", (state, stage, progress, error, now(), job_id))
        db.execute("UPDATE calls SET state=?, error=? WHERE id=(SELECT call_id FROM jobs WHERE id=?)", (state, error, job_id))


def _upsert_detection(call_id: str, kind: str, category: str, severity: str, confidence: float, start: float, end: float, evidence: str, source: str) -> None:
    with store.connection() as db:
        detection_id = _id()
        db.execute("""INSERT INTO detections (id,call_id,kind,category,severity,confidence,start_seconds,end_seconds,
                    evidence,source,source_version,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            detection_id, call_id, kind, category, severity, confidence, start, end, redact_pii(evidence), source, "v1", "needs_review"))
        if kind == "conduct":
            call = db.execute("SELECT agent_id FROM calls WHERE id=?", (call_id,)).fetchone()
            db.execute("""INSERT INTO conduct_signals (id,call_id,agent_id,category,severity,confidence,start_seconds,end_seconds,
                        evidence,source,source_version,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (_id(), call_id, call["agent_id"] if call else None, category, severity, confidence, start, end,
                        redact_pii(evidence), source, "v1", "needs_review"))


def _role_for(item: dict[str, Any], mapping: str | None) -> tuple[str | None, float | None]:
    """Only attribute roles when the caller explicitly provided reliable channel mapping."""
    role = item.get("role")
    if role in {"agent", "customer"}:
        return role, float(item.get("role_confidence") or 1.0)
    speaker = str(item.get("speaker", "")).lower()
    mapping = (mapping or "").replace(" ", "").lower()
    if mapping == "left=agent,right=customer":
        return ({"left": "agent", "right": "customer"}.get(speaker), 1.0)
    if mapping == "left=customer,right=agent":
        return ({"left": "customer", "right": "agent"}.get(speaker), 1.0)
    return None, None


def analyse_segments(call_id: str, segments: list[dict[str, Any]]) -> None:
    """Persist deterministic, reviewable rules against timestamped transcript segments."""
    with store.connection() as db:
        db.execute("DELETE FROM transcript_segments WHERE call_id=?", (call_id,))
        db.execute("DELETE FROM detections WHERE call_id=?", (call_id,))
        db.execute("DELETE FROM issue_records WHERE call_id=?", (call_id,))
        db.execute("DELETE FROM quality_metrics WHERE call_id=?", (call_id,))
        db.execute("DELETE FROM conduct_signals WHERE call_id=?", (call_id,))
        call = db.execute("SELECT channel_mapping FROM calls WHERE id=?", (call_id,)).fetchone()
        mapping = call["channel_mapping"] if call else None
        for item in segments:
            role, role_confidence = _role_for(item, mapping)
            db.execute("""INSERT INTO transcript_segments (id,call_id,start_seconds,end_seconds,speaker,role,role_confidence,
                        raw_text,redacted_text,asr_confidence) VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                _id(), call_id, item["start"], item["end"], item.get("speaker", "Speaker_1"),
                role, role_confidence, item["text"], redact_pii(item["text"]), item.get("asr_confidence")))

    greeting = empathy = closure = 0
    for item in segments:
        text = item["text"].lower()
        start, end = item["start"], item["end"]
        role, _ = _role_for(item, mapping)
        # These measures are only attributed to an agent when a role is reliable.
        if role == "agent" and any(token in text for token in ["hello", "good morning", "good afternoon", "thank you for calling"]): greeting = 1
        if role == "agent" and any(token in text for token in ["sorry", "understand", "apolog", "i can help"]): empathy = 1
        if role == "agent" and any(token in text for token in ["anything else", "have a good day", "goodbye"]): closure = 1
        # A neutral mono speaker can still contain a customer issue, but it is marked for review.
        for taxonomy_id, (state, severity, keywords) in ISSUE_RULES.items():
            if any(keyword in text for keyword in keywords):
                urgency = "high" if severity == "high" else "medium"
                with store.connection() as db:
                    db.execute("""INSERT INTO issue_records (id,call_id,taxonomy_id,state,severity,urgency,resolution_state,
                                confidence,evidence,start_seconds,end_seconds,cluster_id,review_status)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        _id(), call_id, taxonomy_id, state, severity, urgency, "unresolved", .82,
                        redact_pii(item["text"]), start, end, taxonomy_id, "needs_review"))
                _upsert_detection(call_id, "issue", taxonomy_id, severity, .82, start, end, item["text"], "deterministic_phrase_rule")
        if role == "agent":
            for category, keywords in CONDUCT_RULES.items():
                if any(keyword in text for keyword in keywords):
                    _upsert_detection(call_id, "conduct", category, "warning", .9, start, end, item["text"], "deterministic_phrase_rule")
            for rule in store.rows("SELECT * FROM compliance_rules WHERE active=1"):
                if rule["speaker_scope"] not in {"either", role}:
                    continue
                try:
                    matched = re.search(rule["pattern"], item["text"], flags=re.I)
                except re.error:
                    matched = False
                if matched:
                    _upsert_detection(call_id, "compliance", rule["id"], rule["severity"], .95, start, end, item["text"], f"rule_pack:{rule['version']}")
    if mapping and not greeting:
        _upsert_detection(call_id, "quality", "greeting_missing", "info", .7, 0, min(30, segments[-1]["end"] if segments else 0), "No agent greeting found in the first 30 seconds.", "quality_rubric")
    with store.connection() as db:
        for metric, value in [("greeting_present", greeting), ("empathy_present", empathy), ("call_closure_present", closure)]:
            db.execute("INSERT INTO quality_metrics (id,call_id,metric_id,value,calculation_version,evidence) VALUES (?,?,?,?,?,?)", (_id(), call_id, metric, value, "v1", "deterministic transcript rubric"))


def persist_llm_analysis(call_id: str, analysis: dict[str, Any], segments: list[dict[str, Any]]) -> None:
    """Persist LLM findings while tying each finding to a transcript timestamp."""
    segment_rows = {i: item for i, item in enumerate(segments)}
    with store.connection() as db:
        db.execute("UPDATE calls SET analysis_json=? WHERE id=?", (store.dumps(analysis), call_id))
        db.execute("DELETE FROM transcript_segments WHERE call_id=?", (call_id,))
        for index, item in enumerate(segments):
            inferred = next((value for value in analysis.get("segments", []) if value.get("index") == index), {})
            role = inferred.get("role") if inferred.get("role") in {"agent", "customer"} else None
            db.execute("INSERT INTO transcript_segments (id,call_id,start_seconds,end_seconds,speaker,role,role_confidence,raw_text,redacted_text,asr_confidence) VALUES (?,?,?,?,?,?,?,?,?,?)", (_id(), call_id, item["start"], item["end"], item.get("speaker", "Speaker_1"), role, inferred.get("role_confidence"), item["text"], redact_pii(item["text"]), item.get("asr_confidence")))
        for finding in analysis.get("issues", []):
            indices = finding.get("segment_indices", [])
            evidence_item = segment_rows.get(indices[0]) if indices else None
            if not evidence_item:
                continue
            state = finding.get("state", "complaint")
            resolution = "not_applicable" if state == "request" else "unresolved"
            db.execute("INSERT INTO issue_records (id,call_id,taxonomy_id,state,severity,urgency,resolution_state,confidence,evidence,start_seconds,end_seconds,cluster_id,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (_id(), call_id, finding.get("taxonomy_id", "other"), state, finding.get("severity", "warning"), finding.get("urgency", "medium"), resolution, float(finding.get("confidence", 0)), redact_pii(finding.get("evidence", evidence_item["text"])), evidence_item["start"], evidence_item["end"], None, "needs_review"))
        for finding in analysis.get("conduct", []):
            indices = finding.get("segment_indices", [])
            evidence_item = segment_rows.get(indices[0]) if indices else None
            if not evidence_item:
                continue
            category = finding.get("category", "neutral")
            severity = finding.get("severity", "coaching")
            confidence = float(finding.get("confidence", 0))
            evidence = redact_pii(finding.get("evidence", evidence_item["text"]))
            detection_id = _id()
            db.execute("INSERT INTO detections (id,call_id,kind,category,severity,confidence,start_seconds,end_seconds,evidence,source,source_version,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (detection_id, call_id, "conduct", category, severity, confidence, evidence_item["start"], evidence_item["end"], evidence, "nvidia_llm", "v1", "needs_review"))
            call = db.execute("SELECT agent_id FROM calls WHERE id=?", (call_id,)).fetchone()
            db.execute("INSERT INTO conduct_signals (id,call_id,agent_id,category,severity,confidence,start_seconds,end_seconds,evidence,source,source_version,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (_id(), call_id, call["agent_id"] if call else None, category, severity, confidence, evidence_item["start"], evidence_item["end"], evidence, "nvidia_llm", "v1", "needs_review"))
        for metric, value in analysis.get("quality", {}).items():
            if isinstance(value, bool):
                value = int(value)
            if isinstance(value, (int, float)):
                db.execute("INSERT INTO quality_metrics (id,call_id,metric_id,value,calculation_version,evidence) VALUES (?,?,?,?,?,?)", (_id(), call_id, metric, value, "llm:v1", "LLM transcript analysis"))


def process_call(job_id: str) -> None:
    """Run local media processing. Any model failure produces a failed job, never a fake success."""
    job = store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job: return
    call = store.one("SELECT * FROM calls WHERE id=?", (job["call_id"],))
    try:
        _update_job(job_id, "normalising", "normalising audio", 10)
        from app.services.audio_service import AudioService
        output = OUTPUTS_DIR / call["id"]
        output.mkdir(parents=True, exist_ok=True)
        wav = AudioService().process_audio(Path(call["source_path"]), output)
        # Persist measured audio duration; analysis remains fully local.
        from pydub import AudioSegment
        duration = len(AudioSegment.from_wav(str(wav))) / 1000
        with store.connection() as db:
            db.execute("UPDATE calls SET duration_seconds=? WHERE id=?", (duration, call["id"]))
        _update_job(job_id, "transcribing", "local transcription", 40)
        transcript = _transcribe_cpu(wav)
        (output / "transcript.json").write_text(json.dumps(transcript, indent=2), encoding="utf-8")
        if not transcript.get("segments"):
            raise RuntimeError("Local transcription returned no timestamped segments; analysis was not persisted as completed.")
        _update_job(job_id, "analysing", "redacting and applying local rules", 75)
        segments = [{"start": s["start"], "end": s["end"], "speaker": "Speaker_1", "text": s["text"], "asr_confidence": s.get("confidence")} for s in transcript["segments"]]
        if settings.CALL_QUALITY_DIARIZATION_ENABLED:
            from app.services.diarization_service import DiarizationService
            diarization_path = DiarizationService().run_diarization(wav, output, num_speakers=2)
            diarization = json.loads(diarization_path.read_text(encoding="utf-8"))
            for segment, diarized in zip(segments, diarization):
                segment["speaker"] = diarized["speaker"]
        from call_quality.llm_analyzer import analyze_call
        analysis = analyze_call(segments)
        analysis["language"] = transcript.get("language", "unknown")
        persist_llm_analysis(call["id"], analysis, segments)
        _update_job(job_id, "indexing", "storing audit-ready results", 95)
        with store.connection() as db:
            warnings = []
            if not settings.CALL_QUALITY_DIARIZATION_ENABLED:
                warnings.append("Neural diarization disabled: speaker roles were inferred from transcript context by the configured LLM.")
            db.execute("UPDATE calls SET state='completed', completed_at=?, warnings_json=? WHERE id=?", (now(), store.dumps(warnings), call["id"]))
        _update_job(job_id, "completed", "completed", 100)
    except Exception as exc:
        _update_job(job_id, "failed", "failed", 100, f"{type(exc).__name__}: {exc}")


def _transcribe_cpu(wav: Path) -> dict[str, Any]:
    """CPU INT8 transcription using the pinned local faster-whisper model only."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is required for customer-call processing; install requirements.txt. No cloud or placeholder fallback is used.") from exc
    model_name = settings.CALL_QUALITY_WHISPER_MODEL
    cache_dir = str(settings.CALL_QUALITY_MODEL_CACHE)
    key = (model_name, cache_dir)
    if key not in _TRANSCRIBERS:
        _TRANSCRIBERS[key] = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=settings.CALL_QUALITY_CPU_THREADS, download_root=cache_dir)
    model = _TRANSCRIBERS[key]
    segments, info = model.transcribe(str(wav), language=None, word_timestamps=True, vad_filter=True)
    rendered = []
    for segment in segments:
        rendered.append({"start": round(segment.start, 3), "end": round(segment.end, 3), "text": segment.text.strip(), "confidence": round(float(getattr(segment, "avg_logprob", 0.0)), 3)})
    return {"language": getattr(info, "language", "en"), "model": model_name, "segments": rendered}


def report(call_id: str) -> dict[str, Any] | None:
    call = store.one("SELECT * FROM calls WHERE id=?", (call_id,))
    if not call: return None
    for key in ("warnings_json",): call[key.removesuffix("_json")] = store.loads(call.pop(key), [])
    call["transcript"] = store.rows("SELECT start_seconds,end_seconds,speaker,role,role_confidence,redacted_text,asr_confidence FROM transcript_segments WHERE call_id=? ORDER BY start_seconds", (call_id,))
    call["detections"] = store.rows("SELECT * FROM detections WHERE call_id=? ORDER BY start_seconds", (call_id,))
    call["issues"] = store.rows("SELECT * FROM issue_records WHERE call_id=? ORDER BY start_seconds", (call_id,))
    call["conduct_signals"] = store.rows("SELECT * FROM conduct_signals WHERE call_id=? ORDER BY start_seconds", (call_id,))
    call["quality_metrics"] = store.rows("SELECT metric_id,value,calculation_version,evidence FROM quality_metrics WHERE call_id=?", (call_id,))
    call["analysis"] = store.loads(call.pop("analysis_json", None), {})
    return call
