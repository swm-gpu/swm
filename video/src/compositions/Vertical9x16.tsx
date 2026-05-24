import React from 'react';
import { AbsoluteFill, Audio, Sequence, interpolate, staticFile, useVideoConfig } from 'remotion';
import { colors, fonts } from '../styles/tokens';
import { MUSIC_TRACK, MUSIC_VOLUME } from '../config/music';
import { ChapterCard } from '../components/ChapterCard';
import { TerminalFrame } from '../components/TerminalFrame';
import { TypingLine } from '../components/TypingLine';
import { OutputBlock } from '../components/OutputBlock';
import { EndCard } from '../components/EndCard';
import * as L from '../script/scrubbed-logs';

// 25s vertical cutdown (1080×1920):
//   0:00-0:03  Wordmark + tagline
//   0:03-0:08  Winning provider card (cascade compressed to a single highlight)
//   0:08-0:16  pod create + workspace restore (ComfyUI only)
//   0:16-0:21  pod down + "Restore later"
//   0:21-0:25  End card (compact)

export const Vertical9x16: React.FC = () => {
  const { fps, durationInFrames } = useVideoConfig();
  const s = (sec: number) => Math.round(sec * fps);

  return (
    <AbsoluteFill style={{ background: colors.bg, fontFamily: fonts.mono }}>
      <Audio
        src={staticFile(`music/${MUSIC_TRACK}.mp3`)}
        volume={(f) =>
          f < fps
            ? interpolate(f, [0, fps], [0, MUSIC_VOLUME], { extrapolateRight: 'clamp' })
            : f > durationInFrames - fps
            ? interpolate(
                f,
                [durationInFrames - fps, durationInFrames],
                [MUSIC_VOLUME, 0],
                { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }
              )
            : MUSIC_VOLUME
        }
      />

      {/* 0:00-0:03 — tagline */}
      <Sequence from={s(0)} durationInFrames={s(3)}>
        <ChapterCard
          title="Any GPU. Any cloud."
          subtitle="Your terminal. Your workspace."
          showWordmark
          smallTitle
        />
      </Sequence>

      {/* 0:03-0:08 — winning provider showcase */}
      <Sequence from={s(3)} durationInFrames={s(5)}>
        <AbsoluteFill
          style={{
            padding: 60,
            justifyContent: 'center',
            alignItems: 'center',
            gap: 32,
          }}
        >
          <div style={{ width: '100%' }}>
            <TerminalFrame title="~ swm" fontSize={24} padding={28}>
              <TypingLine prompt="$" text={L.gpusCommand} startFrame={6} />
              <div style={{ height: 20 }} />
              <WinnerCard />
            </TerminalFrame>
          </div>
        </AbsoluteFill>
      </Sequence>

      {/* 0:08-0:16 — pod create ComfyUI compressed */}
      <Sequence from={s(8)} durationInFrames={s(8)}>
        <AbsoluteFill
          style={{ padding: 60, justifyContent: 'center', alignItems: 'center' }}
        >
          <div style={{ width: '100%' }}>
            <TerminalFrame title="~ swm" fontSize={22} padding={28}>
              <TypingLine
                prompt="$"
                text="swm pod create -g 4090 -p vastai -w sd-experiments"
                startFrame={6}
              />
              <div style={{ height: 18 }} />
              <OutputBlock
                lines={[
                  '',
                  '✓ Instance ready (vastai:i-a7b2c4)',
                  '  Cost: $0.40/hr',
                  '',
                  '▸ Restoring workspace…',
                  '  ████████████████████  16.6 GB · 145 MB/s',
                  '✓ Workspace restored',
                  '',
                  '$ swm setup start comfyui …',
                  '✓ Tunnel → localhost:8188',
                ]}
                startFrame={s(2.5)}
                framesPerLine={10}
              />
            </TerminalFrame>
          </div>
        </AbsoluteFill>
      </Sequence>

      {/* 0:16-0:21 — pod down + restore later */}
      <Sequence from={s(16)} durationInFrames={s(5)}>
        <AbsoluteFill
          style={{ padding: 60, justifyContent: 'center', alignItems: 'center' }}
        >
          <div style={{ width: '100%' }}>
            <TerminalFrame title="~ swm" fontSize={22} padding={28}>
              <TypingLine prompt="$" text={L.podDownCmd} startFrame={6} />
              <div style={{ height: 14 }} />
              <OutputBlock
                lines={[
                  '',
                  '▸ Pushing 53,750 file(s)',
                  '  ████████████████████  9.35 GB · 73 MB/s',
                  '✓ Workspace preserved in b2:swm-store',
                  '✓ Instance terminated',
                  '',
                  'Restore later:',
                  '$ swm pod create -w sd-experiments',
                ]}
                startFrame={s(1.5)}
                framesPerLine={12}
              />
            </TerminalFrame>
          </div>
        </AbsoluteFill>
      </Sequence>

      {/* 0:21-0:25 — end card */}
      <Sequence from={s(21)} durationInFrames={s(4)}>
        <EndCard compact />
      </Sequence>
    </AbsoluteFill>
  );
};

const WinnerCard: React.FC = () => (
  <div
    style={{
      padding: '24px 28px',
      background: 'rgba(0, 255, 136, 0.06)',
      border: `1px solid ${colors.accent}`,
      borderLeft: `4px solid ${colors.accent}`,
      borderRadius: 8,
      display: 'flex',
      flexDirection: 'column',
      gap: 12,
    }}
  >
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
      <span style={{ color: colors.accentGlow, fontSize: 28, fontWeight: 700 }}>Vast.ai</span>
      <span style={{ color: colors.accent, fontSize: 32, fontWeight: 700 }}>$0.40/hr</span>
    </div>
    <div style={{ color: colors.text, fontSize: 22 }}>RTX 4090 · 24 GB · Oregon, US</div>
    <div style={{ color: colors.ok, fontSize: 20 }}>✓ available</div>
  </div>
);
