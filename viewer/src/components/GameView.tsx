"use client";

import * as React from "react";
import Box from "@mui/material/Box";
import Container from "@mui/material/Container";
import GameHeader from "@/components/GameHeader";
import PhaseSection from "@/components/PhaseSection";
import SeatGrid from "@/components/SeatGrid";
import StatsPanel from "@/components/StatsPanel";
import TraceDrawer from "@/components/TraceDrawer";
import type { GameSample, TraceEntry } from "@/lib/types";

export default function GameView({ data }: { data: GameSample }) {
  const [openTrace, setOpenTrace] = React.useState<TraceEntry | null>(null);

  return (
    <Container maxWidth="xl" sx={{ py: 3 }}>
      <GameHeader data={data} />
      <SeatGrid seats={data.seats} />
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", lg: "1.5fr 1fr" }, gap: 2 }}>
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
