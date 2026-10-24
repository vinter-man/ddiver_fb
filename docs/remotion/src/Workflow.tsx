/**
 * The README's GIF: the feed scrolls, a rule catches one post, the bot sends it; then a wish in
 * plain words and the model's verdicts on two rideshare posts. The notification and the
 * verdicts are the captured ones, so the GIF says what the bot and the model really said.
 */
import React from 'react';
import {
  AbsoluteFill,
  Easing,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import {loadFont as loadInter} from '@remotion/google-fonts/Inter';
import {loadFont as loadMono} from '@remotion/google-fonts/JetBrainsMono';
import captured from './captured.json';
import {C} from './theme';
import {In, RichStyle, Window} from './telegram';

const SANS = loadInter('normal', {weights: ['400', '500', '600', '700']}).fontFamily;
const MONO = loadMono('normal', {weights: ['400', '600']}).fontFamily;

export const FPS = 25;
export const W = 1000;
export const H = 560;
export const TOTAL = 420;

const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

const STAGES = ['Facebook groups', 'collector at home', 'brain on a VPS', 'Telegram'] as const;
const STAGE_AT = [0, 40, 120, 170] as const;
const AI_AT = 266;

/* ── the rail ──────────────────────────────────────────────────────────── */

const Rail: React.FC = () => {
  const frame = useCurrentFrame();
  // The AI rule runs on the brain, so its beat lights the brain again.
  const active =
    frame >= AI_AT ? 2 : frame < STAGE_AT[1] ? 0 : frame < STAGE_AT[2] ? 1 : frame < STAGE_AT[3] ? 2 : 3;
  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        bottom: 0,
        height: 62,
        borderTop: `1px solid ${C.line}`,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 14,
        fontFamily: MONO,
        fontSize: 16,
      }}
    >
      {STAGES.map((s, i) => (
        <React.Fragment key={s}>
          {i > 0 ? <span style={{color: i <= active ? C.gold : C.line}}>→</span> : null}
          <span
            style={{
              color: i === active ? C.gold : i < active ? C.cream : C.grey,
              opacity: i > active ? 0.45 : 1,
              fontWeight: i === active ? 600 : 400,
            }}
          >
            {s}
          </span>
        </React.Fragment>
      ))}
    </div>
  );
};

const Caption: React.FC<{children: React.ReactNode; from: number; to: number}> = ({
  children,
  from,
  to,
}) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [from, from + 10, to - 10, to], [0, 1, 1, 0], clamp);
  const y = interpolate(frame, [from, from + 12], [10, 0], clamp);
  return (
    <div
      style={{
        position: 'absolute',
        left: 44,
        top: 30,
        fontFamily: SANS,
        fontSize: 27,
        fontWeight: 600,
        letterSpacing: -0.6,
        color: C.cream,
        opacity,
        transform: `translateY(${y}px)`,
      }}
    >
      {children}
    </div>
  );
};

/* ── post cards, the banner's own ──────────────────────────────────────── */

type Post = {name: string; group: string; text: string; hit?: string};

const FEED: Post[] = [
  {name: 'Elena', group: 'Sofia Moms', text: 'Which kindergarten in Lozenets has places this year?'},
  {name: 'Petar', group: 'Rides Plovdiv ⇄ Sofia', text: 'Пътувам за София в 7:30, 2 места'},
  {name: 'Georgi', group: 'Second-hand · Sofia', text: 'Bike, 120 eur, pickup only'},
  {name: 'Anna K.', group: 'Sofia Moms', text: 'Any good pediatrician in Mladost?'},
  {name: 'Nikolay', group: 'Free stuff Sofia', text: 'Giving away boxes after the move'},
  {name: 'Desi', group: 'Flats for rent Sofia', text: 'Двустаен в Гео Милев, 600 евро'},
  {name: 'Maria P.', group: 'Second-hand · Sofia', text: 'Selling a stroller, barely used, 80 eur', hit: 'stroller'},
  {name: 'Ivo', group: 'Free stuff Sofia', text: 'Old TV, works, come and take it'},
  {name: 'Kalina', group: 'Sofia Moms', text: 'Lost a blue mitten near South Park 🧤'},
];
const HIT = FEED.findIndex((p) => p.hit);
const CARD_H = 74;
const GAP = 10;

