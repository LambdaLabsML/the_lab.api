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
  reversed: boolean = false,
): string {
  // Build position map from computed layout.
  // When reversed, the "outgoing" edge of a parent is its LEFT side
  // and the "incoming" edge of a child is its RIGHT side.
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

  // Connection edges: parent "out" side → child "in" side
  // Normal: parent.r → child.l (left to right)
  // Reversed: parent.l → child.r (right to left)
  function outX(p: NodePos) { return reversed ? p.l : p.r; }
  function inX(p: NodePos) { return reversed ? p.r : p.l; }
  const dir = reversed ? -1 : 1; // sign for horizontal offsets

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
      const aOut = outX(a), bIn = inX(b);
      if (Math.abs(a.cy - b.cy) < 2) {
        s +=
          '<line x1="' + aOut + '" y1="' + a.cy +
          '" x2="' + bIn + '" y2="' + b.cy +
          '" stroke="' + color +
          '" stroke-width="4" stroke-opacity="0.7"' +
          ' stroke-linecap="round"' + edgeAttr + '/>';
      } else {
        const mx = (aOut + bIn) / 2;
        const dy = b.cy > a.cy ? 1 : -1;
        const vd = Math.abs(b.cy - a.cy);
        const gap = Math.abs(bIn - aOut);
        const r = Math.min(6, vd / 2, gap / 4);
        s +=
          '<path d="M' + aOut + ',' + a.cy +
          ' L' + (mx - dir * r) + ',' + a.cy +
          ' Q' + mx + ',' + a.cy + ' ' + mx + ',' + (a.cy + dy * r) +
          ' L' + mx + ',' + (b.cy - dy * r) +
          ' Q' + mx + ',' + b.cy + ' ' + (mx + dir * r) + ',' + b.cy +
          ' L' + bIn + ',' + b.cy +
          '" fill="none" stroke="' + color +
          '" stroke-width="4" stroke-opacity="0.7"' +
          ' stroke-linecap="round"' + edgeAttr + '/>';
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

      const pOut = outX(p);
      const cIn = inX(c);
      const jx = pOut + dir * SPACING * (i + 1);
      const dy = c.cy > p.cy ? 1 : -1;
      const vertDist = Math.abs(c.cy - p.cy);
      const horizAfter = Math.abs(cIn - jx);
      const r = Math.min(CORNER, vertDist / 2, horizAfter / 2, (SPACING * (i + 1)) / 2);

      s +=
        '<path d="M' + pOut + ',' + p.cy +
        ' L' + (jx - dir * r) + ',' + p.cy +
        ' Q' + jx + ',' + p.cy + ' ' + jx + ',' + (p.cy + dy * r) +
        ' L' + jx + ',' + (c.cy - dy * r) +
        ' Q' + jx + ',' + c.cy + ' ' + (jx + dir * r) + ',' + c.cy +
        ' L' + cIn + ',' + c.cy +
        '" fill="none" stroke="' + br.color +
        '" stroke-width="3" stroke-opacity="0.7"' +
        ' stroke-linecap="round" stroke-linejoin="round"' +
        ' data-from="' + br.pid + '" data-to="' + br.cid + '" class="svg-edge"/>';
    }
  }

  // 3. Station dots — on the "incoming" side of each node
  for (const n of data.nodes) {
    if (!pos[n.id]) continue;
    const dotColor = colorForIdea(n.id, colorMode);
    const dotX = inX(pos[n.id]);
    s +=
      '<circle cx="' + dotX +
      '" cy="' + pos[n.id].cy +
      '" r="6" fill="' + dotColor +
      '" stroke="var(--bg)" stroke-width="2.5"' +
      ' data-idea="' + n.id + '" class="svg-dot"/>';
  }

  return s;
}
