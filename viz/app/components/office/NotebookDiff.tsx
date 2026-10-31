"use client";

import { NotebookPen } from "lucide-react";
import { useState } from "react";
import type { MemoryEntry } from "@/lib/types";
import Markdown from "../Markdown";

const PREVIEW_LINES = 12;

// What one notebook save changed, written for reading: new text rendered as markdown, removed text
// in plain type (no strike-through) so it stays legible.
export default function NotebookDiff({ entry, title = "Notebook updated" }: { entry: MemoryEntry; title?: string }) {
  const [open, setOpen] = useState(false);
  const { added, removed } = entry;
  const long = added.length > PREVIEW_LINES;
  if (!added.length && !removed.length) return null;
  return (
    <div className="rounded-2xl border border-line bg-panel shadow-card">
      <div className="flex items-center gap-2 px-4 pt-3 text-sm">
        <NotebookPen size={14} strokeWidth={1.75} className="text-learn" />
        <span className="font-medium">{title}</span>
        <span className="text-xs text-muted">
          {added.length} line{added.length === 1 ? "" : "s"} added{removed.length ? ` · ${removed.length} removed` : ""}
        </span>
      </div>
      {added.length > 0 && (
        <div className="mx-4 mt-2 rounded-xl bg-learn-soft/60 px-3.5 py-1.5">
          <Markdown text={(open || !long ? added : added.slice(0, PREVIEW_LINES)).join("\n")} />
        </div>
      )}
      {long && (
        <button onClick={() => setOpen(!open)} className="mx-4 mt-1.5 text-xs font-medium text-muted hover:text-ink">
          {open ? "Show less" : `Show all ${added.length} lines`}
        </button>
      )}
      {removed.length > 0 && (
        <div className="mx-4 mt-3">
          <div className="mb-1 text-xs text-muted">Removed</div>
          <div className="whitespace-pre-wrap rounded-xl bg-sunken px-3.5 py-2 text-xs leading-relaxed text-muted">
            {removed.slice(0, 10).join("\n")}
            {removed.length > 10 && `\n…and ${removed.length - 10} more lines`}
          </div>
        </div>
      )}
      <div className="h-3.5" />
    </div>
  );
}
