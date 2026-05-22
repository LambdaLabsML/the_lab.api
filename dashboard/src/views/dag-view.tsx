// ------------------------------------------------------------
// DagView — Preact component that renders the DAG / subway map.
//
// Ported from the updateGraph() function in dashboard.html
// (lines ~690-1070).  Faithfully reproduces the station
// measurement, column/y-packing, pass-through reservations,
// crossing adjustments, and SVG line rendering.
// ------------------------------------------------------------

import { useRef, useEffect } from "preact/hooks";
import { navigateToIdea } from "../lib/navigate";
import type { IdeaNode, StationPos, SubwayLayout } from "../lib/types";
import { graphData, currentLayout, highlightedIdea, allIdeas, allExperiments, runningProgress } from "../state/signals";
import { colorMode, selectedIdea, selectedMetric, improvementsOnly, activeTagFilters, tagFilterMode, reverseTime, showAbandoned, showConcluded, showRunning, colorTheme } from "../state/settings";
import { useSetting } from "../state/settings";
import { _ideaHasGlobalImprovement, resetGlobalBestBeforeCache } from "../lib/colors";
import { drawSubwayLines } from "../lib/subway-lines";
// dynamic — driven by lib/colors.ts (IDEA_PALETTE, getStatusColor, _colorForIdea)
import { IDEA_PALETTE, STATUS_ORDER, _colorForIdea, getStatusColor } from "../lib/colors";
import { filterMetricExperiments } from "../lib/chart-data";
import { escapeHtml, ideaTitle, badgeHtml } from "../lib/format";

// ---------------------------------------------------------------------------
// Module-level station-size cache, keyed by "id:title".
// Persists across renders so we only measure new / changed nodes.
// ---------------------------------------------------------------------------
const _stationSizeCache: Record<string, { w: number; h: number }> = {};

const DEFAULT_MAX_COL_WIDTH = 250;
const MIN_COL_WIDTH = 80;
const COMPACT_H = 22;  // height of compact pill nodes
const COMPACT_W = 42;  // width of compact pill nodes (fits #NNN)

// Per-column width overrides, persisted to localStorage
const colWidthOverrides = useSetting<Record<string, number>>("dagColWidths", {});

