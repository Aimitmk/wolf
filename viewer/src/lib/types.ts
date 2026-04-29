// TypeScript shape of the per-game export consumed by the viewer.
// Source of truth: viewer/sample-data/generate_sample.py.

export type RoleKey =
  | "VILLAGER"
  | "WEREWOLF"
  | "MADMAN"
  | "SEER"
  | "MEDIUM"
  | "KNIGHT";

export type DeathCause = "EXECUTION" | "ATTACK" | null;

export type DiscussionMode = "rounds" | "reactive_voice";

export type SpeechSource = "text" | "voice_stt" | "npc_generated";

export type CoDeclaration = "seer" | "medium" | "knight" | null;

export type TraceRole = "gameplay" | "npc_speech" | "voice_stt" | "text_analysis";

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

/**
 * One Master-side `SpeakRequest` dispatch — the "why this NPC, why now"
 * breadcrumb the viewer surfaces alongside the resulting NPC speech.
 *
 * `selection_reason` is one of:
 *   - "addressed":       state.last_addressed_seat matched this NPC's seat
 *   - "silent_rotation": no addressed seat; this NPC hadn't yet spoken in the phase
 *   - "seat_tiebreak":   all online NPCs already spoke this phase; lowest seat wins
 *
 * Older games (pre-migration) have `selection_reason=null`. Result and
 * playback fields are LEFT-joined and may be `null` for in-flight or
 * rejected requests.
 */
export interface ArbiterDecision {
  request_id: string;
  phase_id: string;
  npc_id: string;
  seat_no: number;
  suggested_intent: string;
  selection_reason: string | null;
  public_state_snapshot: Record<string, unknown> | null;
  logic_packet_id: string;
  created_at_ms: number;
  expires_at_ms: number;
  result_status: string | null;
  result_text: string | null;
  result_intent: string | null;
  result_failure_reason: string | null;
  result_received_at_ms: number | null;
  playback_outcome: string | null;
  playback_failure_reason: string | null;
  playback_finished_at_ms: number | null;
  tts_outcome: string | null;
  tts_duration_ms: number | null;
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
  arbiter_decisions?: ArbiterDecision[];
}
