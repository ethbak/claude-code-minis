import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLlmsTxt from 'starlight-llms-txt';

export default defineConfig({
  site: 'https://ethbak.github.io',
  base: '/claude-code-minis',
  integrations: [
    starlight({
      title: 'claude-code-minis',
      description: 'Small, focused plugins for Claude Code.',
      social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/ethbak/claude-code-minis' }],
      sidebar: [
        { label: 'Overview', link: '/' },
        { label: 'remote-resume (/rresume)', link: '/remote-resume/' },
        { label: 'remote-terminal (!)', link: '/remote-terminal/' },
        { label: 'mode-picker (/mode)', link: '/mode-picker/' },
        { label: 'How waiting-for-input detection works', link: '/waiting-for-input/' },
      ],
      plugins: [starlightLlmsTxt()],
      lastUpdated: true,
    }),
  ],
});
