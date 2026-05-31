# SaaS deployment

This repo is split into two deployable apps:

- `frontend/` — Next.js app for Vercel: marketing site, login, dashboard, and pricing.
- repository root — FastAPI API for Vercel Python or Google Cloud Run: image cleaning API, Supabase/Local ledger, Supabase Storage/R2 storage, Stripe checkout/webhook, and Lemon Squeezy webhook.

## Supabase

Project:

- URL: `https://avqgeqslhywrulnzjgzo.supabase.co`
- Ref: `avqgeqslhywrulnzjgzo`
- Region: `ap-northeast-1`

The database schema is in `supabase/migrations/001_saas_core.sql`.

It creates:

- `profiles` — user credits
- `usage_events` — image-cleaning and credit events
- `orders` — paid order records
- `payment_webhook_events` — webhook audit log
- `consume_credit(...)`
- `grant_credits(...)`

## Backend environment

For the free trial deployment, deploy the API to Vercel with the variables listed in `deploy/vercel-api.env.example`. This runs with:

- `IMGCLEAN_AUTH_MODE=none` so uploads are anonymous.
- `IMGCLEAN_STORAGE_BACKEND=supabase` so cleaned files are stored in Supabase Storage instead of an ephemeral serverless filesystem.
- Billing disabled at the frontend by setting `NEXT_PUBLIC_BILLING_ENABLED=false`.
- Downloads are proxied through `/api/download` when the stored object URL is cross-origin, so browsers download the file instead of opening an image preview tab.

For Cloud Run, use the variables listed in `deploy/cloud-run.env.example`.

Required secrets:

- `imgclean-supabase-secret-key` -> `SUPABASE_SECRET_KEY`
- `imgclean-r2-access-key-id` -> `R2_ACCESS_KEY_ID`
- `imgclean-r2-secret-access-key` -> `R2_SECRET_ACCESS_KEY`
- `imgclean-stripe-secret-key` -> `STRIPE_SECRET_KEY`
- `imgclean-stripe-webhook-secret` -> `STRIPE_WEBHOOK_SECRET`
- `imgclean-lemonsqueezy-webhook-secret` -> `LEMONSQUEEZY_WEBHOOK_SECRET`
- `imgclean-session-secret` -> `IMGCLEAN_SESSION_SECRET`
- `imgclean-airtap-relay-secret` -> `AIRTAP_RELAY_SECRET`
- `imgclean-openai-api-key` -> `OPENAI_API_KEY`
- `imgclean-pushplus-token` -> `PUSHPLUS_TOKEN`
- `imgclean-pushplus-access-key` -> `PUSHPLUS_ACCESS_KEY` (optional, enables PushPlus image hosting)
- `imgclean-wechat-app-id` -> `WECHAT_APP_ID`
- `imgclean-wechat-app-secret` -> `WECHAT_APP_SECRET`
- `imgclean-wechat-miniprogram-app-id` -> `WECHAT_MINIPROGRAM_APP_ID`
- `imgclean-wechat-miniprogram-app-secret` -> `WECHAT_MINIPROGRAM_APP_SECRET`

The Cloud Run service template in `deploy/cloud-run-service.yaml` reads these from Google Secret Manager. Create each secret before applying the service YAML, then replace `IMAGE_PLACEHOLDER` with the pushed container image.

The Vercel API entrypoint is `app.py`. Vercel reads runtime dependencies from `requirements.txt`.

## WeChat OAuth

Create a Website App in WeChat Open Platform, apply for Website WeChat Login, and wait for review. The app must provide a callback domain that matches the backend callback URL host. For the current Vercel API deployment:

```text
Callback URL: https://imgclean-api.vercel.app/api/auth/wechat/callback
Callback domain: imgclean-api.vercel.app
```

Set the backend to `IMGCLEAN_AUTH_MODE=wechat`, configure `IMGCLEAN_SESSION_SECRET`, `WECHAT_APP_ID`, `WECHAT_APP_SECRET`, and either `WECHAT_REDIRECT_URI` or `IMGCLEAN_API_BASE_URL`. Keep `APP_BASE_URL` pointed at the frontend because it is used after a successful login redirect. Then set the frontend to:

```bash
NEXT_PUBLIC_REQUIRE_AUTH=true
NEXT_PUBLIC_AUTH_PROVIDER=wechat
```

The login button redirects to `/api/auth/wechat/login`, which uses the WeChat Open Platform website OAuth scope `snsapi_login`, carries a signed `state` value for CSRF protection, exchanges the callback `code` server-side with `WECHAT_APP_SECRET`, fetches the WeChat profile, and sets an `imgclean_session` signed cookie.

`GET /api/auth/wechat/status` reports whether the backend is configured and returns the callback URL/domain to copy into WeChat Open Platform.

## WeChat Mini Program

