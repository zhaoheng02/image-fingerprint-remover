#!/usr/bin/env python3
"""Print the Airtap prompt for the hourly X -> backend -> Xiaohongshu flow."""
from __future__ import annotations

import argparse
import os
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
    args = parser.parse_args()

    secret = _read_secret(Path(args.secret_file))
    if not secret:
        raise SystemExit("Missing AIRTAP_RELAY_SECRET or secret file.")

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
3. For every post, extract these raw fields exactly: id or canonical URL, author display name, author handle, avatar image URL, relative/absolute publish time, original text, quote author/text when present, original post URL, image URLs, and video URLs.
4. Download or keep Airtap live URLs for images/videos when possible. Do not summarize posts in Airtap; the backend owns formatting, dedupe, and WeChat delivery.
5. For any profile with a new/refreshed avatar, call:
   POST {args.api_base.rstrip("/")}/api/airtap/profiles/upsert
   Headers: content-type: application/json, x-airtap-secret: {secret}
   Body: {{"profiles":[{{"display_name":"...","handle":"...","avatar_url":"..."}}]}}
6. Then call:
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
7. If response.new_count is 0, stop. WeChat will not be pushed and Xiaohongshu should not be posted.
8. If response.new_count is greater than 0, WeChat has already been pushed by the backend. Open Xiaohongshu and create a note using:
   - title: response.channels.xiaohongshu.title
   - body: response.channels.xiaohongshu.body
   - hashtags: response.channels.xiaohongshu.hashtags
   - media: attach the collected local images/videos when the app allows it; otherwise leave the media links in the body.
9. {final_publish}
10. Report the backend publish response summary, Xiaohongshu publish/draft result, and any blocked step. Never expose PushPlus token, OpenAI key, or the relay secret in the final report.
"""
    )


if __name__ == "__main__":
    main()
