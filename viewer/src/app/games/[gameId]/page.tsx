import { notFound } from "next/navigation";
import GameView from "@/components/GameView";
import { loadGameById } from "@/lib/data";

export default async function GameDetailPage({
  params,
}: {
  params: Promise<{ gameId: string }>;
}) {
  const { gameId } = await params;
  const data = await loadGameById(gameId);
  if (data === null) {
    notFound();
  }
  return <GameView data={data} backHref="/" />;
}
