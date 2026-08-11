import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'plex-db-ex',
  description: 'Extended Plex metadata and affinity store',
  cleanUrls: true,
  themeConfig: {
    nav: [
      { text: 'PRD', link: '/PRD' },
      { text: 'Schema', link: '/schema' },
      { text: 'Decisions', link: '/adr/' },
      { text: 'Roadmap', link: '/roadmap' },
    ],
    sidebar: {
      '/adr/': [
        {
          text: 'Decisions',
          items: [
            { text: 'Index', link: '/adr/' },
            { text: '0001 — One writer, many readers', link: '/adr/0001-one-writer-many-readers-sqlite-file-is-the-interface' },
            { text: '0002 — item_id is the entry_id string', link: '/adr/0002-item-id-is-the-entry-id-string' },
            { text: '0003 — Rust reader crate', link: '/adr/0003-rust-reader-crate-behind-a-plugin-capability-grant' },
            { text: '0004 — The store owns watch history', link: '/adr/0004-the-store-owns-watch-history' },
            { text: '0005 — The store walks Plex itself', link: '/adr/0005-the-store-walks-plex-itself-and-augments-never-replaces' },
            { text: '0006 — The identity fixture is duplicated', link: '/adr/0006-the-identity-fixture-is-duplicated-and-guarded-by-a-hash' },
          ],
        },
      ],
      '/': [
        {
          text: 'Reference',
          items: [
            { text: 'PRD', link: '/PRD' },
            { text: 'Schema', link: '/schema' },
            { text: 'The baseline store', link: '/baseline' },
            { text: 'Original spec', link: '/spec' },
            { text: 'Vocabulary', link: '/CONTEXT' },
            { text: 'Decisions', link: '/adr/' },
            { text: 'Roadmap', link: '/roadmap' },
            { text: 'File map', link: '/file-map' },
          ],
        },
      ],
    },
    search: { provider: 'local' },
    socialLinks: [{ icon: 'github', link: 'https://github.com/McBrideMusings/plex-db-ex' }],
    editLink: {
      pattern: 'https://github.com/McBrideMusings/plex-db-ex/edit/main/docs/:path',
      text: 'Edit this page on GitHub',
    },
  },
  vite: {
    server: { host: '0.0.0.0', port: 5193 },
  },
})
