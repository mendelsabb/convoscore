import type { ReactNode } from "react";

export function Card({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <section className="card">
      {title ? <h2>{title}</h2> : null}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  tone?: "ok" | "warn" | "danger" | "info";
}) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className={`value${tone ? ` ${tone}` : ""}`}>{value}</div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  );
}

export function ErrorNotice({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  return <div className="notice notice-error">{message}</div>;
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return (
    <div className="empty">
      <span className="spin" /> <span style={{ marginLeft: 8 }}>{what}…</span>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
