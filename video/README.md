# swm demo videos

Remotion project that renders the swm CLI demo videos in three aspect ratios.

## Compositions

| ID             | Use case                                      | Duration | Resolution     |
| -------------- | --------------------------------------------- | -------- | -------------- |
| `Hero16x9`     | PH gallery, site hero, YouTube, Twitter web   | 90s      | 1920×1080 @ 60 |
| `Vertical9x16` | TikTok, Reels, YT Shorts, X mobile autoplay   | 25s      | 1080×1920 @ 60 |
| `Square1x1`    | LinkedIn feed, Instagram feed                 | 25s      | 1080×1080 @ 60 |

## Develop

```bash
npm install
npm run studio   # opens the Remotion preview at http://localhost:3000
```

The preview hot-reloads on file changes. Hit space to play, drag the
timeline to scrub, click a composition in the left rail to switch.

## Render

```bash
npm run render:hero        # ~3 min on M-series, ~$0.10 on Remotion Lambda
npm run render:vertical
npm run render:square
npm run render:all
```

Outputs land in `out/` (gitignored).

## File layout

```
src/
├── Root.tsx                # composition registry (this is the entry point)
├── compositions/           # one file per aspect ratio
├── components/             # reusable terminal UI primitives
├── script/scrubbed-logs.ts # the sanitized log content shown on screen
└── styles/tokens.ts        # colors, fonts (matches swmgpu.com palette)
```

## Editing the script

The on-screen content lives in `src/script/scrubbed-logs.ts`. Beat timing
lives inline in the composition files as `<Sequence from={...} durationInFrames={...}>`
blocks — search for the time comments like `{/* 0:18-0:30 — pod create ComfyUI */}`.

## Brand tokens

Lifted from `site/src/styles/global.css` so the video reads as a direct
extension of the website, not a foreign object:

- Background: `#0a0a0a`
- Accent (terminal green): `#00ff88`
- Mono font: JetBrains Mono (loaded via `@remotion/google-fonts`)

## Scrub policy

All terminal output in `scrubbed-logs.ts` is sanitized:

- Hostnames → `dev@laptop` or dropped entirely
- IPs → RFC-5737 documentation range (`198.51.100.*`)
- Instance IDs → synthetic (`vastai:i-a7b2c4`, `runpod:i-d3e9f1`)
- Bucket paths → `b2:swm-store`
- Project names → generic (`sd-experiments`, `llama3-finetune`)

Never paste a real log in here without running it through this scrub list first.
