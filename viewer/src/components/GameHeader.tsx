import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { formatJstTime, formatTokens } from "@/lib/format";
import type { GameSample } from "@/lib/types";

export default function GameHeader({ data }: { data: GameSample }) {
  const totals = aggregate(data);
  const victoryColor =
    data.game.victory === "village"
      ? "success"
      : data.game.victory === "wolf"
      ? "error"
      : "default";

  return (
    <Paper sx={{ p: 2, mb: 2 }} variant="outlined">
      <Stack direction="row" spacing={3} alignItems="center" flexWrap="wrap">
        <Box>
          <Typography variant="overline" color="text.secondary">
            game id
          </Typography>
          <Typography variant="h6" sx={{ fontFamily: "monospace" }}>
            {data.game.id}
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          <Chip label={`mode: ${data.game.discussion_mode}`} size="small" />
          {data.game.victory != null && (
            <Chip
              label={`勝利: ${data.game.victory === "village" ? "村人" : "人狼"}`}
              size="small"
              color={victoryColor}
            />
          )}
        </Stack>
        <Box flex={1} />
        <Stack direction="row" spacing={3}>
          <Stat label="開始" value={formatJstTime(data.game.created_at_ms)} />
          {data.game.ended_at_ms && (
            <Stat label="終了" value={formatJstTime(data.game.ended_at_ms)} />
          )}
          <Stat
            label="LLM呼び出し"
            value={`${totals.callCount} 回`}
          />
          <Stat
            label="合計トークン"
            value={formatTokens(totals.totalTokens)}
          />
          <Stat
            label="合計レイテンシ"
            value={`${(totals.totalLatencyMs / 1000).toFixed(1)} s`}
          />
        </Stack>
      </Stack>
    </Paper>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <Box sx={{ minWidth: 90 }}>
      <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1 }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ fontWeight: 500 }}>
        {value}
      </Typography>
    </Box>
  );
}

function aggregate(data: GameSample) {
  let totalTokens = 0;
  let totalLatencyMs = 0;
  for (const t of data.trace) {
    totalLatencyMs += t.latency_ms;
    totalTokens += t.tokens?.total ?? 0;
  }
  return {
    callCount: data.trace.length,
    totalTokens,
    totalLatencyMs,
  };
}
