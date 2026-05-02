"use client";

import Link from "next/link";
import { useDeferredValue, useMemo, useState, type ReactNode } from "react";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import InputAdornment from "@mui/material/InputAdornment";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import type { TableCellProps } from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import TableSortLabel from "@mui/material/TableSortLabel";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import SearchIcon from "@mui/icons-material/Search";
import {
  formatDuration,
  formatJstDate,
  formatLatency,
  formatTokens,
} from "@/lib/format";
import type { GameSummary } from "@/lib/data";

// Columns the table can sort on. Pinned at module scope so the
// header / row code stays in lock-step with the comparator switch.
type SortKey =
  | "id"
  | "victory"
  | "discussion_mode"
  | "created_at_ms"
  | "duration_ms"
  | "seat_count"
  | "human_count"
  | "llm_count"
  | "llm_call_count"
  | "total_tokens"
  | "total_latency_ms";

type SortDir = "asc" | "desc";

interface ColumnDef {
  key: SortKey;
  label: string;
  align?: TableCellProps["align"];
}

const COLUMNS: readonly ColumnDef[] = [
  { key: "id", label: "Game ID" },
  { key: "victory", label: "勝敗" },
  { key: "discussion_mode", label: "モード" },
  { key: "created_at_ms", label: "開始 (JST)" },
  { key: "duration_ms", label: "所要", align: "right" },
  { key: "seat_count", label: "席", align: "center" },
  { key: "human_count", label: "人間", align: "right" },
  { key: "llm_count", label: "LLM", align: "right" },
  { key: "llm_call_count", label: "LLM 呼び出し", align: "right" },
  { key: "total_tokens", label: "合計トークン", align: "right" },
  { key: "total_latency_ms", label: "合計レイテンシ", align: "right" },
];

const VICTORY_RANK: Record<string, number> = {
  village: 0,
  wolf: 1,
};

const ROWS_PER_PAGE_OPTIONS = [10, 25, 50, 100] as const;

