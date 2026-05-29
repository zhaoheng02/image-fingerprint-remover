"use client";

import { useEffect, useState } from "react";
import type { DragEvent } from "react";
import { Download, FileUp, LogOut, RefreshCw, Zap } from "lucide-react";
import Link from "next/link";
import { browserSupabase } from "../../lib/supabase";
import { config } from "../../lib/config";

type Me = {
  authenticated: boolean;
  user_id?: string;
  email?: string;
  name?: string;
  credits: number | null;
};

type CleanResult = {
  ok: boolean;
  filename: string;
  error?: string;
  download_url?: string;
  download_filename?: string;
  credits_remaining?: number;
  input?: { finding_count: number };
  output?: { is_clean: boolean; finding_count: number };
};

type Preview = {
  name: string;
  size: number;
  url: string;
};

export default function DashboardPage() {
  const [sessionToken, setSessionToken] = useState("");
  const [me, setMe] = useState<Me | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [previews, setPreviews] = useState<Preview[]>([]);
  const [results, setResults] = useState<CleanResult[]>([]);
  const [message, setMessage] = useState("Loading account...");
  const [mode, setMode] = useState("safe");

  useEffect(() => {
    if (!config.requireAuth) {
      loadMe("");
      return;
    }
    if (config.authProvider === "wechat") {
      loadMe("");
      return;
    }
    const supabase = browserSupabase();
    supabase.auth.getSession().then(({ data }) => {
      const token = data.session?.access_token ?? "";
      if (!token) {
        setMessage("Please login first.");
        return;
      }
      setSessionToken(token);
      loadMe(token);
    });
  }, []);

  useEffect(() => {
    const nextPreviews = files.map((file) => ({
      name: file.name,
      size: file.size,
      url: URL.createObjectURL(file)
    }));
    setPreviews(nextPreviews);
    return () => {
      nextPreviews.forEach((preview) => URL.revokeObjectURL(preview.url));
    };
  }, [files]);

  async function loadMe(token = sessionToken) {
    if (config.requireAuth && config.authProvider !== "wechat" && !token) return;
    const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
    const credentials: RequestCredentials = config.authProvider === "wechat" ? "include" : "same-origin";
    const response = await fetch(`${config.apiBaseUrl}/api/me`, {
      headers,
      credentials
    });
    if (!response.ok) {
      setMessage(await response.text());
      return;
    }
    setMe(await response.json());
    setMessage("");
  }

  async function cleanImages() {
    if (!files.length) return;
    if (config.requireAuth && config.authProvider !== "wechat" && !sessionToken) return;
    setMessage("Cleaning...");
    const body = new FormData();
    body.set("mode", mode);
    files.forEach((file) => body.append("files", file));
    const headers = sessionToken ? { Authorization: `Bearer ${sessionToken}` } : undefined;
    const credentials: RequestCredentials = config.authProvider === "wechat" ? "include" : "same-origin";
    const response = await fetch(`${config.apiBaseUrl}/api/clean`, {
      method: "POST",
      headers,
      credentials,
      body
    });
    if (!response.ok) {
      setMessage(response.status === 402 ? "No credits left. Buy a pack to continue." : await response.text());
      return;
    }
    const payload = await response.json();
    setResults(payload.results);
    await loadMe(sessionToken);
    setMessage("Done.");
  }

  async function signOut() {
    if (config.authProvider === "wechat") {
      await fetch(`${config.apiBaseUrl}/api/auth/logout`, { method: "POST", credentials: "include" });
      window.location.href = "/login";
      return;
    }
    const supabase = browserSupabase();
    await supabase.auth.signOut();
    window.location.href = "/login";
  }

  function downloadHref(result: CleanResult) {
    const url = result.download_url ?? "";
    const filename = result.download_filename ?? "cleaned-image";
    if (url.startsWith("http")) {
      return `${config.apiBaseUrl}/api/download?url=${encodeURIComponent(url)}&filename=${encodeURIComponent(filename)}`;
    }
    return `${config.apiBaseUrl}${url}`;
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    const dropped = [...event.dataTransfer.files].filter((file) => ["image/png", "image/jpeg"].includes(file.type));
    setFiles(dropped);
    setResults([]);
    setMessage(dropped.length ? `${dropped.length} file(s) selected. Ready to clean.` : "Choose PNG/JPEG files.");
  }

  function formatSize(value: number) {
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }

  return (
    <main className="shell">
      <header className="wrap nav">
        <Link className="brand" href="/">
          <span className="brand-mark"><Zap size={18} /></span>
          ImgClean
        </Link>
        <nav className="nav-links">
          <Link className="nav-link" href="/pricing">Pricing</Link>
          {config.requireAuth ? (
            <button className="button secondary" type="button" onClick={signOut}><LogOut size={16} /> Sign out</button>
          ) : null}
        </nav>
      </header>
      <section className="wrap dashboard">
        <h1>Dashboard</h1>
        <p className="muted">Clean PNG/JPEG files through the hosted API.</p>
        <div className="dashboard-grid">
          <aside className="tile">
            <h3>Account</h3>
            <p className="muted">{me?.name || me?.email || me?.user_id || "Not signed in"}</p>
            <div className="metric-row">
              <span>Credits</span>
              <strong>{me?.credits ?? "Free"}</strong>
            </div>
            <button className="button secondary" type="button" onClick={() => loadMe()}>
              <RefreshCw size={16} /> Refresh
            </button>
          </aside>
          <section className="tile">
            <h3>Clean images</h3>
            <div className="mode-tabs" role="group" aria-label="Cleaning mode">
              {[
                ["safe", "Safe"],
                ["paranoid", "Deep"],
                ["nuclear", "Nuclear"],
                ["watermark", "Watermark"]
              ].map(([value, label]) => (
                <button
                  className={mode === value ? "active" : ""}
                  key={value}
                  type="button"
                  onClick={() => setMode(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            <label
              className="upload-zone"
              onDragOver={(event) => event.preventDefault()}
              onDrop={onDrop}
            >
              <span>
                <FileUp size={28} />
                <strong> Choose PNG/JPEG files</strong>
                <p className="muted">{files.length ? `${files.length} file(s) selected` : "Safe mode removes embedded metadata while preserving pixels."}</p>
              </span>
              <input hidden type="file" multiple accept="image/png,image/jpeg" onChange={(event) => {
                setFiles([...(event.target.files ?? [])]);
                setResults([]);
                setMessage((event.target.files?.length ?? 0) ? "Ready to clean." : "Choose PNG/JPEG files.");
              }} />
            </label>
            {previews.length ? (
              <div className="preview-strip">
                {previews.map((preview) => (
                  <article className="preview-thumb" key={`${preview.name}-${preview.url}`}>
                    <img src={preview.url} alt={`${preview.name} preview`} />
                    <div>
                      <strong>{preview.name}</strong>
                      <span>{formatSize(preview.size)} · Ready</span>
                    </div>
                  </article>
                ))}
              </div>
            ) : null}
            <button className="upload-button" type="button" onClick={cleanImages}>Run cleaner</button>
            <p className="message">{message}</p>
            <div className="result-list">
              {results.map((result) => (
                <div className="file-row" key={result.filename}>
                  <span>{result.filename}</span>
                  {result.ok && result.download_url ? (
                    <a className="button secondary" href={downloadHref(result)} download={result.download_filename ?? "cleaned-image"}><Download size={16} /> Download</a>
                  ) : (
                    <span className="chip">{result.error}</span>
                  )}
                </div>
              ))}
            </div>
          </section>
        </div>
      </section>
    </main>
  );
}
