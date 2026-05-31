import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "airtap_x_hourly_prompt.py"


def _run_prompt(*args: str) -> str:
    env = {**os.environ, "AIRTAP_RELAY_SECRET": "relay-secret"}
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=True,
        env=env,
        text=True,
        capture_output=True,
    )
    return completed.stdout


def test_xhs_smoke_prompt_renders_backend_only_draft_flow():
    prompt = _run_prompt("--xhs-smoke")

    assert "/api/airtap/posts/render" in prompt
    assert "/api/airtap/posts/publish" not in prompt
    assert '"channels": ["xiaohongshu"]' in prompt
    assert "Do not send anything to PushPlus" in prompt
    assert "stop before tapping the final publish button" in prompt
    assert "Never expose PushPlus token, OpenAI key, or the relay secret" in prompt