For the no-payment MVP, use `miniapp/` as the WeChat Mini Program client. It calls `wx.login()`, posts the returned code to `/api/auth/wechat-miniprogram/login`, receives a Bearer token, and sends that token with `wx.uploadFile` requests to `/api/clean`. This follows the WeChat Mini Program login flow documented at `https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/login.html` and the `code2Session` endpoint documented at `https://developers.weixin.qq.com/miniprogram/dev/OpenApiDoc/user-login/code2Session.html`.

Backend variables:

```bash
IMGCLEAN_AUTH_MODE=wechat_miniprogram
IMGCLEAN_CREDITS_ENABLED=false
WECHAT_MINIPROGRAM_APP_ID=<mini program appid>
WECHAT_MINIPROGRAM_APP_SECRET=<mini program appsecret>
IMGCLEAN_SESSION_SECRET=<random secret>
```

If the Mini Program AppID/AppSecret is not ready yet, keep `IMGCLEAN_AUTH_MODE=none`; the mini program can still upload anonymously, but the login button will show that Mini Program login is not configured.

In the Mini Program admin console, add `https://imgclean-api.vercel.app` to the request, uploadFile, and downloadFile legal domains; WeChat documents these network domain requirements at `https://developers.weixin.qq.com/miniprogram/dev/framework/ability/network.html`. The client proxies cross-origin cleaned file downloads through `/api/download`, so the mini program only needs to whitelist the API domain.

## Airtap Relay

Airtap should stay focused on phone/browser automation: open X, collect the raw post fields, and call the backend before pushing. The backend stores avatar mappings, deduplicates posts by `scope`, and returns channel-ready content.

Configure the API:

```bash
AIRTAP_RELAY_SECRET=<random shared secret>
AIRTAP_AI_ENABLED=true
OPENAI_BASE_URL=https://api.xairouter.com
OPENAI_API_KEY=<router or OpenAI key>
AIRTAP_AI_MODEL=gpt-5.5
AIRTAP_STORAGE_BUCKET=imgclean-airtap
AIRTAP_SIGNED_URL_TTL_SECONDS=259200
PUSHPLUS_TOKEN=<pushplus token>
PUSHPLUS_ACCESS_KEY=<optional pushplus access-key for image hosting>
PUSHPLUS_ENDPOINT=https://www.pushplus.plus/send
PUSHPLUS_UPLOAD_TOKEN_ENDPOINT=https://www.pushplus.plus/api/open/userImage/uploadToken
PUSHPLUS_TOPIC=<optional topic>
```

Use `AIRTAP_AI_ENABLED=false` to keep deterministic rendering only. With Supabase storage enabled, avatar files, relay media, and `airtap/state.json` are persisted in `AIRTAP_STORAGE_BUCKET` so the relay can store JSON state separately from image-only upload buckets; local mode stores them under `IMGCLEAN_WEB_DATA_DIR/airtap`. PushPlus documents `POST https://www.pushplus.plus/send` with JSON fields `token`, `title`, `content`, `topic`, and `template`; this relay uses `template=html` for WeChat pushes. PushPlus also recommends image hosting instead of local/base64 images in messages, so the relay uploads WeChat images to PushPlus image storage when `PUSHPLUS_ACCESS_KEY` is configured, and otherwise falls back to the backend Airtap media store with 3-day signed URLs.

Upsert profile/avatar mappings:

```bash
curl -X POST https://imgclean-api.vercel.app/api/airtap/profiles/upsert \
  -H "content-type: application/json" \
  -H "x-airtap-secret: $AIRTAP_RELAY_SECRET" \
  -d '{
    "profiles": [
      {
        "display_name": "xiao mu",
        "handle": "xiaomustock",
        "avatar_url": "https://airtap.ai/content/live/android-files/avatar.png"
      }
    ]
  }'
```

Publish hourly posts. The backend renders channel content and pushes WeChat through PushPlus only when there are new posts:

```bash
curl -X POST https://imgclean-api.vercel.app/api/airtap/posts/publish \
  -H "content-type: application/json" \
  -H "x-airtap-secret: $AIRTAP_RELAY_SECRET" \
  -d '{
    "scope": "x-hourly-watch",
    "channels": ["wechat", "xiaohongshu"],
    "posts": [
      {
        "id": "tweet-id-or-url",
        "author_name": "xiao mu",
        "author_handle": "xiaomustock",
        "published_at": "4分钟前",
        "text": "raw post text",
        "url": "https://x.com/xiaomustock/status/...",
        "quote": {"author_name": "quoted author", "text": "quoted text"},
        "image_urls": ["https://airtap.ai/content/live/android-files/image.png"],
        "video_urls": ["https://airtap.ai/content/live/android-files/video.mp4"]
      }
    ]
  }'
```

