"""Probe OpenRouter models for customer-call analysis compatibility.

The script does not modify application settings. Provide OPENROUTER_API_KEY in
the environment or in .env, then run this file from the repository root.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


DEFAULT_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-r1:free",
    "google/gemma-3-27b-it:free",
    "qwen/qwen3-4b:free",
    "mistralai/mistral-7b-instruct:free",
]
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODELS_ENDPOINT = "https://openrouter.ai/api/v1/models"
PROBE_PROMPT = (
    "Return only one valid JSON object with exactly these keys: "
    "summary, overall_sentiment, customer_sentiment, segments, issues, conduct, quality. "
    "Use empty arrays and an empty object where appropriate. "
    "Do not include markdown or explanatory text."
)


def parse_json_object(content: str) -> dict[str, Any] | None:
    if not isinstance(content, str) or not content.strip():
        return None
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def probe_model(session: requests.Session, api_key: str, model: str, timeout: int) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "temperature": 0,
        "max_tokens": 250,
        "response_format": {"type": "json_object"},
    }
    started = time.perf_counter()
    try:
        response = session.post(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost",
                "X-Title": "Customer Call Quality Analyzer model probe",
            },
            json=payload,
            timeout=timeout,
        )
        elapsed = round(time.perf_counter() - started, 2)
        if response.status_code >= 400:
            return {"model": model, "status": "FAIL", "seconds": elapsed, "error": f"HTTP {response.status_code}: {response.text[:240]}"}
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        parsed = parse_json_object(content)
        required = {"summary", "overall_sentiment", "customer_sentiment", "segments", "issues", "conduct", "quality"}
        missing = sorted(required - set(parsed or {}))
        if missing:
            return {"model": model, "status": "FAIL", "seconds": elapsed, "error": f"JSON missing keys: {', '.join(missing)}"}
        return {"model": model, "status": "PASS", "seconds": elapsed, "json_keys": len(parsed), "response_id": body.get("id")}
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        return {"model": model, "status": "FAIL", "seconds": round(time.perf_counter() - started, 2), "error": f"{type(exc).__name__}: {exc}"}


def discover_free_models(session: requests.Session, api_key: str, timeout: int) -> list[str]:
    response = session.get(MODELS_ENDPOINT, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
    response.raise_for_status()
    models = response.json().get("data", [])
    free = []
    for model in models:
        pricing = model.get("pricing", {})
        if str(pricing.get("prompt")) == "0" and str(pricing.get("completion")) == "0":
            free.append(model.get("id", ""))
    return sorted(model for model in free if model)


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    parser = argparse.ArgumentParser(description="Test OpenRouter models for structured call analysis.")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS, help="OpenRouter model IDs to test")
    parser.add_argument("--timeout", type=int, default=45, help="Per-model HTTP timeout in seconds")
    parser.add_argument("--output", default="outputs/openrouter_model_probe.json", help="JSON report path")
    parser.add_argument("--discover-free", action="store_true", help="Discover currently available free models from OpenRouter")
    parser.add_argument("--max-models", type=int, default=0, help="Limit discovered models; 0 tests every discovered free model")
    args = parser.parse_args()

    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("ERROR: Set OPENROUTER_API_KEY before running this probe. The key is never printed or stored by the script.")
        return 2

    results = []
    with requests.Session() as session:
        if args.discover_free:
            try:
                args.models = discover_free_models(session, api_key, args.timeout)
            except requests.RequestException as exc:
                print(f"ERROR: Could not discover OpenRouter models: {exc}")
                return 1
            if args.max_models > 0:
                args.models = args.models[:args.max_models]
            print(f"Discovered {len(args.models)} available free candidate(s)")
        if not args.models:
            print("No matching free models were returned by OpenRouter.")
            return 1
        for model in args.models:
            print(f"Testing {model} ...", flush=True)
            result = probe_model(session, api_key, model, args.timeout)
            results.append(result)
            print(f"  {result['status']} ({result['seconds']}s)" + (f": {result['error']}" if "error" in result else ""))

    passing = [item for item in results if item["status"] == "PASS"]
    passing.sort(key=lambda item: item["seconds"])
    report = {"endpoint": ENDPOINT, "free_model_discovery": args.discover_free, "tested_models": len(results), "passing_models": passing, "results": results}
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport: {output_path}")
    if passing:
        print(f"Recommended model: {passing[0]['model']} ({passing[0]['seconds']}s)")
        return 0
    print("No tested model passed the structured JSON probe.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())