export default function GamesTable({ games }: { games: GameSummary[] }) {
  // Sort defaults to ``created_at_ms desc`` so the freshest game stays
  // on top — matches the previous always-newest-first behavior the page
  // had before it became sortable.
  const [sortKey, setSortKey] = useState<SortKey>("created_at_ms");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [search, setSearch] = useState("");
  // ``useDeferredValue`` keeps typing latency low when filtering hundreds
  // of rows — React paints the input update before the (potentially)
  // expensive filter+sort+slice runs.
  const deferredSearch = useDeferredValue(search);
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState<number>(25);

  const filtered = useMemo(() => {
    const needle = deferredSearch.trim().toLowerCase();
    if (!needle) return games;
    return games.filter((g) => g.id.toLowerCase().includes(needle));
  }, [games, deferredSearch]);

  const sorted = useMemo(() => {
    const copy = filtered.slice();
    copy.sort(makeComparator(sortKey, sortDir));
    return copy;
  }, [filtered, sortKey, sortDir]);

  // Clamp the page when the filter shrinks the result set below the
  // current offset. Otherwise ``TablePagination`` would happily render
  // a page with zero rows and the user has to click "<" to recover.
  const maxPage = Math.max(0, Math.ceil(sorted.length / rowsPerPage) - 1);
  const safePage = Math.min(page, maxPage);
  const pageStart = safePage * rowsPerPage;
  const pageRows = sorted.slice(pageStart, pageStart + rowsPerPage);

  if (games.length === 0) {
    return null; // empty state is rendered by the page
  }

  const handleSort = (key: SortKey) => {
    setPage(0);
    if (sortKey === key) {
      // Same column → toggle direction.
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      return;
    }
    // New column → start at desc. Every subsequent click on the same
    // header just flips between desc and asc, matching the typical
    // spreadsheet/JIRA/GitHub-style "first click is desc, click again
    // for asc" interaction.
    setSortKey(key);
    setSortDir("desc");
  };

  return (
    <Stack spacing={1.5}>
      <TextField
        size="small"
        value={search}
        onChange={(e) => {
          setSearch(e.target.value);
          setPage(0);
        }}
        placeholder="Game ID で検索 (部分一致)"
        InputProps={{
          startAdornment: (
            <InputAdornment position="start">
              <SearchIcon fontSize="small" />
            </InputAdornment>
          ),
        }}
        sx={{ maxWidth: 360 }}
      />

      <Paper variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              {COLUMNS.map((col) => (
                <TableCell
                  key={col.key}
                  align={col.align}
                  sortDirection={sortKey === col.key ? sortDir : false}
                >
                  <TableSortLabel
                    active={sortKey === col.key}
                    // Inactive columns render the indicator in the
                    // direction they'll start at on first click — desc
                    // — so the affordance previews "click me to sort
                    // newest/biggest first".
                    direction={sortKey === col.key ? sortDir : "desc"}
                    onClick={() => handleSort(col.key)}
                  >
                    {col.label}
                  </TableSortLabel>
                </TableCell>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {pageRows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={COLUMNS.length} align="center" sx={{ py: 4 }}>
                  <Typography variant="body2" color="text.secondary">
                    一致するゲームがありません
                  </Typography>
                </TableCell>
              </TableRow>
            ) : (
              pageRows.map((g) => <GameRow key={g.id} game={g} />)
            )}
          </TableBody>
        </Table>
        <TablePagination
          component="div"
          count={sorted.length}
          page={safePage}
          onPageChange={(_, p) => setPage(p)}
          rowsPerPage={rowsPerPage}
          onRowsPerPageChange={(e) => {
            setRowsPerPage(parseInt(e.target.value, 10));
            setPage(0);
          }}
          rowsPerPageOptions={Array.from(ROWS_PER_PAGE_OPTIONS)}
          labelRowsPerPage="表示件数"
          labelDisplayedRows={({ from, to, count }) =>
            `${from}–${to} / ${count}`
          }
        />
      </Paper>
    </Stack>
  );
}

/**
 * One row of the games-list table.
 *
 * Each cell wraps its content in a Next ``<Link>`` rendered as a block-level
 * element so the entire cell area is clickable, while the ``<tr>``/``<td>``
 * structure remains valid HTML. Wrapping the whole ``<TableRow>`` in an
 * ``<a>`` would put the anchor between ``<tbody>`` and ``<td>`` — the parser
 * foster-parents it out and the cells collapse into one column.
 */
function GameRow({ game }: { game: GameSummary }) {
  const href = `/games/${game.id}`;
  return (
    <TableRow hover sx={{ "& > td": { p: 0 } }}>
      <LinkCell href={href} sx={{ fontFamily: "monospace" }}>
        {game.id}
      </LinkCell>
      <LinkCell href={href}>
        <VictoryChip victory={game.victory} />
      </LinkCell>
      <LinkCell href={href}>
        <Chip
          size="small"
          label={game.discussion_mode}
          variant="outlined"
          sx={{ height: 22, fontFamily: "monospace" }}
        />
      </LinkCell>
      <LinkCell href={href} sx={{ fontFamily: "monospace" }}>
        {formatJstDate(game.created_at_ms)}
      </LinkCell>
      <LinkCell href={href} align="right">
        {formatDuration(game.duration_ms)}
      </LinkCell>
      <LinkCell href={href} align="center">
        {game.seat_count}
      </LinkCell>
      <LinkCell href={href} align="right">
        {game.human_count}
      </LinkCell>
      <LinkCell href={href} align="right">
        {game.llm_count}
      </LinkCell>
      <LinkCell href={href} align="right">
        {game.llm_call_count}
      </LinkCell>
      <LinkCell href={href} align="right">
        {formatTokens(game.total_tokens)}
      </LinkCell>
      <LinkCell href={href} align="right">
        {formatLatency(game.total_latency_ms)}
      </LinkCell>
    </TableRow>
  );
}

function LinkCell({
  href,
  children,
  align,
  sx,
}: {
  href: string;
  children: ReactNode;
  align?: TableCellProps["align"];
  sx?: TableCellProps["sx"];
}) {
  return (
    <TableCell align={align}>
      <Box
        component={Link}
        href={href}
        sx={{
          display: "block",
          width: "100%",
          color: "inherit",
          textDecoration: "none",
          px: 2,
          py: 1,
          textAlign: align ?? "inherit",
          ...sx,
        }}
      >
        {children}
      </Box>
    </TableCell>
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

/**
 * Build a typed comparator for the given sort key + direction.
 *
 * Most columns are numeric (timestamps, counts, totals). The three
 * non-numeric ones get explicit handling:
 *
 * * ``id`` / ``discussion_mode`` — string compare via ``localeCompare``.
 * * ``victory`` — folded onto a stable rank (``village=0``, ``wolf=1``,
 *   ``null=2``) so the sort is deterministic and unfinished games land
 *   together at one end.
 * * ``duration_ms`` — null (game still running) is sorted to the end
 *   regardless of direction so an in-progress row never displaces a
 *   completed one in either view.
 */
function makeComparator(
  key: SortKey,
  dir: SortDir,
): (a: GameSummary, b: GameSummary) => number {
  const sign = dir === "asc" ? 1 : -1;
  return (a, b) => {
    // Pin in-progress / unfinished rows to the bottom regardless of
    // direction so an active game never displaces a completed one in
    // either view. Apply BEFORE the sign multiplier so flipping the
    // direction doesn't bubble nulls back to the top.
    const nullCmp = compareNullsLast(key, a, b);
    if (nullCmp !== null) return nullCmp;
    const cmp = compareByKey(key, a, b);
    return sign * cmp;
  };
}

function compareByKey(key: SortKey, a: GameSummary, b: GameSummary): number {
  switch (key) {
    case "id":
      return a.id.localeCompare(b.id);
    case "discussion_mode":
      return a.discussion_mode.localeCompare(b.discussion_mode);
    case "victory":
      return victoryRank(a.victory) - victoryRank(b.victory);
    case "duration_ms":
      // Null pairs handled by `compareNullsLast`; here both sides are
      // guaranteed non-null so the cast is safe.
      return (a.duration_ms as number) - (b.duration_ms as number);
    default:
      return (a[key] as number) - (b[key] as number);
  }
}

function victoryRank(v: GameSummary["victory"]): number {
  if (v === null) return 2;
  return VICTORY_RANK[v] ?? 3;
}

/** Returns a direction-independent ordering when one side is null, or
 * ``null`` when neither side needs the special-case (caller falls
 * through to the regular comparator + direction sign). */
function compareNullsLast(
  key: SortKey,
  a: GameSummary,
  b: GameSummary,
): number | null {
  if (key !== "duration_ms") return null;
  const aNull = a.duration_ms === null;
  const bNull = b.duration_ms === null;
  if (!aNull && !bNull) return null;
  if (aNull && bNull) return 0;
  return aNull ? 1 : -1;
}
