import React from 'react';
import { AbsoluteFill, Audio, Sequence, interpolate, staticFile, useVideoConfig } from 'remotion';
import { colors, fonts } from '../styles/tokens';
import { MUSIC_TRACK, MUSIC_VOLUME } from '../config/music';
import { ChapterCard } from '../components/ChapterCard';
import { TerminalFrame } from '../components/TerminalFrame';
import { TypingLine } from '../components/TypingLine';
import { OutputBlock } from '../components/OutputBlock';
import { ProviderCascade } from '../components/ProviderCascade';
import { GpuTable } from '../components/GpuTable';
import { EndCard } from '../components/EndCard';
import * as L from '../script/scrubbed-logs';

// 90s arc:
//   0:00-0:04  Tagline + wordmark
//   0:04-0:18  swm gpus cascade + table (the wow moment)
//   0:18-0:30  pod create 4090 ComfyUI + workspace restore
//   0:30-0:40  swm setup start comfyui + tunnel
//   0:40-0:42  Chapter card — "Same workspace pattern, any framework"
//   0:42-0:54  pod create B200 vLLM + start
//   0:54-0:70  swm pod down (real log, scrubbed)
//   0:70-0:80  Restore later + auto-down hint
//   0:80-0:90  End card CTA

export const Hero16x9: React.FC = () => {
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

      {/* 0:00-0:04 — tagline */}
      <Sequence from={s(0)} durationInFrames={s(4)}>
        <ChapterCard
          showWordmark
          pronunciation='("swim")'
          showDivider
          subtitle="Avoid vendor lock-in with a synced workspace across any GPU cloud."
        />
      </Sequence>

      {/* 0:04-0:18 — swm gpus cascade + table */}
      <Sequence from={s(4)} durationInFrames={s(14)}>
        <AbsoluteFill style={{ padding: 80 }}>
          <TerminalFrame title="~ swm" fontSize={26}>
            <TypingLine prompt="$" text={L.gpusCommand} startFrame={6} />
            <div style={{ height: 28 }} />
            <ProviderCascade providers={L.providers} startFrame={s(1.8)} framesBetween={14} />
            <div style={{ height: 28 }} />
            <GpuTable
              rows={L.gpuRows}
              startFrame={s(4.5)}
              framesBetween={12}
              winnerPulseAt={s(7)}
              fontSize={24}
            />
          </TerminalFrame>
        </AbsoluteFill>
      </Sequence>

      {/* 0:18-0:30 — pod create ComfyUI */}
      <Sequence from={s(18)} durationInFrames={s(12)}>
        <AbsoluteFill style={{ padding: 80 }}>
          <TerminalFrame title="~ swm">
            <TypingLine prompt="$" text={L.podCreateComfyCmd} startFrame={6} />
            <div style={{ height: 18 }} />
            <OutputBlock lines={L.podCreateComfyConfirm} startFrame={s(2.5)} framesPerLine={6} />
            <div style={{ height: 14 }} />
            <OutputBlock lines={L.podCreateComfyOutput} startFrame={s(5.5)} framesPerLine={10} />
          </TerminalFrame>
        </AbsoluteFill>
      </Sequence>

      {/* 0:30-0:40 — swm setup start comfyui */}
      <Sequence from={s(30)} durationInFrames={s(10)}>
        <AbsoluteFill style={{ padding: 80 }}>
          <TerminalFrame title="~ swm">
            <TypingLine prompt="$" text={L.setupStartComfyCmd} startFrame={6} />
            <div style={{ height: 18 }} />
            <OutputBlock lines={L.setupStartComfyOutput} startFrame={s(2.2)} framesPerLine={28} />
          </TerminalFrame>
        </AbsoluteFill>
      </Sequence>

      {/* 0:40-0:42 — chapter card: any framework */}
      <Sequence from={s(40)} durationInFrames={s(2)}>
        <ChapterCard
          title="Same workspace pattern. Any framework."
          subtitle="Switch GPUs, switch clouds, your work follows you."
        />
      </Sequence>

      {/* 0:42-0:54 — pod create + start vLLM on B200 */}
      <Sequence from={s(42)} durationInFrames={s(12)}>
        <AbsoluteFill style={{ padding: 80 }}>
          <TerminalFrame title="~ swm" fontSize={26}>
            <TypingLine prompt="$" text={L.podCreateVllmCmd} startFrame={6} />
            <div style={{ height: 14 }} />
            <OutputBlock lines={L.podCreateVllmConfirm} startFrame={s(2.5)} framesPerLine={5} />
            <div style={{ height: 12 }} />
            <OutputBlock lines={L.podCreateVllmOutput} startFrame={s(5)} framesPerLine={8} />
            <div style={{ height: 14 }} />
            <TypingLine prompt="$" text={L.setupStartVllmCmd} startFrame={s(8.5)} />
            <div style={{ height: 10 }} />
            <OutputBlock lines={L.setupStartVllmOutput} startFrame={s(10)} framesPerLine={10} />
          </TerminalFrame>
        </AbsoluteFill>
      </Sequence>

      {/* 0:54-0:70 — swm pod down */}
      <Sequence from={s(54)} durationInFrames={s(16)}>
        <AbsoluteFill style={{ padding: 80 }}>
          <TerminalFrame title="~ swm" fontSize={26}>
            <TypingLine prompt="$" text={L.podDownCmd} startFrame={6} />
            <div style={{ height: 12 }} />
            <OutputBlock lines={L.podDownOutput} startFrame={s(1.8)} framesPerLine={14} />
          </TerminalFrame>
        </AbsoluteFill>
      </Sequence>

      {/* 0:70-0:80 — restore later + auto-down hint */}
      <Sequence from={s(70)} durationInFrames={s(10)}>
        <AbsoluteFill
          style={{
            background: colors.bg,
            justifyContent: 'center',
            alignItems: 'center',
            padding: 100,
            gap: 40,
            fontFamily: fonts.mono,
          }}
        >
          <div
            style={{
              color: colors.textMuted,
              fontSize: 28,
              fontWeight: 600,
              letterSpacing: '0.02em',
            }}
          >
            Restore later:
          </div>
          <div
            style={{
              color: colors.text,
              fontSize: 32,
              background: colors.panel,
              padding: '24px 36px',
              border: `1px solid ${colors.border}`,
              borderLeft: `3px solid ${colors.accent}`,
              borderRadius: 10,
              whiteSpace: 'pre',
            }}
          >
            <span style={{ color: colors.accent, marginRight: 14, fontWeight: 600 }}>$</span>
            {L.restoreLaterLine}
          </div>
          <div
            style={{
              color: colors.textMuted,
              fontSize: 22,
              maxWidth: 1200,
              textAlign: 'center',
              marginTop: 24,
              lineHeight: 1.5,
            }}
          >
            {L.autoDownHint}
          </div>
        </AbsoluteFill>
      </Sequence>

      {/* 0:80-0:90 — end card */}
      <Sequence from={s(80)} durationInFrames={s(10)}>
        <EndCard />
      </Sequence>
    </AbsoluteFill>
  );
};
