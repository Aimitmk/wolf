import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { roleChipStyle, roleJa, roleTint } from "@/lib/format";
import type { Seat } from "@/lib/types";

export default function SeatGrid({ seats }: { seats: Seat[] }) {
  return (
    <Box
      sx={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
        gap: 1.5,
        mb: 2,
      }}
    >
      {seats.map((seat) => (
        <SeatCard key={seat.seat_no} seat={seat} />
      ))}
    </Box>
  );
}

function SeatCard({ seat }: { seat: Seat }) {
  // Per-role tinting: each of seer / medium / knight gets its own
  // colour (alongside the existing wolf-red and madman-orange) so
  // the seat panel scans visually the same way as the timeline's
  // role chip. Helpers in lib/format.ts keep the hue palette in one
  // place across PhaseSection, ClaimHistoryPanel, and this card.
  const tint = roleTint(seat.role);
  const dim = !seat.alive;

  return (
    <Card
      variant="outlined"
      sx={{
        background: tint.bg,
        borderColor: tint.border,
        opacity: dim ? 0.55 : 1,
      }}
    >
      <CardContent sx={{ p: 1.5, "&:last-child": { pb: 1.5 } }}>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Typography variant="caption" color="text.secondary">
            席{seat.seat_no}
          </Typography>
          <Chip
            label={seat.is_llm ? "LLM" : "human"}
            size="small"
            color={seat.is_llm ? "primary" : "default"}
            sx={{ height: 18, fontSize: 10 }}
          />
        </Stack>
        <Typography variant="body1" sx={{ fontWeight: 500 }}>
          {seat.display_name}
        </Typography>
        <Stack direction="row" spacing={0.5} sx={{ mt: 0.5, flexWrap: "wrap" }}>
          <Chip
            label={roleJa(seat.role)}
            size="small"
            {...roleChipStyle(seat.role)}
            sx={{ height: 20 }}
          />
          {seat.persona_key && (
            <Chip
              label={seat.persona_key}
              size="small"
              variant="outlined"
              sx={{ height: 20 }}
            />
          )}
        </Stack>
        {!seat.alive && (
          <Typography variant="caption" color="error" sx={{ mt: 0.5, display: "block" }}>
            {seat.death_cause === "EXECUTION"
              ? `${seat.death_day}日目に処刑`
              : seat.death_cause === "ATTACK"
              ? `${seat.death_day}日目朝に襲撃死`
              : "死亡"}
          </Typography>
        )}
      </CardContent>
    </Card>
  );
}
