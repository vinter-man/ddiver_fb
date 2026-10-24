/**
 * Telegram Desktop on a Mac, Night theme, as a cropped screenshot of the chat with the bot.
 *
 * The palette is the client's own "Night" theme and the face is Open Sans, which the client
 * ships. Icons are drawn, not borrowed from a font: a glyph that is almost the icon is what
 * makes a mock-up read as one.
 */
import React from 'react';
import {Img, staticFile} from 'remotion';
import {loadFont as loadOpenSans} from '@remotion/google-fonts/OpenSans';

const openSans = loadOpenSans('normal', {
  weights: ['400', '600', '700'],
  subsets: ['latin', 'latin-ext', 'cyrillic'],
});

export const SANS = openSans.fontFamily;
export const MONO = 'Menlo, "SF Mono", Consolas, monospace';

export const TG = {
  header: '#17212B',
  chat: '#0E1621',
  bubble: '#182533',
  out: '#2B5278',
  text: '#F5F5F5',
  meta: '#6D7F8F',
  metaOut: '#7DA8D3',
  dim: '#708499',
  link: '#6AB3F3',
  button: 'rgba(255,255,255,0.075)',
  divider: 'rgba(0,0,0,0.38)',
  accent: '#5288C1',
  border: '#0B1219',
} as const;

const Icon: React.FC<{d: string; size?: number; color?: string; stroke?: boolean}> = ({
  d,
  size = 22,
  color = TG.dim,
  stroke = true,
}) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
    <path
      d={d}
      stroke={stroke ? color : 'none'}
      fill={stroke ? 'none' : color}
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

const SEARCH = 'M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM15.5 15.5 20 20';
const SIDEBAR = 'M4 5h16v14H4zM14 5v14';
const DOTS =
  'M12 6.2a1.2 1.2 0 1 0 0-.1zM12 12.1a1.2 1.2 0 1 0 0-.1zM12 18a1.2 1.2 0 1 0 0-.1z';
const CLIP =
  'M20 11.5 12.4 19a5 5 0 0 1-7.1-7.1l7.9-7.8a3.3 3.3 0 0 1 4.7 4.7l-7.9 7.8a1.7 1.7 0 0 1-2.4-2.4l7.2-7.1';
const SMILE =
  'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8.5 14.2s1.3 1.8 3.5 1.8 3.5-1.8 3.5-1.8M9 9.5h.01M15 9.5h.01';
const MIC = 'M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zM18 11.5a6 6 0 0 1-12 0M12 17.5V21';
const MENU = 'M5 7h14M5 12h14M5 17h14';

export const Avatar: React.FC<{size?: number}> = ({size = 42}) => (
  <Img
    src={staticFile('avatar.png')}
    style={{width: size, height: size, borderRadius: '50%', flexShrink: 0, objectFit: 'cover'}}
  />
);

/** The chat pane: header, the messages sitting on the floor, the composer with the bot menu. */
export const Window: React.FC<{children: React.ReactNode; typing?: string}> = ({
  children,
  typing = 'Write a message…',
}) => (
  <div
    style={{
      position: 'absolute',
      inset: 0,
      backgroundColor: TG.chat,
      fontFamily: SANS,
      display: 'flex',
      flexDirection: 'column',
      WebkitFontSmoothing: 'antialiased',
    }}
  >
    <div
      style={{
        backgroundColor: TG.header,
        padding: '9px 18px',
        display: 'flex',
        alignItems: 'center',
        gap: 13,
        borderBottom: `1px solid ${TG.border}`,
        flexShrink: 0,
      }}
    >
      <Avatar />
      <div style={{flex: 1}}>
        <div style={{fontSize: 15, fontWeight: 600, color: TG.text}}>ddiver-fb</div>
        <div style={{fontSize: 13.5, color: TG.dim, marginTop: 1}}>bot</div>
      </div>
      <div style={{display: 'flex', gap: 20, alignItems: 'center'}}>
        <Icon d={SEARCH} />
        <Icon d={SIDEBAR} />
        <Icon d={DOTS} color={TG.dim} />
      </div>
    </div>

    <div
      style={{
        flex: 1,
        padding: '12px 22px 12px',
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'flex-end',
        gap: 8,
        overflow: 'hidden',
      }}
    >
      {children}
    </div>

    <div
      style={{
        backgroundColor: TG.header,
        borderTop: `1px solid ${TG.border}`,
        padding: '11px 16px',
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        flexShrink: 0,
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 5,
          backgroundColor: TG.accent,
          color: '#fff',
          borderRadius: 14,
          padding: '4px 11px 4px 7px',
          fontSize: 13.5,
          fontWeight: 600,
        }}
      >
        <Icon d={MENU} size={16} color="#fff" />
        Menu
      </div>
      <Icon d={CLIP} />
      <span style={{flex: 1, color: TG.dim, fontSize: 14.5}}>{typing}</span>
      <Icon d={SMILE} />
      <Icon d={MIC} />
    </div>
  </div>
);

