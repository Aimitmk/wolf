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

export type TraceRole =
  | "gameplay"
  | "npc_speech"
  | "npc_decision"
  | "voice_stt"
  | "text_analysis";

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

export interface ClaimedSeerResult {
  target_seat: number;
  is_wolf: boolean;
}

export interface ClaimedMediumResult {
  target_seat: number;
  is_wolf: boolean | null;
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
  /**
   * Structured seer-CO result attached to this utterance. Non-null when
   * the speaker announced a NEW divination outcome (real seer or wolf
   * fake-CO). The viewer renders the per-seat history (see
   * `GameSample.claim_history`) on top of these.
   */
  claimed_seer_result?: ClaimedSeerResult | null;
  claimed_medium_result?: ClaimedMediumResult | null;
  created_at_ms: number;
}

export interface ClaimedSeerHistoryEntry {
  day: number;
  target_seat: number;
  target_name: string;
  is_wolf: boolean;
  declared_at_event_id: string;
}

export interface ClaimedMediumHistoryEntry {
  day: number;
  target_seat: number;
  target_name: string;
  is_wolf: boolean | null;
  declared_at_event_id: string;
}

export interface ClaimHistoryEntry {
  claimer_seat: number;
  seer_claims: ClaimedSeerHistoryEntry[];
  medium_claims: ClaimedMediumHistoryEntry[];
}

/** 4-step suspicion scale.
 *
 * - `trust`:  村寄り信頼 (white-leaning)
 * - `low`:    弱い違和感
 * - `medium`: 明確に怪しい
 * - `high`:   処刑第一候補
 */
export type SuspicionLevel = "trust" | "low" | "medium" | "high";

export interface SuspicionEntry {
  /** Origin of the row: discussion speech vs vote / night decision. */
  source: "speech" | "vote";
  /** Speech-derived rows reference their parent SpeechEvent. Vote-derived
   * rows are null (no SpeechEvent backs the vote). */
  event_id: string | null;
  seq: number;
  day: number;
  /** Phase value at write time (DAY_DISCUSSION / DAY_VOTE / NIGHT etc). */
  phase: string;
  /** Vote round (0=normal, 1=runoff, -1=night seer-divine). Null for speech. */
  vote_round: number | null;
  suspecter_seat: number;
  target_seat: number;
  level: SuspicionLevel;
  reason: string;
  /** When non-null, this entry updates a prior suspicion of the same
   * (suspecter, target). The viewer can render an arrow showing the
   * shift; an LLM that silently reverses without setting this field is
   * the anti-fabrication red flag the timeline is designed to surface. */
  update_from_level: SuspicionLevel | null;
  update_reason: string | null;
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

// Wolf-only night coordination utterances. Stored in `logs_private`
// during play (one row per audience-seat fan-out) and deduped at
// export time so the viewer renders one entry per actual utterance.
// `wolf_chat_logs` is empty on day-side phases.
export interface WolfChatLog {
  actor_seat: number;
  text: string;
  created_at_ms: number;
}

export interface PhaseSection {
  day: number;
  phase: string;
  started_at_ms: number;
  public_logs: PublicLog[];
  speech_events: SpeechEvent[];
  votes: Vote[];
  night_actions: NightAction[];
  wolf_chat_logs?: WolfChatLog[];
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
  /**
   * Per-claimer ledger of every structured seer/medium claim, pre-folded
   * by the exporter so the viewer renders the "claim integrity" panel
   * without walking each phase's `speech_events`. Older exports (pre-
   * 2026-05-01) lack the field; the viewer treats absence as `[]`.
   */
  claim_history?: ClaimHistoryEntry[];
  /**
   * Public suspicion timeline (speech + vote derived). Chronological
   * order. Older exports (pre-2026-05-03) lack the field; the viewer
   * treats absence as `[]`.
   */
  suspicions?: SuspicionEntry[];
}
