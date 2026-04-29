"use client";

import * as React from "react";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import LightbulbIcon from "@mui/icons-material/Lightbulb";
import {
  findSeat,
  formatJstTime,
  formatLatency,
  formatTokens,
  nightActionJa,
  phaseJa,
  seatLabel,
  sourceJa,
} from "@/lib/format";
import type {
  ArbiterDecision,
  PhaseSection as PhaseSectionType,
  Seat,
  SpeechEvent,
  TraceEntry,
  Vote,
} from "@/lib/types";

export default function PhaseSection({
  phase,
  seats,
  trace,
  arbiterDecisions,
  onOpenTrace,
}: {
  phase: PhaseSectionType;
  seats: Seat[];
  trace: TraceEntry[];
  arbiterDecisions: ArbiterDecision[];
  onOpenTrace: (entry: TraceEntry) => void;
}) {
  const events = buildTimeline(phase, seats, trace, arbiterDecisions);

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
      <Stack
        direction="row"
        spacing={1}
        alignItems="center"
        justifyContent="space-between"
        sx={{ mb: 1.5 }}
      >
        <Stack direction="row" spacing={1} alignItems="baseline">
          <Typography variant="h6">
            Day {phase.day} — {phaseJa(phase.phase)}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {formatJstTime(phase.started_at_ms)} JST 開始
          </Typography>
        </Stack>
        <Stack direction="row" spacing={1}>
          {events.length > 0 && (
            <Chip
              size="small"
              label={`イベント ${events.length}`}
              variant="outlined"
            />
          )}
        </Stack>
      </Stack>

      {events.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          このフェイズにイベントはありません。
        </Typography>
      ) : (
        <Stack divider={<Divider />} spacing={0}>
          {events.map((ev, i) => (
            <EventRow
              key={i}
              event={ev}
              seats={seats}
              onOpenTrace={onOpenTrace}
            />
          ))}
        </Stack>
      )}
    </Paper>
  );
}

type TimelineEvent =
  | { kind: "log"; ts: number; data: PhaseSectionType["public_logs"][number] }
  | {
      kind: "speech";
      ts: number;
      data: SpeechEvent;
      trace: TraceEntry | null;
      arbiter: ArbiterDecision | null;
    }
  | { kind: "vote"; ts: number; data: Vote; trace: TraceEntry | null }
  | {
      kind: "night_action";
      ts: number;
      data: PhaseSectionType["night_actions"][number];
      trace: TraceEntry | null;
    };

function buildTimeline(
  phase: PhaseSectionType,
  seats: Seat[],
  trace: TraceEntry[],
  arbiterDecisions: ArbiterDecision[],
): TimelineEvent[] {
  const events: TimelineEvent[] = [];
  for (const log of phase.public_logs) {
    events.push({ kind: "log", ts: log.created_at_ms, data: log });
  }
  for (const sp of phase.speech_events) {
    events.push({
      kind: "speech",
      ts: sp.created_at_ms,
      data: sp,
      trace: matchTraceForSpeech(sp, phase, trace),
      arbiter: matchArbiterForSpeech(sp, phase, arbiterDecisions),
    });
  }
  for (const v of phase.votes) {
    events.push({
      kind: "vote",
      ts: v.submitted_at_ms,
      data: v,
      trace: matchTraceForVote(v, phase, trace, seats),
    });
  }
  for (const na of phase.night_actions) {
    events.push({
      kind: "night_action",
      ts: na.submitted_at_ms,
      data: na,
      trace: matchTraceForNightAction(na, phase, trace),
    });
  }
  events.sort((a, b) => a.ts - b.ts);
  return events;
}

