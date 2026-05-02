"use client";

import * as React from "react";
import Link from "next/link";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import ClaimHistoryPanel from "@/components/ClaimHistoryPanel";
import SuspicionPanel from "@/components/SuspicionPanel";
import GameHeader from "@/components/GameHeader";
import PhaseSection from "@/components/PhaseSection";
import SeatGrid from "@/components/SeatGrid";
import StatsPanel from "@/components/StatsPanel";
import TraceDrawer from "@/components/TraceDrawer";
import { buildMatchMaps, type MatchMaps } from "@/lib/match";
import type { GameSample, TraceEntry } from "@/lib/types";

export default function GameView({
  data,
  matches,
  traceFetcher,
  backHref,
  sampleBadge = false,
}: {
  data: GameSample;
  /**
   * Precomputed `(eventKey → traceIndex)` map. Required because the
   * client never reruns the matchers — we ship the lookup table from
   * the server load step (see `lib/data.ts::loadGameWithMatches`). Set
   * to `null` (sample page) to fall through to in-browser computation.
   */
  matches: MatchMaps | null;
  /**
   * Lazy heavy-field loader for the trace drawer. Provided when the
   * page strips ``system_prompt`` / ``user_prompt`` / ``response`` from
   * the SSR payload to keep the initial HTML small. ``null`` = the
   * trace already carries its heavy fields (sample page) and the
   * drawer renders synchronously.
   */
  traceFetcher?:
    | ((index: number) => Promise<{
        system_prompt: string;
        user_prompt: string;
        response: string | null;
      }>)
    | null;
  backHref?: string;
  sampleBadge?: boolean;
}) {
  const [openTrace, setOpenTrace] = React.useState<{
    entry: TraceEntry;
    index: number;
  } | null>(null);

  // Sample page passes `matches=null` (it doesn't pre-slim) — fall
  // through to in-browser computation so the lightbulb buttons keep
  // working without a server round-trip on the static demo.
  const resolvedMatches: MatchMaps = React.useMemo(() => {
    if (matches !== null) return matches;
    return buildMatchMaps(
      data.phases,
      data.trace,
      data.arbiter_decisions ?? [],
      data.seats,
    );
  }, [data, matches]);

  // Index lookup keeps drawer's lazy fetch keyed on the canonical
  // position in `data.trace[]`. Reference equality on TraceEntry is
  // what the server precompute used to assign indices, so we mirror
  // that here with `indexOf`.
  const handleOpen = React.useCallback(
    (entry: TraceEntry) => {
      const index = data.trace.indexOf(entry);
      if (index >= 0) setOpenTrace({ entry, index });
    },
    [data.trace],
  );

  return (
    <Container maxWidth="xl" sx={{ py: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
        {backHref && (
          <Button
            component={Link}
            href={backHref}
            startIcon={<ArrowBackIcon />}
            size="small"
            variant="text"
          >
            ゲーム一覧
          </Button>
        )}
        {sampleBadge && (
          <Chip
            label="SAMPLE"
            size="small"
            color="warning"
            variant="outlined"
            sx={{ height: 22, fontWeight: 600 }}
          />
        )}
      </Stack>
      <GameHeader data={data} />
      <SeatGrid seats={data.seats} />
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: { xs: "1fr", lg: "1.5fr 1fr" },
          gap: 2,
        }}
      >
        <Box>
          {data.phases.map((phase) => (
            <PhaseSection
              key={`d${phase.day}-${phase.phase}`}
              phase={phase}
              seats={data.seats}
              trace={data.trace}
              arbiterDecisions={data.arbiter_decisions ?? []}
              matches={resolvedMatches}
              onOpenTrace={handleOpen}
            />
          ))}
        </Box>
        <Box>
          <Stack spacing={2}>
            <ClaimHistoryPanel data={data} />
            <SuspicionPanel data={data} />
            <StatsPanel data={data} />
          </Stack>
        </Box>
      </Box>
      <TraceDrawer
        entry={openTrace?.entry ?? null}
        index={openTrace?.index ?? null}
        fetcher={traceFetcher ?? null}
        onClose={() => setOpenTrace(null)}
      />
    </Container>
  );
}
