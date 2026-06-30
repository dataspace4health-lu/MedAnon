import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// Documentation site for the MedAnon privacy toolkit.
// Content is organised with the Diátaxis framework:
//   Tutorials · How-to guides · Reference · Explanation
// See docs/methodology.md for the rationale.

const config: Config = {
  title: 'MedAnon',
  tagline: 'Rule-driven FHIR de-identification & pseudonymization',
  favicon: 'img/logo.svg',

  // Set to your published URL. Local builds work with these defaults.
  url: 'https://example.com',
  baseUrl: '/',

  organizationName: 'medanon',
  projectName: 'privacy-toolkit',

  // Surfaced as warnings during local dev; promote to 'throw' in CI.
  onBrokenLinks: 'warn',
  onBrokenMarkdownLinks: 'warn',

  markdown: {
    // Parse .md as CommonMark (robust against inline <placeholders> and
    // {braces} in the migrated docs) and .mdx as MDX (for Mermaid + JSX).
    format: 'detect',
    mermaid: true,
  },
  themes: ['@docusaurus/theme-mermaid'],

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          path: 'docs',
          routeBasePath: '/', // docs are the site root (no separate landing page)
          sidebarPath: './sidebars.ts',
          editUrl: 'https://example.com/edit/main/website/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      defaultMode: 'light',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'MedAnon',
      items: [
        {type: 'doc', docId: 'intro', position: 'left', label: 'Docs'},
        {type: 'doc', docId: 'tutorials/getting-started', position: 'left', label: 'Get Started'},
        {type: 'doc', docId: 'reference/api', position: 'left', label: 'API'},
        {
          href: 'https://example.com/privacy-toolkit',
          label: 'Source',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Learn',
          items: [
            {label: 'Introduction', to: '/'},
            {label: 'Getting Started', to: '/tutorials/getting-started'},
            {label: 'Documentation Methodology', to: '/methodology'},
          ],
        },
        {
          title: 'Operate',
          items: [
            {label: 'Deployment', to: '/how-to/deploy-docker'},
            {label: 'Runbook', to: '/how-to/operations-runbook'},
            {label: 'Security', to: '/explanation/security-model'},
          ],
        },
        {
          title: 'Reference',
          items: [
            {label: 'REST API', to: '/reference/api'},
            {label: 'Configuration', to: '/reference/configuration'},
            {label: 'Config Profiles', to: '/reference/config-profiles'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} MedAnon.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'yaml', 'json', 'python', 'docker'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
