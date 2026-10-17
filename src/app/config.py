"""Application Configuration Module.

Loads settings from environment variables using Pydantic Settings.
Configures paths for data, outputs, logs, model choices, Neo4j, and FAISS.
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """Global configuration settings for AI Meeting Investigator."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Base Directories
    BASE_DIR: Path = Path(__file__).resolve().parents[2]
    DATA_DIR: Path = BASE_DIR / "data"
    OUTPUTS_DIR: Path = BASE_DIR / "outputs"
    LOGS_DIR: Path = BASE_DIR / "logs"

    # API Settings
    APP_NAME: str = "AI Meeting Investigator"
    # Avoid inheriting unrelated system-wide DEBUG values such as "release".
    # Application overrides should use APP_DEBUG=true/false instead.
    DEBUG: bool = Field(default=True, validation_alias="APP_DEBUG")

    # Audio Processing Settings
    AUDIO_SAMPLE_RATE: int = 16000
    AUDIO_CHANNELS: int = 1  # Mono
    ALLOWED_EXTENSIONS: set[str] = {
        ".wav", ".mp3", ".m4a", ".flac", ".ogg",  # Audio
        ".mp4", ".mkv", ".mov", ".avi", ".webm"   # Video
    }

    # Speaker Diarization Model
    DIARIZATION_MODEL: str = "pyannote/speaker-diarization-3.1"
    HF_TOKEN: str = Field(default="", description="Hugging Face User Access Token")

    # Speech Recognition Model
    WHISPER_MODEL: str = "whisper-large-v3"
    WHISPER_DEVICE: str = "cuda"  # 'cuda' or 'cpu'
    # Keep downloaded model weights in stable, shared caches.  These match the
    # libraries' Windows defaults, so weights already downloaded there are reused.
    WHISPER_CACHE_DIR: Path = Path.home() / ".cache" / "whisper"

    # LLM Settings (NVIDIA NIM / OpenAI compatible / Ollama)
    LLM_PROVIDER: str = "nvidia"  # 'nvidia', 'openai', 'ollama', 'mock'
    LLM_MODEL: str = "meta/llama-3.1-8b-instruct"
    LLM_MODELS: str = ""
    LLM_API_KEY: str = Field(default="", description="API key for LLM service (e.g. NVIDIA NIM / OpenAI)")
    LLM_BASE_URL: str = "https://integrate.api.nvidia.com/v1"

    # Embeddings Model
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    HUGGINGFACE_CACHE_DIR: Path = Path.home() / ".cache" / "huggingface"

    CALL_QUALITY_DIARIZATION_ENABLED: bool = False
    CALL_QUALITY_LLM_ENABLED: bool = True
    CALL_QUALITY_LLM_MAX_TOKENS: int = 3500
    CALL_QUALITY_LLM_MIN_INTERVAL_SECONDS: float = 4.0
    CALL_QUALITY_LLM_RETRY_BACKOFF_SECONDS: float = 5.0
    CALL_QUALITY_LLM_REQUEST_TIMEOUT_SECONDS: float = 600.0
    CALL_QUALITY_LLM_MAX_RETRIES: int = 5
    CALL_QUALITY_WHISPER_MODEL: str = "base"
    CALL_QUALITY_MODEL_CACHE: Path = Path("models") / "faster-whisper"
    CALL_QUALITY_CPU_THREADS: int = 6

    # Chunking Parameters
    CHUNK_SIZE: int = 1500  # tokens
    CHUNK_OVERLAP: int = 200  # tokens

    # Neo4j Graph Database
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "password"

    def ensure_directories(self) -> None:
        """Create necessary directories if they do not exist."""
        print("[CONFIG] Initializing directory structure...")
        for path in [self.DATA_DIR, self.OUTPUTS_DIR, self.LOGS_DIR]:
            path.mkdir(parents=True, exist_ok=True)
            print(f"[CONFIG] Ensured directory exists: {path}")


# Global settings instance
settings = Settings()
settings.ensure_directories()
