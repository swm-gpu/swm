import React from 'react';
import { useCurrentFrame, interpolate } from 'remotion';
import { colors } from '../styles/tokens';
import type { ProviderStatus } from '../script/scrubbed-logs';

export const ProviderCascade: React.FC<{
  providers: ProviderStatus[];
  startFrame: number;
  framesBetween?: number;
}> = ({ providers, startFrame, framesBetween = 14 }) => {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - startFrame);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {providers.map((p, i) => {
        const showFrame = i * framesBetween;
        if (elapsed < showFrame) {
          return <div key={i} style={{ height: 30 }} />;
        }
        const fade = interpolate(elapsed - showFrame, [0, 10], [0, 1], {
          extrapolateRight: 'clamp',
        });
        return (
          <div
            key={i}
            style={{
              opacity: fade,
              transform: `translateY(${(1 - fade) * 6}px)`,
              display: 'flex',
              gap: 16,
              alignItems: 'baseline',
              height: 30,
              whiteSpace: 'pre',
            }}
          >
            <span style={{ color: colors.textSubtle, fontSize: 16, minWidth: 72 }}>
              [+{(i * 0.3).toFixed(1)}s]
            </span>
            <span
              style={{
                color: p.status === 'ok' ? colors.ok : colors.error,
                fontWeight: 700,
              }}
            >
              {p.status === 'ok' ? '✓' : '✗'}
            </span>
            <span style={{ color: colors.text, minWidth: 200, fontWeight: 600 }}>{p.name}</span>
            <span style={{ color: colors.textMuted }}>— {p.info}</span>
          </div>
        );
      })}
    </div>
  );
};
