import React from 'react';
import { useCurrentFrame, interpolate, spring, useVideoConfig, AbsoluteFill } from 'remotion';
import { colors, fonts } from '../styles/tokens';
import { Wordmark } from './Wordmark';
import { PHBadge } from './PHBadge';

export const EndCard: React.FC<{ compact?: boolean }> = ({ compact = false }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const enter = spring({ frame, fps, config: { damping: 16 } });

  const fadeAt = (start: number, end: number) =>
    interpolate(frame, [start, end], [0, 1], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    });

  return (
    <AbsoluteFill
      style={{
        background: colors.bg,
        justifyContent: 'center',
        alignItems: 'center',
        fontFamily: fonts.mono,
        gap: compact ? 22 : 32,
        padding: 80,
      }}
    >
      <div style={{ opacity: enter, transform: `scale(${0.96 + enter * 0.04})` }}>
        <Wordmark scale={compact ? 0.8 : 1.1} animate={false} />
      </div>

      <div
        style={{
          opacity: fadeAt(10, 26),
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: 14,
        }}
      >
        <CommandLine cmd="pipx install swm-gpu" compact={compact} />
        <CommandLine cmd="brew install swm-gpu/swm/swm" muted compact={compact} />
      </div>

      <div
        style={{
          opacity: fadeAt(22, 42),
          display: 'flex',
          gap: compact ? 18 : 28,
          color: colors.textMuted,
          fontSize: compact ? 18 : 22,
          marginTop: 12,
          alignItems: 'center',
          flexWrap: 'wrap',
          justifyContent: 'center',
        }}
      >
        <span>swmgpu.com</span>
        <span style={{ color: colors.textSubtle }}>·</span>
        <span style={{ color: colors.accent }}>★ github.com/swm-gpu/swm</span>
      </div>

      <div style={{ opacity: fadeAt(36, 60), marginTop: 16 }}>
        <PHBadge scale={compact ? 0.85 : 1} />
      </div>
    </AbsoluteFill>
  );
};

const CommandLine: React.FC<{ cmd: string; muted?: boolean; compact?: boolean }> = ({
  cmd,
  muted,
  compact,
}) => (
  <div
    style={{
      display: 'flex',
      gap: 14,
      alignItems: 'center',
      fontSize: compact ? 22 : 28,
      color: muted ? colors.textMuted : colors.text,
      background: muted ? 'transparent' : colors.panel,
      padding: muted ? '8px 18px' : `${compact ? 12 : 16}px ${compact ? 22 : 30}px`,
      border: muted ? `1px dashed ${colors.border}` : `1px solid ${colors.border}`,
      borderRadius: 8,
      borderLeft: muted ? `1px dashed ${colors.border}` : `3px solid ${colors.accent}`,
      fontFamily: 'inherit',
    }}
  >
    <span style={{ color: muted ? colors.textSubtle : colors.accent, fontWeight: 600 }}>$</span>
    <span>{cmd}</span>
  </div>
);
