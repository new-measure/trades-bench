"use client";

import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import type { Model } from "@/lib/model";
import type { MemoryEntry, Turn } from "@/lib/types";
import type { SceneHandle, ViewState } from "../Viewer";
import CallHeader from "./CallHeader";
import DecisionStamp from "./DecisionStamp";
import Notebook from "./Notebook";
import { OfficeScene } from "./OfficeScene";
import Transcript from "./Transcript";

export interface NotebookState {
  entry: MemoryEntry | null;
  index: number;
  fresh: boolean;
}

// The notebook as of this moment: every version written before this turn, plus versions written by
// tool calls in this turn that have already played.
function notebookAt(model: Model, turn: Turn | null, visibleSteps: number): NotebookState {
  const memory = model.replay.memory;
  let index = -1;
  const turnId = turn?.id ?? -1;
  for (let i = 0; i < memory.length && memory[i].turn < turnId; i++) index = i;
  let fresh = false;
  if (turn) {
    for (const step of turn.steps.slice(0, visibleSteps)) {
      if (step.memory_entry != null && step.memory_entry > index) {
        index = step.memory_entry;
        fresh = true;
      }
    }
  }
  return { entry: index >= 0 ? memory[index] : null, index, fresh };
}

export default function OfficeTab({ model, view, sceneRef }: { model: Model; view: ViewState; sceneRef: RefObject<SceneHandle | null> }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [panel, setPanel] = useState<"transcript" | "notebook">("transcript");

  useEffect(() => {
    if (!hostRef.current) return;
    const scene = new OfficeScene(hostRef.current, model);
    sceneRef.current = scene;
    return () => {
      sceneRef.current = null;
      scene.dispose();
    };
  }, [model, sceneRef]);

  const live = view.turnId != null;
  const turn = model.turnsById[live ? view.turnId! : view.lastTurnId] ?? null;
  const visibleSteps = live ? view.visibleSteps : turn?.steps.length ?? 0;
  const notebook = useMemo(() => notebookAt(model, turn, visibleSteps), [model, turn, visibleSteps]);

  return (
    <div className="grid h-full grid-cols-[minmax(0,1fr)_minmax(540px,44%)]">
      <section className="relative min-h-0 bg-[radial-gradient(ellipse_at_50%_45%,#fbfaf7_0%,#efede8_60%,#e6e2da_100%)]">
        <div ref={hostRef} className="absolute inset-0 overflow-hidden" />
        {live && view.decided && turn?.decision && <DecisionStamp key={turn.id} decision={turn.decision} />}
      </section>
      <section className="flex min-h-0 flex-col border-l border-line bg-bg">
        <CallHeader turn={turn} live={live} visibleSteps={visibleSteps} decided={live ? view.decided : true} />
        <div className="flex items-center gap-6 border-b border-line bg-panel px-6">
          {(["transcript", "notebook"] as const).map((p) => (
            <button
              key={p}
              onClick={() => setPanel(p)}
              className={`-mb-px flex items-center gap-2 border-b-2 py-3 text-sm font-medium ${
                panel === p ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink"
              }`}
            >
              {p === "transcript" ? "Transcript" : "Notebook"}
              {p === "notebook" && notebook.fresh && <span className="h-1.5 w-1.5 rounded-full bg-learn" title="Updated during this call" />}
            </button>
          ))}
          <div className="flex-1" />
          {panel === "notebook" && notebook.entry && (
            <span className="text-xs text-muted">
              MEMORY.md · v{notebook.index + 1} of {model.replay.memory.length}
            </span>
          )}
        </div>
        <div className="min-h-0 flex-1">
          {panel === "transcript" ? (
            <Transcript model={model} turn={turn} visibleSteps={visibleSteps} live={live} />
          ) : (
            <Notebook model={model} state={notebook} />
          )}
        </div>
      </section>
    </div>
  );
}
