FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY frontend ./frontend
COPY models ./models
COPY data/test_sample_30s ./data/test_sample_30s

RUN mkdir -p data/call_quality_calls outputs/call_quality logs

EXPOSE 8010
CMD ["uvicorn", "call_quality.local_app:app", "--host", "0.0.0.0", "--port", "8010"]
