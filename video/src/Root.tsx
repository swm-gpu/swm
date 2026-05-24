import React from 'react';
import { registerRoot, Composition } from 'remotion';
import { loadFont } from '@remotion/google-fonts/JetBrainsMono';
import { Hero16x9 } from './compositions/Hero16x9';
import { Vertical9x16 } from './compositions/Vertical9x16';
import { Square1x1 } from './compositions/Square1x1';

loadFont('normal', { weights: ['400', '600', '700'] });

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="Hero16x9"
        component={Hero16x9}
        durationInFrames={5400}
        fps={60}
        width={1920}
        height={1080}
      />
      <Composition
        id="Vertical9x16"
        component={Vertical9x16}
        durationInFrames={1500}
        fps={60}
        width={1080}
        height={1920}
      />
      <Composition
        id="Square1x1"
        component={Square1x1}
        durationInFrames={1500}
        fps={60}
        width={1080}
        height={1080}
      />
    </>
  );
};

registerRoot(RemotionRoot);
