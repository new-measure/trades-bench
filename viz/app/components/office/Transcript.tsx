"use client";

import { ArrowDown, Brain, PhoneIncoming, Sparkles, type LucideIcon } from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import type { Model } from "@/lib/model";
import { describeTool } from "@/lib/toolcall";
import type { Step, Turn } from "@/lib/types";
import Markdown from "../Markdown";
import NotebookDiff from "./NotebookDiff";
import ToolCard, { TOOL_ICON, toolIcon } from "./ToolCard";
import { Spinner } from "../ui";

interface Props {
  model: Model;
  turn: Turn | null;
  visibleSteps: number;
  live: boolean;
}

// One row of the activity feed: an icon on a vertical rail, content to the right.
function Item({ icon: Icon, bubble, last, children }: { icon: LucideIcon; bubble: string; last?: boolean; children: React.ReactNode }) {
  return (
    <li className="animate-enter relative flex gap-3.5">
      <div className="relative flex w-8 shrink-0 justify-center">
        {!last && <div className="absolute bottom-0 top-8 w-px bg-line" />}
        <div className={`relative z-10 grid size-8 place-items-center rounded-full ring-4 ring-bg ${bubble}`}>
          <Icon size={15} strokeWidth={1.75} />
        </div>
      </div>
      <div className="min-w-0 flex-1 pb-5 pt-1">{children}</div>
    </li>
  );
}

function Thought({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > 220;
  return (
    <div>
      <p className={`whitespace-pre-wrap text-sm leading-relaxed text-muted ${open || !long ? "" : "line-clamp-2"}`}>{text}</p>
      {long && (
        <button onClick={() => setOpen(!open)} className="mt-0.5 text-xs font-medium text-muted hover:text-ink">
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

// Calls repeat what the header already shows, so they get one line with the raw prompt a click away.
function Prompt({ turn }: { turn: Turn }) {
  const [open, setOpen] = useState(false);
  const text = turn.prompt.replace(/^\[[^\]]*\]\s*/, "").trim();
  const isCall = turn.kind === "call" || turn.kind === "reminder";
  if (!isCall) return <p className="text-sm leading-relaxed">{text}</p>;
  const f = turn.fields;
  const summary = turn.kind === "reminder" ? "Reminder: the caller is still waiting" : /cancel/i.test(text) ? `${f["caller"] ?? "A customer"} calls to cancel an appointment` : `${f["caller"] ?? "A caller"} calls about ${(f["requested_service"] ?? "a job").toLowerCase()}`;
  return (
    <div>
      <p className="text-sm">
        {summary}
        <button onClick={() => setOpen(!open)} className="ml-2 text-xs font-medium text-muted hover:text-ink">
          {open ? "Hide prompt" : "Show prompt"}
        </button>
      </p>
      {open && <pre className="mt-2 whitespace-pre-wrap rounded-xl bg-sunken px-3.5 py-2.5 font-mono text-xs leading-relaxed">{text}</pre>}
    </div>
  );
}

const StepItem = memo(function StepItem({ step, model, last }: { step: Step; model: Model; last: boolean }) {
  const view = useMemo(() => (step.type === "tool" ? describeTool(step) : null), [step]);
  if (step.type === "think")
    return (
      <Item icon={Brain} bubble="bg-sunken text-faint" last={last}>
        <Thought text={step.text ?? ""} />
      </Item>
    );
  if (step.type === "say")
    return (
      <Item icon={Sparkles} bubble="bg-accent-soft text-accent" last={last}>
        <div className="rounded-2xl rounded-tl-sm border border-line bg-panel px-4 py-3 shadow-card">
          <Markdown text={step.text ?? ""} />
        </div>
      </Item>
    );
  const entry = step.memory_entry != null ? model.replay.memory[step.memory_entry] : null;
  const { icon, bubble } = entry ? TOOL_ICON.notebook : toolIcon(view!);
  return (
    <Item icon={icon} bubble={bubble} last={last}>
      {entry && view!.kind === "notebook" ? <NotebookDiff entry={entry} /> : <ToolCard view={view!} />}
      {entry && view!.kind !== "notebook" && (
        <div className="mt-2">
          <NotebookDiff entry={entry} />
        </div>
      )}
    </Item>
  );
});

export default function Transcript({ model, turn, visibleSteps, live }: Props) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [follow, setFollow] = useState(true);

  useEffect(() => {
    setFollow(true);
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [turn?.id]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && follow && live) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [visibleSteps, follow, live]);

  if (!turn) return <div className="p-8 text-muted">Nothing has happened yet.</div>;
  const steps = turn.steps.slice(0, visibleSteps);
  const working = live && visibleSteps < turn.steps.length;

  return (
    <div className="relative h-full">
      <div
        ref={scrollRef}
        className="scroll-thin h-full overflow-y-auto bg-bg px-6 pt-5"
        onScroll={(e) => {
          const el = e.currentTarget;
          setFollow(el.scrollTop + el.clientHeight >= el.scrollHeight - 80);
        }}
      >
        <ol>
          <Item icon={PhoneIncoming} bubble="bg-accent-soft text-accent" last={!steps.length && !working}>
            <Prompt turn={turn} />
          </Item>
          {steps.map((step, i) => (
            <StepItem key={`${turn.id}-${i}`} step={step} model={model} last={i === steps.length - 1 && !working} />
          ))}
          {working && (
            <li className="flex items-center gap-3.5 pb-6 text-sm text-muted">
              <div className="flex w-8 justify-center">
                <Spinner className="text-accent" />
              </div>
              working…
            </li>
          )}
        </ol>
      </div>
      {!follow && working && (
        <button
          onClick={() => setFollow(true)}
          className="absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-accent px-4 py-1.5 text-xs font-medium text-accent-ink shadow-float"
        >
          <ArrowDown size={13} /> Follow live
        </button>
      )}
    </div>
  );
}
