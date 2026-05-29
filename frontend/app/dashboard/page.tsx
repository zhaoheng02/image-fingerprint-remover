"use client";

import { useEffect, useState } from "react";
import { Download, FileUp, LogOut, RefreshCw, Zap } from "lucide-react";
import Link from "next/link";
import { browserSupabase } from "../../lib/supabase";
import { config } from "../../lib/config";

type Me = {
  authenticated: boolean;
  user_id?: string;
  email?: string;
  credits: number | null;
};

type CleanResult = {
  ok: boolean;
  filename: string;
  error?: string;
  download_url?: string;
  credits_remaining?: number;
  input?: { finding_count: number };
  output?: { is_clean: boolean; finding_count: number };
};

export default function DashboardPage() {
  const [sessionToken, setSessionToken] = useState("");
  const [me, setMe] = useState<Me | null>(null);
  const [files, setFiles] = useState<FileList | null>(null);
  const [results, setResults] = useState<CleanResult[]>([]);
  const [message, setMessage] = useState("Loading account...");

  useEffect(() => {
    if (!config.requireAuth) {
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

  async function loadMe(token = sessionToken) {
    if (config.requireAuth && !token) return;
    const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
    const response = await fetch(`${config.apiBaseUrl}/api/me`, {
      headers
    });
    if (!response.ok) {
      setMessage(await response.text());
      return;
    }
    setMe(await response.json());
    setMessage("");
  }

  async function cleanImages() {
    if (!files?.length) return;
    if (config.requireAuth && !sessionToken) return;
    setMessage("Cleaning...");
    const body = new FormData();
    body.set("mode", "safe");
    [...files].forEach((file) => body.append("files", file));
    const headers = sessionToken ? { Authorization: `Bearer ${sessionToken}` } : undefined;
    const response = await fetch(`${config.apiBaseUrl}/api/clean`, {
      method: "POST",
      headers,
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
    const supabase = browserSupabase();
    await supabase.auth.signOut();
    window.location.href = "/login";
  }

  function downloadHref(url: string) {
    return url.startsWith("http") ? url : `${config.apiBaseUrl}${url}`;
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
            <p className="muted">{me?.email || me?.user_id || "Not signed in"}</p>
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
            <label className="upload-zone">
              <span>
                <FileUp size={28} />
                <strong> Choose PNG/JPEG files</strong>
                <p className="muted">{files?.length ? `${files.length} file(s) selected` : "Safe mode removes embedded metadata while preserving pixels."}</p>
              </span>
              <input hidden type="file" multiple accept="image/png,image/jpeg" onChange={(event) => setFiles(event.target.files)} />
            </label>
            <button className="upload-button" type="button" onClick={cleanImages}>Run cleaner</button>
            <p className="message">{message}</p>
            <div className="result-list">
              {results.map((result) => (
                <div className="file-row" key={result.filename}>
                  <span>{result.filename}</span>
                  {result.ok && result.download_url ? (
                    <a className="button secondary" href={downloadHref(result.download_url)}><Download size={16} /> Download</a>
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
