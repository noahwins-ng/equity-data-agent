// ─── QNT-229 #2a: pixel-ring spinner ──────────────────────────────────────
//
// NxN pixel grid: a lit pixel chases clockwise around the perimeter (a fine
// pixel-ring loader). The exact centre pixel breathes as a nucleus; the rest of
// the interior is an empty spacer so the ring reads cleanly. PIXEL_SPIN_MS MUST
// equal the .pixel-chase CSS animation-duration so the negative-delay stagger
// spans exactly one loop (otherwise the comet has a seam).
const PIXEL_GRID = 3;
const PIXEL_SPIN_MS = 1000;

// Perimeter cell indices (row-major) clockwise from the top-left corner.
function ringPerimeter(n: number): number[] {
  const cells: number[] = [];
  for (let c = 0; c < n; c++) cells.push(c); // top row, L→R
  for (let r = 1; r < n; r++) cells.push(r * n + (n - 1)); // right col, T→B
  for (let c = n - 2; c >= 0; c--) cells.push((n - 1) * n + c); // bottom row, R→L
  for (let r = n - 2; r >= 1; r--) cells.push(r * n); // left col, B→T
  return cells;
}
const PIXEL_PERIMETER = ringPerimeter(PIXEL_GRID);
const PIXEL_CENTER = Math.floor(PIXEL_GRID / 2) * PIXEL_GRID + Math.floor(PIXEL_GRID / 2);

export function PixelSpinner() {
  const order = new Array<number>(PIXEL_GRID * PIXEL_GRID).fill(-1);
  PIXEL_PERIMETER.forEach((cellIdx, i) => {
    order[cellIdx] = i;
  });
  return (
    <span aria-hidden className="grid shrink-0 grid-cols-3 gap-0.5">
      {order.map((ord, idx) => {
        if (ord !== -1) {
          return (
            <span
              key={idx}
              className="pixel-chase h-1.5 w-1.5 rounded-[1px] bg-emerald-400"
              style={{
                // Negative stagger keyed to (len - ord) so the bright head
                // travels FORWARD along the perimeter (clockwise).
                animationDelay: `-${((PIXEL_PERIMETER.length - ord) / PIXEL_PERIMETER.length) * PIXEL_SPIN_MS}ms`,
              }}
            />
          );
        }
        if (idx === PIXEL_CENTER) {
          // Single breathing nucleus at the centre of the ring.
          return <span key={idx} className="pixel-core h-1.5 w-1.5 rounded-[1px] bg-emerald-500" />;
        }
        return <span key={idx} className="h-1.5 w-1.5" />; // interior spacer (none at 3x3)
      })}
    </span>
  );
}
