import React from 'react';
import { useCurrentFrame, interpolate, spring, useVideoConfig, AbsoluteFill } from 'remotion';
import { colors, fonts } from '../styles/tokens';
import { Wordmark } from './Wordmark';

export const ChapterCard: React.FC<{
  title?: string;
  subtitle?: string;
  pronunciation?: string;
  showDivider?: boolean;
  accent?: string;
  showWordmark?: boolean;
  smallTitle?: boolean;
}> = ({
  title,
  subtitle,
  pronunciation,
  showDivider = false,
  accent = colors.accent,
  showWordmark = false,
  smallTitle = false,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  const enter = spring({ frame, fps, config: { damping: 18 } });
  const exit = interpolate(
    frame,
    [durationInFrames - 18, durationInFrames],
    [1, 0],
    { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }
  );
  const opacity = enter * exit;
  const titleY = interpolate(enter, [0, 1], [40, 0]);

  return (
    <AbsoluteFill
      style={{
        background: colors.bg,
        justifyContent: 'center',
        alignItems: 'center',
        fontFamily: fonts.mono,
        gap: 32,
        padding: 80,
      }}
    >
      {showWordmark && (
        <div style={{ opacity, transform: `translateY(${titleY * 0.6}px)`, marginBottom: 8 }}>
          <Wordmark animate={false} scale={1} />
        </div>
      )}
      {title && (
        <div
          style={{
            color: accent,
            fontSize: smallTitle ? 56 : 84,
            fontWeight: 700,
            opacity,
            transform: `translateY(${titleY}px)`,
            letterSpacing: '-0.02em',
            textAlign: 'center',
            maxWidth: '90%',
            lineHeight: 1.15,
          }}
        >
          {title}
        </div>
      )}
      {pronunciation && (
        <div
          style={{
            color: colors.textMuted,
            fontSize: 28,
            opacity,
            transform: `translateY(${titleY * 0.5}px)`,
            textAlign: 'center',
          }}
        >
          {pronunciation}
        </div>
      )}
      {showDivider && (
        <div
          style={{
            width: 280,
            height: 1,
            background: colors.borderSubtle,
            opacity: opacity * 0.6,
            transform: `translateY(${titleY * 0.4}px)`,
          }}
        />
      )}
      {subtitle && (
        <div
          style={{
            color: colors.textMuted,
            fontSize: 36,
            opacity,
            transform: `translateY(${titleY * 0.5}px)`,
            textAlign: 'center',
            maxWidth: 1200,
            lineHeight: 1.4,
          }}
        >
          {subtitle}
        </div>
      )}
    </AbsoluteFill>
  );
};
