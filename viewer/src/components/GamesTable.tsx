import Link from "next/link";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import {
  formatDuration,
  formatJstDate,
  formatLatency,
  formatTokens,
} from "@/lib/format";
import type { GameSummary } from "@/lib/data";

export default function GamesTable({ games }: { games: GameSummary[] }) {
  if (games.length === 0) {
    return null; // empty state is rendered by the page
  }

  return (
    <Paper variant="outlined">
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Game ID</TableCell>
            <TableCell>勝敗</TableCell>
            <TableCell>モード</TableCell>
            <TableCell>開始 (JST)</TableCell>
            <TableCell align="right">所要</TableCell>
            <TableCell align="center">席</TableCell>
            <TableCell align="right">LLM 呼び出し</TableCell>
            <TableCell align="right">合計トークン</TableCell>
            <TableCell align="right">合計レイテンシ</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {games.map((g) => (
            <GameRow key={g.id} game={g} />
          ))}
        </TableBody>
      </Table>
    </Paper>
  );
}

/**
 * One row of the games-list table.
 *
 * Wraps the entire row in a Next ``<Link>`` styled to fill the row so the
 * whole strip is clickable AND retains link semantics — right-click ▶
 * "新しいタブで開く" works, screen readers see it as a link, no JS needed.
 *
 * Implementation: render ``Link`` as the ``<tr>`` via MUI's ``component``
 * prop. ``LinkProps`` carries ``href``; the ``component`` cast satisfies
 * MUI's generic without an ``as any``.
 */
function GameRow({ game }: { game: GameSummary }) {
  return (
    <TableRow
      component={Link}
      href={`/games/${game.id}`}
      hover
      sx={{
        cursor: "pointer",
        textDecoration: "none",
        "& > td": { borderBottom: "1px solid", borderColor: "divider" },
      }}
    >
      <TableCell sx={{ fontFamily: "monospace" }}>{game.id}</TableCell>
      <TableCell>
        <VictoryChip victory={game.victory} />
      </TableCell>
      <TableCell>
        <Chip
          size="small"
          label={game.discussion_mode}
          variant="outlined"
          sx={{ height: 22, fontFamily: "monospace" }}
        />
      </TableCell>
      <TableCell sx={{ fontFamily: "monospace" }}>
        {formatJstDate(game.created_at_ms)}
      </TableCell>
      <TableCell align="right">{formatDuration(game.duration_ms)}</TableCell>
      <TableCell align="center">{game.seat_count}</TableCell>
      <TableCell align="right">{game.llm_call_count}</TableCell>
      <TableCell align="right">{formatTokens(game.total_tokens)}</TableCell>
      <TableCell align="right">{formatLatency(game.total_latency_ms)}</TableCell>
    </TableRow>
  );
}

function VictoryChip({ victory }: { victory: GameSummary["victory"] }) {
  if (victory === "village") {
    return <Chip size="small" color="success" label="村人" />;
  }
  if (victory === "wolf") {
    return <Chip size="small" color="error" label="人狼" />;
  }
  return <Chip size="small" variant="outlined" label="未終了 / 中断" />;
}

export function EmptyGamesState() {
  return (
    <Paper variant="outlined" sx={{ p: 4, textAlign: "center" }}>
      <Typography variant="h6" sx={{ mb: 1 }}>
        ゲームの記録がありません
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        ゲームを 1 試合プレイすると、終了時または{" "}
        <Box component="code" sx={{ fontFamily: "monospace" }}>
          /wolf abort
        </Box>{" "}
        実行時に
        <br />
        自動で <code>viewer/games/{"{game_id}"}.json</code> が出力されます。
      </Typography>
      <Typography variant="body2">
        まずは仕組みを試したい場合は{" "}
        <Link
          href="/sample"
          style={{ textDecoration: "underline", color: "inherit" }}
        >
          サンプルゲーム
        </Link>{" "}
        を開いてください。
      </Typography>
    </Paper>
  );
}
