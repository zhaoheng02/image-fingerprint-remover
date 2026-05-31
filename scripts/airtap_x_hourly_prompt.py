#!/usr/bin/env python3
"""Print the Airtap prompt for the hourly X -> backend -> Xiaohongshu flow."""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path


DEFAULT_AUTH_FILE = Path.home() / ".codex" / "secrets" / "imgclean-airtap-relay-secret"


def _read_secret(path: Path) -> str:
    env_secret = os.environ.get("AIRTAP_RELAY_SECRET", "").strip()
    if env_secret:
        return env_secret
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="https://imgclean-api.vercel.app")
    parser.add_argument("--secret-file", default=str(DEFAULT_AUTH_FILE))
    parser.add_argument("--dry-run", action="store_true", help="Tell Airtap to stop before final Xiaohongshu publish.")
    parser.add_argument(
        "--xhs-smoke",
        action="store_true",
        help="Generate a safe Xiaohongshu draft-only smoke prompt without WeChat push or dedupe writes.",
    )
    args = parser.parse_args()

    secret = _read_secret(Path(args.secret_file))
    if not secret:
        raise SystemExit("Missing AIRTAP_RELAY_SECRET or secret file.")
    if args.xhs_smoke:
        _print_xhs_smoke_prompt(args.api_base.rstrip("/"), secret)
        return

    final_publish = (
        "After filling the Xiaohongshu title, body, hashtags, and attachable media, stop before tapping the final publish button. "
        "Report the draft state and screenshots."
        if args.dry_run
        else "After filling the Xiaohongshu title, body, hashtags, and attachable media, publish the note through the Xiaohongshu app."
    )

    print(
        f"""Hourly X monitor and channel publisher.

Run on the cloud phone. Use Chrome for X and Xiaohongshu app for Xiaohongshu. Do not send anything to PushPlus yourself.

Accounts to check on X:
- xiaomustock
- hanking66
- aleabitoreddit
- IamRamenPanda
- ArtofSpecuycky

Collection rules:
1. Open https://x.com/home in Chrome and search/check each account above.
2. On the first run of the day, collect all posts from today. On later runs, collect only posts published in the past hour.
3. For every post, extract these raw fields exactly: id or canonical URL, author display name, author handle, relative/absolute publish time, original text, quote author/text when present, original post URL, image URLs, and video URLs.
4. For avatars, use an already visible/easy profile avatar URL when available. Do not open image tabs or spend extra steps trying to obtain direct avatar URLs; omitting avatar_url is acceptable.
5. Download or keep Airtap live URLs for images/videos when possible. Do not summarize posts in Airtap; the backend owns formatting, dedupe, and WeChat delivery.
6. Backend calls are a hard gate. Use Termux/curl or another reliable HTTP client on the cloud phone. Do not use browser page text as a substitute for an API response. Do not compose Xiaohongshu content yourself.
7. For every account you visited, call `/api/airtap/profiles/upsert` with the display name and handle you observed, even when avatar_url is omitted:
   POST {args.api_base.rstrip("/")}/api/airtap/profiles/upsert
   Headers: content-type: application/json, x-airtap-secret: {secret}
   Body: {{"profiles":[{{"display_name":"...","handle":"..."}}]}}
   This call must return HTTP 200 before you continue.
8. Then call:
   POST {args.api_base.rstrip("/")}/api/airtap/posts/publish
   Headers: content-type: application/json, x-airtap-secret: {secret}
   Body shape:
   {{
     "scope": "x-hourly-watch",
     "channels": ["wechat", "xiaohongshu"],
     "posts": [
       {{
         "id": "tweet id or URL",
         "author_name": "...",
         "author_handle": "...",
         "published_at": "...",
         "text": "...",
         "url": "https://x.com/...",
         "quote": {{"author_name":"...","text":"..."}},
         "image_urls": ["..."],
         "video_urls": ["..."]
       }}
     ]
   }}
9. The publish call must return HTTP 200 JSON. In your final report include the exact values of response.new_count, response.duplicate_count, pushes.wechat.ok/reason, and channels.xiaohongshu.title.
10. If the backend response is missing, non-200, or cannot be parsed, stop and report the backend error. Do not open Xiaohongshu and do not generate local replacement copy.
11. If response.new_count is 0, stop. WeChat will not be pushed and Xiaohongshu should not be posted.
12. If response.new_count is greater than 0, WeChat has already been pushed by the backend. Open Xiaohongshu and create a note using exactly:
   - title: response.channels.xiaohongshu.title
   - body: response.channels.xiaohongshu.body
   - hashtags: response.channels.xiaohongshu.hashtags
   - media: attach the collected local images/videos when the app allows it; otherwise leave the media links in the body.
13. {final_publish}
14. Report the backend publish response summary, Xiaohongshu publish/draft result, and any blocked step. Never expose PushPlus token, OpenAI key, or the relay secret in the final report.
"""
    )


def _print_xhs_smoke_prompt(api_base: str, secret: str) -> None:
    run_date = datetime.now().strftime("%Y-%m-%d")
    print(
        f"""Xiaohongshu draft smoke test for the Airtap -> backend workflow.

Run on the cloud phone. This is a safe dry run: Do not send anything to PushPlus, do not publish a Xiaohongshu note, and do not invent local copy.

Goal:
Verify that the phone can call the backend, receive backend-produced Xiaohongshu copy, open Xiaohongshu, fill a draft, and stop before tapping the final publish button.

Backend call:
Use Termux/curl or another reliable HTTP client on the cloud phone. The backend response is a hard gate.

POST {api_base}/api/airtap/posts/render
Headers: content-type: application/json, x-airtap-secret: {secret}
Body:
{{
  "scope": "xhs-smoke-draft",
  "channels": ["xiaohongshu"],
    "posts": [
    {{
      "id": "xhs-smoke-draft-{run_date}",
      "author_name": "Codex Smoke",
      "author_handle": "codex_smoke",
      "published_at": "{run_date} smoke run",
      "text": "小红书草稿链路测试：这条内容只用于验证 Airtap 手机端能拿到后端生成的小红书标题、正文和标签，并进入编辑页。不要发布。",
      "url": "https://x.com/codex_smoke/status/xhs-smoke-draft-{run_date}",
      "quote": {{"author_name": "Verifier", "text": "这是一条后端 render smoke，不会触发微信 PushPlus。"}},
      "image_urls": [],
      "video_urls": []
    }}
  ]
}}

Required behavior:
1. The backend call must return HTTP 200 JSON before opening Xiaohongshu.
2. Confirm response.new_count is 1, response.channels.xiaohongshu.title is present, and response.channels.xiaohongshu.body is present.
3. Open Xiaohongshu on the phone and create a note using exactly:
   - title: response.channels.xiaohongshu.title
   - body: response.channels.xiaohongshu.body
   - hashtags: response.channels.xiaohongshu.hashtags
4. Stop before tapping the final publish button. Leave the note as a draft or on the final confirmation/editor screen.
5. Report the backend response summary, the Xiaohongshu draft/editor state, and screenshots if available.
6. Never expose PushPlus token, OpenAI key, or the relay secret in the final report.
"""
    )


if __name__ == "__main__":
    main()
