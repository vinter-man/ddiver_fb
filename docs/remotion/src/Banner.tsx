/**
 * The banner: the hand-drawn original with its top three lines set again.
 *
 * The original says ddiver_facebook and "Forty groups"; the repository is ddiver_fb and one
 * collector reads at most 25 groups (DESIGN, D17). The background behind the text is one flat
 * colour, so a patch of it and the same faces at the same places are all the edit takes.
 */
import React from 'react';
import {AbsoluteFill, Img, staticFile} from 'remotion';
import {loadFont as loadInter} from '@remotion/google-fonts/Inter';
import {loadFont as loadMono} from '@remotion/google-fonts/JetBrainsMono';
import {C} from './theme';

const inter = loadInter('normal', {weights: ['500', '600']});
const mono = loadMono('normal', {weights: ['400']});

export const BANNER_W = 1280;
export const BANNER_H = 640;

const Patch: React.FC<{top: number; height: number}> = ({top, height}) => (
  <div
    style={{position: 'absolute', left: 590, top, width: 690, height, backgroundColor: C.bg}}
  />
);

export const Banner: React.FC = () => (
  <AbsoluteFill style={{backgroundColor: C.bg}}>
    <Img src={staticFile('banner-source.png')} style={{width: BANNER_W, height: BANNER_H}} />
    <Patch top={60} height={152} />
    <div
      style={{
        position: 'absolute',
        left: 596,
        top: 64,
        fontFamily: mono.fontFamily,
        fontSize: 15.2,
        color: C.grey,
        letterSpacing: 0.1,
      }}
    >
      github.com/vinter-man/ddiver_fb
    </div>
    <div
      style={{
        position: 'absolute',
        left: 592,
        top: 93,
        fontFamily: inter.fontFamily,
        fontWeight: 600,
        fontSize: 67,
        letterSpacing: -3.8,
        color: C.cream,
        lineHeight: 1,
      }}
    >
      ddiver_fb
    </div>
    <div
      style={{
        position: 'absolute',
        left: 596,
        top: 176,
        fontFamily: inter.fontFamily,
        fontWeight: 500,
        fontSize: 25.5,
        letterSpacing: -0.9,
        color: C.cream,
        whiteSpace: 'nowrap',
      }}
    >
      “Twenty-five groups. Somebody else can scroll them.”
    </div>
  </AbsoluteFill>
);
