import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import { formatLatency, formatTokens, seatLabel } from "@/lib/format";
import type { GameSample, Seat, TraceEntry } from "@/lib/types";

export default function StatsPanel({ data }: { data: GameSample }) {
  const perSeat = aggregatePerSeat(data.trace, data.seats);
  const perPhase = aggregatePerPhase(data.trace);

  return (
    <Stack spacing={2} sx={{ mb: 2 }}>
      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="h6" sx={{ mb: 1 }}>
          席ごとの LLM 利用
        </Typography>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>席</TableCell>
              <TableCell align="right">呼び出し</TableCell>
              <TableCell align="right">prompt tok</TableCell>
              <TableCell align="right">completion tok</TableCell>
              <TableCell align="right">total tok</TableCell>
              <TableCell align="right">latency 合計</TableCell>
              <TableCell align="right">latency 平均</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {perSeat.map((row) => (
              <TableRow key={row.seat.seat_no} hover>
                <TableCell>{seatLabel(row.seat)}</TableCell>
                <TableCell align="right">{row.count}</TableCell>
                <TableCell align="right">{formatTokens(row.promptTokens)}</TableCell>
                <TableCell align="right">{formatTokens(row.completionTokens)}</TableCell>
                <TableCell align="right">{formatTokens(row.totalTokens)}</TableCell>
                <TableCell align="right">{formatLatency(row.latencyMs)}</TableCell>
                <TableCell align="right">
                  {row.count > 0 ? formatLatency(Math.round(row.latencyMs / row.count)) : "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="h6" sx={{ mb: 1 }}>
          フェイズごとの LLM 利用
        </Typography>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>day</TableCell>
              <TableCell>phase</TableCell>
              <TableCell align="right">呼び出し</TableCell>
              <TableCell align="right">total tok</TableCell>
              <TableCell align="right">latency 合計</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {perPhase.map((row) => (
              <TableRow key={`${row.day}-${row.phase}`} hover>
                <TableCell>{row.day}</TableCell>
                <TableCell sx={{ fontFamily: "monospace" }}>{row.phase}</TableCell>
                <TableCell align="right">{row.count}</TableCell>
                <TableCell align="right">{formatTokens(row.totalTokens)}</TableCell>
                <TableCell align="right">{formatLatency(row.latencyMs)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Paper>
    </Stack>
  );
}

interface SeatStats {
  seat: Seat;
  count: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  latencyMs: number;
}

function aggregatePerSeat(trace: TraceEntry[], seats: Seat[]): SeatStats[] {
  const bySeat = new Map<number, SeatStats>();
  for (const seat of seats) {
    bySeat.set(seat.seat_no, {
      seat,
      count: 0,
      promptTokens: 0,
      completionTokens: 0,
      totalTokens: 0,
      latencyMs: 0,
    });
  }
  for (const t of trace) {
    const seatNo = parseSeatFromActor(t.actor);
    if (seatNo == null) continue;
    const s = bySeat.get(seatNo);
    if (!s) continue;
    s.count += 1;
    s.promptTokens += t.tokens?.prompt ?? 0;
    s.completionTokens += t.tokens?.completion ?? 0;
    s.totalTokens += t.tokens?.total ?? 0;
    s.latencyMs += t.latency_ms;
  }
  return Array.from(bySeat.values()).sort((a, b) => a.seat.seat_no - b.seat.seat_no);
}

interface PhaseStats {
  day: number;
  phase: string;
  count: number;
  totalTokens: number;
  latencyMs: number;
}

function aggregatePerPhase(trace: TraceEntry[]): PhaseStats[] {
  const byPhase = new Map<string, PhaseStats>();
  for (const t of trace) {
    if (t.day == null || !t.phase) continue;
    const phaseKey = t.phase.includes("::")
      ? t.phase.split("::")[2] ?? t.phase
      : t.phase;
    const k = `${t.day}|${phaseKey}`;
    const cur = byPhase.get(k) ?? {
      day: t.day,
      phase: phaseKey,
      count: 0,
      totalTokens: 0,
      latencyMs: 0,
    };
    cur.count += 1;
    cur.totalTokens += t.tokens?.total ?? 0;
    cur.latencyMs += t.latency_ms;
    byPhase.set(k, cur);
  }
  return Array.from(byPhase.values()).sort((a, b) => {
    if (a.day !== b.day) return a.day - b.day;
    return a.phase.localeCompare(b.phase);
  });
}

function parseSeatFromActor(actor: string | null): number | null {
  if (!actor) return null;
  const m = actor.match(/seat=(\d+)/);
  return m ? Number(m[1]) : null;
}
