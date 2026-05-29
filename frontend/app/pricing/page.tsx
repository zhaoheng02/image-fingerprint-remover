"use client";

import { useState } from "react";
import { ArrowRight, CreditCard, Zap } from "lucide-react";
import Link from "next/link";
import { config } from "../../lib/config";
import { browserSupabase } from "../../lib/supabase";

export default function PricingPage() {
  const [busyPack, setBusyPack] = useState("");
  const [message, setMessage] = useState("");

  async function startCheckout(pack: "starter" | "growth") {
    if (!config.billingEnabled) {
      window.location.href = "/dashboard";
      return;
    }
    setBusyPack(pack);
    setMessage("");
    const supabase = browserSupabase();
    const { data } = await supabase.auth.getSession();
    const token = data.session?.access_token;
    if (!token) {
      window.location.href = "/login";
      return;
    }
    const response = await fetch(`${config.apiBaseUrl}/api/billing/checkout`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        Authorization: `Bearer ${token}`
      },
      body: JSON.stringify({ pack })
    });
    if (!response.ok) {
      setBusyPack("");
      setMessage(response.status === 503 ? "Checkout is not configured yet." : await response.text());
      return;
    }
    const payload = await response.json();
    window.location.href = payload.checkout_url;
  }

  return (
    <main className="shell">
      <header className="wrap nav">
        <Link className="brand" href="/">
          <span className="brand-mark"><Zap size={18} /></span>
          ImgClean
        </Link>
        <nav className="nav-links">
          <Link className="nav-link" href="/dashboard">Dashboard</Link>
          <Link className="nav-link" href="/login">Login</Link>
        </nav>
      </header>
      <section className="wrap section">
        <h2>Pricing</h2>
        <div className="grid">
          <div className="tile">
            <CreditCard size={24} />
            <h3>Starter pack</h3>
            <div className="price">$9</div>
            <p className="muted">25 image cleanups for early users.</p>
            <button className="button primary" type="button" disabled={busyPack === "starter"} onClick={() => startCheckout("starter")}>
              {busyPack === "starter" ? "Opening checkout" : config.billingEnabled ? "Buy credits" : "Try cleaner"} <ArrowRight size={16} />
            </button>
          </div>
          <div className="tile">
            <CreditCard size={24} />
            <h3>Growth pack</h3>
            <div className="price">$29</div>
            <p className="muted">100 image cleanups for frequent publishing workflows.</p>
            <button className="button secondary" type="button" disabled={busyPack === "growth"} onClick={() => startCheckout("growth")}>
              {busyPack === "growth" ? "Opening checkout" : config.billingEnabled ? "Buy growth pack" : "Open dashboard"}
            </button>
          </div>
          <div className="tile">
            <CreditCard size={24} />
            <h3>Team</h3>
            <div className="price">Custom</div>
            <p className="muted">Add invoicing, higher limits, and private deployment controls.</p>
            <Link className="button secondary" href="/dashboard">Open dashboard</Link>
          </div>
        </div>
        <p className="message">{message}</p>
      </section>
    </main>
  );
}
