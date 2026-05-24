import React from 'react';
import { useCurrentFrame, interpolate, spring, useVideoConfig } from 'remotion';
import { colors } from '../styles/tokens';

export const Wordmark: React.FC<{ scale?: number; animate?: boolean }> = ({
  scale = 1,
  animate = true,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const enter = animate
    ? spring({ frame, fps, config: { damping: 14, stiffness: 120 } })
    : 1;
  const opacity = animate
    ? interpolate(frame, [0, 14], [0, 1], { extrapolateRight: 'clamp' })
    : 1;
  const y = animate ? interpolate(enter, [0, 1], [16, 0]) : 0;

  const w = 61.2 * 6 * scale;
  const h = 38 * 6 * scale;

  return (
    <div
      style={{
        opacity,
        transform: `translateY(${y}px)`,
        display: 'inline-block',
        lineHeight: 0,
      }}
    >
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 61.2 38" width={w} height={h} fill="none">
        <path
          d="M277 -9Q179 -9 122.0 35.5Q65 80 65 156H188Q188 124 211.5 107.0Q235 90 277 90H321Q366 90 390.5 107.0Q415 124 415 156Q415 186 398.0 200.5Q381 215 346 220L209 238Q143 247 107.5 289.0Q72 331 72 398Q72 476 124.0 517.5Q176 559 274 559H320Q412 559 468.0 517.0Q524 475 526 405H402Q401 431 379.0 446.5Q357 462 320 462H274Q234 462 213.0 445.5Q192 429 192 401Q192 376 207.5 364.0Q223 352 253 349L382 331Q458 322 496.5 277.5Q535 233 535 158Q535 78 480.5 34.5Q426 -9 321 -9Z"
          transform="translate(0.00, 34.00) scale(0.034000, -0.034000)"
          fill={colors.accent}
        />
        <path
          d="M102 0 16 550H115L164 213Q168 182 171.0 146.0Q174 110 176 86Q177 110 180.0 146.0Q183 182 189 213L249 550H353L411 213Q417 182 420.5 145.5Q424 109 426 85Q428 109 430.5 145.5Q433 182 438 213L488 550H584L496 0H367L315 339Q310 367 306.0 403.5Q302 440 300 463Q299 440 294.0 403.5Q289 367 285 339L231 0Z"
          transform="translate(20.40, 34.00) scale(0.034000, -0.034000)"
          fill={colors.accent}
        />
        <path
          d="M47 0V550H151V470H154Q157 510 182.0 535.0Q207 560 245 560Q282 560 308.0 536.5Q334 513 343 473Q347 513 372.5 536.5Q398 560 437 560Q487 560 520.0 520.5Q553 481 553 419V0H443V410Q443 437 430.5 452.5Q418 468 395 468Q350 468 350 410V0H250V410Q250 437 238.5 452.5Q227 468 205 468Q157 468 157 410V0Z"
          transform="translate(40.80, 34.00) scale(0.034000, -0.034000)"
          fill={colors.accent}
        />
      </svg>
    </div>
  );
};
