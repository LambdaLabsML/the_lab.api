// ------------------------------------------------------------
// buildSubwayLayout — pure function extracted from dashboard.html
// (lines 580-688).  Takes GraphResponse data, returns a
// SubwayLayout describing lane assignments and row positions.
// ------------------------------------------------------------

import type { GraphResponse, IdeaNode, Lane, LaneChildEntry, SubwayLayout } from './types';

/**
 * Build the logical subway layout from graph data.
 *
 * The layout assigns each idea to a lane (horizontal track) and
 * computes depth (column) and row positions so that the most-active
 * branches are placed first.
 *
 * Returns `null` when there are no nodes.
 */
export function buildSubwayLayout(data: GraphResponse): SubwayLayout | null {
  if (!data.nodes.length) return null;

  const nodeMap: Record<number, IdeaNode> = {};
  const childMap: Record<number, number[]> = {};
  for (const n of data.nodes) {
    nodeMap[n.id] = n;
    childMap[n.id] = [];
  }
  for (const e of data.edges) {
    if (childMap[e.from]) childMap[e.from].push(e.to);
  }

  // Depth = column position (longest path from root)
  const depth: Record<number, number> = {};
  function getDepth(id: number): number {
    if (depth[id] !== undefined) return depth[id];
    const parents = (nodeMap[id].parent_ids || []).filter((p) => nodeMap[p]);
    depth[id] = parents.length ? Math.max(...parents.map(getDepth)) + 1 : 0;
    return depth[id];
  }
  for (const n of data.nodes) getDepth(n.id);

  // Subtree last activity (most-active branch continues parent lane)
  const subAct: Record<number, string> = {};
  function getSubAct(id: number): string {
    if (subAct[id]) return subAct[id];
    const n = nodeMap[id];
    let best = n.last_finish || n.first_start || n.created_at || '';
    for (const c of childMap[id]) {
      const ca = getSubAct(c);
      if (ca > best) best = ca;
    }
    return (subAct[id] = best);
  }
  for (const n of data.nodes) getSubAct(n.id);

  // Sort children: most-active subtree first (continues parent lane)
  for (const id of Object.keys(childMap))
    childMap[Number(id)].sort((a, b) => (getSubAct(b) || '').localeCompare(getSubAct(a) || ''));

  // Assign lanes via DFS + build lane tree for packing
  const lanes: Lane[] = [];
  const ideaLane: Record<number, number> = {};
  const assigned = new Set<number>();
  const laneChildren: Record<number, LaneChildEntry[]> = {};

  function assign(id: number, lane: number): void {
    if (assigned.has(id)) return;
    assigned.add(id);
    if (lane < 0) {
      lane = lanes.length;
      lanes.push({ ideas: [], lastActivity: '' });
    }
    ideaLane[id] = lane;
    lanes[lane].ideas.push(id);
    const act = subAct[id] || '';
    if (act > lanes[lane].lastActivity) lanes[lane].lastActivity = act;
    const ch = childMap[id] || [];
    if (ch.length) {
      assign(ch[0], lane);
      for (let i = 1; i < ch.length; i++) {
        if (assigned.has(ch[i])) continue; // merge target already placed
        const childLane = lanes.length;
        if (!laneChildren[lane]) laneChildren[lane] = [];
        laneChildren[lane].push({ childLane, forkCol: depth[id] });
        assign(ch[i], -1);
      }
    }
  }

  const hasParent = new Set(data.edges.map((e) => e.to));
  const roots = data.nodes.filter((n) => !hasParent.has(n.id)).map((n) => n.id);
  roots.sort((a, b) => (getSubAct(b) || '').localeCompare(getSubAct(a) || ''));
  for (const r of roots) assign(r, -1);

  // Compute deepest subtree activity for each lane (includes descendant lanes)
  const laneSubAct: Record<number, string> = {};
  function getLaneSubAct(li: number): string {
    if (laneSubAct[li]) return laneSubAct[li];
    let best = lanes[li].lastActivity || '';
    for (const c of laneChildren[li] || []) {
      const ca = getLaneSubAct(c.childLane);
      if (ca > best) best = ca;
    }
    return (laneSubAct[li] = best);
  }
  for (let li = 0; li < lanes.length; li++) getLaneSubAct(li);

  // ── Lane geometry: column span + leftmost column ──────────────────────────
  // "Length" of a lane = how many columns its backbone spans (rightmost depth −
  // leftmost depth + 1). The longest lane is the natural backbone of the graph,
  // so it should hug the top row and stay horizontal; shorter branches stack
  // beneath it. Station count and earliest column are tie-breaks.
  const laneSpan: Record<number, number> = {};      // columns covered by backbone
  const laneStations: Record<number, number> = {};  // number of stations in lane
  const laneMinCol: Record<number, number> = {};     // leftmost column (earliest)
  for (let li = 0; li < lanes.length; li++) {
    const ids = lanes[li].ideas;
    let lo = Infinity, hi = -Infinity;
    for (const id of ids) {
      const d = depth[id];
      if (d < lo) lo = d;
      if (d > hi) hi = d;
    }
    laneStations[li] = ids.length;
    laneMinCol[li] = lo === Infinity ? 0 : lo;
    laneSpan[li] = hi < lo ? 0 : hi - lo + 1;
  }

  // The "reach" of a lane is the deepest column reached by it OR any of its
  // descendant fork-lanes — i.e. how far right the whole branch extends. This
  // lets a parent backbone that hands off to a long child still rank as long,
  // keeping the dominant chain near the top.
  const laneReach: Record<number, number> = {};
  function getLaneReach(li: number): number {
    if (laneReach[li] !== undefined) return laneReach[li];
    let best = laneSpan[li] > 0 ? laneMinCol[li] + laneSpan[li] - 1 : laneMinCol[li];
    for (const c of laneChildren[li] || []) {
      const cr = getLaneReach(c.childLane);
      if (cr > best) best = cr;
    }
    laneReach[li] = best;
    return best;
  }
  for (let li = 0; li < lanes.length; li++) getLaneReach(li);

  // Comparator: longest first (descending span), then more stations, then
  // earliest column (left), then most-active subtree, then lane index (stable).
  // Used both to order root lanes and to order sibling forks within a parent.
  function laneOrder(a: number, b: number): number {
    if (laneSpan[a] !== laneSpan[b]) return laneSpan[b] - laneSpan[a];
    if (laneStations[a] !== laneStations[b]) return laneStations[b] - laneStations[a];
    if (laneMinCol[a] !== laneMinCol[b]) return laneMinCol[a] - laneMinCol[b];
    const aa = getLaneSubAct(a) || '', ba = getLaneSubAct(b) || '';
    if (aa !== ba) return ba.localeCompare(aa);
    return a - b;
  }

  // Place lanes via DFS of the lane tree so each parent stays above its forks
  // (children never get a smaller row than their parent — required because a
  // child backbone visually drops down from the parent at the fork column).
  // Within a parent, sibling forks are ordered LONGEST-FIRST so the dominant
  // continuation sits closest to the parent row and the map's vertical extent
  // is dominated by the few long lanes near the top.
  const laneRow: Record<number, number> = {};
  let nextRow = 0;
  function placeLane(li: number): void {
    laneRow[li] = nextRow++;
    const children = (laneChildren[li] || []).slice();
    children.sort((a, b) => laneOrder(a.childLane, b.childLane));
    for (const c of children) placeLane(c.childLane);
  }

  // Find root lanes (no lane-parent). Order them longest-first so the single
  // longest backbone in the whole graph is pinned to row 0 (the top band) and
  // depth grows rightward instead of forcing the main path downward.
  const hasLaneParent = new Set<number>();
  for (const ch of Object.values(laneChildren))
    for (const c of ch) hasLaneParent.add(c.childLane);
  const rootLanes: number[] = [];
  for (let li = 0; li < lanes.length; li++)
    if (!hasLaneParent.has(li)) rootLanes.push(li);
  // For root lanes, rank by the full reach of the branch (so a root that seeds
  // the deepest overall chain wins), then by the generic lane order.
  rootLanes.sort(function (a, b) {
    if (getLaneReach(a) !== getLaneReach(b)) return getLaneReach(b) - getLaneReach(a);
    return laneOrder(a, b);
  });
  for (const rl of rootLanes) placeLane(rl);

  return {
    nodeMap,
    childMap,
    depth,
    ideaLane,
    laneRow,
    laneChildren,
    lanes,
    numLanes: lanes.length,
    maxDepth: Math.max(0, ...Object.values(depth)),
  };
}
