from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict


class SpeechToTextNotConfigured(RuntimeError):
    pass


class SpeechToTextService:
    def __init__(self) -> None:
        self.provider = os.getenv("STT_PROVIDER", "disabled").strip().lower()
        self.whisper_binary = os.getenv("WHISPER_CPP_BINARY", "whisper-cli").strip()
        self.whisper_model = os.getenv("WHISPER_CPP_MODEL", "").strip()
        self.ffmpeg_binary = os.getenv("FFMPEG_BINARY", "ffmpeg").strip()
        self.timeout_seconds = int(os.getenv("STT_TIMEOUT_SECONDS", "120"))

    def status(self) -> Dict[str, Any]:
        configured = self.provider == "whisper_cpp" and bool(self.whisper_binary and self.whisper_model)
        return {
            "provider": self.provider,
            "configured": configured,
            "engine": "Whisper.cpp" if self.provider == "whisper_cpp" else "disabled",
            "model": Path(self.whisper_model).name if self.whisper_model else None,
            "message": (
                "Open-source dictation is ready."
                if configured
                else "Set STT_PROVIDER=whisper_cpp, WHISPER_CPP_BINARY, and WHISPER_CPP_MODEL to enable open-source dictation."
            ),
        }

    def transcribe(self, audio_path: Path, content_type: str | None = None) -> Dict[str, Any]:
        if self.provider != "whisper_cpp" or not self.whisper_binary or not self.whisper_model:
            raise SpeechToTextNotConfigured(self.status()["message"])

        prepared_audio = self._prepare_audio(audio_path, content_type)
        with tempfile.TemporaryDirectory() as td:
            out_prefix = Path(td) / "transcript"
            cmd = [
                self.whisper_binary,
                "-m",
                self.whisper_model,
                "-f",
                str(prepared_audio),
                "-otxt",
                "-of",
                str(out_prefix),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "Whisper.cpp transcription failed.").strip()
                raise RuntimeError(detail)

            transcript_file = out_prefix.with_suffix(".txt")
            text = transcript_file.read_text(encoding="utf-8").strip() if transcript_file.exists() else result.stdout.strip()
        return {"text": text, "provider": self.provider, "model": Path(self.whisper_model).name}

    def _prepare_audio(self, audio_path: Path, content_type: str | None) -> Path:
        if content_type and ("wav" in content_type or "wave" in content_type):
            return audio_path
        if audio_path.suffix.lower() == ".wav":
            return audio_path
        if not self.ffmpeg_binary:
            raise SpeechToTextNotConfigured("Set FFMPEG_BINARY so browser microphone audio can be converted to WAV.")

        converted = audio_path.with_suffix(".wav")
        cmd = [self.ffmpeg_binary, "-y", "-i", str(audio_path), "-ar", "16000", "-ac", "1", str(converted)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Audio conversion failed.").strip()
            raise RuntimeError(detail)
        return converted
