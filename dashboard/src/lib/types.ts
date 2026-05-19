// ------------------------------------------------------------
// TypeScript interfaces for all API response types used by the
// dashboard, extracted from dashboard.html.
// ------------------------------------------------------------

/** A node in the idea graph returned by GET /api/v1/graph */
export interface IdeaNode {
  id: number;
  description: string;
  status: string;          // 'active' | 'concluded' | 'abandoned' | 'suggested'
  source?: string;         // 'agent' | 'human'
  priority?: string;       // 'normal' | 'high'
  branch: string;
  conclusion?: string;
  created_at: string;
  first_start?: string;
  last_finish?: string;
  has_running?: boolean;
  has_queued?: boolean;
  parent_ids?: number[];
  /** Populated when fetching a single idea with ?notes=all */
  notes?: Note[];
  /** Populated when fetching a single idea */
  experiments?: Experiment[];
}

/** An edge in the idea graph */
export interface GraphEdge {
  from: number;
  to: number;
}

/** Response from GET /api/v1/graph */
export interface GraphResponse {
  nodes: IdeaNode[];
  edges: GraphEdge[];
}

/** Response from GET /api/v1/backlog */
export interface BacklogResponse {
  active_ideas: unknown[];
  total_running: number;
  total_pending: number;
  current_branch: string;
}

/** An experiment as returned in chart-data and idea detail endpoints */
export interface Experiment {
  id: number;
  idea_id: number;
  seq?: number;
  label?: string;
  description: string;
  status: string;          // 'running' | 'completed' | 'failed' | 'cancelled' | 'pending'
  idea_status?: string;
  idea_description?: string;
  metrics?: Record<string, number>;
  meta?: Record<string, unknown>;
  tags?: string[];
  started_at?: string;
  finished_at?: string;
  created_at?: string;
  runtime?: string;
  has_output?: boolean;
  /** Synthetic flag added by loadChartData for running experiments */
  _running?: boolean;
}

/** Response from GET /api/v1/chart-data */
export interface ChartDataResponse {
  experiments: Experiment[];
  running: Experiment[];
}

/** A note attached to an idea */
export interface Note {
  text: string;
  level?: string;          // e.g. 'observation', 'decision', 'result'
  created_at?: string;
}

/** Minimal typing for a parsed OpenAPI spec */
export interface OpenAPISpec {
  paths: Record<string, Record<string, OpenAPIOperation>>;
  components?: {
    schemas?: Record<string, unknown>;
  };
  [key: string]: unknown;
}

export interface OpenAPIOperation {
  summary?: string;
  operationId?: string;
  parameters?: unknown[];
  requestBody?: unknown;
  responses?: Record<string, unknown>;
  [key: string]: unknown;
}

// ---- Layout types produced by buildSubwayLayout ----

export interface Lane {
  ideas: number[];
  lastActivity: string;
}

export interface LaneChildEntry {
  childLane: number;
  forkCol: number;
}

export interface SubwayLayout {
  nodeMap: Record<number, IdeaNode>;
  childMap: Record<number, number[]>;
  depth: Record<number, number>;
  ideaLane: Record<number, number>;
  laneRow: Record<number, number>;
  laneChildren: Record<number, LaneChildEntry[]>;
  lanes: Lane[];
  numLanes: number;
  maxDepth: number;
}

