"use client";

import { Pause, Play, SkipBack, SkipForward } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Model } from "@/lib/model";
import { phoenixDate, type Timeline } from "@/lib/timeline";

interface Props {
  model: Model;
  timeline: Timeline;
  playing: boolean;
  speed: number;
  onPlay: () => void;
  onSpeed: (speed: number) => void;
  onSeek: (t: number) => void;
  getT: () => number;
  progressRef: { current: HTMLDivElement | null };
}

const SPEEDS = [0.5, 1, 2, 4, 8, 16, 32];
const MOMENT = {
  learn: { dot: "bg-learn", label: "Rewrote its notes" },
  bad: { dot: "bg-bad", label: "Worst day · lost urgent call" },
  good: { dot: "bg-good", label: "Best day" },
  warn: { dot: "bg-warn", label: "Timed out" },
} as const;

export default function PlaybackBar({ model, timeline, playing, speed, onPlay, onSpeed, onSeek, getT, progressRef }: Props) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [width, setWidth] = useState(0);
  const [hover, setHover] = useState<{ x: number; title: string } | null>(null);
  const frac = (t: number) => t / timeline.duration;

  const days = useMemo(() => {
    const out: { t: number; day: string; weekend: boolean }[] = [];
    for (const seg of timeline.segments) {
      if (seg.turn?.kind !== "opening") continue;
      const day = phoenixDate(seg.sim1);
      if (out.at(-1)?.day === day) continue;
      const weekday = new Date(`${day}T12:00:00Z`).getUTCDay();
      out.push({ t: seg.t0, day, weekend: weekday === 0 || weekday === 6 });
    }
    return out;
  }, [timeline]);

  const flags = useMemo(
    () => model.replay.moments.map((m) => ({ ...m, t: m.turn != null ? timeline.timeOfTurn(m.turn) : timeline.timeOfSim(m.sim_ms) })),
    [model, timeline],
  );
  const calls = useMemo(() => model.replay.turns.filter((t) => t.kind === "call").map((t) => timeline.timeOfTurn(t.id)), [model, timeline]);

  useEffect(() => {
    const el = trackRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !width) return;
    const dpr = window.devicePixelRatio || 1;
    const w = (canvas.width = canvas.clientWidth * dpr);
    const h = (canvas.height = canvas.clientHeight * dpr);
    const ctx = canvas.getContext("2d")!;
    const samples = 600;
    const values = Array.from({ length: samples + 1 }, (_, i) => model.cashAt(timeline.frame((i / samples) * timeline.duration).simMs));
    const start = model.replay.report.starting_cash_cents ?? values[0];
    const min = Math.min(start, ...values);
    const max = Math.max(start, ...values);
    const y = (v: number) => h - 2 - ((v - min) / Math.max(1, max - min)) * (h - 4);
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "#d8d4cb";
    ctx.setLineDash([3 * dpr, 3 * dpr]);
    ctx.beginPath();
    ctx.moveTo(0, y(start));
    ctx.lineTo(w, y(start));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();
    values.forEach((v, i) => (i ? ctx.lineTo((i / samples) * w, y(v)) : ctx.moveTo(0, y(v))));
    ctx.lineTo(w, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = "rgba(0,0,248,0.06)";
    ctx.fill();
    ctx.beginPath();
    values.forEach((v, i) => (i ? ctx.lineTo((i / samples) * w, y(v)) : ctx.moveTo(0, y(v))));
    ctx.lineWidth = 1.25 * dpr;
    ctx.strokeStyle = "rgba(0,0,248,0.55)";
    ctx.stroke();
  }, [model, timeline, width]);

  const seekFromPointer = (clientX: number) => {
    const rect = trackRef.current!.getBoundingClientRect();
    onSeek(Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)) * timeline.duration);
  };
  const jump = (dir: 1 | -1) => {
    const now = getT();
    const next = dir > 0 ? calls.find((x) => x > now + 0.01) : [...calls].reverse().find((x) => x < now - 0.01);
    if (next != null) onSeek(next);
  };

  // day labels only where there is room for them
  let lastLabelX = -Infinity;
  const iconBtn = "grid size-9 place-items-center rounded-lg text-muted transition hover:bg-sunken hover:text-ink";
  return (
    <footer className="flex items-center gap-5 border-t border-line bg-panel px-6">
      <div className="flex items-center gap-1">
        <button className={iconBtn} onClick={() => jump(-1)} title="Previous call (←)">
          <SkipBack size={16} strokeWidth={1.75} />
        </button>
        <button className="grid size-10 place-items-center rounded-full bg-accent text-accent-ink shadow-card transition hover:brightness-110" onClick={onPlay} title="Play / pause (space)">
          {playing ? <Pause size={16} fill="currentColor" strokeWidth={0} /> : <Play size={16} fill="currentColor" strokeWidth={0} className="translate-x-px" />}
        </button>
        <button className={iconBtn} onClick={() => jump(1)} title="Next call (→)">
          <SkipForward size={16} strokeWidth={1.75} />
        </button>
        <div className="ml-2 inline-flex rounded-lg bg-sunken p-0.5">
          {SPEEDS.filter((s) => s >= 1 && s <= 16).map((s) => (
            <button
              key={s}
              onClick={() => onSpeed(s)}
              className={`rounded-md px-2 py-1 text-xs tabular-nums transition ${speed === s ? "bg-panel font-medium text-ink shadow-card" : "text-muted hover:text-ink"}`}
            >
              {s}×
            </button>
          ))}
        </div>
      </div>

      <div className="relative h-[60px] flex-1 select-none">
        <div className="absolute inset-x-0 top-1.5 h-4">
          {days.map((d, i) => {
            const x = frac(d.t) * width;
            const next = days[i + 1] ? frac(days[i + 1].t) * width : width;
            const show = x - lastLabelX >= 44;
            if (show) lastLabelX = x;
            const date = new Date(`${d.day}T12:00:00Z`);
            return (
              <div key={d.day} className="absolute top-0 h-[52px]" style={{ left: x, width: next - x }}>
                {d.weekend && <div className="absolute inset-x-0 top-5 bottom-0 rounded-sm bg-sunken/70" />}
                <div className="absolute top-5 bottom-0 left-0 w-px bg-line" />
                {show && (
                  <span className="relative pl-1 text-xs whitespace-nowrap text-muted">
                    {date.toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" })} {date.getUTCDate()}
                  </span>
                )}
              </div>
            );
          })}
        </div>
        <div
          ref={(el) => {
            // the playback loop writes the position as --p on this element; fill and thumb both read it
            trackRef.current = el;
            progressRef.current = el;
          }}
          className="absolute inset-x-0 top-[22px] h-[34px] cursor-pointer"
          onPointerDown={(e) => {
            e.currentTarget.setPointerCapture(e.pointerId);
            seekFromPointer(e.clientX);
          }}
          onPointerMove={(e) => {
            if (e.buttons) seekFromPointer(e.clientX);
          }}
        >
          <canvas ref={canvasRef} className="pointer-events-none absolute inset-x-0 top-0 h-[22px] w-full" />
          <div className="absolute inset-x-0 bottom-1.5 h-1 rounded-full bg-line">
            <div className="h-full w-[calc(var(--p,0)*100%)] rounded-full bg-accent" />
          </div>
          <div
            className="pointer-events-none absolute bottom-0 size-4 -translate-x-1/2 rounded-full border-2 border-accent bg-panel shadow-card"
            style={{ left: "calc(var(--p, 0) * 100%)" }}
          />
        </div>
        {flags.map((f, i) => (
          <button
            key={i}
            className={`absolute top-[43px] z-10 size-2.5 -translate-x-1/2 rounded-full ring-2 ring-panel transition hover:scale-150 ${MOMENT[f.kind].dot}`}
            style={{ left: frac(f.t) * width }}
            onMouseEnter={() => setHover({ x: frac(f.t) * width, title: f.title })}
            onMouseLeave={() => setHover(null)}
            onClick={() => onSeek(f.t)}
          />
        ))}
        {hover && (
          <div
            className="pointer-events-none absolute bottom-[46px] z-20 w-max max-w-[380px] -translate-x-1/2 rounded-xl border border-line bg-panel px-3 py-2 text-xs shadow-float"
            style={{ left: Math.min(width - 190, Math.max(190, hover.x)) }}
          >
            {hover.title}
          </div>
        )}
      </div>

      <div className="hidden flex-col gap-0.5 text-xs text-muted xl:flex">
        {(["learn", "bad", "good", "warn"] as const).map((k) => (
          <span key={k} className="flex items-center gap-1.5 whitespace-nowrap">
            <span className={`size-2 rounded-full ${MOMENT[k].dot}`} />
            {MOMENT[k].label}
          </span>
        ))}
      </div>
    </footer>
  );
}
