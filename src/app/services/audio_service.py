"""Audio Processing Service Module.

Handles audio and video file validation, audio extraction from video containers,
channel reduction to mono, resampling to 16kHz, volume normalization, and WAV export.
"""

import time
import os
import subprocess
from pathlib import Path
from pydub import AudioSegment, effects
from loguru import logger
from app.config import settings
from app.utils.exceptions import InvalidAudioFormatException, AudioProcessingException


class AudioService:
    """Service dedicated to preparing audio files for diarization and speech recognition."""

    def __init__(self, target_sample_rate: int = None, target_channels: int = None):
        """Initialize AudioService with configured sample rate and channels."""
        self.sample_rate = target_sample_rate or settings.AUDIO_SAMPLE_RATE
        self.channels = target_channels or settings.AUDIO_CHANNELS
        self.ffmpeg_exe = None

        # Auto-detect FFmpeg binary via imageio_ffmpeg or system PATH
        try:
            import imageio_ffmpeg
            self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            ffmpeg_dir = os.path.dirname(self.ffmpeg_exe)
            if ffmpeg_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
            AudioSegment.converter = self.ffmpeg_exe
            AudioSegment.ffmpeg = self.ffmpeg_exe
            import pydub.utils
            old_which = pydub.utils.which
            pydub.utils.which = lambda x: self.ffmpeg_exe if x in ("ffmpeg", "avconv") else old_which(x)
            print(f"[AudioService] Configured FFmpeg binary via imageio_ffmpeg: {self.ffmpeg_exe}")
        except Exception as ex:
            logger.warning(f"[AudioService] Could not set ffmpeg path via imageio_ffmpeg: {ex}")
            self.ffmpeg_exe = "ffmpeg"

        print(f"[AudioService] Initialized AudioService | Target Sample Rate: {self.sample_rate}Hz | Channels: {self.channels} (Mono)")

    def validate_file_format(self, file_path: Path) -> str:
        """Validate input file extension against allowed audio and video formats.

        Args:
            file_path: Path to the input file.

        Returns:
            str: File extension.

        Raises:
            InvalidAudioFormatException: If format is unsupported.
        """
        print(f"\n--- Step 1: Validating Input File Format ---")
        print(f"[AudioService] Input file path: {file_path}")
        print(f"[AudioService] Checking file extension against allowed formats: {settings.ALLOWED_EXTENSIONS}")

        ext = file_path.suffix.lower()
        if ext not in settings.ALLOWED_EXTENSIONS:
            error_msg = f"Unsupported file extension '{ext}'. Allowed extensions: {settings.ALLOWED_EXTENSIONS}"
            logger.error(f"[AudioService] {error_msg}")
            print(f"[AudioService ERROR] {error_msg}")
            raise InvalidAudioFormatException(error_msg)

        is_video = ext in {".mp4", ".mkv", ".mov", ".avi", ".webm"}
        file_type = "VIDEO" if is_video else "AUDIO"
        print(f"[AudioService] Validation SUCCESS: File '{file_path.name}' is a valid {file_type} format ({ext}).")
        return ext

    def process_audio(self, input_file_path: Path, output_dir: Path) -> Path:
        """Process input audio/video file: extract audio track, convert to mono, resample to 16kHz, normalize volume.

        Args:
            input_file_path: Path to input audio or video file.
            output_dir: Directory where normalized.wav will be saved.

        Returns:
            Path: Path to generated normalized.wav file.
        """
        start_time = time.time()
        logger.info(f"[AudioService] Audio processing started for input: {input_file_path}")
        print(f"\n=======================================================")
        print(f"[AudioService] STARTING AUDIO NORMALIZATION PIPELINE")
        print(f"=======================================================")
        print(f"[AudioService] Reading input file from: {input_file_path}")

        ext = self.validate_file_format(input_file_path)
        output_file_path = output_dir / "normalized.wav"

        try:
            # Step A: Convert input media to 16kHz mono WAV using FFmpeg binary directly
            print(f"\n--- Step 2: Decoding Media & Converting to 16kHz Mono WAV ---")
            ffmpeg_cmd = [
                self.ffmpeg_exe, "-y",
                "-i", str(input_file_path),
                "-ac", str(self.channels),
                "-ar", str(self.sample_rate),
                str(output_file_path)
            ]
            print(f"[AudioService] Running FFmpeg command: {' '.join(ffmpeg_cmd)}")
            subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            print(f"[AudioService] FFmpeg conversion SUCCESS -> Created initial {output_file_path.name}")

            # Step B: Load converted WAV with pydub for Peak Volume Normalization
            print(f"\n--- Step 3: Peak Volume Normalization & Quality Audit ---")
            audio = AudioSegment.from_wav(str(output_file_path))
            original_duration_sec = len(audio) / 1000.0
            print(f"[AudioService] Details -> Duration: {original_duration_sec:.2f} seconds | Pre-Norm dBFS: {audio.dBFS:.2f} dBFS")

            # Apply peak normalization to optimize gain for speech recognition
            normalized_audio = effects.normalize(audio)
            print(f"[AudioService] Post-Norm dBFS: {normalized_audio.dBFS:.2f} dBFS")

            # Export final normalized.wav
            normalized_audio.export(str(output_file_path), format="wav")

            elapsed_time = time.time() - start_time
            logger.info(f"[AudioService] Audio normalization finished in {elapsed_time:.2f}s. Saved to: {output_file_path}")
            print(f"=======================================================")
            print(f"[AudioService SUCCESS] Exported {output_file_path.name}")
            print(f"[AudioService SUCCESS] Total Duration: {original_duration_sec:.2f}s | Processing Time: {elapsed_time:.2f}s")
            print(f"=======================================================\n")

            return output_file_path

        except Exception as e:
            stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e, subprocess.CalledProcessError) and e.stderr else "<no FFmpeg stderr captured>"
            error_msg = (
                f"Audio processing failed for '{input_file_path}' using the required FFmpeg conversion. "
                f"Exception={type(e).__name__}: {e}. FFmpeg stderr: {stderr}"
            )
            logger.exception(f"[AudioService] {error_msg}")
            print(f"[AudioService ERROR] {error_msg}")
            raise AudioProcessingException(error_msg) from e
