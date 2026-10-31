// Shared UI kit, following video-arena's: rounded-lg controls, rounded-2xl cards, text-xs smallest, one accent.
import type { ReactNode } from "react";

export const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

type Variant = "primary" | "secondary" | "quiet";
const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent text-accent-ink shadow-card hover:brightness-110 active:brightness-95",
  secondary: "border border-line bg-panel text-ink shadow-card hover:border-accent/50 hover:text-accent",
  quiet: "text-muted hover:text-ink hover:bg-sunken",
};

export function Button({ variant = "secondary", className = "", ...p }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      {...p}
      className={cx("inline-flex h-9 items-center justify-center gap-2 rounded-lg px-3 text-sm font-medium whitespace-nowrap transition select-none", VARIANTS[variant], className)}
    />
  );
}

export function Segmented<T extends string>({ value, options, onPick }: { value: T; options: { v: T; l: ReactNode }[]; onPick: (v: T) => void }) {
  return (
    <div role="radiogroup" className="inline-flex rounded-lg bg-sunken p-0.5">
      {options.map((o) => (
        <button
          key={o.v}
          role="radio"
          aria-checked={value === o.v}
          onClick={() => onPick(o.v)}
          className={cx("flex items-center gap-1.5 rounded-md px-3 py-1 text-sm transition", value === o.v ? "bg-panel font-medium text-ink shadow-card" : "text-muted hover:text-ink")}
        >
          {o.l}
        </button>
      ))}
    </div>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={cx("rounded-2xl border border-line bg-panel shadow-card", className)}>{children}</div>;
}

export function Badge({ tone = "neutral", children, className = "" }: { tone?: "neutral" | "good" | "warn" | "bad" | "accent" | "learn"; children: ReactNode; className?: string }) {
  const tones = {
    neutral: "bg-sunken text-muted",
    good: "bg-good-soft text-good",
    warn: "bg-warn-soft text-warn",
    bad: "bg-bad-soft text-bad",
    accent: "bg-accent-soft text-accent",
    learn: "bg-learn-soft text-learn",
  };
  return <span className={cx("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium", tones[tone], className)}>{children}</span>;
}

export function Spinner({ className = "" }: { className?: string }) {
  return <span aria-hidden className={cx("inline-block size-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-r-transparent", className)} />;
}
