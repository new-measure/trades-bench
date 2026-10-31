import type { Replay, Turn } from "./types";

// Playback runs on a virtual clock (seconds at 1x). Between turns the simulated clock advances
// (fast overnight); during a turn office time is frozen while the agent works the call, and the
// turn's steps play out over its segment.

const SIM_MIN_PER_SECOND = 20;
const GAP_MIN_S = 0.6;
const GAP_MAX_S = 7;
const DAY_END_HOUR = 19.5;

export interface Segment {
  t0: number;
  t1: number;
  sim0: number;
  sim1: number;
  turn: Turn | null;
  work?: number; // seconds of the turn spent on its steps; the rest holds on the decision
}

// Turn progress keeps one scale for every consumer: steps play out over [0, STEPS_END],
// and the decision holds on the screen over [DECISION_AT, 1].
export const STEPS_END = 0.85;
export const DECISION_AT = 0.86;
const DECISION_HOLD_S = 3;
const NO_DECISION_HOLD_S = 0.6;

export interface Frame {
  t: number;
  simMs: number;
  turn: Turn | null;
  turnProgress: number;
  segment: Segment;
}

function turnSeconds(turn: Turn): number {
  const steps = turn.steps.length;
  return Math.min(22, Math.max(5, 3 + steps * 0.7));
}

function gapSeconds(sim0: number, sim1: number): number {
  const minutes = (sim1 - sim0) / 60000;
  return Math.min(GAP_MAX_S, Math.max(GAP_MIN_S, minutes / SIM_MIN_PER_SECOND));
}

export class Timeline {
  readonly segments: Segment[] = [];
  readonly duration: number;
  readonly startMs: number;
  readonly endMs: number;

  constructor(replay: Replay) {
    const turns = replay.turns;
    let t = 0;
    let sim = turns[0].sim_ms - 15 * 60000;
    this.startMs = sim;
    for (const turn of turns) {
      if (turn.sim_ms > sim) {
        const d = gapSeconds(sim, turn.sim_ms);
        this.segments.push({ t0: t, t1: t + d, sim0: sim, sim1: turn.sim_ms, turn: null });
        t += d;
        sim = turn.sim_ms;
      }
      const work = turnSeconds(turn);
      const shown = turn.decision && ["booked", "rejected", "lost", "cancel_ack", "unanswered"].includes(turn.decision.type);
      const d = work + (shown ? DECISION_HOLD_S : NO_DECISION_HOLD_S);
      this.segments.push({ t0: t, t1: t + d, sim0: sim, sim1: sim, turn, work });
      t += d;
    }
    const last = turns[turns.length - 1];
    const end = new Date(last.sim_ms);
    const endOfDay = last.sim_ms + Math.max(0, (DAY_END_HOUR - phoenixHour(end)) * 3600000);
    if (endOfDay > sim) {
      const d = gapSeconds(sim, endOfDay);
      this.segments.push({ t0: t, t1: t + d, sim0: sim, sim1: endOfDay, turn: null });
      t += d;
      sim = endOfDay;
    }
    this.endMs = sim;
    this.duration = t;
  }

  private find(t: number): Segment {
    const segs = this.segments;
    let lo = 0;
    let hi = segs.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (segs[mid].t0 <= t) lo = mid;
      else hi = mid - 1;
    }
    return segs[lo];
  }

  frame(t: number): Frame {
    const clamped = Math.max(0, Math.min(this.duration, t));
    const seg = this.find(clamped);
    const p = seg.t1 > seg.t0 ? (clamped - seg.t0) / (seg.t1 - seg.t0) : 1;
    if (!seg.turn) return { t: clamped, simMs: seg.sim0 + (seg.sim1 - seg.sim0) * p, turn: null, turnProgress: 0, segment: seg };
    const elapsed = clamped - seg.t0;
    const work = seg.work ?? seg.t1 - seg.t0;
    const hold = seg.t1 - seg.t0 - work;
    const turnProgress = elapsed < work ? (STEPS_END * elapsed) / work : DECISION_AT + ((1 - DECISION_AT) * (elapsed - work)) / Math.max(hold, 1e-6);
    return { t: clamped, simMs: seg.sim0, turn: seg.turn, turnProgress: Math.min(1, turnProgress), segment: seg };
  }

  timeOfSim(simMs: number): number {
    for (const seg of this.segments) {
      if (!seg.turn && simMs >= seg.sim0 && simMs <= seg.sim1) {
        const span = seg.sim1 - seg.sim0;
        return seg.t0 + (span > 0 ? ((simMs - seg.sim0) / span) * (seg.t1 - seg.t0) : 0);
      }
      if (seg.turn && seg.sim0 >= simMs) return seg.t0;
    }
    return this.duration;
  }

  timeOfTurn(turnId: number): number {
    const seg = this.segments.find((s) => s.turn?.id === turnId);
    return seg ? seg.t0 : 0;
  }
}

const PHOENIX_OFFSET_MS = -7 * 3600000;

export function phoenix(ms: number): Date {
  return new Date(ms + PHOENIX_OFFSET_MS);
}

export function phoenixHour(d: Date): number {
  const p = phoenix(d.getTime());
  return p.getUTCHours() + p.getUTCMinutes() / 60;
}

export function phoenixDate(ms: number): string {
  return phoenix(ms).toISOString().slice(0, 10);
}

export function fmtClock(ms: number): string {
  const p = phoenix(ms);
  return p.toISOString().slice(11, 16);
}

export function fmtDay(ms: number): string {
  const p = phoenix(ms);
  return p.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
}

export function fmtMoney(cents: number, sign = false): string {
  const v = cents / 100;
  const s = Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (sign) return (v >= 0 ? "+$" : "−$") + s;
  return (v < 0 ? "−$" : "$") + s;
}
