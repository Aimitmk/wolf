import GameView from "@/components/GameView";
import { loadSample } from "@/lib/data";

export default async function SampleGamePage() {
  const data = await loadSample();
  // Sample page intentionally skips the slim/lazy treatment — the
  // bundled JSON is small and we want the demo to render fully without
  // an API round-trip. Pass `matches=null` so GameView falls back to
  // computing the match map in-browser; pass `traceFetcher=null` so
  // TraceDrawer reads the heavy fields directly from each TraceEntry.
  return (
    <GameView
      data={data}
      matches={null}
      traceFetcher={null}
      backHref="/"
      sampleBadge
    />
  );
}
