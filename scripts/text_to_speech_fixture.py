"""Create a WAV customer-support fixture using the Windows built-in speech engine.

Usage:
    python scripts/text_to_speech_fixture.py

The generated file is local and is suitable for the customer-call upload endpoint.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

DEFAULT_TEXT = """Thank you for calling Northstar Bank support. My name is Maya. How can I help you today?
I cannot log in to the mobile app, and I was charged twice for my payment.
I understand how frustrating that is. I can open a technical support case and request a refund review.
Please also let me speak to a manager because this has been unresolved for three days.
I already told you I cannot help with that. You need to stop wasting my time and listen carefully.
That is extremely rude. I am asking for help with a charge on my account.
Fine, I will escalate the case to a supervisor today. Your reference number is 4829017.
Thank you. I will wait for the supervisor to contact me."""


def create_fixture(output: Path, text: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    escaped_output = str(output.resolve()).replace("'", "''")
    utterances = [line.strip() for line in text.splitlines() if line.strip()]
    utterance_script = " ".join(
        f"$s.SelectVoice('{('Microsoft David Desktop' if index % 2 == 0 else 'Microsoft Zira Desktop')}'); "
        f"$s.Speak('{utterance.replace(chr(39), chr(39) + chr(39))}');"
        for index, utterance in enumerate(utterances)
    )
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate=0; $s.Volume=100; "
        f"$s.SetOutputToWaveFile('{escaped_output}'); "
        f"{utterance_script} $s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a local customer-care WAV fixture.")
    parser.add_argument("--output", type=Path, default=Path("data") / "customer_call_fixture.wav")
    parser.add_argument("--text-file", type=Path, help="Optional UTF-8 file containing the conversation.")
    args = parser.parse_args()
    text = args.text_file.read_text(encoding="utf-8") if args.text_file else DEFAULT_TEXT
    create_fixture(args.output, text)
    print(f"Created {args.output.resolve()}")


if __name__ == "__main__":
    main()
