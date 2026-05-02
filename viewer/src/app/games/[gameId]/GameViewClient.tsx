"use client";

import GameView from "@/components/GameView";
import type { TraceFetcher } from "@/components/TraceDrawer";
import type { MatchMaps } from "@/lib/match";
import type { GameSample } from "@/lib/types";

/**
 * Tiny client wrapper around `GameView`. Lives next to the server
 * `page.tsx` so the colocated `gameId` is closed over by the lazy
 * trace fetcher without leaking the URL into `GameView`'s prop API.
 *
 * Why a separate file rather than inlining: server components can't
 * ship a function across the boundary, so the closure has to live in
 * a "use client" file.
 */
export default function GameViewClient({
  gameId,
  data,
  matches,
}: {
  gameId: string;
  data: GameSample;
  matches: MatchMaps;
}) {
  const fetcher: TraceFetcher = async (index) => {
    const res = await fetch(`/api/games/${gameId}/trace/${index}`);
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    return (await res.json()) as {
      system_prompt: string;
      user_prompt: string;
      response: string | null;
    };
  };
  return (
    <GameView
      data={data}
      matches={matches}
      traceFetcher={fetcher}
      backHref="/"
    />
  );
}
