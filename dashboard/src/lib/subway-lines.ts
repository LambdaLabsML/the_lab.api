// ------------------------------------------------------------
// drawSubwayLines — pure function extracted from dashboard.html
// (lines 1072-1191).  Returns an SVG string for the subway
// connection lines and station dots.
// ------------------------------------------------------------

import type { GraphResponse, SubwayLayout, StationPos } from './types';
import { IDEA_PALETTE } from './colors';

interface NodePos {
  cx: number;
  cy: number;
  l: number;
  r: number;
}

/**
 * Build SVG markup for subway lane backbones, branch connections,
 * and station dots.
 *
 * @param data        - The graph data (nodes + edges)
 * @param layout      - The subway layout (lanes, ideaLane, etc.)
 * @param stationPos  - Map of node id -> rendered position
 * @param colorMode   - Current color mode string
 * @param allExperiments - Not used directly here but kept for signature parity
 * @param colorForIdea - Function that returns a color for an idea id
 * @returns SVG inner-HTML string
 */
export function drawSubwayLines(
  data: GraphResponse,
  layout: SubwayLayout,
  stationPos: Record<number, StationPos>,
  colorMode: string,
  colorForIdea: (ideaId: number, mode: string) => string,
): string {
  // Build position map from computed layout
  const pos: Record<number, NodePos> = {};
  for (const n of data.nodes) {
    const p = stationPos[n.id];
    if (!p) continue;
    pos[n.id] = {
      cx: p.x + p.w / 2,
      cy: p.y + p.h / 2,
      l: p.x,
      r: p.x + p.w,
    };
  }

  let s = '';

  // 1. Lane backbone lines — orthogonal routing between consecutive same-lane stations
  for (let li = 0; li < layout.lanes.length; li++) {
    const lane = layout.lanes[li];
    if (lane.ideas.length < 2) continue;
    const ids = lane.ideas.slice();
    const pts = ids.map((id) => pos[id]);
    const color = IDEA_PALETTE[li % IDEA_PALETTE.length];
    for (let i = 0; i < pts.length - 1; i++) {
      if (!pts[i] || !pts[i + 1]) continue;
      const a = pts[i],
        b = pts[i + 1];
      const edgeAttr =
        ' data-from="' + ids[i] + '" data-to="' + ids[i + 1] + '" class="svg-edge"';
      if (Math.abs(a.cy - b.cy) < 2) {
        s +=
          '<line x1="' +
          a.r +
          '" y1="' +
          a.cy +
          '" x2="' +
          b.l +
          '" y2="' +
          b.cy +
          '" stroke="' +
          color +
          '" stroke-width="4" stroke-opacity="0.7"' +
          ' stroke-linecap="round"' +
          edgeAttr +
          '/>';
      } else {
        const mx = (a.r + b.l) / 2;
        const dy = b.cy > a.cy ? 1 : -1;
        const vd = Math.abs(b.cy - a.cy);
        const r = Math.min(
          6,
          vd / 2,
          Math.max(0, mx - a.r) / 2,
          Math.max(0, b.l - mx) / 2,
        );
        s +=
          '<path d="M' +
          a.r +
          ',' +
          a.cy +
          ' L' +
          (mx - r) +
          ',' +
          a.cy +
          ' Q' +
          mx +
          ',' +
          a.cy +
          ' ' +
          mx +
          ',' +
          (a.cy + dy * r) +
          ' L' +
          mx +
          ',' +
          (b.cy - dy * r) +
          ' Q' +
          mx +
          ',' +
          b.cy +
          ' ' +
          (mx + r) +
          ',' +
          b.cy +
          ' L' +
          b.l +
          ',' +
          b.cy +
          '" fill="none" stroke="' +
          color +
          '" stroke-width="4" stroke-opacity="0.7"' +
          ' stroke-linecap="round"' +
          edgeAttr +
          '/>';
      }
    }
  }

  // 2. Branch connections — orthogonal routing, non-intersecting
  interface Branch {
    pid: number;
    cid: number;
    color: string;
  }

  const branches: Branch[] = [];
  for (const n of data.nodes) {
    for (const pid of n.parent_ids || []) {
      if (!pos[pid] || !pos[n.id]) continue;
      if (layout.ideaLane[n.id] === layout.ideaLane[pid]) continue;
      branches.push({
        pid,
        cid: n.id,
        color: IDEA_PALETTE[layout.ideaLane[n.id] % IDEA_PALETTE.length],
      });
    }
  }

  const byParent: Record<number, Branch[]> = {};
  for (const br of branches) {
    if (!byParent[br.pid]) byParent[br.pid] = [];
    byParent[br.pid].push(br);
  }

  const SPACING = 10;
  const CORNER = 6;

  for (const pid of Object.keys(byParent)) {
    const group = byParent[Number(pid)];
    const p = pos[Number(pid)];
    if (!p) continue;
    // Sort by vertical distance descending — furthest drops first (closest junction)
    group.sort(function (a, b) {
      return Math.abs(pos[b.cid].cy - p.cy) - Math.abs(pos[a.cid].cy - p.cy);
    });

    for (let i = 0; i < group.length; i++) {
      const br = group[i];
      const c = pos[br.cid];
      if (!c) continue;

      const jx = p.r + SPACING * (i + 1);
      const dy = c.cy > p.cy ? 1 : -1;
      const vertDist = Math.abs(c.cy - p.cy);
      const horizAfter = Math.max(0, c.l - jx);
      const r = Math.min(CORNER, vertDist / 2, horizAfter / 2, (SPACING * (i + 1)) / 2);

      s +=
        '<path d="' +
        'M' +
        p.r +
        ',' +
        p.cy +
        ' L' +
        (jx - r) +
        ',' +
        p.cy +
        ' Q' +
        jx +
        ',' +
        p.cy +
        ' ' +
        jx +
        ',' +
        (p.cy + dy * r) +
        ' L' +
        jx +
        ',' +
        (c.cy - dy * r) +
        ' Q' +
        jx +
        ',' +
        c.cy +
        ' ' +
        (jx + r) +
        ',' +
        c.cy +
        ' L' +
        c.l +
        ',' +
        c.cy +
        '" fill="none" stroke="' +
        br.color +
        '" stroke-width="3" stroke-opacity="0.7"' +
        ' stroke-linecap="round" stroke-linejoin="round"' +
        ' data-from="' +
        br.pid +
        '" data-to="' +
        br.cid +
        '" class="svg-edge"/>';
    }
  }

  // 3. Station dots — colored by current mode
  for (const n of data.nodes) {
    if (!pos[n.id]) continue;
    const dotColor = colorForIdea(n.id, colorMode);
    s +=
      '<circle cx="' +
      pos[n.id].l +
      '" cy="' +
      pos[n.id].cy +
      '" r="6" fill="' +
      dotColor +
      '" stroke="#0d1117" stroke-width="2.5"' +
      ' data-idea="' +
      n.id +
      '" class="svg-dot"/>';
  }

  return s;
}
