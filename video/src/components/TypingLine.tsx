import React from 'react';
import { useCurrentFrame } from 'remotion';
import { colors } from '../styles/tokens';

export const TypingLine: React.FC<{
  text: string;
  prompt?: string;
  startFrame: number;
  framesPerChar?: number;
  showCursor?: boolean;
  cursorAfter?: boolean;
}> = ({
  text,
  prompt = '$',
  startFrame,
  framesPerChar = 1.6,
  showCursor = true,
  cursorAfter = true,
}) => {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - startFrame);
  const charsShown = Math.min(text.length, Math.floor(elapsed / framesPerChar));
  const displayed = text.slice(0, charsShown);
  const done = charsShown >= text.length;
  const cursorOn = Math.floor(frame / 18) % 2 === 0;
  const cursorVisible = showCursor && (!done || cursorAfter);

  if (elapsed <= 0) {
    return null;
  }

  return (
    <div style={{ display: 'flex', whiteSpace: 'pre', alignItems: 'center' }}>
      <span style={{ color: colors.accent, marginRight: 14, fontWeight: 600 }}>{prompt}</span>
      <span style={{ color: colors.text }}>{displayed}</span>
      {cursorVisible && (
        <span
          style={{
            display: 'inline-block',
            width: 12,
            height: 22,
            background: cursorOn ? colors.accent : 'transparent',
            marginLeft: 4,
          }}
        />
      )}
    </div>
  );
};
