import React from 'react';
import { useCurrentFrame, interpolate } from 'remotion';
import { colors } from '../styles/tokens';
import type { GpuRow } from '../script/scrubbed-logs';

type Col = {
  key: keyof GpuRow;
  label: string;
  width: number;
  align?: 'right' | 'center';
};

const columns: Col[] = [
  { key: 'provider', label: 'Provider', width: 182 },
  { key: 'gpu',      label: 'GPU',      width: 182 },
  { key: 'flag',     label: '-g',       width: 308 },
  { key: 'vram',     label: 'VRAM',     width: 126, align: 'right' },
  { key: 'n',        label: '×',        width: 70,  align: 'center' },
  { key: 'price',    label: '$/hr',     width: 126, align: 'right' },
  { key: 'stock',    label: 'Stock',    width: 168 },
  { key: 'cuda',     label: 'CUDA',     width: 112, align: 'right' },
  { key: 'region',   label: 'Regions',  width: 210 },
  { key: 'secure',   label: 'Secure',   width: 98,  align: 'center' },
];

export const GpuTable: React.FC<{
  rows: GpuRow[];
  startFrame: number;
  framesBetween?: number;
  winnerPulseAt?: number;
  fontSize?: number;
}> = ({ rows, startFrame, framesBetween = 10, winnerPulseAt, fontSize = 24 }) => {
  const frame = useCurrentFrame();

  if (frame < startFrame) return null;

  const elapsed = frame - startFrame;

  return (
    <div
      style={{
        background: colors.panelLight,
        border: `1px solid ${colors.border}`,
        borderRadius: 8,
        overflow: 'hidden',
        fontSize,
      }}
    >
      <div
        style={{
          display: 'flex',
          padding: '14px 18px',
          borderBottom: `1px solid ${colors.border}`,
          background: '#141414',
          color: colors.textMuted,
          fontWeight: 700,
          textTransform: 'none',
          letterSpacing: '0.02em',
        }}
      >
        {columns.map((c) => (
          <div
            key={c.key}
            style={{ width: c.width, textAlign: c.align ?? 'left', paddingRight: 8 }}
          >
            {c.label}
          </div>
        ))}
      </div>

      {rows.map((row, i) => {
        const showFrame = i * framesBetween;
        if (elapsed < showFrame) return null;
        const fade = interpolate(elapsed - showFrame, [0, 8], [0, 1], {
          extrapolateRight: 'clamp',
        });

        const pulse =
          row.winner && winnerPulseAt != null && elapsed >= winnerPulseAt
            ? interpolate(
                (elapsed - winnerPulseAt) % 90,
                [0, 30, 60, 90],
                [0, 1, 0, 0],
                { extrapolateRight: 'clamp' }
              )
            : 0;

        const bg = row.winner
          ? `rgba(0, 255, 136, ${0.05 + pulse * 0.18})`
          : 'transparent';

        const boxShadow = row.winner
          ? pulse > 0.05
            ? `inset 3px 0 0 ${colors.accent}, 0 0 ${pulse * 32}px rgba(0, 255, 136, ${
                pulse * 0.4
              })`
            : `inset 3px 0 0 ${colors.accent}`
          : 'none';

        return (
          <div
            key={i}
            style={{
              display: 'flex',
              padding: '11px 18px',
              borderBottom:
                i < rows.length - 1 ? `1px solid ${colors.borderSubtle}` : 'none',
              background: bg,
              opacity: fade,
              transform: `translateY(${(1 - fade) * 4}px)`,
              color: row.winner ? colors.accentGlow : colors.text,
              boxShadow,
              transition: 'background 0.1s ease',
            }}
          >
            {columns.map((c) => {
              const v = row[c.key];
              const display =
                typeof v === 'boolean' ? (v ? '✓' : '—') : String(v ?? '—');
              const isPrice = c.key === 'price';
              return (
                <div
                  key={c.key}
                  style={{
                    width: c.width,
                    textAlign: c.align ?? 'left',
                    paddingRight: 8,
                    color: isPrice && row.winner ? colors.accent : undefined,
                    fontWeight: isPrice && row.winner ? 700 : undefined,
                  }}
                >
                  {display}
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
};
