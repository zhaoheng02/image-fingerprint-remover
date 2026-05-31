#!/usr/bin/env python3
"""Print Airtap prompts for the X -> backend channel workflows."""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path


DEFAULT_AUTH_FILE = Path.home() / ".codex" / "secrets" / "imgclean-airtap-relay-secret"
ACCOUNTS = ("xiaomustock", "hanking66", "aleabitoreddit", "IamRamenPanda", "ArtofSpecuycky")


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
    parser.add_argument(
        "--channel-plan",
        choices=("wechat-hourly", "xhs-8h", "cloud-routine"),
        default="wechat-hourly",
        help="Which Airtap workflow prompt to print.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Use render-only/draft-only validation instead of publishing.")
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

    api_base = args.api_base.rstrip("/")
    if args.channel_plan == "cloud-routine":
        _print_cloud_routine_prompt(api_base, secret, dry_run=args.dry_run)
    elif args.channel_plan == "xhs-8h":
        _print_channel_prompt(api_base, secret, channel_plan="xhs-8h", dry_run=args.dry_run)
    else:
        _print_channel_prompt(api_base, secret, channel_plan="wechat-hourly", dry_run=args.dry_run)


def _search_urls() -> str:
    return "\n".join(
        f"- {handle}: https://x.com/search?q=from%3A{handle}&src=typed_query&f=live" for handle in ACCOUNTS
    )


