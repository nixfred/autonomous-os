import { C } from "@/components/setup/shared";

// Small presentational pieces shared by the Connectors pages that collect more
// than one pasted value (the Facebook page keeps its own copies).

export function InfoCard({
  tone,
  title,
  icon,
  children,
}: {
  tone: "blue" | "warning" | "plain";
  title: string | null;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  const bg = tone === "warning" ? "var(--lm-amber-dim)" : tone === "blue" ? "var(--lm-info-bg, rgba(30,64,175,0.10))" : C.bg;
  const border = tone === "warning" ? "var(--lm-amber-glow)" : tone === "blue" ? "var(--lm-info-border, rgba(59,130,246,0.35))" : C.border;
  const titleColor = tone === "plain" ? C.textDim : C.amber;
  return (
    <div style={{
      background: bg, border: `1px solid ${border}`, borderRadius: 10,
      padding: "12px 14px", marginBottom: 14, fontSize: 12.5, color: C.textDim, lineHeight: 1.6,
    }}>
      {(title || icon) && (
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: title ? 8 : 0 }}>
          {icon}
          {title && <div style={{ fontSize: 12, fontWeight: 700, letterSpacing: "0.05em", color: titleColor }}>{title}</div>}
        </div>
      )}
      <div>{children}</div>
    </div>
  );
}

export function Step({ n, last, children }: { n: number; last?: boolean; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 10, marginBottom: last ? 0 : 8 }}>
      <div style={{
        flexShrink: 0,
        width: 18, height: 18, borderRadius: 9,
        background: C.amberDim, color: C.amber,
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: 12, fontWeight: 700,
      }}>{n}</div>
      <div style={{ fontSize: 12.5, color: C.textDim, lineHeight: 1.6, minWidth: 0 }}>{children}</div>
    </div>
  );
}

export function FieldLabel({ htmlFor, children }: { htmlFor: string; children: React.ReactNode }) {
  return (
    <label htmlFor={htmlFor} style={{ display: "block", fontSize: 12.5, fontWeight: 600, color: C.text, marginBottom: 5 }}>
      {children}
    </label>
  );
}
