# ImgClean Mini Program

This directory is a WeChat Mini Program client for the hosted ImgClean API.

## Setup

1. Register a WeChat Mini Program and copy its AppID/AppSecret.
2. Replace `appid` in `project.config.json`.
3. Keep `config.js` pointed at the API:

   ```js
   module.exports = {
     apiBaseUrl: "https://imgclean-api.vercel.app"
   };
   ```

4. In the Mini Program admin console, add `https://imgclean-api.vercel.app` to the request, uploadFile, and downloadFile legal domains.
5. Configure the API deployment:

   ```bash
   IMGCLEAN_AUTH_MODE=wechat_miniprogram
   IMGCLEAN_CREDITS_ENABLED=false
   WECHAT_MINIPROGRAM_APP_ID=<your mini program appid>
   WECHAT_MINIPROGRAM_APP_SECRET=<your mini program appsecret>
   IMGCLEAN_SESSION_SECRET=<random secret>
   ```

For local smoke testing without Mini Program login, keep the backend in `IMGCLEAN_AUTH_MODE=none`; upload and cleaning still work anonymously.
