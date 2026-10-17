"""Generate ten unlabeled customer-care WAV scenarios and a JSON manifest.

Uses Windows System.Speech locally. Each turn alternates the installed David/Zira
voices; the transcript intentionally contains no Agent/Customer labels.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

SCENARIOS = [
    ("01_angry_customer", "Angry customer", "billing", ["I have called three times and my payment is still wrong.", "I am sorry this has been frustrating. I will investigate the duplicate charge now.", "That is not good enough. I need this fixed today.", "I understand. I have opened an urgent billing case and given you reference 731204."]),
    ("02_excellent_experience", "Excellent experience", "account", ["Thank you for calling Northstar support. How may I help you?", "I needed to update my address.", "I can take care of that now and confirm the change before we finish.", "That was quick and clear. Thank you for your help.", "You are very welcome. Have a wonderful day."]),
    ("03_login_incident", "Login failure", "login", ["I cannot sign in to the mobile app after resetting my password.", "I can help with that. Please try the secure reset link I am sending.", "The link worked, and I can access my account now.", "Great. Is there anything else I can help with today?"]),
    ("04_duplicate_charge", "Duplicate charge", "payment", ["I was charged twice for the same order.", "I can see why that is concerning. I will submit a refund review.", "When should I expect the money back?", "The review normally takes three business days, and I will provide the case number."]),
    ("05_escalation", "Manager escalation", "service", ["Your delivery was promised yesterday and it still has not arrived.", "I apologize. I will check the shipment immediately.", "Please let me speak to a manager because this is unacceptable.", "I will escalate this case and ask a supervisor to contact you today."]),
    ("06_rude_agent", "Rude agent", "conduct", ["I need help cancelling a service I no longer use.", "I already told you, I cannot deal with that. Stop repeating yourself.", "I am asking politely for cancellation assistance.", "Read the terms yourself and call back when you understand them."]),
    ("07_neutral_question", "Normal information question", "information", ["Could you tell me what time your branches close today?", "Our branches close at five o'clock today.", "Is Saturday service available?", "Yes, selected branches are open from nine until noon."]),
    ("08_unresolved", "Unresolved technical issue", "technical", ["The application still crashes whenever I upload a document.", "I have recorded the error and escalated it to our technical team.", "So it is not fixed yet?", "Not yet, but the support team will update you within one business day."]),
    ("09_cancellation_request", "Cancellation request", "subscription", ["Please cancel my premium subscription before the next renewal.", "I can process that request and explain what access remains active.", "Yes, please cancel it today.", "The cancellation is confirmed and there will be no next renewal."]),
    ("10_mixed_compliance", "Mixed compliance call", "compliance", ["Before we continue, are you recording this call?", "Yes, this call may be recorded for quality purposes.", "I need a refund for an item that arrived damaged.", "I will submit the refund request and email the next steps."]),
]


def create_audio(output: Path, lines: list[str]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    escaped_output = str(output.resolve()).replace("'", "''")
    utterances = " ".join(
        f"$s.SelectVoice('{('Microsoft David Desktop' if index % 2 == 0 else 'Microsoft Zira Desktop')}'); $s.Speak('{line.replace(chr(39), chr(39) + chr(39))}');"
        for index, line in enumerate(lines)
    )
    script = "Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Rate=0; $s.Volume=100; " + f"$s.SetOutputToWaveFile('{escaped_output}'); {utterances} $s.Dispose()"
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("data") / "call_quality_dataset")
    args = parser.parse_args()
    manifest = []
    for filename, scenario, product, lines in SCENARIOS:
        path = args.output_dir / f"{filename}.wav"
        create_audio(path, lines)
        manifest.append({"filename": path.name, "path": str(path), "scenario": scenario, "product": product, "turns": len(lines), "text": lines})
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Created {len(manifest)} WAV files and {manifest_path}")


if __name__ == "__main__":
    main()
