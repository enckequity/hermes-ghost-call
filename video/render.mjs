// Render video/build/scenes.html frame by frame and mux it with video/build/mix.wav.
//   node video/render.mjs                 full video -> video/out/ghost-call-launch.mp4
//   node video/render.mjs --stills 1,20   PNG stills at those seconds -> video/build/still-*.png
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
// playwright-core from npm, or a path to an existing copy in PLAYWRIGHT_CORE
const PW = process.env.PLAYWRIGHT_CORE || 'playwright-core';
const { chromium } = createRequire(import.meta.url)(PW);

const build = path.join(here, 'build');
const out = path.join(here, 'out');
mkdirSync(out, { recursive: true });
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, colorScheme: 'dark' });
const page = await ctx.newPage();
page.on('pageerror', (e) => console.error('page error:', e.message));
await page.goto(pathToFileURL(path.join(build, 'scenes.html')).href);
await page.evaluate(() => window.ready);
const { duration, fps } = await page.evaluate(() => ({ duration: window.DURATION, fps: window.FPS }));

const si = process.argv.indexOf('--stills');
if (si > 0) {
  for (const s of process.argv[si + 1].split(',').map(Number)) {
    await page.evaluate((t) => window.seek(t), s);
    await page.screenshot({ path: path.join(build, `still-${String(s).replace('.', '_')}.png`) });
  }
  await browser.close();
  process.exit(0);
}

const target = path.join(out, 'ghost-call-launch.mp4');
const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps),
  '-c:v', 'mjpeg', '-i', '-', '-i', path.join(build, 'mix.wav'),
  '-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
  '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-shortest', target], { stdio: ['pipe', 'inherit', 'inherit'] });
const frames = Math.round(duration * fps);
const t0 = Date.now();
for (let f = 0; f < frames; f++) {
  await page.evaluate((t) => window.seek(t), f / fps);
  const jpg = await page.screenshot({ type: 'jpeg', quality: 94 });
  if (!ff.stdin.write(jpg)) await new Promise((r) => ff.stdin.once('drain', r));
  if (f % 300 === 0) console.log(`frame ${f}/${frames} (${((Date.now() - t0) / 1000).toFixed(0)} s)`);
}
ff.stdin.end();
await new Promise((r) => ff.on('close', r));
await browser.close();
console.log('wrote', target);
