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

  // Place lanes via DFS of lane tree.
  // At each lane, process forks column-by-column (left -> right).
  // Within each column's forks, most recently active subtree placed first
  // (closest to parent row).
  const laneRow: Record<number, number> = {};
  let nextRow = 0;
  function placeLane(li: number): void {
    laneRow[li] = nextRow++;
    const children = (laneChildren[li] || []).slice();
    children.sort(function (a, b) {
      if (a.forkCol !== b.forkCol) return a.forkCol - b.forkCol;
      // Same column: most recently active subtree first (closest to parent)
      return (getLaneSubAct(b.childLane) || '').localeCompare(getLaneSubAct(a.childLane) || '');
    });
    for (const c of children) placeLane(c.childLane);
  }

  // Find root lanes (no lane-parent) and sort by activity
  const hasLaneParent = new Set<number>();
  for (const ch of Object.values(laneChildren))
    for (const c of ch) hasLaneParent.add(c.childLane);
  const rootLanes: number[] = [];
  for (let li = 0; li < lanes.length; li++)
    if (!hasLaneParent.has(li)) rootLanes.push(li);
  rootLanes.sort(function (a, b) {
    return (getLaneSubAct(b) || '').localeCompare(getLaneSubAct(a) || '');
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
