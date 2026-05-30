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
- `imgclean-wechat-app-id` -> `WECHAT_APP_ID`
- `imgclean-wechat-app-secret` -> `WECHAT_APP_SECRET`

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

## Watermark Removal

`POST /api/clean` supports `mode=watermark`. It auto-detects likely visible text watermarks and inpaints the mask with OpenCV when available. For difficult images, callers can still provide either `watermark_box=x,y,w,h` or a `watermark_mask` file where white pixels mark the watermark area.

## Frontend environment

Vercel frontend should use the variables listed in `deploy/vercel.env.example`.

The pricing page calls `POST /api/billing/checkout` on the backend. The backend creates a Stripe Checkout Session, records a pending order, and grants credits only after `checkout.session.completed` is received and verified at `/api/webhooks/stripe`.

Lemon Squeezy remains supported through `/api/webhooks/lemonsqueezy` for orders that pass `custom_data[user_id]` and `custom_data[credits]`.

## External services still required

- Google Cloud project with billing enabled for Cloud Run.
- Cloudflare R2 bucket and S3 API token.
- Stripe account in live mode, or Lemon Squeezy store and signed webhook.

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
