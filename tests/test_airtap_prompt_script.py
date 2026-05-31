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


def test_hourly_dry_run_prompt_uses_bounded_x_search_flow():
    prompt = _run_prompt("--dry-run")

    assert "https://x.com/search?q=from%3Axiaomustock" in prompt
    assert "https://x.com/search?q=from%3Ahanking66" in prompt
    assert "Dry-run sampling limit" in prompt
    assert "at most 1 qualifying post per account" in prompt
    assert "DRY RUN SAFETY" in prompt
    assert "Do not tap Publish, Post, Next, 下一步, 发布, or any final submission button" in prompt
    assert "Do not write huge JSON by typing it into the terminal manually" in prompt
    assert "python3 - <<'PY'" in prompt


def test_hourly_production_prompt_has_no_empty_dry_run_step():
    prompt = _run_prompt()

    assert "DRY RUN SAFETY" not in prompt
    assert "\n15. \n" not in prompt
    assert "publish the note through the Xiaohongshu app" in prompt
