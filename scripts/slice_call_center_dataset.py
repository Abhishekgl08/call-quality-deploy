"""Create fixed-duration copies of the original call-center recordings.

Original files are never modified. The default output is a 30-second dataset
inside the isolated call_quality_project directory.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT.parent / "data" / "Call center data samples"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "sliced_call_center_30s"


def duration_seconds(ffmpeg: str, source: Path) -> float:
    command = [ffmpeg, "-i", str(source)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in result.stderr.splitlines():
        if "Duration:" in line:
            value = line.split("Duration:", 1)[1].split(",", 1)[0].strip()
            hours, minutes, seconds = value.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise RuntimeError(f"Could not determine duration for {source}")


def slice_file(ffmpeg: str, source: Path, output_dir: Path, slice_seconds: int) -> list[dict]:
    duration = duration_seconds(ffmpeg, source)
    group = source.parent.name
    group_dir = output_dir / group
    group_dir.mkdir(parents=True, exist_ok=True)
    records = []
    start = 0.0
    index = 1
    while start < duration:
        length = min(slice_seconds, duration - start)
        target = group_dir / f"{source.stem}_part_{index:04d}_{int(start):06d}s.mp3"
        command = [
            ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{length:.3f}",
            "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", str(target),
        ]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        records.append({
            "slice": target.name,
            "path": str(target.relative_to(PROJECT_ROOT)),
            "source_file": source.name,
            "source_group": group,
            "start_seconds": round(start, 3),
            "end_seconds": round(start + length, 3),
            "duration_seconds": round(length, 3),
        })
        start += slice_seconds
        index += 1
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Slice call-center recordings without modifying originals.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seconds", type=int, default=30, help="Slice length in seconds (default: 30)")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable path")
    args = parser.parse_args()
    if args.seconds <= 0:
        raise SystemExit("--seconds must be greater than zero")
    args.source = args.source.resolve()
    args.output = args.output.resolve()
    sources = sorted(args.source.rglob("*.mp3"))
    if not sources:
        raise SystemExit(f"No MP3 files found under {args.source}")
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    for index, source in enumerate(sources, 1):
        print(f"[{index}/{len(sources)}] {source.parent.name} / {source.name}", flush=True)
        records.extend(slice_file(args.ffmpeg, source, args.output, args.seconds))
    manifest = {
        "source": str(args.source),
        "output": str(args.output),
        "slice_seconds": args.seconds,
        "original_files": len(sources),
        "slice_files": len(records),
        "items": records,
    }
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Created {len(records)} slices from {len(sources)} original files")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