const Card: React.FC<{post: Post; lit: number; dim: number}> = ({post, lit, dim}) => {
  const parts = post.hit ? post.text.split(post.hit) : [post.text];
  return (
    <div
      style={{
        height: CARD_H,
        boxSizing: 'border-box',
        backgroundColor: C.card,
        border: `2px solid ${lit > 0 ? `rgba(230,240,250,${lit})` : C.card}`,
        padding: '11px 16px',
        display: 'flex',
        gap: 13,
        opacity: 1 - dim * 0.55,
        fontFamily: SANS,
        position: 'relative',
      }}
    >
      <div
        style={{
          width: 26,
          height: 26,
          borderRadius: '50%',
          backgroundColor: lit > 0 ? C.gold : '#1D4065',
          flexShrink: 0,
          marginTop: 2,
        }}
      />
      <div style={{minWidth: 0}}>
        <div style={{fontSize: 14, color: C.grey, whiteSpace: 'nowrap'}}>
          <span style={{color: lit > 0 ? C.cream : '#B9C9DA', fontWeight: 600}}>{post.name}</span>
          <span style={{fontFamily: MONO, fontSize: 12.5}}> · {post.group}</span>
          {post.hit ? (
            <span
              style={{
                position: 'absolute',
                right: 14,
                top: 12,
                fontFamily: MONO,
                fontSize: 12.5,
                color: C.gold,
                opacity: lit,
              }}
            >
              🔑 rule: {post.hit}
            </span>
          ) : null}
        </div>
        <div
          style={{
            fontSize: 17,
            color: C.cream,
            marginTop: 3,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {parts[0]}
          {post.hit ? (
            <span
              style={{
                backgroundColor: `rgba(224,170,56,${0.25 + lit * 0.75})`,
                color: lit > 0.5 ? '#1A1405' : C.cream,
                borderRadius: 4,
                padding: '0 4px',
              }}
            >
              {post.hit}
            </span>
          ) : null}
          {parts[1] ?? null}
        </div>
      </div>
    </div>
  );
};

const Feed: React.FC = () => {
  const frame = useCurrentFrame();
  // The feed scrolls the way a reader scrolls it, and settles with the matching post in view.
  const end = -(HIT * (CARD_H + GAP)) + 150;
  const y = interpolate(frame, [0, 95], [120, end], {
    ...clamp,
    easing: Easing.bezier(0.3, 0.1, 0.2, 1),
  });
  const lit = interpolate(frame, [100, 112], [0, 1], clamp);
  const fade = interpolate(frame, [250, 268], [1, 0], clamp);
  return (
    <div
      style={{
        position: 'absolute',
        left: 44,
        top: 92,
        width: 440,
        height: 400,
        overflow: 'hidden',
        opacity: fade,
        maskImage: 'linear-gradient(transparent 0, black 40px, black 360px, transparent 400px)',
      }}
    >
      <div style={{transform: `translateY(${y}px)`, display: 'flex', flexDirection: 'column', gap: GAP}}>
        {FEED.map((post, i) => (
          <Card key={i} post={post} lit={i === HIT ? lit : 0} dim={i === HIT ? 0 : lit} />
        ))}
      </div>
    </div>
  );
};

/* ── Telegram on the right ─────────────────────────────────────────────── */

const Phone: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const shown = spring({frame: frame - 165, fps, config: {damping: 18}});
  const msg = spring({frame: frame - 190, fps, config: {damping: 15}});
  const fade = interpolate(frame, [250, 268], [1, 0], clamp);
  const n = captured.notification;
  return (
    <>
      <div
        style={{
          position: 'absolute',
          left: 530,
          top: 66,
          fontFamily: MONO,
          fontSize: 14,
          color: C.grey,
          opacity: shown * fade,
        }}
      >
        to Telegram, 3 min later
      </div>
      <div
        style={{
          position: 'absolute',
          left: 530,
          top: 92,
          width: 430,
          height: 400,
          overflow: 'hidden',
          borderRadius: 10,
          border: `1px solid ${C.line}`,
          opacity: shown * fade,
          transform: `translateX(${(1 - shown) * 30}px)`,
        }}
      >
        <div style={{position: 'absolute', width: 645, height: 600, transform: 'scale(0.6667)', transformOrigin: '0 0'}}>
          <Window>
            <RichStyle />
            <div style={{opacity: msg, transform: `translateY(${(1 - msg) * 26}px)`}}>
              <In html={n.html} time={n.time} keyboard={n.buttons} width={590} />
            </div>
          </Window>
        </div>
      </div>
    </>
  );
};

/* ── the AI rule ───────────────────────────────────────────────────────── */

const Verdicts: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const shown = interpolate(frame, [270, 285], [0, 1], clamp);
  const wish = spring({frame: frame - 278, fps, config: {damping: 18}});
  const rows = captured.verdicts;
  return (
    <div style={{position: 'absolute', left: 44, top: 90, width: 912, opacity: shown}}>
      <div
        style={{
          fontFamily: SANS,
          fontSize: 18,
          color: C.cream,
          backgroundColor: C.cardHi,
          border: `1px solid ${C.line}`,
          padding: '12px 16px',
          transform: `translateY(${(1 - wish) * 10}px)`,
          opacity: wish,
        }}
      >
        <span style={{fontFamily: MONO, fontSize: 14, color: C.grey}}>/ai add {captured.ids.rides} </span>
        <br />
        “{captured.wish}”
      </div>
      <div style={{display: 'flex', flexDirection: 'column', gap: 8, marginTop: 14}}>
        {rows.map((v, i) => {
          const s = spring({frame: frame - (300 + i * 18), fps, config: {damping: 16}});
          const yes = v.percent >= 70;
          return (
            <div
              key={i}
              style={{
                display: 'flex',
                gap: 16,
                alignItems: 'center',
                backgroundColor: C.card,
                border: `2px solid ${yes ? 'rgba(224,170,56,0.9)' : C.card}`,
                padding: '10px 16px',
                opacity: s,
                transform: `translateX(${(1 - s) * -16}px)`,
                fontFamily: SANS,
              }}
            >
              <div
                style={{
                  width: 64,
                  fontFamily: MONO,
                  fontSize: 22,
                  fontWeight: 600,
                  color: yes ? C.gold : C.greyDim,
                  textAlign: 'right',
                  flexShrink: 0,
                }}
              >
                {v.percent}%
              </div>
              <div style={{minWidth: 0}}>
                <div style={{fontSize: 17, color: yes ? C.cream : C.grey}}>{v.text}</div>
                <div style={{fontSize: 14, color: yes ? '#C9B27A' : C.greyDim, marginTop: 3}}>
                  {v.reason}
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <div
        style={{
          marginTop: 14,
          fontFamily: MONO,
          fontSize: 14,
          color: C.grey,
          opacity: interpolate(frame, [376, 388], [0, 1], clamp),
        }}
      >
        {captured.verdicts.length} posts judged · DeepSeek V4.1 Flash · about $0.0002 a post
      </div>
    </div>
  );
};

export const Workflow: React.FC = () => (
  <AbsoluteFill style={{backgroundColor: C.bg}}>
    <Caption from={0} to={102}>A browser at home reads your groups&apos; feed</Caption>
    <Caption from={102} to={168}>A rule catches one post</Caption>
    <Caption from={168} to={262}>The bot tells you</Caption>
    <Caption from={262} to={TOTAL + 20}>Or say it in words, and a model judges</Caption>
    <Feed />
    <Phone />
    <Verdicts />
    <Rail />
  </AbsoluteFill>
);
