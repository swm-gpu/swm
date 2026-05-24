import React from 'react';
import { useCurrentFrame, interpolate } from 'remotion';
import { colors } from '../styles/tokens';

const colorFor = (line: string): string => {
  if (/^\s*✓/.test(line)) return colors.ok;
  if (/^\s*✗/.test(line)) return colors.error;
  if (/^\s*▸/.test(line)) return colors.cyan;
  if (/Cost:/.test(line)) return colors.amber;
  if (/^\s*████/.test(line)) return colors.accent;
  if (/(Proceed\?|Provider:|GPU:|Workspace:|Storage:|Lifecycle:|Action:)/.test(line))
    return colors.textMuted;
  if (/Tunnel active|localhost:/.test(line)) return colors.accent;
  if (/terminated/.test(line)) return colors.warn;
  return colors.text;
};

const isStrong = (line: string): boolean =>
  /Tunnel active|terminated|Workspace restored|ComfyUI started|vLLM running/.test(line);

export const OutputBlock: React.FC<{
  lines: string[];
  startFrame: number;
  framesPerLine?: number;
}> = ({ lines, startFrame, framesPerLine = 6 }) => {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - startFrame);

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {lines.map((line, i) => {
        const lineFrame = i * framesPerLine;
        const visible = elapsed >= lineFrame;
        if (!visible) return <div key={i} style={{ height: 0 }} />;
        const fade = interpolate(elapsed - lineFrame, [0, 6], [0, 1], {
          extrapolateRight: 'clamp',
        });
        return (
          <div
            key={i}
            style={{
              whiteSpace: 'pre',
              opacity: fade,
              transform: `translateY(${(1 - fade) * 4}px)`,
              color: colorFor(line),
              fontWeight: isStrong(line) ? 600 : 400,
              minHeight: 32,
            }}
          >
            {line || '\u00A0'}
          </div>
        );
      })}
    </div>
  );
};
