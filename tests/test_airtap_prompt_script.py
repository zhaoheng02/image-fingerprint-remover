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


def test_wechat_hourly_prompt_uses_bounded_x_search_flow():
    prompt = _run_prompt("--dry-run")

    assert "https://x.com/search?q=from%3Axiaomustock" in prompt
    assert "https://x.com/search?q=from%3Ahanking66" in prompt
    assert "Hourly X monitor for WeChat PushPlus" in prompt
    assert "/api/airtap/posts/render" in prompt
    assert '"scope": "x-hourly-wechat"' in prompt
    assert '"channels": ["wechat"]' in prompt
    assert "过去 1 小时" in prompt
    assert "Dry-run sampling limit" in prompt
    assert "at most 1 qualifying post per account" in prompt
    assert "Do not write huge JSON by typing it into the terminal manually" in prompt
    assert "python3 - <<'PY'" in prompt
    assert '"channels": ["wechat", "xiaohongshu"]' not in prompt


def test_wechat_production_prompt_has_no_xiaohongshu_publish_step():
    prompt = _run_prompt()

    assert "DRY RUN SAFETY" not in prompt
    assert "\n15. \n" not in prompt
    assert "/api/airtap/posts/publish" in prompt
    assert '"scope": "x-hourly-wechat"' in prompt
    assert '"channels": ["wechat"]' in prompt
    assert "Do not open Xiaohongshu in this WeChat-only plan" in prompt


def test_xhs_8h_prompt_uses_separate_scope_and_human_summary_direction():
    prompt = _run_prompt("--channel-plan", "xhs-8h")

    assert "8-hour X digest for Xiaohongshu" in prompt
    assert "过去 8 小时" in prompt
    assert '"scope": "x-8h-xhs"' in prompt
    assert '"channels": ["xiaohongshu"]' in prompt
    assert "Do not paste raw X links into the note body" in prompt
    assert "publish the note through the Xiaohongshu mobile app only" in prompt
    assert "Do not use the web publisher" in prompt


def test_cloud_routine_prompt_only_collects_the_latest_hour():
    prompt = _run_prompt("--channel-plan", "cloud-routine")

    assert "single Airtap cloud routine" in prompt
    assert "computer running Codex is not part of production execution" in prompt
    assert 'scope "x-hourly-wechat", channels ["wechat"]' in prompt
    assert 'scope "x-8h-xhs", channels ["xiaohongshu"]' not in prompt
    assert "Do not collect an 8-hour history in Airtap" in prompt
    assert "Do not open Xiaohongshu" in prompt
