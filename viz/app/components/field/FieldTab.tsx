"use client";

import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import type { Model, Visit } from "@/lib/model";
import { fmtClock, fmtDay, fmtMoney, phoenixDate } from "@/lib/timeline";
import type { SceneHandle, ViewState } from "../Viewer";
import { FieldScene } from "./FieldScene";

interface Props {
  model: Model;
  zones: unknown;
  view: ViewState;
  sceneRef: RefObject<SceneHandle | null>;
}

const STATUS_STYLE = {
  driving: "bg-accent-soft text-accent",
  working: "bg-good-soft text-good",
  off: "bg-sunken text-faint",
  idle: "bg-sunken text-muted",
} as const;

function Card({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="rounded-2xl border border-line bg-panel shadow-card">
      <div className="flex items-center justify-between px-4 pt-3.5 text-sm font-medium tracking-tight">
        {title}
        {right}
      </div>
      <div className="px-4 pb-4 pt-2">{children}</div>
    </div>
  );
}

function Row({ label, cents, strong = false }: { label: string; cents: number; strong?: boolean }) {
  return (
    <div className={`flex justify-between py-0.5 text-sm ${strong ? "mt-1 border-t border-line pt-2 font-semibold" : ""}`}>
      <span className={strong ? "text-ink" : "text-muted"}>{label}</span>
      <span className={`font-mono tabular-nums ${cents < 0 ? "text-bad" : cents > 0 ? "text-good" : "text-faint"}`}>{fmtMoney(cents, true)}</span>
    </div>
  );
}

