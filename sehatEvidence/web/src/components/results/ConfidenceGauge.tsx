/**
 * Hand-rolled SVG arc gauge (no charting library -- see the plan's
 * rationale). Visually anchors the Verifier's own 0.70 "weakly
 * supported" threshold (agents/verifier.py: min_support_confidence)
 * with a tick mark, so a 0.62 confidence claim visibly sits below the
 * line that got it flagged, not just as a bare number.
 */
const THRESHOLD = 0.7;
const SIZE = 40;
const STROKE = 4;
const RADIUS = (SIZE - STROKE) / 2;
const CIRC = 2 * Math.PI * RADIUS;
// 270° sweep starting at -225° (bottom-left), matching a common gauge look.
const SWEEP = 270;
const START_ANGLE = -225;

function angleToOffset(fraction: number): number {
  const arcLength = (SWEEP / 360) * CIRC;
  return CIRC - fraction * arcLength;
}

export function ConfidenceGauge({ confidence }: { confidence: number | null }) {
  if (confidence == null) {
    return (
      <div className="flex items-center gap-1.5 font-mono text-[11px] text-ink-faint">
        <span className="h-2 w-2 rounded-full border border-ink-faint" aria-hidden="true" />
        no confidence score
      </div>
    );
  }

  const clamped = Math.max(0, Math.min(1, confidence));
  const color = clamped >= THRESHOLD ? "var(--eb-pass)" : "var(--eb-flag)";
  const arcLength = (SWEEP / 360) * CIRC;
  const dashArray = `${arcLength} ${CIRC}`;

  const thresholdRotation = START_ANGLE + THRESHOLD * SWEEP;

  return (
    <div className="flex items-center gap-2">
      <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label={`Confidence ${clamped.toFixed(2)} of 1, weakly-supported threshold at 0.70`}>
        <g transform={`rotate(${START_ANGLE} ${SIZE / 2} ${SIZE / 2})`}>
          <circle
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={RADIUS}
            fill="none"
            stroke="var(--eb-rule)"
            strokeWidth={STROKE}
            strokeDasharray={dashArray}
            strokeLinecap="round"
          />
          <circle
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={RADIUS}
            fill="none"
            stroke={color}
            strokeWidth={STROKE}
            strokeDasharray={`${arcLength} ${CIRC}`}
            strokeDashoffset={angleToOffset(clamped)}
            strokeLinecap="round"
          />
        </g>
        {/* Threshold tick */}
        <g transform={`rotate(${thresholdRotation} ${SIZE / 2} ${SIZE / 2})`}>
          <line x1={SIZE / 2 + RADIUS - STROKE} y1={SIZE / 2} x2={SIZE / 2 + RADIUS + STROKE} y2={SIZE / 2} stroke="var(--eb-ink-faint)" strokeWidth={1.5} />
        </g>
        <text x={SIZE / 2} y={SIZE / 2 + 4} textAnchor="middle" className="font-mono" fontSize="10" fill="var(--eb-ink)">
          {clamped.toFixed(2)}
        </text>
      </svg>
      <span className="font-mono text-[10.5px] text-ink-faint">
        {clamped >= THRESHOLD ? "above" : "below"} 0.70 threshold
      </span>
    </div>
  );
}
