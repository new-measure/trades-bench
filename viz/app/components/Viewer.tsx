"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Model } from "@/lib/model";
import { DECISION_AT, STEPS_END, Timeline, type Frame } from "@/lib/timeline";
import type { Replay, RunIndexEntry } from "@/lib/types";
import FieldTab from "./field/FieldTab";
import OfficeTab from "./office/OfficeTab";
import PlaybackBar from "./PlaybackBar";
import TopBar from "./TopBar";

export type Tab = "office" | "field";

// What the React panels render. Recomputed every animation frame, but only committed to state
// when one of these values changes, so the 3D scenes run at 60fps without re-rendering the UI.
export interface ViewState {
  simMinute: number;
  turnId: number | null;
  lastTurnId: number;
  visibleSteps: number;
  decided: boolean;
  fieldTick: number;
}

export interface SceneHandle {
  update(frame: Frame, lastTurnId: number, wall: number): void;
}

function stepsVisible(frame: Frame): number {
  const turn = frame.turn;
  if (!turn) return 0;
  return Math.min(turn.steps.length, Math.ceil(Math.min(1, frame.turnProgress / STEPS_END) * turn.steps.length));
}

async function loadJson<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export default function Viewer() {
  const [runs, setRuns] = useState<RunIndexEntry[]>([]);
  const [zones, setZones] = useState<unknown>(null);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("office");
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [view, setView] = useState<ViewState | null>(null);

  const tRef = useRef(0);
  const playingRef = useRef(false);
  const speedRef = useRef(1);
  const tabRef = useRef<Tab>("office");
  const sceneRef = useRef<SceneHandle | null>(null);
  const progressRef = useRef<HTMLDivElement | null>(null);
  const viewKeyRef = useRef("");

  const model = useMemo(() => (replay ? new Model(replay) : null), [replay]);
  const timeline = useMemo(() => (replay ? new Timeline(replay) : null), [replay]);

  playingRef.current = playing;
  speedRef.current = speed;
  tabRef.current = tab;

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("tab") === "field") setTab("field");
    Promise.all([loadJson<RunIndexEntry[]>("/data/index.json"), loadJson("/data/zones.json")])
      .then(([index, z]) => {
        setRuns(index);
        setZones(z);
        const wanted = params.get("run") ?? index[0]?.label;
        if (!wanted) throw new Error("no runs in /data/index.json");
        return loadJson<Replay>(`/data/${wanted}/replay.json`);
      })
      .then(setReplay)
      .catch((e) => setError(String(e)));
  }, []);

  const selectRun = useCallback((label: string) => {
    setPlaying(false);
    setReplay(null);
    tRef.current = 0;
    loadJson<Replay>(`/data/${label}/replay.json`).then(setReplay).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    const url = new URL(window.location.href);
    if (replay) url.searchParams.set("run", replay.label);
    url.searchParams.set("tab", tab);
    window.history.replaceState(null, "", url);
  }, [replay, tab]);

  const seek = useCallback((t: number) => {
    tRef.current = t;
  }, []);

  // Playback loop: advances the clock, drives the active 3D scene, and publishes ViewState changes.
  useEffect(() => {
    if (!timeline || !model) return;
    let raf = 0;
    let last = performance.now();
    const loop = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      if (playingRef.current) {
        tRef.current += dt * speedRef.current;
        if (tRef.current >= timeline.duration) {
          tRef.current = timeline.duration;
          setPlaying(false);
        }
      }
      const frame = timeline.frame(tRef.current);
      let lastTurnId = frame.turn?.id ?? -1;
      if (!frame.turn) {
        for (const seg of timeline.segments) {
          if (seg.t0 > frame.segment.t0) break;
          if (seg.turn) lastTurnId = seg.turn.id;
        }
      }
      sceneRef.current?.update(frame, lastTurnId, now / 1000);
      progressRef.current?.style.setProperty("--p", String(tRef.current / timeline.duration));

      const next: ViewState = {
        simMinute: Math.floor(frame.simMs / 60000) * 60000,
        turnId: frame.turn?.id ?? null,
        lastTurnId,
        visibleSteps: stepsVisible(frame),
        decided: !!frame.turn && frame.turnProgress >= DECISION_AT,
        fieldTick: tabRef.current === "field" ? Math.floor(frame.simMs / 120000) : 0,
      };
      const key = `${next.simMinute}|${next.turnId}|${next.lastTurnId}|${next.visibleSteps}|${next.decided}|${next.fieldTick}`;
      if (key !== viewKeyRef.current) {
        viewKeyRef.current = key;
        setView(next);
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [timeline, model]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "SELECT" || target.tagName === "TEXTAREA") return;
      if (!timeline || !replay) return;
      if (e.code === "Space") {
        e.preventDefault();
        setPlaying((p) => !p);
      } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        const times = replay.turns.filter((t) => t.kind === "call").map((t) => timeline.timeOfTurn(t.id));
        const now = tRef.current;
        const next = e.key === "ArrowRight" ? times.find((x) => x > now + 0.01) : [...times].reverse().find((x) => x < now - 0.01);
        if (next != null) tRef.current = next;
      } else if (e.key === "1") setTab("office");
      else if (e.key === "2") setTab("field");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [timeline, replay]);

  if (error) {
    return (
      <div className="grid h-screen place-items-center p-8">
        <div className="max-w-xl rounded-2xl border border-line bg-panel p-6 shadow-card">
          <h1 className="mb-2 text-lg font-medium text-bad">Couldn&apos;t load the replay</h1>
          <p className="mb-4 text-sm text-muted">{error}</p>
          <p className="text-sm">Build the viewer data first:</p>
          <pre className="mt-2 rounded-xl bg-sunken p-3 font-mono text-xs">uv run python viz/build/build_replay.py runs/seed5/opus5-xhigh-a1</pre>
        </div>
      </div>
    );
  }
  if (!replay || !model || !timeline || !view) {
    return <div className="grid h-screen place-items-center text-muted">Loading run…</div>;
  }

  return (
    <div className="grid h-screen grid-rows-[56px_minmax(0,1fr)_76px] overflow-hidden bg-bg">
      <TopBar
        runs={runs}
        replay={replay}
        model={model}
        tab={tab}
        onTab={setTab}
        onRun={selectRun}
        simMs={view.simMinute}
      />
      <main className="min-h-0">
        {tab === "office" ? (
          <OfficeTab model={model} view={view} sceneRef={sceneRef} />
        ) : (
          <FieldTab model={model} zones={zones} view={view} sceneRef={sceneRef} />
        )}
      </main>
      <PlaybackBar
        model={model}
        timeline={timeline}
        playing={playing}
        speed={speed}
        onPlay={() => setPlaying((p) => !p)}
        onSpeed={setSpeed}
        onSeek={seek}
        getT={() => tRef.current}
        progressRef={progressRef}
      />
    </div>
  );
}