export default function FieldTab({ model, zones, view, sceneRef }: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [selected, setSelected] = useState<Visit | null>(null);
  const [toasts, setToasts] = useState<{ id: number; text: string }[]>([]);
  const lastSim = useRef(view.simMinute);
  const sim = view.simMinute;

  useEffect(() => {
    if (!hostRef.current || !model.hasWorld) return;
    const scene = new FieldScene(hostRef.current, model, zones as never, setSelected);
    sceneRef.current = { update: (frame, _lastTurnId, wall) => scene.update(frame, wall) };
    return () => {
      sceneRef.current = null;
      scene.dispose();
    };
  }, [model, zones, sceneRef]);

  // Fixed costs (payroll, overhead, on-call) post at close; surface them as they happen.
  useEffect(() => {
    const prev = lastSim.current;
    lastSim.current = sim;
    if (sim <= prev || sim - prev > 6 * 3600000) return;
    const entries = model.ledgerBetween(prev, sim).filter((e) => e.job_id == null && e.amount_cents < 0);
    if (!entries.length) return;
    const byType = new Map<string, number>();
    for (const e of entries) byType.set(e.type, (byType.get(e.type) ?? 0) + e.amount_cents);
    const text = `${fmtClock(sim)} — ${[...byType].map(([t, c]) => `${t.replace(/_/g, " ")} ${fmtMoney(c, true)}`).join(" · ")}`;
    const id = Date.now();
    setToasts((ts) => [...ts, { id, text }]);
    setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), 4500);
  }, [sim, model]);

  const day = useMemo(() => {
    const today = phoenixDate(sim);
    const entries = model.ledger.filter((e) => e.service_date === today && e.ts <= sim);
    const sum = (types: string[]) => entries.filter((e) => types.includes(e.type)).reduce((s, e) => s + e.amount_cents, 0);
    return {
      revenue: sum(["revenue"]),
      materials: sum(["material_cost"]),
      payroll: sum(["payroll_base", "payroll_off_hours"]),
      oncall: sum(["payroll_off_hours_bonus"]),
      overhead: sum(["operating_overhead", "recruiting_cost", "training_cost"]),
      net: entries.filter((e) => e.type !== "opening_balance_adjustment").reduce((s, e) => s + e.amount_cents, 0),
    };
  }, [model, sim]);

  const techs = useMemo(() => {
    const today = phoenixDate(sim);
    return [...model.techs.values()].map((tech) => {
      const done = model.visits.filter((v) => v.tech?.id === tech.id && v.paidAt != null && v.paidAt <= sim);
      return {
        tech,
        status: model.techStatus(tech.id, sim),
        today: done.filter((v) => phoenixDate(v.paidAt!) === today).length,
        total: done.length,
        revenue: done.reduce((s, v) => s + v.revenueCents, 0),
        margin: done.reduce((s, v) => s + v.revenueCents - v.materialCents, 0),
        oncall: model.ledger.filter((e) => e.technician_id === tech.id && e.type === "payroll_off_hours_bonus" && e.ts <= sim).reduce((s, e) => s + e.amount_cents, 0),
      };
    });
  }, [model, sim]);

  if (!model.hasWorld) {
    return (
      <div className="grid h-full place-items-center p-8 text-center">
        <div>
          <h2 className="text-lg font-medium">No field data for this run</h2>
          <p className="mt-1 text-muted">This run has no run.db. Rebuild it from the logs:</p>
          <pre className="mt-3 rounded-xl bg-sunken p-3 font-mono text-sm">uv run python viz/build/rebuild_run.py &lt;run_dir&gt;</pre>
        </div>
      </div>
    );
  }

  const state = selected ? model.visitState(selected, sim) : null;
  return (
    <div className="grid h-full grid-cols-[minmax(0,1fr)_380px]">
      <div className="relative min-h-0 bg-[#dcd6ca]">
        <div ref={hostRef} className="absolute inset-0 overflow-hidden" />
        <div className="pointer-events-none absolute left-1/2 top-4 flex -translate-x-1/2 flex-col items-center gap-2">
          {toasts.map((t) => (
            <div key={t.id} className="animate-enter rounded-xl border border-line bg-panel px-4 py-2 font-mono text-xs shadow-float">
              {t.text}
            </div>
          ))}
        </div>
        <div className="pointer-events-none absolute bottom-4 left-4 flex gap-4 rounded-xl border border-line bg-panel/90 px-4 py-2 text-xs text-muted shadow-card backdrop-blur">
          <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-faint" />booked</span>
          <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-warn" />on site</span>
          <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-good" />paid (height = revenue)</span>
          <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-bad" />failed / lost call</span>
          <span>click a pillar for details</span>
        </div>
      </div>
      <aside className="scroll-thin min-h-0 space-y-3 overflow-y-auto border-l border-line bg-bg p-4">
        <Card title={`${fmtDay(sim)} · day so far`}>
          <Row label="Revenue" cents={day.revenue} />
          <Row label="Materials" cents={day.materials} />
          <Row label="Payroll" cents={day.payroll} />
          <Row label="On-call charges" cents={day.oncall} />
          <Row label="Overhead" cents={day.overhead} />
          <Row label="Net" cents={day.net} strong />
        </Card>
        <Card title="Technicians">
          <div className="space-y-4">
            {techs.map((t) => (
              <div key={t.tech.id}>
                <div className="flex items-center gap-2">
                  <span className="h-2.5 w-2.5 rounded-full" style={{ background: t.tech.color }} />
                  <span className="text-sm font-medium">{t.tech.name}</span>
                  <span className={`ml-auto rounded-full px-2 py-0.5 text-xs ${STATUS_STYLE[t.status.kind]}`}>{t.status.label}</span>
                </div>
                <div className="mt-0.5 pl-[18px] text-xs text-muted">{t.tech.skills.join(", ").replace(/_/g, " ")}</div>
                <div className="mt-1.5 grid grid-cols-3 gap-2 pl-[18px] text-xs tabular-nums">
                  <div><div className="text-muted">Jobs</div><div className="font-mono">{t.today} today · {t.total}</div></div>
                  <div><div className="text-muted">Revenue</div><div className="font-mono">{fmtMoney(t.revenue)}</div></div>
                  <div><div className="text-muted">Margin</div><div className="font-mono text-good">{fmtMoney(t.margin)}</div></div>
                </div>
                {t.oncall < 0 && <div className="mt-1 pl-[18px] font-mono text-xs text-bad">on-call charges {fmtMoney(t.oncall)}</div>}
              </div>
            ))}
          </div>
        </Card>
        <Card
          title={selected ? `${selected.job.job_number} · ${selected.job.job_type.replace(/_/g, " ")}` : "Job details"}
          right={selected && <button className="text-muted hover:text-ink" onClick={() => setSelected(null)}>✕</button>}
        >
          {!selected ? (
            <p className="text-sm text-muted">Click a pillar on the map to see that job's story: the call it came from, the drive, the work and the money.</p>
          ) : (
            <div className="space-y-2 text-sm">
              <div>
                {selected.job.customer} <span className="text-muted">({selected.job.segment})</span>
                <div className="text-muted">{selected.job.street}, {selected.job.city}</div>
              </div>
              <table className="w-full">
                <tbody className="[&_td]:py-0.5 [&_td:last-child]:text-right [&_td:last-child]:font-mono">
                  <tr><td className="text-muted">Call</td><td>#{selected.job.call_id}</td></tr>
                  <tr><td className="text-muted">Technician</td><td>{selected.tech?.name ?? "—"}</td></tr>
                  <tr><td className="text-muted">Booked</td><td>{fmtDay(selected.appt.created_at)} {fmtClock(selected.appt.created_at)}</td></tr>
                  <tr><td className="text-muted">Slot</td><td>{fmtDay(selected.appt.scheduled_start)} {fmtClock(selected.appt.scheduled_start)}–{fmtClock(selected.appt.scheduled_end)}</td></tr>
                  <tr><td className="text-muted">Status now</td><td>{state ?? "not booked yet"}</td></tr>
                  {selected.events.map((e, i) => (
                    <tr key={i} className={e.ts <= sim ? "" : "opacity-40"}>
                      <td className="text-muted">{e.type.replace(/_/g, " ")}</td>
                      <td>{fmtDay(e.ts)} {fmtClock(e.ts)}</td>
                    </tr>
                  ))}
                  <tr><td className="text-muted">Revenue</td><td>{fmtMoney(selected.revenueCents)}</td></tr>
                  <tr><td className="text-muted">Materials</td><td>{fmtMoney(-selected.materialCents)}</td></tr>
                  <tr className="font-semibold"><td>Margin</td><td className="text-good">{fmtMoney(selected.revenueCents - selected.materialCents, true)}</td></tr>
                  {selected.job.outcome && <tr><td className="text-muted">Outcome</td><td>{selected.job.outcome.replace(/_/g, " ")}</td></tr>}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </aside>
    </div>
  );
}
