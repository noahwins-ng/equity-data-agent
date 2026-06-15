// ─── Inline-chip prose renderer ───────────────────────────────────────────
//
// The synthesis prompt produces inline citations like `(source: technical)`
// and free-text values inside the prose. The chat-panel design wants
// "value · source · date" chips. We surface the citation as a chip rendered
// in monospaced muted style; the prose author chooses how dense to be.
// Falls back gracefully when no chip-shaped tokens are present.

const CHIP_PATTERN = /\(source:\s*([A-Za-z|\s]+)\)/g;

type ProseSegment = { type: "text"; text: string } | { type: "chip"; text: string };

function splitProseIntoSegments(text: string): ProseSegment[] {
  if (!text) return [];
  const segments: ProseSegment[] = [];
  let lastIdx = 0;
  for (const match of text.matchAll(CHIP_PATTERN)) {
    const matchStart = match.index ?? 0;
    if (matchStart > lastIdx) {
      segments.push({ type: "text", text: text.slice(lastIdx, matchStart) });
    }
    segments.push({ type: "chip", text: match[1].trim() });
    lastIdx = matchStart + match[0].length;
  }
  if (lastIdx < text.length) {
    segments.push({ type: "text", text: text.slice(lastIdx) });
  }
  return segments;
}

export function ProseBlock({ text }: { text: string }) {
  if (!text.trim()) return null;
  const segments = splitProseIntoSegments(text);
  return (
    <p className="text-xs leading-relaxed text-zinc-200">
      {segments.map((seg, i) =>
        seg.type === "chip" ? (
          <span
            key={i}
            className="mx-0.5 inline-block rounded border border-zinc-700 bg-zinc-900 px-1 py-px font-mono text-[10px] uppercase tracking-wide text-zinc-400"
            title="cited source"
          >
            {seg.text}
          </span>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </p>
  );
}
