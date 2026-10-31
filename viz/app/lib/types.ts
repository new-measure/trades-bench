export interface Step {
  type: "think" | "say" | "tool";
  text?: string;
  tool?: string;
  input?: string;
  output?: string;
  ops?: string[];
  error?: boolean;
  memory?: boolean;
  memory_entry?: number;
}

export interface Decision {
  type: "booked" | "lost" | "rejected" | "cancel_ack" | "unanswered" | "pending" | "attempted";
  job_id?: number;
  job_type?: string;
  appointment_id?: number;
  technician?: string;
  technician_id?: number;
  scheduled_start?: number;
  scheduled_end?: number;
}

export interface Turn {
  id: number;
  stem: string;
  date: string;
  kind: "opening" | "call" | "reminder" | "closing";
  sim_ms: number;
  prompt: string;
  call_id: number | null;
  fields: Record<string, string>;
  steps: Step[];
  summary: string;
  duration_s: number | null;
  output_tokens: number | null;
  timed_out: boolean;
  ops: string[];
  decision?: Decision;
}

export interface MemoryEntry {
  turn: number;
  action_index: number | null;
  content: string;
  added: string[];
  removed: string[];
}

export interface Technician {
  id: number;
  name: string;
  city: string;
  lat: number;
  lon: number;
  skills: string[];
  color: string;
}

export interface Call {
  id: number;
  arrived_at: number;
  call_kind: string;
  status: string;
  terminal_reason: string | null;
  caller_name: string;
  segment: string;
  requested_service_name: string;
  urgent: number;
  created_job_id: number | null;
  lat: number | null;
  lon: number | null;
  service_city: string;
}

export interface Job {
  id: number;
  job_number: string;
  call_id: number;
  status: string;
  outcome: string | null;
  job_type: string;
  customer: string;
  segment: string;
  street: string;
  city: string;
  lat: number;
  lon: number;
  revenue_cents: number | null;
  material_cost_cents: number | null;
}

export interface Appointment {
  id: number;
  job_id: number;
  status: string;
  scheduled_start: number;
  scheduled_end: number;
  actual_start: number | null;
  actual_end: number | null;
  cancelled_at: number | null;
  created_at: number;
  technician_id: number | null;
}

export interface ApptEvent {
  appointment_id: number;
  technician_id: number | null;
  type: string;
  ts: number;
}

export interface Drive {
  tech: number;
  kind: string;
  lat0: number;
  lon0: number;
  lat1: number;
  lon1: number;
  t0: number;
  t1: number;
}

export interface Shift {
  tech: number;
  shift_type: string;
  t0: number;
  t1: number;
  service_date: string;
  on_call_rotation_id: number | null;
}

export interface LedgerEntry {
  ts: number;
  service_date: string;
  type: string;
  amount_cents: number;
  balance_after_cents: number;
  job_id: number | null;
  technician_id: number | null;
  description: string | null;
}

export interface World {
  technicians: Technician[];
  calls: Call[];
  jobs: Job[];
  appointments: Appointment[];
  events: ApptEvent[];
  drives: Drive[];
  shifts: Shift[];
  ledger: LedgerEntry[];
}

export interface Moment {
  sim_ms: number;
  turn?: number;
  kind: "good" | "bad" | "warn" | "learn";
  title: string;
}

export interface Artifact {
  name: string;
  origin: "recorded" | "rebuilt" | "missing";
  path: string | null;
  note: string;
}

export interface Replay {
  label: string;
  policy: string | null;
  report: {
    starting_cash_cents?: number;
    final_cash_cents?: number;
    score_cents?: number;
    daily_cash_cents?: [string, number][];
    first_service_date?: string;
    last_service_date?: string;
    run_status?: string;
  };
  sources: { artifacts: Artifact[]; world_verified: boolean | null; memory_exact: boolean };
  turns: Turn[];
  memory: MemoryEntry[];
  world: World | null;
  moments: Moment[];
}

export interface RunIndexEntry {
  label: string;
  policy: string | null;
  score_cents: number | null;
  has_field: boolean;
  world_verified: boolean | null;
}
