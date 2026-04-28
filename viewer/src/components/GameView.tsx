"use client";

import * as React from "react";
import Link from "next/link";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import GameHeader from "@/components/GameHeader";
import PhaseSection from "@/components/PhaseSection";
import SeatGrid from "@/components/SeatGrid";
import StatsPanel from "@/components/StatsPanel";
import TraceDrawer from "@/components/TraceDrawer";
import type { GameSample, TraceEntry } from "@/lib/types";

export default function GameView({
  data,
  backHref,
  sampleBadge = false,
}: {
  data: GameSample;
  backHref?: string;
  sampleBadge?: boolean;
}) {
  const [openTrace, setOpenTrace] = React.useState<TraceEntry | null>(null);

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
              onOpenTrace={setOpenTrace}
            />
          ))}
        </Box>
        <Box>
          <StatsPanel data={data} />
        </Box>
      </Box>
      <TraceDrawer entry={openTrace} onClose={() => setOpenTrace(null)} />
    </Container>
  );
}
