# Model Storage

This directory is the persistent model/cache mount point for local runs and Docker.

- The customer-call transcription model is downloaded by `faster-whisper` into the configured cache path.
- The LLM is accessed through the OpenAI-compatible provider configured in `.env`; LLM weights are not stored here.
- `models.yaml` records the selected model metadata.

Do not commit downloaded model weights or credentials.
