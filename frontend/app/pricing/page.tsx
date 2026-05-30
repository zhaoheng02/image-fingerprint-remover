import { FileImage, ShieldCheck, Zap } from "lucide-react";
import Link from "next/link";
import { config } from "../../lib/config";

export default function PricingPage() {
  return (
    <main className="shell">
      <header className="wrap nav">
        <Link className="brand" href="/">
          <span className="brand-mark"><Zap size={18} /></span>
          ImgClean
        </Link>
        <nav className="nav-links">
          <Link className="nav-link" href="/dashboard">Dashboard</Link>
          {config.requireAuth ? <Link className="nav-link" href="/login">Login</Link> : null}
        </nav>
      </header>
      <section className="wrap section">
        <h2>{config.billingEnabled ? "Pricing" : "Mini Program MVP"}</h2>
        {!config.billingEnabled ? (
          <p className="muted">
            Billing is disabled. The current build is focused on free image cleanup through the web dashboard and WeChat Mini Program.
          </p>
        ) : null}
        <div className="grid">
          <div className="tile">
            <FileImage size={24} />
            <h3>Web dashboard</h3>
            <p className="muted">Upload PNG/JPEG files and download cleaned images without a paid checkout.</p>
            <Link className="button primary" href="/dashboard">Open dashboard</Link>
          </div>
          <div className="tile">
            <Zap size={24} />
            <h3>Mini Program</h3>
            <p className="muted">The miniapp client is ready for AppID/AppSecret and legal domain binding.</p>
            <Link className="button secondary" href="/">Back home</Link>
          </div>
          <div className="tile">
            <ShieldCheck size={24} />
            <h3>API backend</h3>
            <p className="muted">The deployed API supports anonymous mode now and WeChat Mini Program login once credentials are added.</p>
            <Link className="button secondary" href="/dashboard">Try cleaner</Link>
          </div>
        </div>
      </section>
    </main>
  );
}
