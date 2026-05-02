"use client";

/**
 * SuspicionPanel — public suspicion timeline + (suspecter, target) matrix.
 *
 * Why this panel exists
 * ---------------------
 * Discussion-time speeches and vote decisions both emit structured
 * `Suspicion` records with (target_seat, level, reason). The bot
 * persists every entry to the immutable `suspicions` table so:
 *
 *   - LLMs that silently reverse a prior level (e.g. trust → high
 *     without setting `update_from_level`) are detectable evidence
 *     of fabrication.
 *   - The viewer can render *who suspected whom and how strongly* as
 *     both a chronological timeline (left) and a pivot matrix (right),
 *     letting a post-game reviewer see emerging consensus and outliers
 *     at a glance.
 *
 * Pure presentation: reads `data.suspicions` (pre-folded by the
 * exporter) plus `data.seats` for name resolution. Older exports
 * (pre-2026-05-03) lack `suspicions` and the panel collapses to a
 * compact "no suspicion records" state.
 */

import * as React from "react";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import Typography from "@mui/material/Typography";
import type { GameSample, Seat, SuspicionEntry, SuspicionLevel } from "@/lib/types";

const LEVEL_LABEL: Record<SuspicionLevel, string> = {
  trust: "信頼",
  low: "弱疑",
  medium: "疑",
  high: "強疑",
};

// Color the level with a small ramp so the matrix reads as a heatmap.
// trust=cool, high=warm. Background uses MUI palette tokens for
// dark/light theme parity.
const LEVEL_BG: Record<SuspicionLevel, string> = {
  trust: "rgba(46, 204, 113, 0.18)",
  low: "rgba(241, 196, 15, 0.16)",
  medium: "rgba(230, 126, 34, 0.22)",
  high: "rgba(231, 76, 60, 0.28)",
};

const LEVEL_FG: Record<SuspicionLevel, string> = {
  trust: "#27ae60",
  low: "#d4ac0d",
  medium: "#d35400",
  high: "#c0392b",
};

const SOURCE_LABEL: Record<SuspicionEntry["source"], string> = {
  speech: "発言",
  vote: "投票",
};

function formatPhase(phase: string, voteRound: number | null): string {
  if (phase === "DAY_DISCUSSION") return "議論";
  if (phase === "DAY_VOTE") return voteRound === 1 ? "決選投票" : "投票";
  if (phase === "DAY_RUNOFF") return "決選投票";
  if (phase === "DAY_RUNOFF_SPEECH") return "決選演説";
  if (phase === "NIGHT" || phase === "NIGHT_0") {
    return voteRound === -1 ? "夜・占い" : "夜";
  }
  return phase;
}

function levelChip(level: SuspicionLevel) {
  return (
    <Chip
      size="small"
      label={LEVEL_LABEL[level]}
      sx={{
        bgcolor: LEVEL_BG[level],
        color: LEVEL_FG[level],
        fontWeight: 600,
        minWidth: 48,
      }}
    />
  );
}

function seatLookup(seats: readonly Seat[]): Record<number, string> {
  const out: Record<number, string> = {};
  for (const s of seats) out[s.seat_no] = s.display_name;
  return out;
}

interface MatrixCell {
  level: SuspicionLevel;
  reason: string;
  day: number;
  source: "speech" | "vote";
}

/**
 * Build the (suspecter, target) → latest-cell pivot. We pick the LAST
 * row chronologically so the matrix reflects each speaker's currently-
 * declared opinion. Earlier rows are still visible in the timeline tab.
 */
function buildMatrix(
  suspicions: readonly SuspicionEntry[],
): Map<number, Map<number, MatrixCell>> {
  const matrix = new Map<number, Map<number, MatrixCell>>();
  for (const s of suspicions) {
    let row = matrix.get(s.suspecter_seat);
    if (!row) {
      row = new Map();
      matrix.set(s.suspecter_seat, row);
    }
    row.set(s.target_seat, {
      level: s.level,
      reason: s.reason,
      day: s.day,
      source: s.source,
    });
  }
  return matrix;
}

