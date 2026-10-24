import React from 'react';
import {Composition, Still} from 'remotion';
import {Banner, BANNER_H, BANNER_W} from './Banner';
import {FPS, H, TOTAL, W, Workflow} from './Workflow';
import {ShotAi, ShotAlerts, ShotGroups, ShotNotification, ShotStatus, ShotTest} from './shots';

/** Each shot is as tall as its own content: no letterboxing in the README. */
const SHOT_W = 1000;

export const Root: React.FC = () => (
  <>
    <Composition
      id="workflow"
      component={Workflow}
      durationInFrames={TOTAL}
      fps={FPS}
      width={W}
      height={H}
    />
    <Still id="banner" component={Banner} width={BANNER_W} height={BANNER_H} />
    <Still id="shot-notification" component={ShotNotification} width={SHOT_W} height={540} />
    <Still id="shot-groups" component={ShotGroups} width={SHOT_W} height={640} />
    <Still id="shot-test" component={ShotTest} width={SHOT_W} height={330} />
    <Still id="shot-status" component={ShotStatus} width={SHOT_W} height={560} />
    <Still id="shot-ai" component={ShotAi} width={SHOT_W} height={640} />
    <Still id="shot-alerts" component={ShotAlerts} width={SHOT_W} height={560} />
  </>
);
