// Builds standalone HTML pages from the artifact fragments in docs/_src/.
//
// WHY: those fragments are authored for a host that supplies the document
// wrapper (<!doctype>, <head>, charset, viewport). Served directly by GitHub
// Pages or opened from disk they have none of that — they render in quirks
// mode and, with no viewport meta, lay out at desktop width on a phone.
//
// One source, two destinations: the fragment stays publishable as-is, and this
// emits the standalone version. Run: npm run build:pages

import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const SRC = fileURLToPath(new URL('../docs/_src/', import.meta.url));
const OUT = fileURLToPath(new URL('../docs/', import.meta.url));

// Non-ASCII becomes numeric entities so the page reads correctly regardless of
// what charset the host declares — GitHub Pages sends utf-8, a file:// open may
// not, and a misconfigured server sends latin-1.
const asciify = (s) => [...s].map((c) => (c.codePointAt(0) > 127 ? `&#${c.codePointAt(0)};` : c)).join('');

function build(name) {
  const raw = readFileSync(SRC + name, 'utf8');
  const title = raw.match(/<title>([\s\S]*?)<\/title>/i)?.[1] ?? 'agent-data-observability';
  const body = asciify(raw.replace(/<title>[\s\S]*?<\/title>/i, '').trim());

  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<meta name="description" content="Six conditions testing whether AI agents flood data warehouses with redundant queries. Only the simulated one did.">
<meta property="og:title" content="${title.replace(/&#\d+;/g, '-')}">
<meta property="og:type" content="article">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>%F0%9F%93%89</text></svg>">
<style>*,*::before,*::after{box-sizing:border-box}body{margin:0}</style>
</head>
<body>
${body}
</body>
</html>
`;
  writeFileSync(OUT + name, html);
  return { name, bytes: html.length, title };
}

const built = readdirSync(SRC).filter((f) => f.endsWith('.html')).map(build);
for (const b of built) console.log(`  built docs/${b.name}  (${(b.bytes / 1024).toFixed(1)} KB)`);