def _print_channel_prompt(api_base: str, secret: str, *, channel_plan: str, dry_run: bool) -> None:
    is_wechat = channel_plan == "wechat-hourly"
    channel = "wechat" if is_wechat else "xiaohongshu"
    scope = "x-hourly-wechat" if is_wechat else "x-8h-xhs"
    window_text = "past hour" if is_wechat else "past 8 hours"
    zh_window = "过去 1 小时" if is_wechat else "过去 8 小时"
    title = "Hourly X monitor for WeChat PushPlus" if is_wechat else "8-hour X digest for Xiaohongshu"
    endpoint = "/api/airtap/posts/render" if dry_run else "/api/airtap/posts/publish"
    call_kind = "render-only dry run" if dry_run else "publish"
    sampling_rule = (
        "Dry-run sampling limit: collect at most 1 qualifying post per account and at most 3 posts total. "
        "If more qualifying posts are visible, report the count/handles you skipped because this was a dry-run validation."
        if dry_run
        else f"Production collection limit: do not impose an artificial post cap. Stop each account only after the visible feed is older than the {window_text} window."
    )
    xhs_steps = ""
    if not is_wechat:
        final_publish = (
            "After filling the Xiaohongshu title, body, hashtags, and attachable media, stop before tapping the final publish button. "
            "Report the draft state and screenshots."
            if dry_run
            else "After filling the Xiaohongshu title, body, hashtags, and attachable media, publish the note through the Xiaohongshu app."
        )
        dry_run_safety = (
            "DRY RUN SAFETY: Do not tap Publish, Post, Next, 下一步, 发布, or any final submission button. "
            "If the Xiaohongshu editor shows only a final submission step, stop immediately and report the draft/editor state."
            if dry_run
            else ""
        )
        xhs_steps = f"""
15. If response.new_count is 0, stop. Do not open Xiaohongshu.
16. If response.new_count is greater than 0, open Xiaohongshu and create a note using exactly:
   - title: response.channels.xiaohongshu.title
   - body: response.channels.xiaohongshu.body
   - hashtags: response.channels.xiaohongshu.hashtags
   - media: attach the collected local images/videos when the app allows it. Do not paste raw X links into the note body.
17. {dry_run_safety}
18. {final_publish}
19. Report response.new_count, response.duplicate_count, channels.xiaohongshu.title, Xiaohongshu publish/draft result, and any blocked step. Never expose PushPlus token, OpenAI key, or the relay secret in the final report."""
    else:
        xhs_steps = """
15. If response.new_count is 0, stop; there is nothing to push this hour.
16. If response.new_count is greater than 0, the backend already handled WeChat PushPlus delivery. Do not open Xiaohongshu in this WeChat-only plan.
17. Report response.new_count, response.duplicate_count, pushes.wechat.ok/reason, and any blocked step. Never expose PushPlus token, OpenAI key, or the relay secret in the final report."""

    print(
        f"""{title}.

Run on the cloud phone. Use Chrome for X. Do not send anything to PushPlus yourself.
This plan is {call_kind} for the {channel} channel only.

Accounts to check on X:
- xiaomustock
- hanking66
- aleabitoreddit
- IamRamenPanda
- ArtofSpecuycky

Collection rules:
1. Open these X search URLs directly in Chrome instead of navigating through home/profile pages:
{_search_urls()}
2. Collect posts from {zh_window}. Stop each account only after the visible feed is older than that time window.
3. {sampling_rule}
4. For every post, extract raw fields only. Do not translate, rewrite, summarize, shorten, or infer the post body.
   Required fields per post: id or canonical URL, author display name, author handle, relative/absolute publish time, original visible text, original post URL, image URLs, video URLs, and link cards.
   If the post contains visible images or video, `image_urls` / `video_urls` must not be silently empty. Use direct X media URLs when available; otherwise download/open the media on the cloud phone and pass stable Airtap live file URLs such as `https://airtap.ai/content/live/android-files/...`.
   If a media URL cannot be obtained, add a short `media_error` field explaining what failed.
5. If the post quotes/reposts another X post, open or expand the quoted post. The `quote` object must include the quoted URL/id, author display name, handle, publish time, full visible text, image URLs, video URLs, and link cards. Do not send only the small collapsed quote stub.
   If the quoted post is no longer visible or cannot be opened, still include `quote.url` and `quote.author_handle` when visible, plus `quote_error`.
6. For avatars, use an already visible/easy profile avatar URL when available. Do not open image tabs or spend extra steps trying to obtain direct avatar URLs; omitting avatar_url is acceptable because the backend has a profile cache.
7. Backend calls are a hard gate. Use Termux/curl or another reliable HTTP client on the cloud phone. Do not use browser page text as a substitute for an API response. Do not compose Xiaohongshu content yourself.
8. Do not write huge JSON by typing it into the terminal manually. In Termux, use a Python heredoc like `python3 - <<'PY'` to create compact request JSON files, then call curl with `--data-binary @file.json`.
9. Before posting to the backend, self-check the JSON: any post that visibly has a quote must contain `quote`; any post that visibly has media must contain `image_urls` or `video_urls` or a `media_error`.
10. For every account you visited, call `/api/airtap/profiles/upsert` with the display name and handle you observed, even when avatar_url is omitted:
   POST {api_base}/api/airtap/profiles/upsert
   Headers: content-type: application/json, x-airtap-secret: {secret}
   Body: {{"profiles":[{{"display_name":"...","handle":"..."}}]}}
   This call must return HTTP 200 before you continue.
11. Then call:
   POST {api_base}{endpoint}
   Headers: content-type: application/json, x-airtap-secret: {secret}
   Body shape:
   {{
     "scope": "{scope}",
     "channels": ["{channel}"],
     "posts": [
       {{
         "id": "tweet id or URL",
         "author_name": "...",
         "author_handle": "...",
         "published_at": "...",
         "text": "...",
         "url": "https://x.com/...",
         "quote": {{"author_name":"...","author_handle":"...","published_at":"...","text":"...","url":"https://x.com/...","image_urls":["..."],"video_urls":["..."],"link_cards":[{{"provider":"youtube","title":"...","url":"https://www.youtube.com/watch?v=...","thumbnail_url":"..."}}]}},
         "image_urls": ["..."],
         "video_urls": ["..."],
         "link_cards": [{{"provider":"youtube","title":"...","url":"https://www.youtube.com/watch?v=...","thumbnail_url":"..."}}]
       }}
     ]
   }}
12. The backend call must return HTTP 200 JSON. If the backend response is missing, non-200, or cannot be parsed, stop and report the backend error.
13. Inspect the backend JSON response before finishing: report response.new_count, response.duplicate_count, pushes.wechat.ok/reason, and the count of posts you sent with quote/image/video fields. Do not expose secrets.
14. Do not generate local replacement copy. For Xiaohongshu, use response.channels.xiaohongshu exactly; for WeChat, the backend already pushes.
{xhs_steps}
"""
    )


