import { fmtClock, fmtDay } from "@/lib/timeline";
import type { Decision, Turn } from "@/lib/types";

export function turnTitle(turn: Turn): string {
  if (turn.kind === "opening") return "Morning planning";
  if (turn.kind === "closing") return "End-of-day review";
  if (turn.kind === "reminder") return `Reminder · call #${turn.fields["call_id"] ?? "?"}`;
  return turn.call_id != null ? `Call #${turn.call_id}` : "Call";
}

export function turnWhen(turn: Turn): string {
  return `${fmtDay(turn.sim_ms)} · ${fmtClock(turn.sim_ms)}`;
}

export const DECISION_STYLE: Record<Decision["type"], { label: string; cls: string }> = {
  booked: { label: "Booked", cls: "bg-good-soft text-good" },
  rejected: { label: "Rejected", cls: "bg-sunken text-ink" },
  lost: { label: "Lost — customer declined", cls: "bg-bad-soft text-bad" },
  cancel_ack: { label: "Cancellation acknowledged", cls: "bg-warn-soft text-warn" },
  unanswered: { label: "Unanswered", cls: "bg-bad-soft text-bad" },
  pending: { label: "Still open", cls: "bg-sunken text-muted" },
  attempted: { label: "Action attempted", cls: "bg-sunken text-muted" },
};

export function decisionDetail(decision: Decision): string {
  if (decision.type === "booked") {
    const parts = [decision.job_type?.replace("hvac_", "HVAC ").replace(/_/g, " "), decision.technician];
    if (decision.scheduled_start) parts.push(`${fmtDay(decision.scheduled_start)} ${fmtClock(decision.scheduled_start)}`);
    return parts.filter(Boolean).join(" · ");
  }
  if (decision.type === "lost") return "The slot offered was refused and the call ended.";
  return "";
}
