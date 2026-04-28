import GameView from "@/components/GameView";
import { loadSample } from "@/lib/data";

export default async function SampleGamePage() {
  const data = await loadSample();
  return <GameView data={data} backHref="/" sampleBadge />;
}
