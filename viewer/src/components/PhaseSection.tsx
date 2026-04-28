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
  onOpenTrace,
}: {
  phase: PhaseSectionType;
  seats: Seat[];
  trace: TraceEntry[];
  onOpenTrace: (entry: TraceEntry) => void;
}) {
  const events = buildTimeline(phase, seats, trace);

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
  | { kind: "speech"; ts: number; data: SpeechEvent; trace: TraceEntry | null }
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

function matchTraceForSpeech(
  sp: SpeechEvent,
  phase: PhaseSectionType,
  trace: TraceEntry[],
): TraceEntry | null {
  if (sp.speaker_seat == null) return null;
  // Heuristic: pick the trace entry whose actor mentions the same seat # and
  // whose phase matches and that hasn't been claimed yet by a closer event.
  return (
    trace.find(
      (t) =>
        t.role !== "voice_stt" &&
        t.day === phase.day &&
        (t.phase === phase.phase || t.phase?.includes(phase.phase)) &&
        actorSeat(t) === sp.speaker_seat &&
        responseContains(t, sp.text.slice(0, 20)),
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
          </Stack>
          <Typography variant="body2">{event.data.text}</Typography>
          {event.data.summary && (
            <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
              要約: {event.data.summary}
            </Typography>
          )}
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
