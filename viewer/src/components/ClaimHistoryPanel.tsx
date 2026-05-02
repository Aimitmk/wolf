"use client";

/**
 * ClaimHistoryPanel — per-seat ledger of every divination/medium claim
 * each CO'd seat made in this game.
 *
 * Why this panel exists
 * ---------------------
 * Wolves (and the madman) faking a seer/medium CO have to keep their
 * announced results consistent across phases. Without a per-seat
 * ledger surfaced visibly, drift goes undetected: in game
 * `a51615d32274` (2026-04-30) the wolf seer Yuriko declared
 * "シゲミチ白" on day 1, then on day 2 silently dropped シゲミチ and
 * grafted "コメット白" from her wolf-partner's claim, with nobody at
 * the table noticing. This panel stacks every claim against day +
 * expected count so the discrepancy reads at a glance:
 *
 *   - Day-N integrity rule (seer): a real seer at day N's morning
 *     should have exactly N cumulative results (NIGHT_0's random
 *     white declared on day 1 + one per subsequent night, each
 *     surfaced the morning after). The header chip shows
 *     "通算 X / 期待 Y" and turns red when X ≠ Y.
 *   - Per-claim row: day, target, verdict (黒/白). The wolf badge
 *     contrast against the claimer's actual role makes fake CO obvious
 *     post-game.
 *
 * Pure presentation: reads `data.claim_history` (pre-folded by the
 * exporter) plus `data.seats` for role/name resolution. Older exports
 * (pre-2026-05-01) lack `claim_history` and the panel collapses to a
 * compact "no claims recorded" state rather than blowing up.
 */

import * as React from "react";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import type {
  ClaimHistoryEntry,
  ClaimedMediumHistoryEntry,
  ClaimedSeerHistoryEntry,
  GameSample,
  Seat,
} from "@/lib/types";

// Older exports (pre-2026-05-02) kept one ledger row per speech
// utterance, so an LLM seat that restated the same divination /
// medium result across multiple discussion rounds appears as
// repeated rows. The Python collector now dedupes at export time;
// this guard re-runs the same fold in-browser so legacy JSON files
// also render cleanly.
function dedupeSeerClaims(
  claims: readonly ClaimedSeerHistoryEntry[],
): ClaimedSeerHistoryEntry[] {
  const seen = new Set<string>();
  const out: ClaimedSeerHistoryEntry[] = [];
  for (const c of claims) {
    const key = `${c.day}|${c.target_seat}|${c.is_wolf ? 1 : 0}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(c);
  }
  return out;
}

function dedupeMediumClaims(
  claims: readonly ClaimedMediumHistoryEntry[],
): ClaimedMediumHistoryEntry[] {
  const seen = new Set<string>();
  const out: ClaimedMediumHistoryEntry[] = [];
  for (const c of claims) {
    const verdict = c.is_wolf === null ? "n" : c.is_wolf ? "1" : "0";
    const key = `${c.day}|${c.target_seat}|${verdict}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(c);
  }
  return out;
}

const CO_LABEL: Record<string, string> = {
  seer: "占い",
  medium: "霊媒",
  knight: "騎士",
};

export default function ClaimHistoryPanel({ data }: { data: GameSample }) {
  const history = data.claim_history ?? [];
  if (history.length === 0) {
    return (
      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="h6">CO 結果履歴</Typography>
        <Typography variant="body2" sx={{ color: "text.secondary", mt: 1 }}>
          このゲームには記録された占い/霊媒 CO 結果がありません。
        </Typography>
      </Paper>
    );
  }

  const seatLookup = new Map<number, Seat>(data.seats.map((s) => [s.seat_no, s]));
  const dayCount = computeDayCount(data);

  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Typography variant="h6" sx={{ mb: 0.5 }}>
        CO 結果履歴
      </Typography>
      <Typography variant="caption" sx={{ color: "text.secondary" }}>
        各 CO 者が公表した占い/霊媒結果の累積。期待件数と一致しない場合、
        嘘 CO の可能性が高い。
      </Typography>
      <Stack spacing={1.5} sx={{ mt: 1.5 }}>
        {history.map((entry) => (
          <ClaimerRow
            key={entry.claimer_seat}
            entry={entry}
            claimer={seatLookup.get(entry.claimer_seat)}
            seatLookup={seatLookup}
            currentDay={dayCount}
          />
        ))}
      </Stack>
    </Paper>
  );
}

function ClaimerRow({
  entry,
  claimer,
  seatLookup,
  currentDay,
}: {
  entry: ClaimHistoryEntry;
  claimer: Seat | undefined;
  seatLookup: Map<number, Seat>;
  currentDay: number;
}) {
  // Real-role guard: a wolf/madman/villager whose declared role does
  // not match their seat role is rendering the panel's value most.
  // The role chip on the header carries that contrast so post-game
  // readers see "Yuriko (狼) — 占い 通算 1 件" without scanning seats.
  const seerClaims = dedupeSeerClaims(entry.seer_claims);
  const mediumClaims = dedupeMediumClaims(entry.medium_claims);
  const seerCount = seerClaims.length;
  const mediumCount = mediumClaims.length;
  const expectedSeer = expectedSeerCountForDay(currentDay);
  // Medium count == executions seen so far; we don't have a clean way
  // to count executions per game in the viewer, so we render the raw
  // count without a mismatch chip rather than risk a false positive.
  const seerMismatch = seerCount > 0 && seerCount !== expectedSeer;

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.75 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
          {claimer?.display_name ?? `席${entry.claimer_seat}`}
        </Typography>
        {claimer && (
          <Chip
            label={roleLabel(claimer.role)}
            size="small"
            color={isWolfSide(claimer.role) ? "error" : "default"}
            variant={isWolfSide(claimer.role) ? "filled" : "outlined"}
            sx={{ height: 20, fontSize: 11 }}
          />
        )}
        {seerCount > 0 && (
          <Chip
            label={`占い 通算 ${seerCount} / 期待 ${expectedSeer}`}
            size="small"
            color={seerMismatch ? "warning" : "default"}
            variant={seerMismatch ? "filled" : "outlined"}
            sx={{ height: 20, fontSize: 11 }}
          />
        )}
        {mediumCount > 0 && (
          <Chip
            label={`霊媒 通算 ${mediumCount}`}
            size="small"
            variant="outlined"
            sx={{ height: 20, fontSize: 11 }}
          />
        )}
      </Stack>
      <Stack spacing={0.5} sx={{ pl: 1 }}>
        {seerClaims.map((c, idx) => (
          <SeerClaimRow
            key={`s-${c.declared_at_event_id}-${idx}`}
            claim={c}
            seatLookup={seatLookup}
            label={CO_LABEL.seer}
          />
        ))}
        {mediumClaims.map((c, idx) => (
          <MediumClaimRow
            key={`m-${c.declared_at_event_id}-${idx}`}
            claim={c}
            seatLookup={seatLookup}
            label={CO_LABEL.medium}
          />
        ))}
      </Stack>
    </Box>
  );
}

