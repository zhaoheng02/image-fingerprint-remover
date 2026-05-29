"use client";

import { useEffect, useState } from "react";
import { Chrome, LogIn, Zap } from "lucide-react";
import Link from "next/link";
import { browserSupabase } from "../../lib/supabase";
import { config } from "../../lib/config";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [wechatReady, setWechatReady] = useState<boolean | null>(null);

  useEffect(() => {
    if (config.authProvider !== "wechat") return;
    fetch(`${config.apiBaseUrl}/api/auth/wechat/status`)
      .then((response) => response.json())
      .then((payload) => {
        setWechatReady(Boolean(payload.configured));
        if (!payload.configured) {
          setMessage(`微信 OAuth 未配置：缺少 ${payload.missing.join(", ")}`);
        }
      })
      .catch(() => {
        setWechatReady(false);
        setMessage("无法检查微信 OAuth 配置。");
      });
  }, []);

  async function signInWithPassword() {
    setMessage("Signing in...");
    const supabase = browserSupabase();
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) {
      setMessage(error.message);
      return;
    }
    window.location.href = "/dashboard";
  }

  async function signInWithGoogle() {
    setMessage("Redirecting to Google...");
    const supabase = browserSupabase();
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: `${window.location.origin}/dashboard` }
    });
    if (error) setMessage(error.message);
  }

  async function signInWithWechat() {
    setMessage("检查微信 OAuth 配置...");
    const statusResponse = await fetch(`${config.apiBaseUrl}/api/auth/wechat/status`);
    const status = await statusResponse.json();
    if (!status.configured) {
      setWechatReady(false);
      setMessage(`微信 OAuth 未配置：缺少 ${status.missing.join(", ")}`);
      return;
    }
    setWechatReady(true);
    setMessage("跳转到微信扫码登录...");
    const returnTo = `${window.location.origin}/dashboard`;
    window.location.href = `${config.apiBaseUrl}/api/auth/wechat/login?return_to=${encodeURIComponent(returnTo)}`;
  }

  return (
    <main className="shell">
      <header className="wrap nav">
        <Link className="brand" href="/">
          <span className="brand-mark"><Zap size={18} /></span>
          ImgClean
        </Link>
      </header>
      <section className="wrap auth-surface">
        <div className="form">
          <h1>Login</h1>
          <p className="muted">{config.authProvider === "wechat" ? "使用微信扫码登录。" : "Use Supabase Auth. Google OAuth works after it is enabled in the Supabase dashboard."}</p>
          {config.authProvider === "wechat" ? (
            <button type="button" onClick={signInWithWechat} disabled={wechatReady === false}>
              <LogIn size={17} /> 微信登录
            </button>
          ) : (
            <>
              <button type="button" onClick={signInWithGoogle}>
                <Chrome size={17} /> Continue with Google
              </button>
              <div className="field">
                <label htmlFor="email">Email</label>
                <input id="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" />
              </div>
              <div className="field">
                <label htmlFor="password">Password</label>
                <input id="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" />
              </div>
              <button type="button" onClick={signInWithPassword}>
                <LogIn size={17} /> Sign in
              </button>
            </>
          )}
          <p className="message">{message}</p>
        </div>
      </section>
    </main>
  );
}
