from __future__ import annotations

import base64
import hashlib
from html import escape

from fastapi.responses import HTMLResponse

_STYLES = """
:root {
  color-scheme: dark;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif;
  background: #07111f;
  color: #e8eef7;
}
* {
  box-sizing: border-box;
}
body {
  min-height: 100vh;
  margin: 0;
  background:
    radial-gradient(circle at 15% 10%, rgba(45, 212, 191, 0.14), transparent 32rem),
    radial-gradient(circle at 90% 80%, rgba(59, 130, 246, 0.13), transparent 30rem),
    #07111f;
}
main {
  width: min(1080px, calc(100% - 2rem));
  margin: 0 auto;
  padding: 4.5rem 0 3rem;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 5rem;
}
.brand {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  color: #f8fafc;
  font-size: 0.9rem;
  font-weight: 750;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.mark {
  display: grid;
  width: 2.2rem;
  height: 2.2rem;
  place-items: center;
  border: 1px solid rgba(94, 234, 212, 0.36);
  border-radius: 0.7rem;
  background: linear-gradient(145deg, rgba(45, 212, 191, 0.24), rgba(37, 99, 235, 0.18));
  color: #5eead4;
}
.status {
  display: inline-flex;
  align-items: center;
  gap: 0.55rem;
  padding: 0.55rem 0.8rem;
  border: 1px solid rgba(52, 211, 153, 0.22);
  border-radius: 999px;
  background: rgba(16, 185, 129, 0.08);
  color: #a7f3d0;
  font-size: 0.78rem;
  font-weight: 650;
}
.status::before {
  width: 0.48rem;
  height: 0.48rem;
  border-radius: 50%;
  background: #34d399;
  box-shadow: 0 0 0 0.25rem rgba(52, 211, 153, 0.12);
  content: "";
}
.eyebrow {
  margin: 0 0 1rem;
  color: #5eead4;
  font-size: 0.76rem;
  font-weight: 750;
  letter-spacing: 0.18em;
  text-transform: uppercase;
}
h1 {
  max-width: 780px;
  margin: 0;
  color: #f8fafc;
  font-size: clamp(2.6rem, 7vw, 5.25rem);
  font-weight: 760;
  letter-spacing: -0.055em;
  line-height: 0.98;
}
.lead {
  max-width: 650px;
  margin: 1.6rem 0 0;
  color: #9fb0c7;
  font-size: clamp(1rem, 2.5vw, 1.18rem);
  line-height: 1.7;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem 1.5rem;
  margin-top: 2rem;
  color: #71839c;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 0.78rem;
}
.grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1rem;
  margin-top: 4.5rem;
}
.card {
  min-height: 190px;
  padding: 1.4rem;
  border: 1px solid rgba(148, 163, 184, 0.14);
  border-radius: 1rem;
  background: linear-gradient(145deg, rgba(15, 30, 49, 0.92), rgba(10, 23, 39, 0.82));
  color: inherit;
  text-decoration: none;
  transition: border-color 160ms ease, transform 160ms ease;
}
.card:hover {
  border-color: rgba(94, 234, 212, 0.38);
  transform: translateY(-2px);
}
.card-number {
  color: #52657e;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 0.72rem;
}
.card h2 {
  margin: 2.6rem 0 0.65rem;
  color: #e8eef7;
  font-size: 1rem;
  font-weight: 700;
}
.card p {
  margin: 0;
  color: #8193aa;
  font-size: 0.88rem;
  line-height: 1.55;
}
.path {
  display: block;
  margin-top: 1rem;
  color: #5eead4;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 0.75rem;
}
footer {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  margin-top: 4rem;
  padding-top: 1.5rem;
  border-top: 1px solid rgba(148, 163, 184, 0.12);
  color: #60728a;
  font-size: 0.75rem;
}
@media (max-width: 760px) {
  main {
    padding-top: 1.5rem;
  }
  .topbar {
    margin-bottom: 3.5rem;
  }
  .grid {
    grid-template-columns: 1fr;
    margin-top: 3.5rem;
  }
  .card {
    min-height: 165px;
  }
  footer {
    flex-direction: column;
  }
}
""".strip()

_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLES.encode()).digest()).decode()


def build_landing_page(
    *,
    service_name: str,
    version: str,
    api_prefix: str,
    docs_enabled: bool,
) -> HTMLResponse:
    service = escape(service_name)
    safe_version = escape(version)
    prefix = escape(api_prefix.rstrip("/"))
    docs_card = ""
    if docs_enabled:
        docs_card = """
        <a class="card" href="/docs">
          <span class="card-number">04</span>
          <h2>API documentation</h2>
          <p>Explore schemas and endpoints in the interactive developer reference.</p>
          <span class="path">/docs</span>
        </a>
        """

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Secure API service for Ozo Donation.">
  <title>{service} · API</title>
  <style>{_STYLES}</style>
</head>
<body>
  <main>
    <header class="topbar">
      <div class="brand"><span class="mark">O</span> Ozo Platform</div>
      <span class="status">Operational</span>
    </header>

    <section aria-labelledby="page-title">
      <p class="eyebrow">Secure service infrastructure</p>
      <h1 id="page-title">Donation and audio, delivered reliably.</h1>
      <p class="lead">
        {service} provides authenticated administration, encrypted visit processing,
        supporter data, and resilient audio delivery through a focused API.
      </p>
      <div class="meta">
        <span>VERSION {safe_version}</span>
        <span>HTTPS ENFORCED</span>
        <span>REQUEST TRACING ACTIVE</span>
      </div>
    </section>

    <nav class="grid" aria-label="API resources">
      <a class="card" href="/health">
        <span class="card-number">01</span>
        <h2>Service health</h2>
        <p>Machine-readable availability and deployment status.</p>
        <span class="path">/health</span>
      </a>
      <a class="card" href="{prefix}/supporters">
        <span class="card-number">02</span>
        <h2>Supporters</h2>
        <p>Public supporter data backed by resilient caching.</p>
        <span class="path">{prefix}/supporters</span>
      </a>
      <a class="card" href="{prefix}/audio/metadata">
        <span class="card-number">03</span>
        <h2>Audio metadata</h2>
        <p>Current audio version, integrity details, and delivery metadata.</p>
        <span class="path">{prefix}/audio/metadata</span>
      </a>
      {docs_card}
    </nav>

    <footer>
      <span>© Ozo Platform · Backend services</span>
      <span>No credentials or infrastructure details are exposed here.</span>
    </footer>
  </main>
</body>
</html>"""

    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Content-Security-Policy": (
                "default-src 'none'; "
                f"style-src 'sha256-{_STYLE_HASH}'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "X-Robots-Tag": "noindex, nofollow",
        },
    )
