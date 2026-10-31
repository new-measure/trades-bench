"use client";

import { useEffect, useMemo, useRef } from "react";
import type { Model } from "@/lib/model";
import Markdown from "../Markdown";
import { turnTitle, turnWhen } from "./labels";
import NotebookDiff from "./NotebookDiff";
import type { NotebookState } from "./OfficeTab";

interface Section {
  heading: string;
  body: string;
  changed: boolean;
}

function sections(content: string, added: Set<string>): Section[] {
  const out: Section[] = [];
  let current: Section = { heading: "", body: "", changed: false };
  for (const line of content.split("\n")) {
    if (/^#{1,3}\s/.test(line)) {
      if (current.heading || current.body.trim()) out.push(current);
      current = { heading: line.replace(/^#+\s*/, ""), body: "", changed: false };
      continue;
    }
    current.body += line + "\n";
    if (line.trim() && added.has(line)) current.changed = true;
  }
  if (current.heading || current.body.trim()) out.push(current);
  return out;
}

export default function Notebook({ model, state }: { model: Model; state: NotebookState }) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const entry = state.entry;
  const parts = useMemo(() => (entry ? sections(entry.content, new Set(entry.added)) : []), [entry]);

  useEffect(() => {
    const el = scrollRef.current?.querySelector<HTMLElement>("[data-changed=true]");
    if (el && scrollRef.current) scrollRef.current.scrollTo({ top: el.offsetTop - 16, behavior: "smooth" });
  }, [entry]);

  if (!entry) {
    return (
      <div className="p-8 text-muted">
        <p className="text-base text-ink">The notebook is empty.</p>
        <p className="mt-1 text-sm">The agent starts each day with no memory except what it writes to MEMORY.md.</p>
      </div>
    );
  }
  const writtenIn = model.turnsById[entry.turn];

  return (
    <div ref={scrollRef} className="scroll-thin h-full space-y-3 overflow-y-auto bg-bg px-6 py-5">
      <div className="text-sm text-muted">
        Last written during <span className="text-ink">{writtenIn ? turnTitle(writtenIn) : "?"}</span>
        {writtenIn ? ` · ${turnWhen(writtenIn)}` : ""}
        {!model.replay.sources.memory_exact && <span className="ml-2 rounded-full bg-warn-soft px-2 py-0.5 text-xs text-warn">approximate</span>}
      </div>
      <NotebookDiff entry={entry} title={state.fresh ? "Changed during this call" : "Most recent change"} />
      {parts.map((s, i) => (
        <section
          key={i}
          data-changed={s.changed}
          className={`rounded-2xl border bg-panel px-5 py-4 shadow-card ${s.changed ? "border-learn/40 ring-2 ring-learn-soft" : "border-line"}`}
        >
          {s.heading && (
            <h3 className="mb-1 flex items-center gap-2 text-base font-medium tracking-tight">
              {s.heading.replace(/\*\*/g, "")}
              {s.changed && <span className="rounded-full bg-learn-soft px-2 py-0.5 text-xs font-medium text-learn">Updated</span>}
            </h3>
          )}
          {s.body.trim() && <Markdown text={s.body} />}
        </section>
      ))}
    </div>
  );
}