def _print_cloud_routine_prompt(api_base: str, secret: str, *, dry_run: bool) -> None:
    endpoint = "/api/airtap/posts/render" if dry_run else "/api/airtap/posts/publish"
    dry_note = (
        "This is a dry-run validation: use render endpoints only and do not tap any Xiaohongshu final publish button."
        if dry_run
        else "This is production: WeChat is pushed by the backend; Xiaohongshu is dispatched later by the server from stored hourly posts."
    )
    print(
        f"""Cloud routine: X monitor hourly ingestion for WeChat and server-side Xiaohongshu batching.

Run this as the single Airtap cloud routine. The computer running Codex is not part of production execution.
{dry_note}

Every run:
1. Determine current Asia/Shanghai time.
2. Collect posts only from the past hour for xiaomustock, hanking66, aleabitoreddit, IamRamenPanda, ArtofSpecuycky.
3. Do not collect an 8-hour history in Airtap. The server stores hourly posts and creates the 8-hour Xiaohongshu batch later.
4. Call POST {api_base}/api/airtap/profiles/upsert for observed profiles.
5. Call POST {api_base}{endpoint} with scope "x-hourly-wechat", channels ["wechat"], and the collected posts.
6. WeChat content must come from the backend. Do not push to PushPlus yourself and do not show raw X links as message body.
7. Do not open Xiaohongshu. A separate server/GitHub scheduled job will later call the backend dispatch endpoint. The backend will push the Xiaohongshu note to WeChat for approval, and only after the signed confirmation link is tapped will the backend create a separate Airtap task for Xiaohongshu publishing.
8. Backend calls are a hard gate. Use Termux/curl or another reliable HTTP client on the cloud phone. Do not use browser page text as a substitute for API JSON.
9. Never expose PushPlus token, OpenAI key, or the relay secret in reports. The relay secret must only be sent as x-airtap-secret: {secret}.

Use these X search URLs directly:
{_search_urls()}

For every post, extract raw data only and send it in JSON with these exact snake_case keys: id, author_name, author_handle, published_at, text, url, image_urls, video_urls, link_cards, quote. Do not translate, rewrite, summarize, shorten, or infer the post body. If id and handle are visible, set url to https://x.com/<handle>/status/<id> even if the browser address bar is unavailable. If the post visibly contains media, image_urls / video_urls must not be silently empty: use direct media URLs or Airtap live file URLs after opening/downloading the media; otherwise include media_error. A short token like "含图 HJpvlt6XUAIesk5" is not a media URL; either turn it into a real https://pbs.twimg.com/media/... URL / Airtap file URL or set media_error. If there is a quoted X post, the JSON post must contain a quote object with quoted id/url, author_name, author_handle, published_at, text, image_urls, video_urls, and link_cards. Do not put quote details only in your final report. If the quote cannot be opened, still include quote.url or quote.id/quote.author_handle plus quote_error. For YouTube cards, include title, URL, and thumbnail URL when visible. Before curl, validate posts_publish.json: every collected post has url; every visible quote has quote; every visible media item has image_urls/video_urls or media_error. If validation fails, fix the JSON before POST. Do not summarize inside Airtap; the backend owns formatting, dedupe, WeChat delivery, and Xiaohongshu copy.
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
