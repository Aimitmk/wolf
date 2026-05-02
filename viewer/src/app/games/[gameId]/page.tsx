import { notFound } from "next/navigation";
import GameViewClient from "./GameViewClient";
import { loadGameWithMatches } from "@/lib/data";

export default async function GameDetailPage({
  params,
}: {
  params: Promise<{ gameId: string }>;
}) {
  const { gameId } = await params;
  const result = await loadGameWithMatches(gameId);
  if (result === null) {
    notFound();
  }
  // The client wrapper builds the lazy-fetch closure from `gameId`.
  // Server components can't ship a function as a prop, so the fetcher
  // is constructed in a "use client" sibling instead of here.
  return (
    <GameViewClient
      gameId={gameId}
      data={result.data}
      matches={result.matches}
    />
  );
}