function SeerClaimRow({
  claim,
  seatLookup,
  label,
}: {
  claim: ClaimedSeerHistoryEntry;
  seatLookup: Map<number, Seat>;
  label: string;
}) {
  const target = seatLookup.get(claim.target_seat);
  // Real-vs-claim mismatch: a wolf was claimed white when the target
  // is actually a wolf (or vice versa). Surfaces the cleanest "this
  // CO is lying" tell straight in the row without forcing the user
  // to cross-reference roles manually.
  const truthIsWolf = target ? target.role === "WEREWOLF" : null;
  const lieFlag =
    truthIsWolf !== null && truthIsWolf !== claim.is_wolf;

  return (
    <Stack direction="row" spacing={1} alignItems="center">
      <Chip
        label={`day ${claim.day}`}
        size="small"
        variant="outlined"
        sx={{ height: 20, fontSize: 10, minWidth: 56 }}
      />
      <Typography variant="caption" sx={{ color: "text.secondary", minWidth: 36 }}>
        {label}
      </Typography>
      <Typography variant="body2">
        {claim.target_name}{" "}
        <strong style={{ color: claim.is_wolf ? "#d32f2f" : "#1976d2" }}>
          {claim.is_wolf ? "黒" : "白"}
        </strong>
      </Typography>
      {lieFlag && (
        <Chip
          label="嘘"
          size="small"
          color="error"
          variant="filled"
          sx={{ height: 18, fontSize: 10 }}
        />
      )}
    </Stack>
  );
}

function MediumClaimRow({
  claim,
  seatLookup,
  label,
}: {
  claim: ClaimedMediumHistoryEntry;
  seatLookup: Map<number, Seat>;
  label: string;
}) {
  const target = seatLookup.get(claim.target_seat);
  const truthIsWolf = target ? target.role === "WEREWOLF" : null;
  // Medium claims with `is_wolf=null` mean "no execution yesterday";
  // we don't flag those as lies even if the target's role is a wolf
  // — a void result is silence, not a falsehood.
  const lieFlag =
    claim.is_wolf !== null &&
    truthIsWolf !== null &&
    truthIsWolf !== claim.is_wolf;
  const verdict =
    claim.is_wolf === null
      ? "結果なし"
      : claim.is_wolf
        ? "黒"
        : "白";
  const verdictColor =
    claim.is_wolf === null ? "#888" : claim.is_wolf ? "#d32f2f" : "#1976d2";
  return (
    <Stack direction="row" spacing={1} alignItems="center">
      <Chip
        label={`day ${claim.day}`}
        size="small"
        variant="outlined"
        sx={{ height: 20, fontSize: 10, minWidth: 56 }}
      />
      <Typography variant="caption" sx={{ color: "text.secondary", minWidth: 36 }}>
        {label}
      </Typography>
      <Typography variant="body2">
        {claim.target_name}{" "}
        <strong style={{ color: verdictColor }}>{verdict}</strong>
      </Typography>
      {lieFlag && (
        <Chip
          label="嘘"
          size="small"
          color="error"
          variant="filled"
          sx={{ height: 18, fontSize: 10 }}
        />
      )}
    </Stack>
  );
}

function expectedSeerCountForDay(day: number): number {
  // Mirrors `wolfbot.master.claim.claim_history.expected_seer_claim_count_for_day`:
  // each entry is tagged with the day it was *announced* (NIGHT_0
  // surfaces day 1 morning, NIGHT_K surfaces day K+1 morning), so by
  // day-N morning the cumulative count is exactly N. This matches the
  // validator's "1 entry per declared day" rule (`same_day_priors`).
  if (day < 1) return 0;
  return day;
}

function computeDayCount(data: GameSample): number {
  // The exporter doesn't carry an explicit "current day" field, so we
  // infer from the highest day_number that appears anywhere in the
  // phase log. Empty games default to 0 (= day 1's morning baseline).
  let max = 0;
  for (const phase of data.phases) {
    if (phase.day > max) max = phase.day;
  }
  return max;
}

function isWolfSide(role: string): boolean {
  return role === "WEREWOLF" || role === "MADMAN";
}

const ROLE_LABEL: Record<string, string> = {
  VILLAGER: "村人",
  WEREWOLF: "人狼",
  MADMAN: "狂人",
  SEER: "占い師",
  MEDIUM: "霊媒師",
  KNIGHT: "騎士",
};

function roleLabel(role: string): string {
  return ROLE_LABEL[role] ?? role;
}