function TimelineTab({
  suspicions,
  names,
}: {
  suspicions: readonly SuspicionEntry[];
  names: Record<number, string>;
}) {
  return (
    <Stack spacing={1.5} sx={{ mt: 2 }}>
      {suspicions.map((s, idx) => {
        const sname = names[s.suspecter_seat] ?? `席${s.suspecter_seat}`;
        const tname = names[s.target_seat] ?? `席${s.target_seat}`;
        const phase = formatPhase(s.phase, s.vote_round);
        const updated = s.update_from_level !== null;
        return (
          <Paper
            key={`${s.event_id ?? "vote"}-${s.seq}-${idx}`}
            variant="outlined"
            sx={{ p: 1.5 }}
          >
            <Stack direction="row" alignItems="center" spacing={1.5}>
              <Chip
                size="small"
                label={`day${s.day} ${phase}`}
                sx={{ bgcolor: "rgba(127,127,127,0.12)" }}
              />
              <Chip
                size="small"
                label={SOURCE_LABEL[s.source]}
                sx={{
                  bgcolor:
                    s.source === "speech"
                      ? "rgba(52, 152, 219, 0.16)"
                      : "rgba(155, 89, 182, 0.16)",
                  color:
                    s.source === "speech" ? "#2980b9" : "#8e44ad",
                  fontWeight: 600,
                }}
              />
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                {sname}
              </Typography>
              <Typography variant="body2" sx={{ color: "text.secondary" }}>
                →
              </Typography>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                {tname}
              </Typography>
              {levelChip(s.level)}
              {updated && (
                <Chip
                  size="small"
                  label={`${LEVEL_LABEL[s.update_from_level!]}→${
                    LEVEL_LABEL[s.level]
                  }`}
                  sx={{
                    bgcolor: "rgba(155, 89, 182, 0.20)",
                    color: "#8e44ad",
                    fontWeight: 600,
                  }}
                />
              )}
            </Stack>
            <Typography
              variant="body2"
              sx={{ color: "text.secondary", mt: 1, ml: 0.5 }}
            >
              {s.reason}
            </Typography>
            {updated && s.update_reason && (
              <Typography
                variant="caption"
                sx={{ color: "text.secondary", mt: 0.5, ml: 0.5, display: "block" }}
              >
                更新理由: {s.update_reason}
              </Typography>
            )}
          </Paper>
        );
      })}
    </Stack>
  );
}

