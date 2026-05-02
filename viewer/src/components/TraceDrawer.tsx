"use client";

import * as React from "react";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Drawer from "@mui/material/Drawer";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";
import { formatJstTime, formatLatency, formatTokens } from "@/lib/format";
import type { TraceEntry } from "@/lib/types";

/**
 * Heavy fields the drawer renders. Sourced either from `entry` itself
 * (sample page, no slimming) or fetched lazily from
 * `/api/games/[gameId]/trace/[index]` on first open.
 */
interface HeavyFields {
  system_prompt: string;
  user_prompt: string;
  response: string | null;
}

export type TraceFetcher = (index: number) => Promise<HeavyFields>;

export default function TraceDrawer({
  entry,
  index,
  fetcher,
  onClose,
}: {
  entry: TraceEntry | null;
  index: number | null;
  /**
   * Lazy-load callback for heavy fields. ``null`` ⇒ the entry already
   * carries them (no fetch needed); otherwise the drawer calls
   * `fetcher(index)` once on open and shows a spinner until it
   * resolves. Errors render an inline message; the user can close and
   * retry.
   */
  fetcher: TraceFetcher | null;
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
      {entry !== null && (
        <Body
          entry={entry}
          index={index}
          fetcher={fetcher}
          onClose={onClose}
        />
      )}
    </Drawer>
  );
}

function Body({
  entry,
  index,
  fetcher,
  onClose,
}: {
  entry: TraceEntry;
  index: number | null;
  fetcher: TraceFetcher | null;
  onClose: () => void;
}) {
  const ts = (() => {
    try {
      return formatJstTime(new Date(entry.ts).getTime());
    } catch {
      return entry.ts;
    }
  })();

  // Decide whether the entry is already loaded with its heavy strings
  // or if we need to lazy-fetch them. The slim payload puts empty
  // strings in `system_prompt` / `user_prompt`; if the original was
  // genuinely empty (rare, but theoretically possible for a malformed
  // call) we'd still try to fetch — that just returns an empty
  // payload, harmless. The boolean is cheap to compute and cleaner
  // than threading an explicit `slim: true` flag through the prop
  // chain.
  const needsFetch =
    fetcher !== null &&
    index !== null &&
    entry.system_prompt === "" &&
    entry.user_prompt === "";

  const [heavy, setHeavy] = React.useState<HeavyFields | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);

  React.useEffect(() => {
    // Reset on entry change so a previous payload doesn't bleed across
    // drawer-open events. Skip the fetch when the entry already has
    // its heavy fields.
    setHeavy(null);
    setError(null);
    if (!needsFetch) return;
    setLoading(true);
    let cancelled = false;
    fetcher!(index!)
      .then((data) => {
        if (!cancelled) setHeavy(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [entry, index, needsFetch, fetcher]);

  // Pull from `heavy` when the lazy fetch resolved, otherwise from
  // `entry` (sample-page / non-slim path). The fallback mirrors the
  // pre-refactor behavior so the sample page renders identically.
  const systemPrompt = heavy?.system_prompt ?? entry.system_prompt;
  const userPrompt = heavy?.user_prompt ?? entry.user_prompt;
  const response = heavy?.response ?? entry.response;

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

      {error && (
        <Box
          sx={{
            p: 1.5,
            mb: 2,
            bgcolor: "warning.lighter",
            color: "warning.dark",
            border: "1px solid",
            borderColor: "warning.light",
            borderRadius: 1,
          }}
        >
          <Typography variant="caption" sx={{ fontWeight: 600 }}>
            プロンプトの読み込みに失敗
          </Typography>
          <pre>{error}</pre>
        </Box>
      )}

      <Section title="System prompt">
        <PromptBody loading={loading && !heavy} text={systemPrompt} />
      </Section>
      <Section title="User prompt">
        <PromptBody loading={loading && !heavy} text={userPrompt} />
      </Section>
      <Section title="Response">
        <PromptBody loading={loading && !heavy} text={response ?? "(no response)"} />
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

function PromptBody({ loading, text }: { loading: boolean; text: string }) {
  if (loading) {
    return (
      <Stack
        direction="row"
        spacing={1}
        alignItems="center"
        sx={{ color: "text.secondary", py: 1 }}
      >
        <CircularProgress size={14} />
        <Typography variant="body2">読み込み中…</Typography>
      </Stack>
    );
  }
  return <pre>{text}</pre>;
}
