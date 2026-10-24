/**
 * The README's bot screenshots. Every word in them comes from src/captured.json, which
 * tools/readme_shots.py writes by running the bot's own code: only the window is drawn here.
 *
 * The shots are one morning of one chat, in order, and each is cropped mid-conversation: the
 * message before it is cut by the top edge, as it is in a real screenshot of a busy chat.
 */
import React from 'react';
import captured from './captured.json';
import {DayDivider, In, Out, RichStyle, Window} from './telegram';

const Shot: React.FC<{children: React.ReactNode}> = ({children}) => (
  <Window>
    <RichStyle />
    {children}
  </Window>
);

const n = captured.notification;
const earlier = captured.earlier;

const Notification: React.FC = () => <In html={n.html} time={n.time} keyboard={n.buttons} width={760} />;
const Groups: React.FC = () => (
  <In html={captured.groups.html} time="10:58" keyboard={captured.groups.buttons} width={520} />
);
const Status: React.FC = () => <In html={captured.status.html} time="11:04" />;
const Test: React.FC = () => <In html={captured.test.html} time="11:06" />;

export const ShotGroups: React.FC = () => (
  <Shot>
    <In html={earlier.html} time={earlier.time} keyboard={earlier.buttons} />
    <DayDivider />
    <Out time="10:58">/menu</Out>
    <Groups />
  </Shot>
);

export const ShotNotification: React.FC = () => (
  <Shot>
    <Groups />
    <Notification />
  </Shot>
);

export const ShotStatus: React.FC = () => (
  <Shot>
    <Notification />
    <Out time="11:04">{captured.status.command}</Out>
    <Status />
  </Shot>
);

export const ShotTest: React.FC = () => (
  <Shot>
    <Status />
    <Out time="11:06">{captured.test.command}</Out>
    <Test />
  </Shot>
);

export const ShotAi: React.FC = () => (
  <Shot>
    <Test />
    <Out time="11:09">{captured.ai.command}</Out>
    <In html={captured.ai.html} time="11:09" width={820} />
  </Shot>
);

export const ShotAlerts: React.FC = () => (
  <Shot>
    <In html={earlier.html} time="22:47" keyboard={earlier.buttons} />
    <DayDivider>October 17</DayDivider>
    {captured.alerts.map((a, i) =>
      a.who === 'me' ? (
        <Out key={i} time={a.time}>
          {a.html}
        </Out>
      ) : (
        <In key={i} html={a.html} time={a.time} width={720} />
      ),
    )}
  </Shot>
);
