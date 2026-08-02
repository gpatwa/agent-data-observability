// Time compression for the simulation. The agent sleeps think_ms/DILATION of
// real time; assemble.mjs scales elapsed wall-clock back up by DILATION before
// applying warehouse billing. Query execution time is real and never scaled.
export const DILATION = 100;

export const PG = {
  host: process.env.PGHOST ?? 'localhost',
  port: Number(process.env.PGPORT ?? 55432),
  user: process.env.PGUSER ?? 'postgres',
  database: process.env.PGDATABASE ?? 'postgres',
};
