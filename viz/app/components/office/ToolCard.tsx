"use client";

import { Check, ChevronRight, Database, FileText, NotebookPen, Search, SquareTerminal, X, Zap, type LucideIcon } from "lucide-react";
import { useState } from "react";
import type { CodeBlock, ToolView } from "@/lib/toolcall";

export const TOOL_ICON: Record<ToolView["kind"], { icon: LucideIcon; bubble: string }> = {
  action: { icon: Zap, bubble: "bg-warn-soft text-warn" },
  lookup: { icon: Search, bubble: "bg-sunken text-muted" },
  notebook: { icon: NotebookPen, bubble: "bg-learn-soft text-learn" },
  file: { icon: FileText, bubble: "bg-sunken text-muted" },
  script: { icon: SquareTerminal, bubble: "bg-sunken text-muted" },
};

export function toolIcon(view: ToolView): { icon: LucideIcon; bubble: string } {
  if (view.kind === "lookup" && view.ops.every((op) => op === "query.sql")) return { icon: Database, bubble: TOOL_ICON.lookup.bubble };
  return TOOL_ICON[view.kind];
}

function Code({ block, maxLines = 16 }: { block: CodeBlock; maxLines?: number }) {
  const [open, setOpen] = useState(false);
  const lines = block.code.split("\n");
  const long = lines.length > maxLines;
  return (
    <div>
      {block.label && <div className="mb-1 text-xs text-muted">{block.label}</div>}
      <pre className="scroll-thin overflow-x-auto rounded-xl bg-sunken px-3.5 py-2.5 font-mono text-xs leading-relaxed text-ink">
        {open || !long ? block.code : lines.slice(0, maxLines).join("\n")}
      </pre>
      {long && (
        <button onClick={() => setOpen(!open)} className="mt-1 text-xs font-medium text-muted hover:text-ink">
          {open ? "Show less" : `Show all ${lines.length} lines`}
        </button>
      )}
    </div>
  );
}

function Status({ view }: { view: ToolView }) {
  if (view.status === "error")
    return (
      <span className="flex items-center gap-1 rounded-full bg-bad-soft px-2 py-0.5 font-mono text-xs text-bad">
        <X size={11} /> {view.errorCode ?? "error"}
      </span>
    );
  if (view.status === "ok") return <Check size={14} strokeWidth={2} className="text-good" />;
  return null;
}

// Mutations (the agent's decisions) render as an open card with the request it sent.
// Lookups render as a quiet one-line row that expands on click.
export default function ToolCard({ view }: { view: ToolView }) {
  const [open, setOpen] = useState(false);
  const isAction = view.kind === "action";
  const primary = isAction ? view.blocks.filter((b) => b.label !== "full command") : [];
  const details = isAction ? view.blocks.filter((b) => b.label === "full command") : view.blocks;

  const header = (
    <button onClick={() => setOpen(!open)} className="group flex w-full items-center gap-2.5 text-left">
      <span className={`text-sm ${isAction ? "font-medium text-ink" : "text-ink/85 group-hover:text-ink"}`}>{view.title}</span>
      <span className="flex min-w-0 flex-1 flex-wrap gap-1">
        {view.ops.slice(0, 3).map((op) => (
          <span key={op} className="rounded-md bg-sunken px-1.5 py-0.5 font-mono text-xs text-muted">
            {op}
          </span>
        ))}
      </span>
      <Status view={view} />
      <ChevronRight size={14} className={`text-faint transition-transform ${open ? "rotate-90" : ""}`} />
    </button>
  );

  const body = (
    <>
      {primary.length > 0 && (
        <div className="mt-2.5 space-y-2">
          {primary.map((b, i) => (
            <Code key={i} block={b} />
          ))}
        </div>
      )}
      {open && (
        <div className="mt-3 space-y-3">
          {details.map((b, i) => (
            <Code key={i} block={b} />
          ))}
          {view.output && <Code block={{ ...view.output, label: "Result" }} maxLines={18} />}
        </div>
      )}
    </>
  );

  if (isAction)
    return (
      <div className="rounded-2xl border border-line bg-panel px-4 py-3 shadow-card">
        {header}
        {body}
      </div>
    );
  return <div className="py-1">{header}{body}</div>;
}
