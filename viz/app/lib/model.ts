import type { Appointment, ApptEvent, Drive, Job, LedgerEntry, MemoryEntry, Replay, Technician, Turn } from "./types";

// Indexes over a replay that both tabs query every frame.

export interface Visit {
  appt: Appointment;
  job: Job;
  tech: Technician | undefined;
  events: ApptEvent[];
  revenueCents: number;
  materialCents: number;
  paidAt: number | null;
}

export type VisitState = "scheduled" | "enroute" | "working" | "completed" | "failed" | "cancelled";

function upperBound<T>(items: T[], key: (item: T) => number, value: number): number {
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (key(items[mid]) <= value) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export class Model {
  readonly replay: Replay;
  readonly visits: Visit[] = [];
  readonly techs = new Map<number, Technician>();
  readonly drivesByTech = new Map<number, Drive[]>();
  readonly ledger: LedgerEntry[];
  readonly turnsById: Turn[];

  constructor(replay: Replay) {
    this.replay = replay;
    this.turnsById = replay.turns;
    const world = replay.world;
    this.ledger = world?.ledger ?? [];
    if (!world) return;
    for (const tech of world.technicians) this.techs.set(tech.id, tech);
    const jobs = new Map(world.jobs.map((j) => [j.id, j]));
    const events = new Map<number, ApptEvent[]>();
    for (const e of world.events) {
      if (!events.has(e.appointment_id)) events.set(e.appointment_id, []);
      events.get(e.appointment_id)!.push(e);
    }
    const revenue = new Map<number, { rev: number; mat: number; paid: number | null }>();
    for (const entry of world.ledger) {
      if (entry.job_id == null) continue;
      const r = revenue.get(entry.job_id) ?? { rev: 0, mat: 0, paid: null };
      if (entry.type === "revenue") {
        r.rev += entry.amount_cents;
        r.paid = entry.ts;
      } else if (entry.type === "material_cost") r.mat += -entry.amount_cents;
      revenue.set(entry.job_id, r);
    }
    for (const appt of world.appointments) {
      const job = jobs.get(appt.job_id);
      if (!job) continue;
      const money = revenue.get(job.id);
      this.visits.push({
        appt,
        job,
        tech: appt.technician_id != null ? this.techs.get(appt.technician_id) : undefined,
        events: events.get(appt.id) ?? [],
        revenueCents: money?.rev ?? 0,
        materialCents: money?.mat ?? 0,
        paidAt: money?.paid ?? null,
      });
    }
    this.buildRoutes(world.drives);
  }

  // The engine records drives home in _technician_movements; outbound legs are the
  // dispatched -> arrived span of each appointment, starting wherever the tech last was.
  private buildRoutes(homeDrives: Drive[]): void {
    const visitsByAppt = new Map(this.visits.map((v) => [v.appt.id, v]));
    const legs = new Map<number, Drive[]>();
    const push = (d: Drive) => {
      if (!legs.has(d.tech)) legs.set(d.tech, []);
      legs.get(d.tech)!.push(d);
    };
    for (const d of homeDrives) push(d);
    const dispatched = new Map<number, ApptEvent>();
    for (const e of this.replay.world!.events) {
      if (e.technician_id == null) continue;
      if (e.type === "dispatched") dispatched.set(e.appointment_id, e);
      else if (e.type === "arrived" && dispatched.has(e.appointment_id)) {
        const visit = visitsByAppt.get(e.appointment_id);
        const start = dispatched.get(e.appointment_id)!;
        if (visit) push({ tech: e.technician_id, kind: "to_job", lat0: NaN, lon0: NaN, lat1: visit.job.lat, lon1: visit.job.lon, t0: start.ts, t1: e.ts });
      }
    }
    for (const [techId, list] of legs) {
      list.sort((a, b) => a.t0 - b.t0 || a.t1 - b.t1);
      const tech = this.techs.get(techId);
      let lat = tech?.lat ?? 0;
      let lon = tech?.lon ?? 0;
      for (const leg of list) {
        if (Number.isNaN(leg.lat0)) {
          leg.lat0 = lat;
          leg.lon0 = lon;
        }
        lat = leg.lat1;
        lon = leg.lon1;
      }
      this.drivesByTech.set(techId, list);
    }
  }

  get hasWorld(): boolean {
    return this.replay.world != null;
  }

  cashAt(simMs: number): number {
    const start = this.replay.report.starting_cash_cents ?? 0;
    if (this.ledger.length) {
      const i = upperBound(this.ledger, (e) => e.ts, simMs);
      return i ? this.ledger[i - 1].balance_after_cents : start;
    }
    const daily = this.replay.report.daily_cash_cents ?? [];
    let cash = start;
    for (const [day, cents] of daily) {
      if (Date.parse(`${day}T19:00:00-07:00`) <= simMs) cash = cents;
    }
    return cash;
  }

  ledgerBetween(t0: number, t1: number): LedgerEntry[] {
    const a = upperBound(this.ledger, (e) => e.ts, t0);
    const b = upperBound(this.ledger, (e) => e.ts, t1);
    return this.ledger.slice(a, b);
  }

  visitState(v: Visit, simMs: number): VisitState | null {
    if (v.appt.created_at > simMs) return null;
    let state: VisitState = "scheduled";
    for (const e of v.events) {
      if (e.ts > simMs) break;
      if (e.type === "dispatched") state = "enroute";
      else if (e.type === "arrived" || e.type === "work_started") state = "working";
      else if (e.type === "work_completed") state = "completed";
      else if (e.type === "work_failed" || e.type === "no_show") state = "failed";
      else if (e.type === "cancelled") state = "cancelled";
    }
    if (v.appt.cancelled_at != null && v.appt.cancelled_at <= simMs) state = "cancelled";
    return state;
  }

  techPosition(techId: number, simMs: number): { lat: number; lon: number; driving: Drive | null; f: number } {
    const tech = this.techs.get(techId)!;
    const drives = this.drivesByTech.get(techId) ?? [];
    const i = upperBound(drives, (d) => d.t0, simMs);
    if (i === 0) return { lat: tech.lat, lon: tech.lon, driving: null, f: 0 };
    const d = drives[i - 1];
    if (simMs >= d.t1) return { lat: d.lat1, lon: d.lon1, driving: null, f: 1 };
    const f = (simMs - d.t0) / Math.max(1, d.t1 - d.t0);
    return { lat: d.lat0 + (d.lat1 - d.lat0) * f, lon: d.lon0 + (d.lon1 - d.lon0) * f, driving: d, f };
  }

  techStatus(techId: number, simMs: number): { label: string; kind: "driving" | "working" | "off" | "idle" } {
    const pos = this.techPosition(techId, simMs);
    if (pos.driving) return { label: pos.driving.kind.includes("home") ? "heading home" : "driving to job", kind: "driving" };
    const working = this.visits.find((v) => v.tech?.id === techId && this.visitState(v, simMs) === "working");
    if (working) return { label: `on site · ${working.job.job_type.replace(/_/g, " ")}`, kind: "working" };
    if (!this.onShift(techId, simMs)) return { label: "off shift", kind: "off" };
    return { label: "idle", kind: "idle" };
  }

  onShift(techId: number, simMs: number): boolean {
    return (this.replay.world?.shifts ?? []).some((s) => s.tech === techId && s.t0 <= simMs && simMs < s.t1);
  }

  // The notebook as the agent last saved it before (or during) this point in the turn.
  memoryAt(turnId: number, progress: number): { entry: MemoryEntry | null; fresh: boolean } {
    const entries = this.replay.memory;
    let found: MemoryEntry | null = null;
    for (const entry of entries) {
      if (entry.turn < turnId || (entry.turn === turnId && progress >= 0.75)) found = entry;
      else break;
    }
    return { entry: found, fresh: found != null && found.turn === turnId };
  }
}