function MatrixTab({
  suspicions,
  seats,
  names,
}: {
  suspicions: readonly SuspicionEntry[];
  seats: readonly Seat[];
  names: Record<number, string>;
}) {
  const matrix = buildMatrix(suspicions);
  // Stable seat order for both axes — by seat_no so deciphering with the
  // SeatGrid above is easy.
  const sortedSeats = [...seats].sort((a, b) => a.seat_no - b.seat_no);
  return (
    <Box sx={{ overflowX: "auto", mt: 2 }}>
      <table
        style={{
          borderCollapse: "collapse",
          fontSize: 13,
          minWidth: "100%",
        }}
      >
        <thead>
          <tr>
            <th
              style={{
                padding: "6px 10px",
                textAlign: "left",
                borderBottom: "1px solid rgba(127,127,127,0.3)",
                position: "sticky",
                left: 0,
                background: "var(--mui-palette-background-paper, #fff)",
                zIndex: 1,
              }}
            >
              疑う側 \ 対象
            </th>
            {sortedSeats.map((s) => (
              <th
                key={`th-${s.seat_no}`}
                style={{
                  padding: "6px 10px",
                  textAlign: "center",
                  borderBottom: "1px solid rgba(127,127,127,0.3)",
                  whiteSpace: "nowrap",
                  fontWeight: 600,
                }}
              >
                {names[s.seat_no] ?? `席${s.seat_no}`}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sortedSeats.map((suspecter) => (
            <tr key={`tr-${suspecter.seat_no}`}>
              <td
                style={{
                  padding: "6px 10px",
                  borderBottom: "1px solid rgba(127,127,127,0.15)",
                  fontWeight: 600,
                  whiteSpace: "nowrap",
                  position: "sticky",
                  left: 0,
                  background: "var(--mui-palette-background-paper, #fff)",
                  zIndex: 1,
                }}
              >
                {names[suspecter.seat_no] ?? `席${suspecter.seat_no}`}
              </td>
              {sortedSeats.map((target) => {
                if (suspecter.seat_no === target.seat_no) {
                  return (
                    <td
                      key={`cell-${suspecter.seat_no}-${target.seat_no}`}
                      style={{
                        padding: "6px 10px",
                        textAlign: "center",
                        borderBottom: "1px solid rgba(127,127,127,0.15)",
                        color: "rgba(127,127,127,0.5)",
                      }}
                    >
                      —
                    </td>
                  );
                }
                const cell = matrix
                  .get(suspecter.seat_no)
                  ?.get(target.seat_no);
                if (!cell) {
                  return (
                    <td
                      key={`cell-${suspecter.seat_no}-${target.seat_no}`}
                      style={{
                        padding: "6px 10px",
                        textAlign: "center",
                        borderBottom: "1px solid rgba(127,127,127,0.15)",
                        color: "rgba(127,127,127,0.4)",
                      }}
                    >
                      ·
                    </td>
                  );
                }
                return (
                  <td
                    key={`cell-${suspecter.seat_no}-${target.seat_no}`}
                    style={{
                      padding: "4px 6px",
                      textAlign: "center",
                      borderBottom: "1px solid rgba(127,127,127,0.15)",
                      background: LEVEL_BG[cell.level],
                    }}
                    title={`day${cell.day} ${SOURCE_LABEL[cell.source]}: ${cell.reason}`}
                  >
                    <Typography
                      variant="caption"
                      sx={{
                        color: LEVEL_FG[cell.level],
                        fontWeight: 700,
                      }}
                    >
                      {LEVEL_LABEL[cell.level]}
                    </Typography>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <Typography variant="caption" sx={{ color: "text.secondary", mt: 2, display: "block" }}>
        各セルは「疑う側 (行) → 対象 (列)」の最新評価。セルにマウスオーバーで day と理由を表示。`·` は未評価、`—` は自席。
      </Typography>
    </Box>
  );
}

export default function SuspicionPanel({ data }: { data: GameSample }) {
  const suspicions = data.suspicions ?? [];
  const [tab, setTab] = React.useState<"timeline" | "matrix">("matrix");

  if (suspicions.length === 0) {
    return (
      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="h6">疑い履歴</Typography>
        <Typography variant="body2" sx={{ color: "text.secondary", mt: 1 }}>
          このゲームには構造化された疑い記録がありません (古いゲームか、まだ発話/投票が無い)。
        </Typography>
      </Paper>
    );
  }
  const names = seatLookup(data.seats);

  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={1}
      >
        <Typography variant="h6">疑い履歴 ({suspicions.length} 件)</Typography>
        <Tabs
          value={tab}
          onChange={(_, v) => setTab(v)}
          sx={{ minHeight: 32 }}
        >
          <Tab
            label="マトリクス"
            value="matrix"
            sx={{ minHeight: 32, textTransform: "none" }}
          />
          <Tab
            label="タイムライン"
            value="timeline"
            sx={{ minHeight: 32, textTransform: "none" }}
          />
        </Tabs>
      </Stack>
      {tab === "matrix" ? (
        <MatrixTab suspicions={suspicions} seats={data.seats} names={names} />
      ) : (
        <TimelineTab suspicions={suspicions} names={names} />
      )}
    </Paper>
  );
}