/**
 * Match an `npc_generated` SpeechEvent back to the Master-side
 * `SpeakRequest` dispatch that produced it. The DB doesn't carry a
 * direct foreign key (request_id is in `npc_speak_results`, not in
 * `speech_events`), so we match on:
 *
 *   1. phase_id (canonical {gid}::dayN::PHASE::seq)
 *   2. seat_no — the NPC's assigned seat
 *   3. result_text equality with the spoken utterance — disambiguates
 *      multiple dispatches to the same seat in the same phase
 *
 * Falls back to the most recent dispatch for the seat in the phase
 * when text doesn't match (text may differ slightly: leading whitespace,
 * the NPC's first/last words trimmed, etc.). Returns `null` when the
 * speech is from a human / text channel, since arbiter dispatch only
 * fires for `npc_generated`.
 */
function matchArbiterForSpeech(
  sp: SpeechEvent,
  phase: PhaseSectionType,
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
  // Latest dispatch for the seat in this phase, by created_at_ms.
  return sameSeat.reduce((best, cur) =>
    cur.created_at_ms > best.created_at_ms ? cur : best,
  );
}

function matchTraceForSpeech(
  sp: SpeechEvent,
  phase: PhaseSectionType,
  trace: TraceEntry[],
): TraceEntry | null {
  if (sp.speaker_seat == null) return null;
  // Per-source matcher. Each speech source maps to a specific trace role
  // because the LLM call shape differs:
  //   npc_generated → role=npc_speech, response is a JSON envelope whose
  //                   `text` field equals the spoken utterance.
  //   voice_stt     → role=voice_stt, the analyzer step has the
  //                   transcription as user_prompt and JSON analysis as
  //                   response. Match by user_prompt+seat+segment.
  //   text          → role=text_analysis, user_prompt IS the typed message.
  const phaseMatches = (t: TraceEntry) =>
    t.day === phase.day &&
    (t.phase === phase.phase || t.phase?.includes(phase.phase));
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
    // The voice path emits two trace lines per segment (transcribe + analyze).
    // Prefer the analyze step because it carries both the transcript and the
    // structured fields the user wants to inspect.
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
  // source === "text"
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

function matchTraceForVote(
  v: Vote,
  phase: PhaseSectionType,
  trace: TraceEntry[],
  seats: Seat[],
): TraceEntry | null {
  const targetSeat = findSeat(seats, v.target_seat);
  if (!targetSeat) return null;
  return (
    trace.find(
      (t) =>
        t.role === "gameplay" &&
        t.day === phase.day &&
        t.phase === phase.phase &&
        actorSeat(t) === v.voter_seat &&
        t.metadata?.task === "vote" &&
        responseContains(t, `席${v.target_seat}`),
    ) ?? null
  );
}

function matchTraceForNightAction(
  na: PhaseSectionType["night_actions"][number],
  phase: PhaseSectionType,
  trace: TraceEntry[],
): TraceEntry | null {
  return (
    trace.find(
      (t) =>
        t.role === "gameplay" &&
        t.day === phase.day &&
        t.phase === phase.phase &&
        actorSeat(t) === na.actor_seat &&
        t.metadata?.task === "night_action",
    ) ?? null
  );
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

function EventRow({
  event,
  seats,
  onOpenTrace,
}: {
  event: TimelineEvent;
  seats: Seat[];
  onOpenTrace: (e: TraceEntry) => void;
}) {
  const time = formatJstTime(event.ts);

  if (event.kind === "log") {
    return (
      <Box sx={{ py: 1, display: "flex", gap: 1.5 }}>
        <TimeCell time={time} />
        <Box flex={1}>
          <Stack direction="row" spacing={1} alignItems="center">
            <Chip
              label={event.data.kind}
              size="small"
              variant="outlined"
              sx={{ height: 18, fontSize: 10 }}
            />
          </Stack>
          <Typography
            variant="body2"
            color="text.primary"
            sx={{ whiteSpace: "pre-wrap", mt: 0.25 }}
          >
            {event.data.text}
          </Typography>
        </Box>
      </Box>
    );
  }

  if (event.kind === "speech") {
    const speaker = findSeat(seats, event.data.speaker_seat);
    return (
      <Box sx={{ py: 1, display: "flex", gap: 1.5 }}>
        <TimeCell time={time} />
        <Box flex={1}>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.25 }}>
            <Typography variant="body2" sx={{ fontWeight: 500 }}>
              {speaker ? seatLabel(speaker) : "(不明)"}
            </Typography>
            <Chip
              label={sourceJa(event.data.source)}
              size="small"
              variant="outlined"
              color={event.data.source === "voice_stt" ? "secondary" : "default"}
              sx={{ height: 18, fontSize: 10 }}
            />
            {event.data.co_declaration && (
              <Chip
                label={`${event.data.co_declaration} CO`}
                size="small"
                color="warning"
                sx={{ height: 18, fontSize: 10 }}
              />
            )}
            {event.data.stt_confidence != null && (
              <Tooltip title="STT confidence">
                <Chip
                  label={`conf ${event.data.stt_confidence.toFixed(2)}`}
                  size="small"
                  variant="outlined"
                  sx={{ height: 18, fontSize: 10 }}
                />
              </Tooltip>
            )}
            {event.arbiter && <ArbiterChip decision={event.arbiter} />}
          </Stack>
          <Typography variant="body2">{event.data.text}</Typography>
          {event.data.summary && (
            <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
              要約: {event.data.summary}
            </Typography>
          )}
          {event.arbiter && <ArbiterDetail decision={event.arbiter} />}
        </Box>
        <TraceButton entry={event.trace} onOpen={onOpenTrace} />
      </Box>
    );
  }

  if (event.kind === "vote") {
    const voter = findSeat(seats, event.data.voter_seat);
    const target = findSeat(seats, event.data.target_seat);
    return (
      <Box sx={{ py: 1, display: "flex", gap: 1.5 }}>
        <TimeCell time={time} />
        <Box flex={1}>
          <Stack direction="row" spacing={1} alignItems="center">
            <Chip label="投票" size="small" sx={{ height: 18, fontSize: 10 }} />
            <Typography variant="body2">
              {voter ? seatLabel(voter) : "?"} → {target ? seatLabel(target) : "棄権"}
            </Typography>
          </Stack>
        </Box>
        <TraceButton entry={event.trace} onOpen={onOpenTrace} />
      </Box>
    );
  }

  // night_action
  const actor = findSeat(seats, event.data.actor_seat);
  const target = findSeat(seats, event.data.target_seat);
  return (
    <Box sx={{ py: 1, display: "flex", gap: 1.5 }}>
      <TimeCell time={time} />
      <Box flex={1}>
        <Stack direction="row" spacing={1} alignItems="center">
          <Chip
            label={nightActionJa(event.data.kind)}
            size="small"
            color="info"
            sx={{ height: 18, fontSize: 10 }}
          />
          <Typography variant="body2">
            {actor ? seatLabel(actor) : "?"} → {target ? seatLabel(target) : "—"}
          </Typography>
        </Stack>
      </Box>
      <TraceButton entry={event.trace} onOpen={onOpenTrace} />
    </Box>
  );
}