/** Position of a station in the rendered graph */
export interface StationPos {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Chart data returned by buildChartData */
export interface ChartDataResult {
  labels: string[];
  pointColors: string[];
  pointBgColors: string[];
  pointStyles: string[];
  pointRadii: number[];
  pointBorderWidths: number[];
  values: number[];
  expData: Experiment[];
}

/** Response from GET /api/v1/experiments/:id/progress */
export interface ProgressResponse {
  progress: Record<string, unknown> | null;
}

/** Request body for POST /api/v1/ideas/suggest */
export interface SuggestIdeaRequest {
  description: string;
  parent_ids?: number[];
  priority?: "normal" | "high";
  resources?: { url: string; label: string }[];
}

export interface SandboxCapabilities {
  available: boolean;
  missing: string[];
  details: string;
}

export interface SandboxObservedEntry {
  host: string;
  port: number;
  kind: string;
  ips: string[];
  labels: string[];
  attempts: number;
  allowed: number;
  blocked: number;
  first_seen?: string;
  last_seen?: string;
  top_reason: string;
}

export interface SandboxFileBind {
  path: string;
  mode: "rw" | "ro";
}

export interface PromptMeta {
  role: string;
  size: number;
  updated_at: string;
}

export interface MessageEntry {
  id: number;
  from_agent: string | null;
  from_role: string | null;
  /** "agent:<id>" | "role:<role>" | "all" */
  to: string;
  text: string;
  created_at: string;
  read_by: string[];
}

export interface AgentEntry {
  agent_id: string;
  role: string;
  branch: string;
  parent_branch: string;
  worktree: string;
  pid: number | null;
  created_at: string;
  /** Live branch currently checked out in the agent's worktree (may differ
   *  from `branch`, which is the initial agent_init_<id>). */
  current_branch?: string;
  /** When current_branch matches idea/<N>, summary of that idea. */
  current_idea?: { id: number; description: string; status: string };
  /** Count of unread messages addressed to this agent. */
  unread_messages?: number;
}

export interface SandboxState {
  enabled: boolean;
  mode: string;
  allowlist: string[];
  denylist: string[];
  file_rw: string[];
  file_ro: string[];
  builtin_allowlist: string[];
  builtin_file_rw: string[];
  builtin_file_ro: string[];
  builtin_file_binds: SandboxFileBind[];
  capabilities: SandboxCapabilities;
  observed: SandboxObservedEntry[];
}

/** Full idea detail (single idea endpoint with notes + experiments) */
export type IdeaDetail = IdeaNode;

/** Response shape from GET /api/v1/ideas/:id/experiments */
export type IdeaExperimentsResponse = Experiment[];

/** A queued, running, or recently-finished experiment as returned by /queue */
export interface QueueExp {
  id: string | number;
  label: string;
  idea_id: number;
  description: string;
  status: string;
  priority: number;
  requirements: { units?: number; kind: string; tags: string[] };
  depends_on: string[];
  created_at: string;
  started_at: string | null;
  queued_at: string | null;
  finished_at?: string | null;
  tags: string[];
  assigned_resource: string | null;
  assigned_units: number[] | null;
  /** Populated for finished rows in the `recent` history section. */
  error?: string | null;
  metrics?: Record<string, unknown> | null;
}

/** A holder of a unit slot on a resource */
export interface ResourceHolder {
  experiment_label: string;
  units: number[];
}

/** Computed utilization for a resource */
export interface ResourceUtilization {
  capacity: number;
  in_use_units: number;
  free_units: number;
  running_jobs: number;
  max_parallel_jobs: number;
  default_units_per_job: number;
  holders: ResourceHolder[];
}

/** A queue resource (e.g. a GPU pool) returned by GET /api/v1/resources */
export interface ResourceState {
  name: string;
  kind: string;
  unit_kind: string;
  capacity: number;
  jobs_per_unit: number;
  default_units_per_job: number;
  max_parallel_jobs: number;
  tags: string[];
  executor_config: Record<string, unknown>;
  utilization: ResourceUtilization;
}

/** Snapshot returned by GET /api/v1/queue */
export interface QueueSnapshot {
  queued: QueueExp[];
  running: QueueExp[];
  /** Recent history: completed / failed / cancelled, newest first. */
  recent?: QueueExp[];
  resources: ResourceState[];
  config: { paused: boolean; dispatch_interval_s: number };
}

/** Body for PUT /api/v1/resources/:name */
export interface ResourceUpsertBody {
  name: string;
  kind: string;
  unit_kind: string;
  capacity: number;
  jobs_per_unit: number;
  tags: string[];
  executor_config: Record<string, unknown>;
}

/** A log entry built from idea, experiment, and note data */
export interface LogEntry {
  type: string;
  time: string;
  ideaId: number;
  title: string;
  body: string;
  status?: string;
  metrics?: Record<string, number>;
  runtime?: string;
  extra?: string;
}