export function DagView() {
  const containerRef = useRef<HTMLDivElement>(null);

  const theme = colorTheme.value;  // subscribe so graph redraws on theme change
  const data = graphData.value;
  const layout = currentLayout.value;
  const mode = colorMode.value;
  const highlighted = highlightedIdea.value;
  const metric = selectedMetric.value;
  const ideas = allIdeas.value;
  const experiments = allExperiments.value;
  const progress = runningProgress.value;

  // Build per-idea progress: max pct across running experiments for each idea
  const ideaProgress: Record<number, number> = {};
  for (const exp of experiments) {
    if (!exp._running) continue;
    const label = exp.label || String(exp.id);
    const pct = progress[label];
    if (typeof pct === "number") {
      ideaProgress[exp.idea_id] = Math.max(ideaProgress[exp.idea_id] || 0, pct);
    }
  }
  const compactMode = improvementsOnly.value;
  const reversed = reverseTime.value;

  // Build set of hidden idea statuses
  const hiddenStatuses = new Set<string>();
  if (!showAbandoned.value) hiddenStatuses.add("abandoned");
  if (!showConcluded.value) hiddenStatuses.add("concluded");
  if (!showRunning.value) { hiddenStatuses.add("active"); hiddenStatuses.add("suggested"); }

  // In compact mode, only ideas with global improvements get full boxes.
  // Status-hidden ideas are also compacted.
  const tags = activeTagFilters.value;
  const tagMode = tagFilterMode.value;
  // Reset the global-best cache so milestone detection uses the current filtered set
  resetGlobalBestBeforeCache();
  const metricExperiments = metric
    ? filterMetricExperiments(metric, experiments, tags, tagMode, hiddenStatuses)
    : [];
  const isImportant: Record<number, boolean> = {};
  if (data) {
    for (const n of data.nodes) {
      const statusHidden = hiddenStatuses.has(n.has_running ? "active" : n.status);
      if (statusHidden) {
        isImportant[n.id] = false;
      } else if (compactMode) {
        isImportant[n.id] =
          !!n.has_running ||
          (!!metric && _ideaHasGlobalImprovement(n.id, metric, metricExperiments));
      } else {
        isImportant[n.id] = true;
      }
    }
  }

  // Override compactMode if any status filters are active
  const effectiveCompactMode = compactMode || hiddenStatuses.size > 0;

  // =========================================================================
  // COMBINED MEASUREMENT + RENDER — single effect to avoid ordering issues
  // between useLayoutEffect and useEffect in Preact.
  // =========================================================================
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    if (!data || !data.nodes.length || !layout) {
      container.innerHTML =
        '<div style="padding:40px;color:var(--text-faint);text-align:center">No ideas yet</div>';
      return;
    }

    // --- Measure station heights ---
    // Heights are measured once and cached. Widths are controlled by colWidth
    // (capped at DEFAULT_MAX_COL_WIDTH or user override), so we only need heights.
    const STATION_HEIGHT = 28; // fallback if measurement fails
    const heights: Record<number, number> = {};
    for (const n of data.nodes) {
      if (effectiveCompactMode && !isImportant[n.id]) {
        heights[n.id] = COMPACT_H;
        continue;
      }
      const key = n.id + ":" + ideaTitle(n.description);
      if (_stationSizeCache[key]) {
        heights[n.id] = _stationSizeCache[key].h;
      }
    }

    // Measure any nodes not in the cache
    const needsMeasure = data.nodes.filter(
      (n) => heights[n.id] === undefined
    );
    if (needsMeasure.length > 0) {
      let mHtml = '<div style="position:absolute;visibility:hidden;top:0;left:0">';
      for (const n of needsMeasure) {
        const ds = n.has_running ? "running" : (n.has_queued ? "queued" : n.status);
        const sc = getStatusColor(ds);
        const lc = IDEA_PALETTE[layout.ideaLane[n.id] % IDEA_PALETTE.length];
        mHtml +=
          '<div class="subway-station" data-id="' + n.id +
          '" style="position:static;border-color:' + sc +
          ';border-left:4px solid ' + lc +
          (n.status === "suggested" ? ";border-style:dashed" : "") +
          '"><div class="subway-header"><span class="subway-id">#' +
          n.id + "</span>" + badgeHtml(ds, ideaProgress[n.id]) +
          '<span class="subway-desc">' +
          escapeHtml(ideaTitle(n.description)) +
          "</span></div></div>";
      }
      mHtml += "</div>";
      container.innerHTML = mHtml;
      container.querySelectorAll(".subway-station").forEach((el) => {
        const id = parseInt((el as HTMLElement).dataset.id!);
        const h = (el as HTMLElement).offsetHeight || STATION_HEIGHT;
        heights[id] = h;
        const node = data.nodes.find((nn) => nn.id === id);
        if (node) _stationSizeCache[id + ":" + ideaTitle(node.description)] = {
          w: 0, h  // w unused, kept for cache shape
        };
      });
    }

    // Ensure every node has a height
    for (const n of data.nodes) {
      if (heights[n.id] === undefined) heights[n.id] = STATION_HEIGHT;
    }

    // --- Compute column gap from max fan-out ---
    const ROUTING_SPACING = 10;
    const branchCounts: Record<number, number> = {};
    for (const n of data.nodes) {
      for (const pid of n.parent_ids || []) {
        if (layout.ideaLane[n.id] !== layout.ideaLane[pid])
          branchCounts[pid] = (branchCounts[pid] || 0) + 1;
      }
    }
    const maxFanOut = Math.max(0, ...Object.values(branchCounts));
    const colGap = Math.max(30, maxFanOut * ROUTING_SPACING + 20);

    // --- Per-column fan-out: how many branch lines originate from each column ---
    // These lines extend into the GAP after the column, not into the column itself.
    const colBranchOut: Record<number, number> = {};
    for (const n of data.nodes) {
      for (const pid of n.parent_ids || []) {
        if (layout.ideaLane[n.id] === layout.ideaLane[pid]) continue;
        const pc = layout.depth[pid];
        colBranchOut[pc] = (colBranchOut[pc] || 0) + 1;
      }
    }

    // --- Column widths & x-positions ---
    // A column is compact only when ALL its nodes are compact.
    // Any important node forces the column to full width.
    const overrides = colWidthOverrides.value;
    const colWidth: Record<number, number> = {};
    for (let c = 0; c <= layout.maxDepth; c++) {
      const nodesInCol = data.nodes.filter((n) => layout.depth[n.id] === c);
      const hasImportant = effectiveCompactMode
        ? nodesInCol.some((n) => isImportant[n.id])
        : true;
      if (!hasImportant) {
        colWidth[c] = COMPACT_W;
      } else if (overrides[c] !== undefined) {
        colWidth[c] = Math.max(MIN_COL_WIDTH, overrides[c]);
      } else {
        colWidth[c] = DEFAULT_MAX_COL_WIDTH;
      }
    }
    // Pre-compute the gap between each pair of adjacent columns (d, d+1).
    // The gap must fit branch lines from the PARENT column. In the DAG,
    // parents are at lower depth. Lines exit the parent's outgoing edge
    // into the gap toward the child.
    //   Normal:   parent(d).right → gap → child(d+1).left   — gap is AFTER col d
    //   Reversed: parent(d).left  → gap → child(d+1).right  — gap is BEFORE col d
    const COMPACT_GAP = 14;
    const gapAfterDepth: Record<number, number> = {};
    for (let d = 0; d < layout.maxDepth; d++) {
      const fanOut = colBranchOut[d] || 0;
      const minGap = Math.max(COMPACT_GAP, fanOut * ROUTING_SPACING + 10);
      // Use tighter gap if both sides are compact
      const leftCompact = colWidth[d] <= COMPACT_W;
      const rightCompact = colWidth[d + 1] <= COMPACT_W;
      gapAfterDepth[d] = (leftCompact && rightCompact) ? minGap : Math.max(colGap, minGap);
    }

    const colX: Record<number, number> = {};
    let cx = 16;
    if (reversed) {
      // Layout left→right: maxDepth, gap, maxDepth-1, gap, ..., 0
      for (let c = layout.maxDepth; c >= 0; c--) {
        colX[c] = cx;
        cx += colWidth[c] || 0;
        if (c > 0) cx += gapAfterDepth[c - 1] || COMPACT_GAP;
      }
    } else {
      // Layout left→right: 0, gap, 1, gap, ..., maxDepth
      for (let c = 0; c <= layout.maxDepth; c++) {
        colX[c] = cx;
        cx += colWidth[c] || 0;
        if (c < layout.maxDepth) cx += gapAfterDepth[c] || COMPACT_GAP;
      }
    }
    const totalW = cx + 16;

    // --- Per-column y-packing with pass-through reservations ---
    const ROW_GAP = 3;
    const PAD_Y = 12;
    const PASS_H = 14;

    // Group stations by column
    const colNodes: Record<number, IdeaNode[]> = {};
    for (let idx = 0; idx < data.nodes.length; idx++) {
      const n = data.nodes[idx];
      const col = layout.depth[n.id];
      if (!colNodes[col]) colNodes[col] = [];
      colNodes[col].push(n);
    }

    // Compute per-lane SUBTREE activity (includes all descendant lanes via lane tree).
    const laneDirectAct: Record<number, string> = {};
    for (let idx = 0; idx < data.nodes.length; idx++) {
      const n = data.nodes[idx];
      const li = layout.ideaLane[n.id];
      const act = n.last_finish || n.first_start || n.created_at || "";
      if (!laneDirectAct[li] || act > laneDirectAct[li]) laneDirectAct[li] = act;
    }
    const laneFullAct: Record<number, string> = {};
    function computeLaneFullAct(li: number): string {
      if (laneFullAct[li] !== undefined) return laneFullAct[li];
      let best = laneDirectAct[li] || "";
      const ch = layout.laneChildren[li];
      if (ch) {
        for (let j = 0; j < ch.length; j++) {
          const ca = computeLaneFullAct(ch[j].childLane);
          if (ca > best) best = ca;
        }
      }
      laneFullAct[li] = best;
      return best;
    }
    for (let li = 0; li < layout.lanes.length; li++) computeLaneFullAct(li);

    // Compute per-lane "worst" status (for sorting: concluded=0, active=1, abandoned=2)
    const laneStatus: Record<number, number> = {};
    for (let idx = 0; idx < data.nodes.length; idx++) {
      const n = data.nodes[idx];
      const li = layout.ideaLane[n.id];
      const rank = STATUS_ORDER[n.status] !== undefined ? STATUS_ORDER[n.status] : 1;
      if (laneStatus[li] === undefined || rank > laneStatus[li]) laneStatus[li] = rank;
    }

    // Build explicit pass-through set by scanning ALL edges.
    const needsPass: Record<string, number> = {};
    // a) Same-lane backbone gaps
    for (let li = 0; li < layout.lanes.length; li++) {
      const laneIdeas = layout.lanes[li].ideas;
      if (laneIdeas.length < 2) continue;
      const cols: number[] = [];
      for (let j = 0; j < laneIdeas.length; j++) cols.push(layout.depth[laneIdeas[j]]);
      cols.sort((a, b) => a - b);
      for (let j = 0; j < cols.length - 1; j++) {
        for (let cc = cols[j] + 1; cc < cols[j + 1]; cc++) needsPass[li + "," + cc] = li;
      }
    }
    // b) Cross-lane edges that skip columns
    for (let idx = 0; idx < data.edges.length; idx++) {
      const e = data.edges[idx];
      const fl = layout.ideaLane[e.from];
      const tl = layout.ideaLane[e.to];
      if (fl === undefined || tl === undefined) continue;
      if (fl === tl) continue;
      const fc = layout.depth[e.from];
      const tc = layout.depth[e.to];
      if (fc === undefined || tc === undefined) continue;
      for (let cc = fc + 1; cc < tc; cc++) {
        needsPass[tl + "," + cc] = e.to;
      }
    }

    // Build slots per column: just stations, sorted by status then activity
    interface Slot {
      type: string;
      nodeId: number;
      lane: number;
      h: number;
      statusRank: number;
      act: string;
    }
    const colSlots: Record<number, Slot[]> = {};
    for (let c = 0; c <= layout.maxDepth; c++) {
      const slots: Slot[] = [];
      const nodes = colNodes[c] || [];
      for (let j = 0; j < nodes.length; j++) {
        const n = nodes[j];
        const li = layout.ideaLane[n.id];
        const sr = STATUS_ORDER[n.status] !== undefined ? STATUS_ORDER[n.status] : 1;
        slots.push({
          type: "station",
          nodeId: n.id,
          lane: li,
          h: heights[n.id],
          statusRank: sr,
          act: laneFullAct[li] || "",
        });
      }
      // Sort: 1) status (concluded=0, active=1, abandoned=2)  2) subtree activity desc
      slots.sort((a, b) => {
        if (a.statusRank !== b.statusRank) return a.statusRank - b.statusRank;
        if (a.act !== b.act) return b.act > a.act ? 1 : -1;
        return 0;
      });
      colSlots[c] = slots;
    }

    // === PASS 1: stack stations without pass-throughs ===
    const stationPos: Record<number, StationPos> = {};
    let maxY = 0;
    for (let c = 0; c <= layout.maxDepth; c++) {
      const slots = colSlots[c] || [];
      let y = PAD_Y;
      for (let i = 0; i < slots.length; i++) {
        const slot = slots[i];
        let minY = y;
        // Children never above parent
        const pids = (layout.nodeMap[slot.nodeId]?.parent_ids || []);
        for (let j = 0; j < pids.length; j++) {
          const pp = stationPos[pids[j]];
          if (pp) minY = Math.max(minY, pp.y);
        }
        y = minY;
        const isCompact = effectiveCompactMode && !isImportant[slot.nodeId];
        const nodeW = isCompact ? COMPACT_W : colWidth[c];
        stationPos[slot.nodeId] = { x: colX[c], y: y, w: nodeW, h: slot.h };
        y += slot.h + ROW_GAP;
      }
      if (y > maxY) maxY = y;
    }

    // === PASS 2: push stations down where horizontal line segments cross them ===
    const crossings: Record<number, number[]> = {};
    // a) Same-lane backbone: A(colA) -> B(colB) routes horizontally at B.cy
    for (let li = 0; li < layout.lanes.length; li++) {
      const laneIdeas = layout.lanes[li].ideas;
      if (laneIdeas.length < 2) continue;
      const sorted = laneIdeas.slice().sort((a, b) => layout.depth[a] - layout.depth[b]);
      for (let j = 0; j < sorted.length - 1; j++) {
        const aCol = layout.depth[sorted[j]],
          bCol = layout.depth[sorted[j + 1]];
        if (bCol <= aCol + 1) continue;
        const bP = stationPos[sorted[j + 1]];
        if (!bP) continue;
        const bCy = bP.y + bP.h / 2;
        for (let cc = aCol + 1; cc < bCol; cc++) {
          if (!crossings[cc]) crossings[cc] = [];
          crossings[cc].push(bCy);
        }
      }
    }
    // b) Cross-lane branch: routes horizontally at child.cy
    for (let idx = 0; idx < data.edges.length; idx++) {
      const e = data.edges[idx];
      const fl = layout.ideaLane[e.from],
        tl = layout.ideaLane[e.to];
      if (fl === undefined || tl === undefined || fl === tl) continue;
      const fc = layout.depth[e.from],
        tc = layout.depth[e.to];
      if (fc === undefined || tc === undefined || tc <= fc + 1) continue;
      const cP = stationPos[e.to];
      if (!cP) continue;
      const cCy = cP.y + cP.h / 2;
      for (let cc = fc + 1; cc < tc; cc++) {
        if (!crossings[cc]) crossings[cc] = [];
        crossings[cc].push(cCy);
      }
    }
    // For each column with crossings, check overlap with station boxes and push down
    for (let c = 0; c <= layout.maxDepth; c++) {
      const crs = crossings[c];
      if (!crs || !crs.length) continue;
      crs.sort((a, b) => a - b);
      // Deduplicate nearby crossings
      const unique = [crs[0]];
      for (let j = 1; j < crs.length; j++) {
        if (crs[j] - unique[unique.length - 1] > 4) unique.push(crs[j]);
      }
      const slots = colSlots[c] || [];
      for (let ci = 0; ci < unique.length; ci++) {
        const lineY = unique[ci];
        const halfH = PASS_H / 2;
        // Find any station that overlaps lineY +/- halfH
        for (let j = 0; j < slots.length; j++) {
          const sp = stationPos[slots[j].nodeId];
          if (!sp) continue;
          if (lineY + halfH > sp.y && lineY - halfH < sp.y + sp.h) {
            // Push this station and all below it down
            const pushTo = lineY + halfH + 2;
            const shift = pushTo - sp.y;
            if (shift > 0) {
              for (let k = j; k < slots.length; k++) {
                const sp2 = stationPos[slots[k].nodeId];
                if (!sp2) continue;
                sp2.y += shift;
              }
            }
            break; // only push once per crossing
          }
        }
      }
    }

    // Recompute maxY after adjustments
    maxY = 0;
    for (const nid in stationPos) {
      const sp = stationPos[nid as unknown as number];
      const bot = sp.y + sp.h + ROW_GAP;
      if (bot > maxY) maxY = bot;
    }
    const totalH = maxY + PAD_Y;

    // --- RENDER --- (continues in the same effect)

    // Helper: color for a given idea, closing over current mode/metric/state
    function colorForIdea(ideaId: number, m: string): string {
      return _colorForIdea(ideaId, m, metric, layout!, ideas, metricExperiments);
    }

    // --- Build SVG lines ---
    const svgContent = drawSubwayLines(data, layout, stationPos, mode, colorForIdea, reversed);

    // --- Build station HTML ---
    let html =
      '<svg class="subway-svg" id="subway-svg" width="' +
      totalW +
      '" height="' +
      totalH +
      '"></svg>';

    for (const n of data.nodes) {
      const p = stationPos[n.id];
      if (!p) continue;
      const ds = n.has_running ? "running" : n.status;
      const nodeColor = colorForIdea(n.id, mode);

      if (effectiveCompactMode && !isImportant[n.id]) {
        // Compact pill node — positioned at column start like full stations
        html +=
          '<div class="subway-dot" data-id="' + n.id +
          '" title="#' + n.id + ': ' + escapeHtml(n.description) +
          '" style="left:' + p.x +
          'px;top:' + p.y +
          'px;width:' + COMPACT_W +
          'px;height:' + COMPACT_H +
          'px;background:' + nodeColor +
          '"><span class="subway-dot-id">#' + n.id + '</span></div>';
      } else {
        // Full station node
        html +=
          '<div class="subway-station" data-id="' +
          n.id +
          '" title="' +
          escapeHtml(n.description) +
          '" style="' +
          "left:" + p.x + "px;top:" + p.y + "px;width:" + p.w + "px" +
          ";border-color:" + nodeColor +
          '">' +
          '<div class="subway-header"><span class="subway-id">#' +
          n.id + "</span>" + badgeHtml(ds, ideaProgress[n.id]) +
          '<span class="subway-desc">' +
          escapeHtml(ideaTitle(n.description)) +
          "</span></div></div>";
      }
    }

    // Add column resize handles
    for (let c = 0; c <= layout.maxDepth; c++) {
      const handleX = colX[c] + colWidth[c];
      html +=
        '<div class="dag-col-resize" data-col="' + c + '" style="' +
        "left:" + (handleX - 3) + "px;top:0;height:" + totalH + 'px"></div>';
    }

    container.innerHTML = html;
    container.style.minWidth = totalW + "px";
    container.style.minHeight = totalH + "px";

    // Inject SVG content
    const svgEl = container.querySelector("#subway-svg");
    if (svgEl) {
      svgEl.innerHTML = svgContent;
    }

    // --- Column resize drag handlers ---
    container.querySelectorAll(".dag-col-resize").forEach((handle) => {
      const col = parseInt((handle as HTMLElement).dataset.col!);
      let startX = 0;
      let startWidth = 0;

      function onMouseMove(e: MouseEvent) {
        const delta = e.clientX - startX;
        const newWidth = Math.max(MIN_COL_WIDTH, startWidth + delta);
        const updated = { ...colWidthOverrides.value, [col]: newWidth };
        colWidthOverrides.value = updated;
      }

      function onMouseUp() {
        document.removeEventListener("mousemove", onMouseMove);
        document.removeEventListener("mouseup", onMouseUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }

      (handle as HTMLElement).addEventListener("mousedown", (e: MouseEvent) => {
        e.preventDefault();
        startX = e.clientX;
        startWidth = colWidth[col] || DEFAULT_MAX_COL_WIDTH;
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
        document.addEventListener("mousemove", onMouseMove);
        document.addEventListener("mouseup", onMouseUp);
      });
    });

    // --- Build parent map for ancestor traversal ---
    const parentMap: Record<number, number[]> = {};
    for (let idx = 0; idx < data.nodes.length; idx++) {
      const n = data.nodes[idx];
      parentMap[n.id] = (n.parent_ids || []).filter((p) => layout.nodeMap[p]);
    }

    function getAncestors(ideaId: number): Record<number, boolean> {
      const ancestors: Record<number, boolean> = {};
      const queue = [ideaId];
      while (queue.length) {
        const cur = queue.shift()!;
        if (ancestors[cur]) continue;
        ancestors[cur] = true;
        const pids = parentMap[cur] || [];
        for (let j = 0; j < pids.length; j++) queue.push(pids[j]);
      }
      return ancestors;
    }

    function applyHighlight(ideaId: number | null) {
      const ancestors = ideaId ? getAncestors(ideaId) : null;
      // Highlight/dim stations
      container.querySelectorAll(".subway-station").forEach((el) => {
        const id = parseInt((el as HTMLElement).dataset.id!);
        if (!ancestors) {
          el.classList.remove("highlighted", "dimmed");
          return;
        }
        if (ancestors[id]) {
          el.classList.add("highlighted");
          el.classList.remove("dimmed");
        } else {
          el.classList.add("dimmed");
          el.classList.remove("highlighted");
        }
      });
      // Highlight/dim SVG edges
      const svg = container.querySelector("#subway-svg");
      if (svg) {
        svg.querySelectorAll(".svg-edge").forEach((el) => {
          const from = parseInt(el.getAttribute("data-from")!);
          const to = parseInt(el.getAttribute("data-to")!);
          if (!ancestors) {
            (el as SVGElement).style.opacity = "";
            (el as SVGElement).style.strokeWidth = "";
            return;
          }
          if (ancestors[from] && ancestors[to]) {
            (el as SVGElement).style.opacity = "1";
            (el as SVGElement).style.strokeWidth = "5";
          } else {
            (el as SVGElement).style.opacity = "0.1";
            (el as SVGElement).style.strokeWidth = "";
          }
        });
        svg.querySelectorAll(".svg-dot").forEach((el) => {
          const id = parseInt(el.getAttribute("data-idea")!);
          if (!ancestors) {
            (el as SVGElement).style.opacity = "";
            return;
          }
          (el as SVGElement).style.opacity = ancestors[id] ? "1" : "0.15";
        });
      }
    }

    // --- Attach hover and click handlers to stations AND dots ---
    container.querySelectorAll(".subway-station, .subway-dot").forEach((el) => {
      el.addEventListener("click", () => {
        const id = parseInt((el as HTMLElement).dataset.id!);
        navigateToIdea(id);
      });
      el.addEventListener("mouseenter", () => {
        const id = parseInt((el as HTMLElement).dataset.id!);
        highlightedIdea.value = id;
      });
      el.addEventListener("mouseleave", () => {
        highlightedIdea.value = null;
      });
    });

    // Apply current highlight state (in case it was set before render)
    applyHighlight(highlightedIdea.value);
  }, [data, layout, mode, metric, ideas, experiments, effectiveCompactMode, tags, tagMode, reversed, showAbandoned.value, showConcluded.value, showRunning.value, theme]);

  // =========================================================================
  // HIGHLIGHT EFFECT — reacts to highlightedIdea changes without full re-render
  // =========================================================================
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !data || !layout) return;

    const parentMap: Record<number, number[]> = {};
    for (const n of data.nodes) {
      parentMap[n.id] = (n.parent_ids || []).filter((p) => layout.nodeMap[p]);
    }

    function getAncestors(ideaId: number): Record<number, boolean> {
      const ancestors: Record<number, boolean> = {};
      const queue = [ideaId];
      while (queue.length) {
        const cur = queue.shift()!;
        if (ancestors[cur]) continue;
        ancestors[cur] = true;
        const pids = parentMap[cur] || [];
        for (let j = 0; j < pids.length; j++) queue.push(pids[j]);
      }
      return ancestors;
    }

    const ancestors = highlighted ? getAncestors(highlighted) : null;

    // Highlight/dim stations and dots
    container.querySelectorAll(".subway-station, .subway-dot").forEach((el) => {
      const id = parseInt((el as HTMLElement).dataset.id!);
      if (!ancestors) {
        el.classList.remove("highlighted", "dimmed");
        return;
      }
      if (ancestors[id]) {
        el.classList.add("highlighted");
        el.classList.remove("dimmed");
      } else {
        el.classList.add("dimmed");
        el.classList.remove("highlighted");
      }
    });

    // Highlight/dim SVG edges
    const svg = container.querySelector("#subway-svg");
    if (svg) {
      svg.querySelectorAll(".svg-edge").forEach((el) => {
        const from = parseInt(el.getAttribute("data-from")!);
        const to = parseInt(el.getAttribute("data-to")!);
        if (!ancestors) {
          (el as SVGElement).style.opacity = "";
          (el as SVGElement).style.strokeWidth = "";
          return;
        }
        if (ancestors[from] && ancestors[to]) {
          (el as SVGElement).style.opacity = "1";
          (el as SVGElement).style.strokeWidth = "5";
        } else {
          (el as SVGElement).style.opacity = "0.1";
          (el as SVGElement).style.strokeWidth = "";
        }
      });
      svg.querySelectorAll(".svg-dot").forEach((el) => {
        const id = parseInt(el.getAttribute("data-idea")!);
        if (!ancestors) {
          (el as SVGElement).style.opacity = "";
          return;
        }
        (el as SVGElement).style.opacity = ancestors[id] ? "1" : "0.15";
      });
    }
  }, [highlighted, data, layout]);

  // =========================================================================
  // JSX — the container div hosts imperatively managed DOM content.
  // =========================================================================
  return (
    <div id="graph-container">
      <div
        id="graph"
        ref={containerRef}
        style={{ position: "relative", minHeight: "100%" }}
      />
    </div>
  );
}
