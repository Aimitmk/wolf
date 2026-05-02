// Pure trace ↔ event matchers. Extracted from the original PhaseSection
// inline implementation so the server can precompute the matches at game
// load time and ship only `{eventKey: traceIndex}` to the client. The
// client never needs to look at heavy ``system_prompt`` / ``user_prompt``
// / ``response`` strings to build the lightbulb buttons — the lookup is
// O(1) by event key.
//
// Why this lives here and not in `PhaseSection`:
// * It must be importable from a Node-only ``loadGame`` path (no React).
// * Both server (precompute) and the trace API route handler need the
//   same logic to keep matches deterministic across redeploys.

import type {
  ArbiterDecision,
  NightAction,
  PhaseSection,
  Seat,
  SpeechEvent,
  TraceEntry,
  Vote,
} from "./types";

export type WolfChatLog = NonNullable<PhaseSection["wolf_chat_logs"]>[number];

/**
 * Two parallel maps of ``eventKey → traceIndex``. Built once at server
 * render time per game and shipped down with the slim payload.
 *
 * Keys are stable strings so the maps survive JSON serialization and
 * map cleanly into ``Record<string, number>`` for the wire.
 *
 * * ``trace`` — primary map. Speech / vote / action / wolf-chat events.
 * * ``arbiter`` — secondary map. Speech events → arbiter decision id.
 *   Kept separate because the value type differs (request_id, not int).
 */
export interface MatchMaps {
  trace: Record<string, number>;
  arbiter: Record<string, string>;
}

// ───────────────────────────────────────────── event-key derivation
//
// Each event-trace pair needs a deterministic string key. Speech events
// already carry a server-issued ``event_id``; the others get composed
// from their natural primary key columns.

export function speechKey(sp: SpeechEvent): string {
  return `speech:${sp.event_id}`;
}

export function voteKey(v: Vote): string {
  // (day, round, voter, target) is the unique key in the votes table.
  // Include ``submitted_at_ms`` so a re-vote (same voter changes target)
  // doesn't collide with the original.
  return `vote:${v.day}:${v.round}:${v.voter_seat}:${v.target_seat ?? "null"}:${v.submitted_at_ms}`;
}

export function nightActionKey(na: NightAction): string {
  return `night:${na.day}:${na.actor_seat}:${na.kind}:${na.target_seat ?? "null"}:${na.submitted_at_ms}`;
}

export function wolfChatKey(wc: WolfChatLog): string {
  // No native id; compose from (actor, ts, text-prefix). Text prefix
  // keeps two same-second utterances disambiguated.
  return `wolfchat:${wc.actor_seat}:${wc.created_at_ms}:${wc.text.slice(0, 20)}`;
}

// ───────────────────────────────────────────── precompute entry point

/**
 * Precompute every ``event → trace_index`` and ``speech → arbiter``
 * mapping for a game. Single linear pass over each phase; each matcher
 * is the same one PhaseSection used to run client-side, lifted verbatim
 * out of that file.
 */
export function buildMatchMaps(
  phases: PhaseSection[],
  trace: TraceEntry[],
  arbiterDecisions: ArbiterDecision[],
  seats: Seat[],
): MatchMaps {
  const traceMap: Record<string, number> = {};
  const arbiterMap: Record<string, string> = {};

  for (const phase of phases) {
    for (const sp of phase.speech_events) {
      const t = matchTraceForSpeech(sp, phase, trace);
      if (t !== null) {
        const idx = trace.indexOf(t);
        if (idx >= 0) traceMap[speechKey(sp)] = idx;
      }
      const a = matchArbiterForSpeech(sp, phase, arbiterDecisions);
      if (a !== null) arbiterMap[speechKey(sp)] = a.request_id;
    }
    for (const v of phase.votes) {
      const t = matchTraceForVote(v, phase, trace, seats);
      if (t !== null) {
        const idx = trace.indexOf(t);
        if (idx >= 0) traceMap[voteKey(v)] = idx;
      }
    }
    for (const na of phase.night_actions) {
      const t = matchTraceForNightAction(na, phase, trace);
      if (t !== null) {
        const idx = trace.indexOf(t);
        if (idx >= 0) traceMap[nightActionKey(na)] = idx;
      }
    }
    for (const wc of phase.wolf_chat_logs ?? []) {
      const t = matchTraceForWolfChat(wc, phase, trace);
      if (t !== null) {
        const idx = trace.indexOf(t);
        if (idx >= 0) traceMap[wolfChatKey(wc)] = idx;
      }
    }
  }
  return { trace: traceMap, arbiter: arbiterMap };
}

// ───────────────────────────────────────────── matchers (pure)

/**
 * Match an `npc_generated` SpeechEvent back to the Master-side
 * `SpeakRequest` dispatch that produced it. The DB doesn't carry a
 * direct foreign key, so we match on (phase_id, seat_no, result_text)
 * and fall back to the latest dispatch for the seat in the phase.
 */
