import GameView from "@/components/GameView";
import { loadGame } from "@/lib/data";

export default async function Page() {
  const data = await loadGame();
  return <GameView data={data} />;
}
