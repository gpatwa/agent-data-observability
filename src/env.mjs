// Loads .env from the repo root so credentials live in one gitignored file
// instead of being exported by hand in every shell.
//
// PRECEDENCE: a variable already set in the real environment always wins. That
// matters — otherwise a stale .env would silently override the value you just
// exported to test something, which is a miserable thing to debug.
//
// No dependency: .env is a handful of KEY=value lines and Node can read a file.

import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const ENV_PATH = fileURLToPath(new URL('../.env', import.meta.url));

export function loadEnv(path = ENV_PATH) {
  if (!existsSync(path)) return { loaded: false, keys: [] };
  const keys = [];
  for (const raw of readFileSync(path, 'utf8').split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq === -1) continue;
    const key = line.slice(0, eq).trim().replace(/^export\s+/, '');
    let val = line.slice(eq + 1).trim();
    // strip one layer of matching quotes
    if ((val.startsWith("'") && val.endsWith("'")) || (val.startsWith('"') && val.endsWith('"'))) {
      val = val.slice(1, -1);
    }
    if (!key) continue;
    if (process.env[key] === undefined) {   // shell wins
      process.env[key] = val;
      keys.push(key);
    }
  }
  return { loaded: true, keys };
}

loadEnv();
