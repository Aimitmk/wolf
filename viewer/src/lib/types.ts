// TypeScript shape of the per-game export consumed by the viewer.
// Source of truth: viewer/sample-data/generate_sample.py.

export type RoleKey =
  | "VILLAGER"
  | "WEREWOLF"
  | "MADMAN"
  | "SEER"
  | "MEDIUM"
  | "KNIGHT";

export type DeathCause = "EXECUTED" | "ATTACK" | null;

export type DiscussionMode = "rounds" | "reactive_voice";

export type SpeechSource = "rounds_text" | "voice_stt" | "npc_generated";

export type CoDeclaration = "seer" | "medium" | "knight" | null;

export type TraceRole = "gameplay" | "npc_speech" | "voice_stt";

export interface Seat {
  seat_no: number;
  display_name: string;
  is_llm: boolean;
  persona_key: string | null;
  discord_user_id: string | null;
  role: RoleKey;
  alive: boolean;
  death_cause: DeathCause;
  death_day: number | null;
}

export interface PublicLog {
  kind: string;
  actor_seat: number | null;
  text: string;
  created_at_ms: number;
}

export interface SpeechEvent {
  event_id: string;
  source: SpeechSource;
  speaker_seat: number | null;
  text: string;
  stt_confidence: number | null;
  summary: string | null;
  co_declaration: CoDeclaration;
  addressed_seat_no: number | null;
  created_at_ms: number;
}

export interface Vote {
  day: number;
  round: number;
  voter_seat: number;
  target_seat: number | null;
  submitted_at_ms: number;
}

export interface NightAction {
  day: number;
  actor_seat: number;
  kind: string; // ATTACK / DIVINE / GUARD / DIVINE_NIGHT0_RANDOM_WHITE
  target_seat: number | null;
  submitted_at_ms: number;
}

export interface PhaseSection {
  day: number;
  phase: string;
  started_at_ms: number;
  public_logs: PublicLog[];
  speech_events: SpeechEvent[];
  votes: Vote[];
  night_actions: NightAction[];
}

export interface TokenUsage {
  prompt: number | null;
  completion: number | null;
  total: number | null;
}

export interface TraceEntry {
  ts: string;
  role: TraceRole;
  provider: string;
  model: string;
  phase: string | null;
  day: number | null;
  actor: string | null;
  system_prompt: string;
  user_prompt: string;
  response: string | null;
  latency_ms: number;
  tokens: TokenUsage | null;
  error: string | null;
  metadata?: Record<string, unknown> | null;
  // Synthetic field set by the sample generator for npc/voice rows; viewer
  // tolerates its absence on real-world traces.
  file_stem?: string;
}

export interface GameSample {
  game: {
    id: string;
    guild_id: string;
    host_user_id: string;
    discussion_mode: DiscussionMode;
    created_at_ms: number;
    ended_at_ms: number | null;
    victory: "village" | "wolf" | null;
    main_text_channel_id: string;
    main_vc_channel_id: string;
  };
  seats: Seat[];
  phases: PhaseSection[];
  trace: TraceEntry[];
}
