/**
 * The banner: the hand-drawn original with its top three lines set again.
 *
 * The original says ddiver_facebook and "Forty groups"; the repository is ddiver_fb and one
 * collector reads at most 25 groups (DESIGN, D17). Two post cards lose their local names too,
 * like the GIF and the screenshots: Georgi is Daniel, Ivo is Leo,
 * Mladost is South Park. The background behind the text is one flat
 * colour, so a patch of it and the same faces at the same places are all the edit takes.
 */
import React from 'react';
import {AbsoluteFill, Img, staticFile} from 'remotion';
import {loadFont as loadInter} from '@remotion/google-fonts/Inter';
import {loadFont as loadMono} from '@remotion/google-fonts/JetBrainsMono';
import {C} from './theme';

const inter = loadInter('normal', {weights: ['400', '500', '600']});
const mono = loadMono('normal', {weights: ['400']});

export const BANNER_W = 1280;
export const BANNER_H = 640;

const Patch: React.FC<{top: number; height: number}> = ({top, height}) => (
  <div
    style={{position: 'absolute', left: 590, top, width: 690, height, backgroundColor: C.bg}}
  />
);

/** A line of a post card, set again over the card's own flat colour. */
const CardLine: React.FC<{
  left: number;
  top: number;
  width: number;
  height: number;
  textTop: number;
  size: number;
  weight: number;
  spacing: number;
  children: React.ReactNode;
}> = ({left, top, width, height, textTop, size, weight, spacing, children}) => (
  <>
    <div style={{position: 'absolute', left, top, width, height, backgroundColor: C.card}} />
    <div
      style={{
        position: 'absolute',
        left: left + 2,
        top: textTop,
        fontFamily: inter.fontFamily,
        fontSize: size,
        fontWeight: weight,
        letterSpacing: spacing,
        color: C.grey,
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </div>
  </>
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
    <CardLine left={636} top={277} width={210} height={20} textTop={276.8} size={13.5} weight={400} spacing={-0.6}>
      Any good pediatrician near South Park?
    </CardLine>
    <CardLine left={636} top={315} width={40} height={16} textTop={314.8} size={12} weight={600} spacing={-0.7}>
      Daniel
    </CardLine>
    <CardLine left={636} top={426} width={27} height={14} textTop={425.8} size={12} weight={600} spacing={-0.7}>
      Leo ·
    </CardLine>
  </AbsoluteFill>
);
