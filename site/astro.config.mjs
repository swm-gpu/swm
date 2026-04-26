// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
	site: 'https://swmgpu.com',
	integrations: [
		starlight({
			title: 'swm',
			description: 'Your GPU stack, your terminal, any cloud.',
			logo: { src: './src/assets/swm-logo.svg', replacesTitle: true },
			social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/swm-gpu/swm' }],
			customCss: ['./src/styles/global.css'],
			defaultLocale: 'root',
			head: [
				{
					tag: 'link',
					attrs: {
						rel: 'preconnect',
						href: 'https://fonts.googleapis.com',
					},
				},
				{
					tag: 'link',
					attrs: {
						rel: 'preconnect',
						href: 'https://fonts.gstatic.com',
						crossorigin: true,
					},
				},
				{
					tag: 'link',
					attrs: {
						rel: 'stylesheet',
						href: 'https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,400;0,600;0,700;1,400&display=swap',
					},
				},
			],
			sidebar: [
				{ label: 'Overview', slug: 'overview' },
				{
					label: 'Getting Started',
					items: [
						{ label: 'For CLI Users', slug: 'getting-started/for-cli-users' },
						{ label: 'For Agent Users', slug: 'getting-started/for-agent-users' },
						{ label: 'Configuration', slug: 'getting-started/configuration' },
					],
				},
				{
					label: 'Agent Integration',
					items: [
						{ label: 'Skill Overview', slug: 'agent-integration/skill-overview' },
						{ label: 'Supported Platforms', slug: 'agent-integration/supported-platforms' },
						{ label: 'Custom Skills', slug: 'agent-integration/custom-skills' },
					],
				},
				{
					label: 'Core Concepts',
					items: [
						{ label: 'Providers', slug: 'concepts/providers' },
						{ label: 'Workspaces & Storage', slug: 'concepts/workspaces-and-storage' },
						{ label: 'Frameworks', slug: 'concepts/frameworks' },
						{ label: 'Lifecycle Guard', slug: 'concepts/lifecycle-guard' },
						{ label: 'Cost Tracking', slug: 'concepts/cost-tracking' },
					],
				},
				{
					label: 'Command Reference',
					items: [
						{ label: 'swm gpus', slug: 'commands/gpus' },
						{ label: 'swm pod', slug: 'commands/pod' },
						{ label: 'swm setup', slug: 'commands/setup' },
						{ label: 'swm sync', slug: 'commands/sync' },
						{ label: 'swm costs', slug: 'commands/costs' },
						{ label: 'swm models', slug: 'commands/models' },
						{ label: 'swm guard', slug: 'commands/guard' },
						{ label: 'swm storage', slug: 'commands/storage' },
						{ label: 'swm pricing', slug: 'commands/pricing' },
						{ label: 'ssh / run / upload / download', slug: 'commands/ssh-run-upload-download' },
						{ label: 'swm config', slug: 'commands/config' },
					],
				},
				{
					label: 'Guides',
					items: [
						{ label: 'vLLM + Open WebUI Stack', slug: 'guides/vllm-open-webui-stack' },
						{ label: 'Fine-tune with Axolotl', slug: 'guides/finetune-axolotl' },
						{ label: 'ComfyUI Remote', slug: 'guides/comfyui-remote' },
						{ label: 'Cost Budgets', slug: 'guides/cost-budgets' },
						{ label: 'Migrate Workspace', slug: 'guides/migrate-workspace' },
						{ label: 'Tar Mode Sync', slug: 'guides/tar-mode-sync' },
					],
				},
			],
		}),
	],
});
