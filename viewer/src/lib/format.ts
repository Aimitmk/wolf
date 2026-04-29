import type { RoleKey, Seat } from "./types";

const ROLE_JA: Record<RoleKey, string> = {
  VILLAGER: "村人",
  WEREWOLF: "人狼",
  MADMAN: "狂人",
  SEER: "占い師",
  MEDIUM: "霊媒師",
  KNIGHT: "騎士",
};

export function roleJa(role: RoleKey): string {
  return ROLE_JA[role] ?? role;
}

const ROLE_FACTION: Record<RoleKey, "village" | "wolf"> = {
  VILLAGER: "village",
  SEER: "village",
  MEDIUM: "village",
  KNIGHT: "village",
  WEREWOLF: "wolf",
  MADMAN: "wolf",
};

export function roleFaction(role: RoleKey): "village" | "wolf" {
  return ROLE_FACTION[role] ?? "village";
}

const PHASE_JA: Record<string, string> = {
  SETUP: "セットアップ",
  NIGHT_0: "初日夜 (Night 0)",
  DAY_DISCUSSION: "議論",
  DAY_VOTE: "投票",
  DAY_RUNOFF_SPEECH: "決選演説",
  DAY_RUNOFF: "決選投票",
  NIGHT: "夜",
  GAME_OVER: "終了",
  WAITING_HOST_DECISION: "ホスト待ち",
};

export function phaseJa(phase: string): string {
  return PHASE_JA[phase] ?? phase;
}

const NIGHT_ACTION_JA: Record<string, string> = {
  ATTACK: "襲撃",
  DIVINE: "占い",
  GUARD: "護衛",
  SEER_DIVINE: "占い",
  WOLF_ATTACK: "襲撃",
  KNIGHT_GUARD: "護衛",
  DIVINE_NIGHT0_RANDOM_WHITE: "初日ランダム白",
};

export function nightActionJa(kind: string): string {
  return NIGHT_ACTION_JA[kind] ?? kind;
}

export function seatLabel(seat: Seat): string {
  return `席${seat.seat_no} ${seat.display_name}`;
}

export function findSeat(seats: Seat[], seatNo: number | null): Seat | null {
  if (seatNo == null) return null;
  return seats.find((s) => s.seat_no === seatNo) ?? null;
}

/**
 * Format epoch ms as ``HH:MM:SS`` in Asia/Tokyo for the timeline columns.
 * Game data uses real wall-clock timestamps so JST is the natural axis.
 */
export function formatJstTime(ms: number): string {
  const d = new Date(ms);
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

/** ``YYYY-MM-DD HH:mm`` in JST. Used by the games-list table. */
export function formatJstDate(ms: number): string {
  const d = new Date(ms);
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

export function formatTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString("en-US");
}

export function formatLatency(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

/** Compact human duration (e.g. "12:34" or "1h 02m" for longer games). */
export function formatDuration(ms: number | null): string {
  if (ms == null) return "進行中";
  const totalSeconds = Math.floor(ms / 1000);
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h > 0) {
    return `${h}h ${m.toString().padStart(2, "0")}m`;
  }
  return `${m}:${s.toString().padStart(2, "0")}`;
}

const SOURCE_JA: Record<string, string> = {
  text: "テキスト議論",
  voice_stt: "音声→STT",
  npc_generated: "NPC生成",
};

export function sourceJa(source: string): string {
  return SOURCE_JA[source] ?? source;
}
