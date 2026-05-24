import React from 'react';
import { colors, fonts } from '../styles/tokens';

export const TerminalFrame: React.FC<{
  title?: string;
  children: React.ReactNode;
  width?: number | string;
  height?: number | string;
  padding?: number;
  fontSize?: number;
}> = ({
  title = '~ swm',
  children,
  width = '100%',
  height = '100%',
  padding = 32,
  fontSize = 28,
}) => {
  return (
    <div
      style={{
        width,
        height,
        background: colors.panel,
        borderRadius: 12,
        border: `1px solid ${colors.border}`,
        boxShadow: `0 24px 80px rgba(0, 0, 0, 0.6), 0 0 0 1px ${colors.borderSubtle}`,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        fontFamily: fonts.mono,
      }}
    >
      <div
        style={{
          height: 40,
          display: 'flex',
          alignItems: 'center',
          padding: '0 18px',
          background: '#161616',
          borderBottom: `1px solid ${colors.border}`,
          flexShrink: 0,
        }}
      >
        <div style={{ display: 'flex', gap: 8 }}>
          <Dot color="#ff5f57" />
          <Dot color="#febc2e" />
          <Dot color="#28c840" />
        </div>
        <div
          style={{
            flex: 1,
            textAlign: 'center',
            color: colors.textMuted,
            fontSize: 14,
            fontWeight: 600,
            letterSpacing: '0.02em',
          }}
        >
          {title}
        </div>
        <div style={{ width: 60 }} />
      </div>

      <div
        style={{
          flex: 1,
          padding,
          color: colors.text,
          fontSize,
          lineHeight: 1.5,
          overflow: 'hidden',
        }}
      >
        {children}
      </div>
    </div>
  );
};

const Dot: React.FC<{ color: string }> = ({ color }) => (
  <div style={{ width: 12, height: 12, borderRadius: '50%', background: color }} />
);
