import React from 'react';
import { colors } from '../styles/tokens';

export const PHBadge: React.FC<{ scale?: number }> = ({ scale = 1 }) => {
  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 14,
        padding: `${12 * scale}px ${22 * scale}px`,
        background: 'transparent',
        border: `1.5px solid ${colors.orange}`,
        borderRadius: 10,
        color: colors.orange,
      }}
    >
      <div
        style={{
          width: 32 * scale,
          height: 32 * scale,
          borderRadius: '50%',
          border: `2px solid ${colors.orange}`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 18 * scale,
          fontWeight: 800,
          fontFamily: 'system-ui, -apple-system, sans-serif',
        }}
      >
        P
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <span
          style={{
            fontSize: 12 * scale,
            letterSpacing: '0.1em',
            textTransform: 'uppercase',
            opacity: 0.7,
            fontWeight: 600,
          }}
        >
          Launching soon on
        </span>
        <span style={{ fontSize: 18 * scale, fontWeight: 700, letterSpacing: '-0.01em' }}>
          Product Hunt
        </span>
      </div>
    </div>
  );
};
