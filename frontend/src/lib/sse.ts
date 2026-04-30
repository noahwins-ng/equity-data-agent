/**
 * Minimal Server-Sent Events parser for the agent chat panel (QNT-74).
 *
 * Per ADR-008 / ADR-014 §4: no Vercel AI SDK, no third-party `eventsource`
 * library — the protocol is small enough that a hand-rolled parser is easier
 * to audit than a dependency. We accept the canonical W3C subset our FastAPI
 * endpoint emits: ``event: <name>\n`` lines and ``data: <json>\n`` lines,
 * separated by a blank line per event.
 *
 * Keeping the parser in `lib/` (not inside the chat-panel component) lets the
 * eval harness or a future debugging surface consume the same stream without
 * importing React.
 */

export type SseEvent = {
  /** SSE event name. Falls back to `"message"` when no `event:` line is seen. */
  event: string;
  /** Raw `data:` payload — typically JSON; the caller decides how to parse. */
  data: string;
};

/**
 * Parse a `Response` body (SSE) into an async iterable of events.
 *
 * Handles partial frames at chunk boundaries — events are only yielded once a
 * full `\n\n` terminator is observed in the buffer.
 */
export async function* parseSseStream(
  response: Response,
  signal?: AbortSignal,
): AsyncGenerator<SseEvent, void, void> {
  if (!response.body) {
    throw new Error("SSE response has no body");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      if (signal?.aborted) {
        await reader.cancel();
        return;
      }
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let separatorIdx;
      while ((separatorIdx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, separatorIdx);
        buffer = buffer.slice(separatorIdx + 2);
        const parsed = parseFrame(frame);
        if (parsed) yield parsed;
      }
    }
    // Drain any final frame that wasn't followed by `\n\n`. Most servers
    // terminate with a blank line, but accepting a trailing event is more
    // forgiving and matches every reference implementation we tested.
    if (buffer.trim()) {
      const parsed = parseFrame(buffer);
      if (parsed) yield parsed;
    }
  } finally {
    reader.releaseLock();
  }
}

function parseFrame(frame: string): SseEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue; // empty or SSE comment
    const colon = line.indexOf(":");
    if (colon === -1) continue;
    const field = line.slice(0, colon);
    // Spec says exactly one optional space after the colon.
    let value = line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }
  if (!dataLines.length) return null;
  return { event, data: dataLines.join("\n") };
}
