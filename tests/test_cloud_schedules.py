from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_github_action_dispatches_xhs_without_local_computer():
    workflow = ROOT / ".github" / "workflows" / "xhs-dispatch.yml"
    content = workflow.read_text(encoding="utf-8")

    assert "cron: '0 0,8,16 * * *'" in content
    assert "https://imgclean-api.vercel.app/api/airtap/xhs/dispatch" in content
    assert "X_AIRTAP_RELAY_SECRET" in content
    assert "x-airtap-secret" in content
