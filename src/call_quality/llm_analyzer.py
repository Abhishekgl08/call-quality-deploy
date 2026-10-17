"""Structured customer-call analysis through the configured NVIDIA-compatible endpoint."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
from typing import Any

from app.config import settings


class CallAnalysisError(RuntimeError):
    """Raised when the configured analyzer cannot produce valid structured output."""


_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_MODEL_INDEX = 0


def _json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise CallAnalysisError("LLM returned no JSON analysis object")
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise CallAnalysisError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CallAnalysisError("LLM analysis must be a JSON object")
    return value


def analyze_call(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify turns and produce evidence-linked analysis; never returns a fake fallback."""
    if not settings.CALL_QUALITY_LLM_ENABLED:
        return {"summary": "LLM analysis disabled by configuration.", "segments": [], "issues": [], "conduct": [], "quality": {}}
    if settings.LLM_PROVIDER not in {"nvidia", "openai"} or not settings.LLM_API_KEY:
        raise CallAnalysisError("Customer-call LLM analysis requires a configured NVIDIA/OpenAI-compatible API key")
    transcript = "\n".join(f"[{i}] {s['start']:.3f}-{s['end']:.3f}: {s['text']}" for i, s in enumerate(segments))
    prompt = f'''You are a customer-support quality analyst. Analyze this customer-support call transcript in its original language.
There are at most two people. Infer Agent and Customer from language/context. Do not use voice tone.
Return ONLY valid JSON with this shape:
{{"summary":"...","overall_sentiment":"positive|neutral|negative|mixed","customer_sentiment":"positive|neutral|negative|mixed","segments":[{{"index":0,"role":"agent|customer|neutral","role_confidence":0.0}}],"issues":[{{"taxonomy_id":"...","state":"normal_query|request|complaint|incident|financial_dispute|escalation|unresolved_issue","severity":"info|warning|high|critical","urgency":"low|medium|high","confidence":0.0,"segment_indices":[0],"evidence":"..."}}],"conduct":[{{"category":"professional|neutral|empathetic|rude_or_dismissive|abusive_or_profane|hostile_or_threatening|interruption_or_talk_over|refusal_to_assist|misleading_commitment|empathy_missed|policy_breach","severity":"coaching|warning|critical","confidence":0.0,"segment_indices":[0],"evidence":"..."}}],"quality":{{"greeting_present":true,"acknowledgement_present":true,"call_closure_present":true,"unresolved_issue":false,"interruption_estimate":0.0,"silence_hold_seconds":0.0}}}}
Every issue/conduct evidence must cite one or more transcript segment indices. Normal questions and harmless requests are not issues.
TRANSCRIPT:\n{transcript}'''
    configured_models = [model.strip() for model in settings.LLM_MODELS.split(",") if model.strip()]
    models = configured_models or [settings.LLM_MODEL]
    global _MODEL_INDEX
    with _REQUEST_LOCK:
        model_start = _MODEL_INDEX % len(models)
        _MODEL_INDEX += 1
    payload = {
        "model": models[model_start],
        "messages": [
            {"role": "system", "content": "Return only one valid JSON object. Do not include reasoning, markdown, or explanatory prose."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": settings.CALL_QUALITY_LLM_MAX_TOKENS,
        "reasoning": {"exclude": True},
    }
    endpoint = f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"
    last_error: Exception | None = None
    max_attempts = max(1, settings.CALL_QUALITY_LLM_MAX_RETRIES)
    for attempt in range(max_attempts):
        try:
            global _LAST_REQUEST_AT
            with _REQUEST_LOCK:
                wait = settings.CALL_QUALITY_LLM_MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_AT)
                if wait > 0:
                    time.sleep(wait)
                _LAST_REQUEST_AT = time.monotonic()
            request_payload = dict(payload)
            request_payload["model"] = models[(model_start + attempt) % len(models)]
            request_payload["response_format"] = {"type": "json_object"}
            if attempt:
                request_payload["messages"] = [{"role": "system", "content": "Return only one valid JSON object. Do not include reasoning, markdown, or prose."}, {"role": "user", "content": prompt}]
            request = urllib.request.Request(endpoint, data=json.dumps(request_payload).encode("utf-8"), headers={"Authorization": f"Bearer {settings.LLM_API_KEY}", "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=settings.CALL_QUALITY_LLM_REQUEST_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            try:
                result = _json_object(content)
            except CallAnalysisError as exc:
                raise CallAnalysisError(f"{exc}; response preview: {content[:240]!r}") from exc
            result.setdefault("segments", [])
            result.setdefault("issues", [])
            result.setdefault("conduct", [])
            result.setdefault("quality", {})
            return result
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code not in {429, 503}:
                raise CallAnalysisError(f"OpenRouter HTTP {exc.code}: {response_body}") from exc
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else settings.CALL_QUALITY_LLM_RETRY_BACKOFF_SECONDS * (2 ** attempt)
            except ValueError:
                delay = settings.CALL_QUALITY_LLM_RETRY_BACKOFF_SECONDS * (2 ** attempt)
            last_error = RuntimeError(f"OpenRouter HTTP {exc.code}; retrying after {delay:g}s")
            if attempt + 1 < max_attempts:
                time.sleep(delay)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < max_attempts:
                time.sleep(settings.CALL_QUALITY_LLM_RETRY_BACKOFF_SECONDS * (2 ** attempt))
    raise CallAnalysisError(f"Customer-call LLM analysis failed after {max_attempts} attempts: {last_error}") from last_error
