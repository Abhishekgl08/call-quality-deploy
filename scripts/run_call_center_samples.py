"""Process every recording in the call-center samples folder sequentially."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests


def process_file(base_url: str, path: Path, timeout: int) -> dict:
    with path.open("rb") as audio:
        response = requests.post(
            f"{base_url}/calls",
            files={"file": (path.name, audio, "audio/mpeg")},
            data={
                "agent_id": "call-center-dataset",
                "team": "Call center data samples",
                "source_system": "real-dataset",
                "process_immediately": "true",
            },
            timeout=60,
        )
    response.raise_for_status()
    submitted = response.json()
    job_id = submitted["job_id"]
    while True:
        job = requests.get(f"{base_url}/jobs/{job_id}", timeout=30).json()
        if job["state"] in {"completed", "failed"}:
            break
        time.sleep(timeout)
    result = {
        "folder": path.parent.name,
        "file": path.name,
        "call_id": submitted["call_id"],
        "job_id": job_id,
        "state": job["state"],
        "stage": job["stage"],
        "error": job.get("error"),
    }
    if job["state"] == "completed":
        report = requests.get(f"{base_url}/calls/{submitted['call_id']}", timeout=30).json()
        analysis = report.get("analysis", {})
        result.update({
            "duration_seconds": report.get("duration_seconds"),
            "language": analysis.get("language"),
            "transcript_segments": len(report.get("transcript", [])),
            "summary_present": bool(analysis.get("summary")),
            "issue_count": len(report.get("issues", [])),
        })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run all call-center sample recordings sequentially.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--folder", type=Path, default=Path("data") / "Call center data samples")
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("outputs") / "call_center_samples_results.json")
    args = parser.parse_args()

    files = sorted(args.folder.rglob("*.mp3"))
    if not files:
        print(f"No MP3 recordings found under {args.folder}")
        return 1
    requests.get(f"{args.base_url}/quality/health", timeout=30).raise_for_status()
    results = []
    print(f"Processing {len(files)} recordings sequentially")
    for index, path in enumerate(files, 1):
        print(f"[{index}/{len(files)}] {path.parent.name} / {path.name}", flush=True)
        result = process_file(args.base_url, path, args.poll_seconds)
        results.append(result)
        print(json.dumps(result), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    passed = sum(item["state"] == "completed" for item in results)
    print(f"Completed: {passed}/{len(results)}")
    print(f"Report: {args.output}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())