function TimeCell({ time }: { time: string }) {
  return (
    <Typography
      variant="caption"
      color="text.secondary"
      sx={{ minWidth: 64, fontFamily: "monospace", pt: 0.25 }}
    >
      {time}
    </Typography>
  );
}

const ARBITER_REASON_JA: Record<string, string> = {
  addressed: "指名",
  silent_rotation: "未発言ローテ",
  lru_rotation: "LRU ローテ",
  low_info_diversion: "応酬迂回",
  all_demoted_fallback: "全降格フォールバック",
  seat_tiebreak: "席順",
};

const ARBITER_REASON_TIP: Record<string, string> = {
  addressed:
    "直前の発言で addressed_seat_no がこの NPC の席だったため最優先で選ばれた",
  silent_rotation:
    "このフェーズでまだ発言していない NPC を優先して選んだ",
  lru_rotation:
    "全員が一度発言済み — 直前の話者を除外し、席番号の若い順で選んだ",
  low_info_diversion:
    "2席だけが応酬し情報が増えていない (CO等なし) ため、両者を降格して別の NPC を選んだ",
  all_demoted_fallback:
    "降格対象しか候補が残っていなかったため、降格対象から拾った (オフライン等で第3者不在の縮退)",
  seat_tiebreak:
    "他の候補が無く、席番号の若い順で選んだ (通常 1 NPC のみ生存時の縮退)",
};

