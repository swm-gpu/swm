// All terminal content shown on screen. Sanitized per the heavy-scrub policy.
// See video/README.md for the scrub list.

export type ProviderStatus = {
  name: string;
  info: string;
  status: 'ok' | 'err';
};

export type GpuRow = {
  provider: string;
  gpu: string;
  flag: string;
  vram: string;
  n: number;
  price: string;
  stock: string;
  cuda: string;
  region: string;
  secure: boolean;
  winner?: boolean;
};

// ── Beat 2: swm gpus cascade ──────────────────────────────────────────────

export const gpusCommand = 'swm gpus -g 4090';

export const providers: ProviderStatus[] = [
  { name: 'RunPod',      info: '38 GPUs',  status: 'ok' },
  { name: 'GCP',         info: '12 GPUs',  status: 'ok' },
  { name: 'Vast.ai',     info: '187 GPUs', status: 'ok' },
  { name: 'Lambda Labs', info: '14 GPUs',  status: 'ok' },
  { name: 'Vultr',       info: '8 GPUs',   status: 'ok' },
  { name: 'CoreWeave',   info: '4 GPUs',   status: 'ok' },
  { name: 'Azure',       info: '6 GPUs',   status: 'ok' },
  { name: 'AWS',         info: '11 GPUs',  status: 'ok' },
];

export const gpuRows: GpuRow[] = [
  { provider: 'Vast.ai',   gpu: '4090',      flag: '4090',                vram: '24 GB', n: 1, price: '$0.40', stock: 'available',   cuda: '12.8', region: 'Oregon, US', secure: false, winner: true },
  { provider: 'RunPod',    gpu: '4090',      flag: '"RTX 4090"',          vram: '24 GB', n: 1, price: '$0.69', stock: 'High',        cuda: '12.8', region: '—',          secure: true },
  { provider: 'Lambda',    gpu: '4090',      flag: 'gpu_1x_rtx4090',      vram: '24 GB', n: 1, price: '$0.75', stock: 'unavailable', cuda: '12.8', region: '—',          secure: false },
  { provider: 'CoreWeave', gpu: '4090',      flag: 'nvidia.com/rtx-4090', vram: '24 GB', n: 1, price: '$0.82', stock: '—',           cuda: '12.8', region: '—',          secure: true },
  { provider: 'Vultr',     gpu: '4090',      flag: 'rtx4090',             vram: '24 GB', n: 1, price: '$0.89', stock: 'available',   cuda: '12.8', region: 'Tokyo, JP',  secure: false },
  { provider: 'AWS',       gpu: 'g6.4xlarge', flag: 'g6.4xlarge',         vram: '24 GB', n: 1, price: '$1.32', stock: '—',           cuda: '12.8', region: '—',          secure: true },
];

// ── Beat 3: ComfyUI lifecycle on 4090 ─────────────────────────────────────

export const podCreateComfyCmd =
  'swm pod create -g 4090 -n comfy -p vastai --workspace sd-experiments';

export const podCreateComfyConfirm = [
  '  Provider:   Vast.ai',
  '  GPU:        RTX 4090 × 1',
  '  Workspace:  restore sd-experiments',
  '  Lifecycle:  auto-down after 60m idle',
  '',
  '  Proceed? y',
];

export const podCreateComfyOutput = [
  '',
  '✓ Instance ready (vastai:i-a7b2c4)',
  '  Cost: $0.40/hr · SSH: 198.51.100.42:30571',
  '',
  '▸ Restoring workspace…',
  '  ████████████████████████  16.6 GB · 145 MB/s · 1m12s',
  '✓ Workspace restored',
];

// ── Beat 4: comfyui start + tunnel ────────────────────────────────────────

export const setupStartComfyCmd = 'swm setup start comfyui vastai:i-a7b2c4';

export const setupStartComfyOutput = [
  '▸ Starting ComfyUI',
  '✓ ComfyUI started',
  '  Port 8188 not exposed — opening SSH tunnel…',
  '✓ Tunnel active → localhost:8188',
];

// ── Beat 5: switch to B200 + vLLM (placeholder — paste real log later) ────

export const podCreateVllmCmd =
  'swm pod create -g B200 -n llm -p runpod --workspace llama3-finetune';

export const podCreateVllmConfirm = [
  '  Provider:   RunPod',
  '  GPU:        B200 × 1',
  '  Workspace:  restore llama3-finetune',
  '  Lifecycle:  auto-down after 60m idle',
  '',
  '  Proceed? y',
];

export const podCreateVllmOutput = [
  '',
  '✓ Instance ready (runpod:i-d3e9f1)',
  '  Cost: $5.98/hr · SSH: 198.51.100.91:22',
  '',
  '▸ Restoring workspace…',
  '  ████████████████████████  42.3 GB · 220 MB/s · 3m12s',
  '✓ Workspace restored',
];

export const setupStartVllmCmd = 'swm setup start vllm runpod:i-d3e9f1';

export const setupStartVllmOutput = [
  '▸ Starting vLLM (model: meta-llama/Meta-Llama-3-70B-Instruct)',
  '  Loading model weights…  100% · 42.3 GB',
  '  KV cache: 16384 tokens × 80 layers · ready',
  '✓ vLLM running',
  '  Port 8000 not exposed — opening SSH tunnel…',
  '✓ Tunnel active → localhost:8000',
];

// ── Beat 6: pod down (real log, scrubbed) ─────────────────────────────────

export const podDownCmd = 'swm pod down';

export const podDownOutput = [
  '',
  'Shutting down RunPod instance i-d3e9f1',
  '  Workspace: llama3-finetune',
  '  Storage:   b2:swm-store',
  '  Action:    push workspace → terminate pod',
  '',
  '  Proceed? y',
  '',
  '▸ Pushing 53,750 changed file(s) → llama3-finetune/',
  '  ████████████████████████  9.35 GB · 73 MB/s · 2m08s',
  '✓ Workspace synced & wiped on pod',
  '',
  '✓ RunPod instance i-d3e9f1 terminated.',
  '  Workspace llama3-finetune preserved in b2:swm-store.',
];

// ── Beat 7: continuity payoff ─────────────────────────────────────────────

export const restoreLaterLine =
  'swm pod create -p runpod -g B200 -n llm -w llama3-finetune';

export const autoDownHint = 'Or let the idle timer do it — auto-down at 60m, on by default.';
