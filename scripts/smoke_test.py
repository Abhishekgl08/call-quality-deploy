"""Upload the bundled 30-second fixture and verify a completed report."""
from __future__ import annotations

import time
from pathlib import Path

import requests

BASE_URL = "http://127.0.0.1:8010"
AUDIO = Path(__file__).parents[1] / "data" / "test_sample_30s" / "customer_support_30s.wav"


def main() -> int:
    with AUDIO.open("rb") as handle:
        response = requests.post(
            f"{BASE_URL}/calls",
            files={"file": (AUDIO.name, handle, "audio/wav")},
            data={"team": "local-smoke-test", "source_system": "bundled-fixture", "process_immediately": "true"},
            timeout=30,
        )
    response.raise_for_status()
    submitted = response.json()
    print(f"Submitted call {submitted['call_id']}")
    while True:
        job = requests.get(f"{BASE_URL}/jobs/{submitted['job_id']}", timeout=30).json()
        print(f"{job['state']} | {job['stage']} | {job['progress']}%")
        if job["state"] in {"completed", "failed"}:
            break
        time.sleep(5)
    if job["state"] != "completed":
        print(job.get("error"))
        return 1
    report = requests.get(f"{BASE_URL}/calls/{submitted['call_id']}", timeout=30).json()
    print({
        "state": report["state"],
        "duration_seconds": report["duration_seconds"],
        "transcript_segments": len(report["transcript"]),
        "summary_present": bool(report["analysis"].get("summary")),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