export function matchArbiterForSpeech(
  sp: SpeechEvent,
  phase: PhaseSection,
  decisions: ArbiterDecision[],
): ArbiterDecision | null {
  if (sp.source !== "npc_generated" || sp.speaker_seat == null) return null;
  const phaseMatches = (d: ArbiterDecision) =>
    d.phase_id.includes(`::day${phase.day}::${phase.phase}`);
  const seatMatches = (d: ArbiterDecision) => d.seat_no === sp.speaker_seat;
  const exactText =
    decisions.find(
      (d) =>
        phaseMatches(d) && seatMatches(d) && d.result_text === sp.text,
    ) ?? null;
  if (exactText) return exactText;
  const sameSeat = decisions.filter((d) => phaseMatches(d) && seatMatches(d));
  if (sameSeat.length === 0) return null;
  return sameSeat.reduce((best, cur) =>
    cur.created_at_ms > best.created_at_ms ? cur : best,
  );
}

/**
 * Match a SpeechEvent to its trace by source-specific role + needle
 * search on either the response (npc_generated) or the user prompt
 * (voice_stt / text). Required heavy fields: `response`,
 * `user_prompt`. Run server-side BEFORE those fields are stripped.
 */
export function matchTraceForSpeech(
  sp: SpeechEvent,
  phase: PhaseSection,
  trace: TraceEntry[],
): TraceEntry | null {
  if (sp.speaker_seat == null) return null;
  const phaseMatches = (t: TraceEntry) =>
    t.day === phase.day &&
    (t.phase === phase.phase || (t.phase?.includes(phase.phase) ?? false));
  const needle = sp.text.slice(0, 20);

  if (sp.source === "npc_generated") {
    return (
      trace.find(
        (t) =>
          t.role === "npc_speech" &&
          phaseMatches(t) &&
          actorSeat(t) === sp.speaker_seat &&
          responseContains(t, needle),
      ) ?? null
    );
  }
  if (sp.source === "voice_stt") {
    const analyze =
      trace.find(
        (t) =>
          t.role === "voice_stt" &&
          phaseMatches(t) &&
          actorSeat(t) === sp.speaker_seat &&
          t.metadata?.step === "analyze" &&
          userPromptContains(t, needle),
      ) ?? null;
    if (analyze !== null) return analyze;
    return (
      trace.find(
        (t) =>
          t.role === "voice_stt" &&
          phaseMatches(t) &&
          actorSeat(t) === sp.speaker_seat &&
          userPromptContains(t, needle),
      ) ?? null
    );
  }
  return (
    trace.find(
      (t) =>
        t.role === "text_analysis" &&
        phaseMatches(t) &&
        actorSeat(t) === sp.speaker_seat &&
        userPromptContains(t, needle),
    ) ?? null
  );
}

export function matchTraceForVote(
  v: Vote,
  phase: PhaseSection,
  trace: TraceEntry[],
  seats: Seat[],
): TraceEntry | null {
  const targetSeat = seats.find((s) => s.seat_no === v.target_seat);
  if (!targetSeat) return null;
  return (
    trace.find(
      (t) =>
        isDecisionRole(t) &&
        phaseMatchesLoose(t, phase) &&
        actorSeat(t) === v.voter_seat &&
        t.metadata?.task === "vote" &&
        (t.role !== "gameplay" || responseContains(t, `席${v.target_seat}`)),
    ) ?? null
  );
}

export function matchTraceForNightAction(
  na: NightAction,
  phase: PhaseSection,
  trace: TraceEntry[],
): TraceEntry | null {
  const naKindLower =
    typeof na.kind === "string" ? na.kind.toLowerCase() : null;
  return (
    trace.find((t) => {
      if (!isDecisionRole(t)) return false;
      if (!phaseMatchesLoose(t, phase)) return false;
      if (actorSeat(t) !== na.actor_seat) return false;
      if (t.metadata?.task !== "night_action") return false;
      const traceKind = t.metadata?.action_kind;
      if (traceKind == null) return true;
      return (
        typeof traceKind === "string" &&
        naKindLower != null &&
        traceKind.toLowerCase() === naKindLower
      );
    }) ?? null
  );
}

export function matchTraceForWolfChat(
  wc: WolfChatLog,
  phase: PhaseSection,
  trace: TraceEntry[],
): TraceEntry | null {
  let best: TraceEntry | null = null;
  let bestDelta = Infinity;
  for (const t of trace) {
    if (!isDecisionRole(t)) continue;
    if (!phaseMatchesLoose(t, phase)) continue;
    if (actorSeat(t) !== wc.actor_seat) continue;
    if (t.metadata?.task !== "wolf_chat") continue;
    const ts = t.ts ? new Date(t.ts).getTime() : 0;
    const delta = Math.abs(ts - wc.created_at_ms);
    if (delta < bestDelta) {
      best = t;
      bestDelta = delta;
    }
  }
  return best;
}

// ───────────────────────────────────────────── helpers

function isDecisionRole(t: TraceEntry): boolean {
  return t.role === "gameplay" || t.role === "npc_decision";
}

function phaseMatchesLoose(t: TraceEntry, phase: PhaseSection): boolean {
  if (t.day !== phase.day) return false;
  if (t.phase === phase.phase) return true;
  return typeof t.phase === "string" && t.phase.includes(`::${phase.phase}`);
}

function actorSeat(t: TraceEntry): number | null {
  if (!t.actor) return null;
  const m = t.actor.match(/seat=(\d+)/);
  return m ? Number(m[1]) : null;
}

function responseContains(t: TraceEntry, needle: string): boolean {
  return t.response != null && t.response.includes(needle);
}

function userPromptContains(t: TraceEntry, needle: string): boolean {
  return typeof t.user_prompt === "string" && t.user_prompt.includes(needle);
}