export const DayDivider: React.FC<{children?: React.ReactNode}> = ({children = 'October 24'}) => (
  <div style={{display: 'flex', justifyContent: 'center', margin: '4px 0 6px'}}>
    <span
      style={{
        backgroundColor: TG.divider,
        color: '#E4ECF2',
        fontSize: 13,
        fontWeight: 600,
        padding: '3px 10px',
        borderRadius: 12,
      }}
    >
      {children}
    </span>
  </div>
);

const Ticks: React.FC = () => (
  <svg width={16} height={10} viewBox="0 0 16 10" style={{marginLeft: 3}}>
    <path d="M1 5.2 3.9 8 9.6 1.8" stroke={TG.metaOut} strokeWidth={1.5} fill="none" />
    <path d="M6.4 8 12.3 1.8" stroke={TG.metaOut} strokeWidth={1.5} fill="none" />
  </svg>
);

/** Bold, code and links exactly where the bot's own renderer put them. */
const Rich: React.FC<{html: string}> = ({html}) => (
  <div
    className="tg-rich"
    style={{whiteSpace: 'pre-wrap', wordBreak: 'break-word'}}
    dangerouslySetInnerHTML={{__html: html}}
  />
);

export const RichStyle: React.FC = () => (
  <style>{`
    .tg-rich b { font-weight: 700; }
    .tg-rich code { font-family: ${MONO}; font-size: 0.9em; color: #E7EEF5; }
    .tg-rich a { color: ${TG.link}; text-decoration: none; }
  `}</style>
);

/** Something the owner typed: right-hand side, blue, two ticks. */
export const Out: React.FC<{children: React.ReactNode; time: string}> = ({children, time}) => (
  <div style={{display: 'flex', justifyContent: 'flex-end'}}>
    <div
      style={{
        backgroundColor: TG.out,
        borderRadius: '13px 13px 4px 13px',
        padding: '6px 10px 6px 12px',
        maxWidth: '70%',
        fontSize: 14.5,
        lineHeight: 1.42,
        color: TG.text,
        display: 'flex',
        alignItems: 'flex-end',
        gap: 9,
      }}
    >
      <span>{children}</span>
      <span
        style={{
          fontSize: 12,
          color: TG.metaOut,
          whiteSpace: 'nowrap',
          display: 'flex',
          alignItems: 'center',
          marginBottom: -1,
        }}
      >
        {time}
        <Ticks />
      </span>
    </div>
  </div>
);

/** A message from the bot, with its inline keyboard hanging underneath. */
export const In: React.FC<{
  html: string;
  time: string;
  keyboard?: string[][];
  width?: number | string;
}> = ({html, time, keyboard, width}) => (
  <div
    style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'flex-start',
      width: width ?? 'auto',
      maxWidth: width ? undefined : '82%',
    }}
  >
    <div
      style={{
        backgroundColor: TG.bubble,
        borderRadius: '13px 13px 13px 4px',
        padding: '7px 11px 6px 12px',
        fontSize: 14.5,
        lineHeight: 1.42,
        color: TG.text,
        width: '100%',
        boxSizing: 'border-box',
        position: 'relative',
      }}
    >
      <Rich html={html} />
      <div style={{textAlign: 'right', fontSize: 12, color: TG.meta, marginTop: -2}}>{time}</div>
    </div>
    {keyboard ? (
      <div style={{width: '100%', marginTop: 3, display: 'flex', flexDirection: 'column', gap: 3}}>
        {keyboard.map((row, i) => (
          <div key={i} style={{display: 'flex', gap: 3}}>
            {row.map((label) => (
              <div
                key={label}
                style={{
                  flex: 1,
                  backgroundColor: TG.button,
                  borderRadius: 8,
                  padding: '8px 10px',
                  textAlign: 'center',
                  fontSize: 13.5,
                  fontWeight: 600,
                  color: TG.text,
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
              >
                {label}
              </div>
            ))}
          </div>
        ))}
      </div>
    ) : null}
  </div>
);
