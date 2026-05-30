import { ArrowRight, Database, FileImage, ShieldCheck, Zap } from "lucide-react";
import Link from "next/link";
import { config } from "../lib/config";

export default function HomePage() {
  return (
    <main className="shell">
      <Header />
      <section className="wrap hero">
        <div>
          <h1>Image privacy cleanup for creator workflows</h1>
          <p>
            Upload PNG and JPEG files, remove embedded identifiers, and deliver cleaned
            files through a hosted API or WeChat Mini Program.
          </p>
          <div className="hero-actions">
            <Link className="button primary" href="/dashboard">
              Open app <ArrowRight size={17} />
            </Link>
            {config.billingEnabled ? (
              <Link className="button secondary" href="/pricing">
                View pricing
              </Link>
            ) : null}
          </div>
        </div>
        <div className="product-panel" aria-label="ImgClean product preview">
          <div className="panel-head">
            <span className="status-dot" />
            Cloud Run backend connected
          </div>
          <div className="panel-body">
            <div className="file-row">
              <span>campaign-image.png</span>
              <span className="chip">4 findings</span>
            </div>
            <div className="file-row">
              <span>campaign-image.cleaned.png</span>
              <span className="chip">clean</span>
            </div>
            <div className="metric-row">
              <span>Mini Program mode</span>
              <strong>free</strong>
            </div>
            <div className="metric-row">
              <span>R2 signed download</span>
              <strong>6h</strong>
            </div>
          </div>
        </div>
      </section>
      <section className="wrap section">
        <h2>Production pieces</h2>
        <div className="grid">
          <Feature icon={<ShieldCheck size={24} />} title="Privacy engine" text="The existing offline cleaner runs as a Cloud Run container." />
          <Feature icon={<Database size={24} />} title="Usage ledger" text="Supabase can store users and usage events when authentication is enabled." />
          <Feature icon={<FileImage size={24} />} title="R2 storage" text="Original and cleaned files can be stored with short-lived download URLs." />
        </div>
      </section>
    </main>
  );
}

function Header() {
  return (
    <header className="wrap nav">
      <Link className="brand" href="/">
        <span className="brand-mark"><Zap size={18} /></span>
        ImgClean
      </Link>
      <nav className="nav-links">
        {config.billingEnabled ? <Link className="nav-link" href="/pricing">Pricing</Link> : null}
        {config.requireAuth ? <Link className="nav-link" href="/login">Login</Link> : null}
        <Link className="button secondary" href="/dashboard">Dashboard</Link>
      </nav>
    </header>
  );
}

function Feature({ icon, title, text }: { icon: React.ReactNode; title: string; text: string }) {
  return (
    <div className="tile">
      {icon}
      <h3>{title}</h3>
      <p>{text}</p>
    </div>
  );
}
