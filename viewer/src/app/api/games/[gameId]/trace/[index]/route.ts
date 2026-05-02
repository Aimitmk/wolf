// Lazy fetch endpoint for one LLM trace entry's heavy fields.
//
// The detail page strips `system_prompt` / `user_prompt` / `response`
// from the SSR payload to keep the initial HTML small. When the user
// opens TraceDrawer, it calls this route with the trace's index in
// `data.trace[]` to fetch just those three strings on demand.
//
// `index` is the array position — stable for the lifetime of the
// exported JSON file, which is itself immutable post-game.

import { NextResponse } from "next/server";
import { loadTraceHeavyFields } from "@/lib/data";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ gameId: string; index: string }> },
) {
  const { gameId, index } = await params;
  const idx = Number(index);
  if (!Number.isInteger(idx) || idx < 0) {
    return NextResponse.json({ error: "invalid index" }, { status: 400 });
  }
  const heavy = await loadTraceHeavyFields(gameId, idx);
  if (heavy === null) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
  return NextResponse.json(heavy);
}
