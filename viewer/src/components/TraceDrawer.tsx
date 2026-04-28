"use client";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Drawer from "@mui/material/Drawer";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";
import { formatJstTime, formatLatency, formatTokens } from "@/lib/format";
import type { TraceEntry } from "@/lib/types";

export default function TraceDrawer({
  entry,
  onClose,
}: {
  entry: TraceEntry | null;
  onClose: () => void;
}) {
  return (
    <Drawer
      anchor="right"
      open={entry !== null}
      onClose={onClose}
      slotProps={{
        paper: { sx: { width: { xs: "100%", md: 720 } } },
      }}
    >
      {entry && <Body entry={entry} onClose={onClose} />}
    </Drawer>
  );
}

function Body({
  entry,
  onClose,
}: {
  entry: TraceEntry;
  onClose: () => void;
}) {
  const ts = (() => {
    try {
      return formatJstTime(new Date(entry.ts).getTime());
    } catch {
      return entry.ts;
    }
  })();

  return (
    <Box sx={{ p: 2 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
        <Typography variant="h6" sx={{ flex: 1 }}>
          LLM 呼び出し詳細
        </Typography>
        <IconButton size="small" onClick={onClose} aria-label="close">
          <CloseIcon />
        </IconButton>
      </Stack>

      <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: "wrap" }}>
        <Chip label={entry.role} size="small" color="primary" />
        <Chip label={entry.provider} size="small" variant="outlined" />
        <Chip
          label={entry.model}
          size="small"
          variant="outlined"
          sx={{ fontFamily: "monospace" }}
        />
        {entry.day != null && (
          <Chip label={`Day ${entry.day}`} size="small" variant="outlined" />
        )}
        {entry.phase && (
          <Chip label={entry.phase} size="small" variant="outlined" />
        )}
        {typeof entry.metadata?.task === "string" && (
          <Chip
            label={`task: ${entry.metadata.task}`}
            size="small"
            color="secondary"
          />
        )}
      </Stack>

      <Stack direction="row" spacing={3} sx={{ mb: 2 }}>
        <Stat label="time" value={ts} />
        <Stat label="latency" value={formatLatency(entry.latency_ms)} />
        <Stat label="tokens (prompt)" value={formatTokens(entry.tokens?.prompt)} />
        <Stat
          label="tokens (completion)"
          value={formatTokens(entry.tokens?.completion)}
        />
        <Stat label="tokens (total)" value={formatTokens(entry.tokens?.total)} />
      </Stack>

      {entry.actor && (
        <Tooltip title="actor">
          <Typography
            variant="caption"
            sx={{ display: "block", color: "text.secondary", mb: 1, fontFamily: "monospace" }}
          >
            {entry.actor}
          </Typography>
        </Tooltip>
      )}

      {entry.error && (
        <Box
          sx={{
            p: 1.5,
            mb: 2,
            bgcolor: "error.lighter",
            color: "error.dark",
            border: "1px solid",
            borderColor: "error.light",
            borderRadius: 1,
          }}
        >
          <Typography variant="caption" sx={{ fontWeight: 600 }}>
            ERROR
          </Typography>
          <pre>{entry.error}</pre>
        </Box>
      )}

      <Section title="System prompt">
        <pre>{entry.system_prompt}</pre>
      </Section>
      <Section title="User prompt">
        <pre>{entry.user_prompt}</pre>
      </Section>
      <Section title="Response">
        <pre>{entry.response ?? "(no response)"}</pre>
      </Section>
      {entry.metadata && Object.keys(entry.metadata).length > 0 && (
        <Section title="Metadata">
          <pre>{JSON.stringify(entry.metadata, null, 2)}</pre>
        </Section>
      )}
    </Box>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1 }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ fontWeight: 500 }}>
        {value}
      </Typography>
    </Box>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Box sx={{ mb: 2 }}>
      <Typography
        variant="caption"
        sx={{ fontWeight: 600, color: "text.secondary", textTransform: "uppercase" }}
      >
        {title}
      </Typography>
      <Box
        sx={{
          mt: 0.5,
          p: 1.25,
          bgcolor: "grey.100",
          border: "1px solid",
          borderColor: "grey.300",
          borderRadius: 1,
          maxHeight: 360,
          overflow: "auto",
        }}
      >
        {children}
      </Box>
    </Box>
  );
}
