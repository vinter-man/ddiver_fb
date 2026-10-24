/**
 * Builds every image the README uses, into ../assets.
 *
 * The words in the bot screenshots come from src/captured.json (`uv run python -m
 * tools.readme_shots` at the repository root writes it from the bot's own code); this only
 * draws them. Run with `npm run build`. Needs ffmpeg on PATH for the GIF.
 */
import {execFileSync} from 'node:child_process';
import {mkdirSync} from 'node:fs';

const OUT = '../assets';
const TMP = 'out';
const SHOTS = ['notification', 'groups', 'test', 'status', 'ai', 'alerts'];

const run = (cmd, args) => execFileSync(cmd, args, {stdio: 'inherit'});

mkdirSync(OUT, {recursive: true});
mkdirSync(TMP, {recursive: true});

run('npx', ['remotion', 'still', 'banner', `${OUT}/banner.png`, '--log=error']);
// Twice the CSS size, as a screenshot from a Retina screen is.
for (const shot of SHOTS) {
  run('npx', ['remotion', 'still', `shot-${shot}`, `${OUT}/${shot}.png`, '--scale=2', '--log=error']);
}

run('npx', ['remotion', 'render', 'workflow', `${TMP}/workflow.mp4`, '--log=error', '--codec=h264', '--crf=16']);

// Two passes: one palette for the whole clip, then dither against it. A GIF made without
// this is both uglier and several times larger.
const FILTER = 'fps=12,scale=900:-1:flags=lanczos';
run('ffmpeg', ['-y', '-v', 'error', '-i', `${TMP}/workflow.mp4`,
  '-vf', `${FILTER},palettegen=max_colors=160:stats_mode=diff`, `${TMP}/palette.png`]);
run('ffmpeg', ['-y', '-v', 'error', '-i', `${TMP}/workflow.mp4`, '-i', `${TMP}/palette.png`,
  '-lavfi', `${FILTER}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`,
  `${OUT}/workflow.gif`]);

console.log('\nWrote', OUT);