function ArbiterChip({ decision }: { decision: ArbiterDecision }) {
  const label = decision.selection_reason
    ? ARBITER_REASON_JA[decision.selection_reason] ?? decision.selection_reason
    : "発話選定";
  const tip =
    decision.selection_reason &&
    ARBITER_REASON_TIP[decision.selection_reason]
      ? ARBITER_REASON_TIP[decision.selection_reason]
      : "Master が SpeakRequest を送出した記録";
  return (
    <Tooltip title={tip}>
      <Chip
        label={`発話選定: ${label}`}
        size="small"
        color="info"
        variant="outlined"
        sx={{ height: 18, fontSize: 10 }}
      />
    </Tooltip>
  );
}

function ArbiterDetail({ decision }: { decision: ArbiterDecision }) {
  const snap = decision.public_state_snapshot;
  const addressed =
    snap && typeof snap.last_addressed_seat === "number"
      ? `席${snap.last_addressed_seat}`
      : "なし";
  const silent = Array.isArray(snap?.silent_seats)
    ? `[${(snap!.silent_seats as number[]).join(", ")}]`
    : "—";
  const onlineNpcs = Array.isArray(snap?.online_npc_seats)
    ? `[${(snap!.online_npc_seats as number[]).join(", ")}]`
    : "—";
  const latencyMs =
    decision.result_received_at_ms != null
      ? decision.result_received_at_ms - decision.created_at_ms
      : null;
  const playbackMs =
    decision.playback_finished_at_ms != null &&
    decision.result_received_at_ms != null
      ? decision.playback_finished_at_ms - decision.result_received_at_ms
      : null;
  const status = decision.result_status ?? "in-flight";
  const failure =
    decision.result_failure_reason ??
    decision.playback_failure_reason ??
    null;
  return (
    <Box
      sx={{
        mt: 0.5,
        pl: 1,
        borderLeft: "2px solid",
        borderColor: "info.light",
        fontSize: 11,
      }}
    >
      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ display: "block", lineHeight: 1.6 }}
      >
        addressed={addressed} silent={silent} online_npcs={onlineNpcs}
      </Typography>
      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ display: "block", lineHeight: 1.6 }}
      >
        result={status}
        {latencyMs != null && ` (LLM ${latencyMs}ms)`}
        {playbackMs != null && ` / 再生 ${playbackMs}ms`}
        {decision.tts_outcome && ` / TTS ${decision.tts_outcome}`}
        {failure && (
          <Box component="span" sx={{ color: "error.main", ml: 0.5 }}>
            ・失敗理由: {failure}
          </Box>
        )}
      </Typography>
    </Box>
  );
}

function TraceButton({
  entry,
  onOpen,
}: {
  entry: TraceEntry | null;
  onOpen: (e: TraceEntry) => void;
}) {
  if (!entry) {
    return <Box sx={{ width: 32 }} />;
  }
  const tokens = entry.tokens?.total ?? null;
  return (
    <Tooltip
      title={
        <Box sx={{ fontSize: 11 }}>
          <div>{entry.model}</div>
          <div>tokens: {formatTokens(tokens)}</div>
          <div>latency: {formatLatency(entry.latency_ms)}</div>
        </Box>
      }
    >
      <IconButton
        size="small"
        onClick={() => onOpen(entry)}
        aria-label="show LLM prompt"
      >
        <LightbulbIcon fontSize="small" color="warning" />
      </IconButton>
    </Tooltip>
  );
}
