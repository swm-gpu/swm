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

// 25s square cutdown (1080×1080):
//   0:00-0:03  Wordmark + tagline
//   0:03-0:09  swm gpus → winning row card
//   0:09-0:16  pod create + restore
//   0:16-0:21  pod down + "Restore later"
//   0:21-0:25  End card

export const Square1x1: React.FC = () => {
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

      <Sequence from={s(0)} durationInFrames={s(3)}>
        <ChapterCard
          title="One CLI. Any cloud."
          subtitle="Your GPU workflow, anywhere."
          showWordmark
          smallTitle
        />
      </Sequence>

      <Sequence from={s(3)} durationInFrames={s(6)}>
        <AbsoluteFill
          style={{ padding: 50, justifyContent: 'center', alignItems: 'center' }}
        >
          <div style={{ width: '100%', height: '100%' }}>
            <TerminalFrame title="~ swm" fontSize={20} padding={28}>
              <TypingLine prompt="$" text={L.gpusCommand} startFrame={6} />
              <div style={{ height: 18 }} />
              <CompactRow provider="RunPod"    price="$0.69" stock="High"        accent={false} />
              <CompactRow provider="Lambda"    price="$0.75" stock="unavailable" accent={false} />
              <CompactRow provider="Vast.ai"   price="$0.40" stock="available"   accent={true}  />
              <CompactRow provider="CoreWeave" price="$0.82" stock="—"           accent={false} />
              <CompactRow provider="AWS"       price="$1.32" stock="—"           accent={false} />
            </TerminalFrame>
          </div>
        </AbsoluteFill>
      </Sequence>

      <Sequence from={s(9)} durationInFrames={s(7)}>
        <AbsoluteFill style={{ padding: 50, justifyContent: 'center' }}>
          <TerminalFrame title="~ swm" fontSize={19} padding={26}>
            <TypingLine
              prompt="$"
              text="swm pod create -g 4090 -p vastai -w sd-experiments"
              startFrame={6}
            />
            <div style={{ height: 14 }} />
            <OutputBlock
              lines={[
                '',
                '✓ Instance ready (vastai:i-a7b2c4)',
                '  Cost: $0.40/hr',
                '',
                '▸ Restoring workspace…',
                '  ████████████████  16.6 GB · 145 MB/s',
                '✓ Workspace restored',
                '',
                '$ swm setup start comfyui …',
                '✓ Tunnel → localhost:8188',
              ]}
              startFrame={s(2.5)}
              framesPerLine={11}
            />
          </TerminalFrame>
        </AbsoluteFill>
      </Sequence>

      <Sequence from={s(16)} durationInFrames={s(5)}>
        <AbsoluteFill style={{ padding: 50, justifyContent: 'center' }}>
          <TerminalFrame title="~ swm" fontSize={19} padding={26}>
            <TypingLine prompt="$" text={L.podDownCmd} startFrame={6} />
            <div style={{ height: 12 }} />
            <OutputBlock
              lines={[
                '',
                '▸ Pushing 53,750 file(s) → b2:swm-store',
                '  ████████████████  9.35 GB · 73 MB/s',
                '✓ Workspace preserved',
                '✓ Instance terminated',
                '',
                'Restore later:',
                '$ swm pod create -w sd-experiments',
              ]}
              startFrame={s(1.5)}
              framesPerLine={13}
            />
          </TerminalFrame>
        </AbsoluteFill>
      </Sequence>

      <Sequence from={s(21)} durationInFrames={s(4)}>
        <EndCard compact />
      </Sequence>
    </AbsoluteFill>
  );
};

const CompactRow: React.FC<{
  provider: string;
  price: string;
  stock: string;
  accent: boolean;
}> = ({ provider, price, stock, accent }) => (
  <div
    style={{
      display: 'flex',
      padding: '10px 16px',
      marginBottom: 4,
      background: accent ? 'rgba(0, 255, 136, 0.07)' : 'transparent',
      borderLeft: accent ? `3px solid ${colors.accent}` : `3px solid transparent`,
      borderRadius: 4,
      color: accent ? colors.accentGlow : colors.text,
      fontWeight: accent ? 600 : 400,
    }}
  >
    <span style={{ flex: 1 }}>{provider}</span>
    <span
      style={{
        width: 100,
        textAlign: 'right',
        color: accent ? colors.accent : undefined,
        fontWeight: accent ? 700 : 400,
      }}
    >
      {price}
    </span>
    <span style={{ width: 160, textAlign: 'right', color: colors.textMuted }}>{stock}</span>
  </div>
);
