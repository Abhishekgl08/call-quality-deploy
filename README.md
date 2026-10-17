# Call Quality Project

Self-contained customer-call quality analysis application. This folder is intentionally separate from the legacy meeting-investigator code in the parent project.

## What is included

- `src/call_quality/`: customer-call API, SQLite store, transcription pipeline, LLM analysis, and local launcher.
- `src/app/`: only the shared configuration and audio-normalization service required by the customer-call application.
- `frontend/`: static browser dashboard served by the API at `/app/`.
- `scripts/`: free-model probe, dataset runners, fixture generators, and smoke test.
- `data/test_sample_30s/`: one small 30-second copy of the real call-center speech sample for testing.
- `data/sliced_call_center_30s/`: generated 30-second copies of the original call-center recordings for larger sequential tests.
- `models/`: local model/cache mount point.
- `outputs/` and `logs/`: runtime directories, ignored by Git.
- `Dockerfile` and `docker-compose.yml`: container deployment setup.
- `.env.example`: configuration template. Put real credentials in `.env`; do not commit it.

The original dataset remains at the parent path:

```text
../data/Call center data samples/
```

Only the small test copy belongs in this project folder.

## Local setup

### Linux / macOS:
```bash
cp .env.example .env
# Edit .env and set LLM_API_KEY
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH="src"
python -m uvicorn call_quality.local_app:app --host 127.0.0.1 --port 8010
```

### Windows (PowerShell):
```powershell
Copy-Item .env.example .env
# Edit .env and set LLM_API_KEY
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONPATH = "$PWD/src"
.\.venv\Scripts\python.exe -m uvicorn call_quality.local_app:app --host 127.0.0.1 --port 8010
```

Open:

- UI: `http://127.0.0.1:8010/app/`
- API docs: `http://127.0.0.1:8010/docs`
- Health: `http://127.0.0.1:8010/quality/health`

## Smoke test

With the API running in another terminal:

```bash
# macOS / Linux
PYTHONPATH=src python scripts/smoke_test.py

# Windows (PowerShell)
$env:PYTHONPATH = "$PWD/src"
.\.venv\Scripts\python.exe scripts/smoke_test.py
```

The smoke test uploads `data/test_sample_30s/customer_support_30s.wav`, waits for the job, and checks that the report contains transcript segments and a summary.

## Docker

Create `.env` from `.env.example`, set the API key, then run:

```powershell
docker compose up --build
```

Open `http://localhost:8010/app/`.

The compose file mounts `data`, `outputs`, `logs`, and `models` so recordings, reports, and cached models persist outside the container.

## Dataset use

This isolated project keeps the complete original dataset outside the project. To expand the local test set without modifying the originals, run:

```powershell
python scripts/slice_call_center_dataset.py
```

This creates 30-second MP3 slices under `data/sliced_call_center_30s/` and writes `manifest.json` with the source file, group, start time, end time, and duration for every slice. The final partial slice is retained, so no recording content is discarded. Use `--seconds 60` for one-minute slices.

Run the generated slices through the API with:

```powershell
python scripts/run_call_center_samples.py --folder data/sliced_call_center_30s --poll-seconds 10 --output outputs/sliced_call_center_30s_results.json
```

The original dataset remains at `../data/Call center data samples/` and is never overwritten.

## Runtime flow

1. Upload audio through the UI or `POST /calls`.
2. Register a deduplicated call and SQLite job.
3. Convert audio to normalized mono 16 kHz WAV with FFmpeg.
4. Transcribe locally using faster-whisper.
5. Apply local issue, conduct, and compliance rules.
6. Send transcript-only structured analysis to the configured OpenAI-compatible provider.
7. Persist redacted transcript, analysis, evidence, metrics, warnings, and review records.
8. View the result in the dashboard or through the API.

Free OpenRouter models are subject to changing endpoint availability and rate limits. Run `scripts/test_openrouter_models.py --discover-free` before large external-LLM batches.