The response contains `channels.wechat.title`, `channels.wechat.content`, `pushes.wechat`, and `channels.xiaohongshu.title/body/hashtags`. Re-posting the same `id` or `url` under the same `scope` returns `duplicate_count`, omits old content, and skips PushPlus with `reason=no_new_posts`.

`POST /api/airtap/posts/render` remains available for dry runs; it returns channel-ready content without pushing or marking posts as sent. Only `/api/airtap/posts/publish` records the dedupe state.

`GET /api/airtap/debug/summary` returns protected relay state counts, profile aliases, and recent dedupe records for production audits. It requires the same `x-airtap-secret` header and does not expose stored secret values.

Phone-side Airtap publishing contract:

1. Airtap scrapes X only for the latest hour of raw fields: author display name, handle, easily available avatar URL, published time, text, quote, original URL, image URLs, video URLs, and link cards. Quoted X posts should include quoted URL/id, author, handle, publish time, full visible text, media URLs, and link cards instead of only the collapsed quote stub.
2. Airtap calls `/api/airtap/profiles/upsert` for display-name/handle mappings. Avatar URLs are optional; do not ask Airtap to spend extra steps opening image tabs to obtain direct avatar URLs.
3. Airtap calls `/api/airtap/posts/publish` with `scope="x-hourly-wechat"` and `channels=["wechat"]`. WeChat delivery is handled by the backend through PushPlus; Airtap must not call PushPlus directly.
4. The backend stores the full hourly post payload after dedupe. Airtap must not scrape an 8-hour X history.
5. A cloud scheduler calls `/api/airtap/xhs/dispatch` every 8 hours. That endpoint reads the stored hourly posts, generates the Xiaohongshu note, and pushes a WeChat approval preview. Only after the user taps the signed confirmation link does the backend create the Airtap task to publish through the Xiaohongshu mobile app. Do not use the Xiaohongshu web publisher, browser, desktop uploader, or third-party web publishing tooling as a fallback.
6. Never put PushPlus tokens or OpenAI keys into Airtap prompts. The only Airtap-side secret should be `AIRTAP_RELAY_SECRET`, sent as the `x-airtap-secret` header or Bearer token when calling the backend.

Generate the exact Airtap prompt from the local machine:

```bash
scripts/airtap_x_hourly_prompt.py --dry-run
scripts/airtap_x_hourly_prompt.py --xhs-smoke
scripts/airtap_x_hourly_prompt.py
```

The script reads `AIRTAP_RELAY_SECRET` from the environment, or from `~/.codex/secrets/imgclean-airtap-relay-secret`. Use `--xhs-smoke` to verify the cloud phone can call `/api/airtap/posts/render` and fill a Xiaohongshu draft without sending WeChat or writing dedupe state. Use `--dry-run` to validate the hourly X ingestion with render-only backend calls. Omit `--dry-run` for the production Airtap routine. WeChat content is self-contained in the PushPlus HTML; do not ask Airtap to create extra WeChat links or call PushPlus.

For the 8-hour Xiaohongshu trigger, `.github/workflows/xhs-dispatch.yml` calls the backend at UTC 00/08/16. Configure the repository secret `X_AIRTAP_RELAY_SECRET` with the same value as `AIRTAP_RELAY_SECRET`. The backend also needs `AIRTAP_PERSONAL_ACCESS_TOKEN` so it can create the Airtap publish task without depending on the local computer.

## Watermark Removal

`POST /api/clean` supports `mode=watermark`. It auto-detects likely visible text watermarks and inpaints the mask with OpenCV when available. For difficult images, callers can still provide either `watermark_box=x,y,w,h` or a `watermark_mask` file where white pixels mark the watermark area.

## Frontend environment

Vercel frontend should use the variables listed in `deploy/vercel.env.example`.

The pricing page calls `POST /api/billing/checkout` on the backend. The backend creates a Stripe Checkout Session, records a pending order, and grants credits only after `checkout.session.completed` is received and verified at `/api/webhooks/stripe`.

Lemon Squeezy remains supported through `/api/webhooks/lemonsqueezy` for orders that pass `custom_data[user_id]` and `custom_data[credits]`.

## External services still required

- Google Cloud project with billing enabled for Cloud Run.
- Cloudflare R2 bucket and S3 API token.
- Stripe account in live mode, or Lemon Squeezy store and signed webhook, only if billing is enabled.

## Local development

Backend:

```bash
IMGCLEAN_AUTH_MODE=dev \
IMGCLEAN_LEDGER_BACKEND=local \
IMGCLEAN_STORAGE_BACKEND=local \
IMGCLEAN_INITIAL_CREDITS=5 \
IMGCLEAN_CORS_ORIGINS=http://127.0.0.1:3000,http://localhost:3000 \
.venv/bin/python -m imgclean_web
```

Frontend:

```bash
cd frontend
npm run dev
```

## Verification

```bash
.venv/bin/python -m pytest tests/ -q
cd frontend && npm run build
```
