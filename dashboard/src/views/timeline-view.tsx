import { useRef, useEffect } from "preact/hooks";
import { graphData } from "../state/signals";
import { selectedIdea, reverseTime } from "../state/settings";
import { STATUS_BAR_COLORS } from "../lib/colors";
import { truncate } from "../lib/format";

export function TimelineView() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const nodesRef = useRef<any[]>([]);
  const data = graphData.value;
  const reversed = reverseTime.value;

  useEffect(() => {
    if (!data || !canvasRef.current || !wrapRef.current) return;
    const nodes = data.nodes.filter((n) => n.first_start);
    if (!nodes.length) return;
    if (reversed) {
      nodes.sort((a, b) => (b.first_start || "").localeCompare(a.first_start || ""));
    } else {
      nodes.sort((a, b) => (a.first_start || "").localeCompare(b.first_start || ""));
    }
    nodesRef.current = nodes;

    const canvas = canvasRef.current;
    const wrapEl = wrapRef.current;
    const rowH = 36, labelW = 220, padTop = 30, padRight = 20, padBottom = 40;
    const h = padTop + nodes.length * rowH + padBottom;
    wrapEl.style.height = h + "px";
    canvas.width = wrapEl.clientWidth || 800;
    canvas.height = h;

    const ctx = canvas.getContext("2d")!;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // Alternating row backgrounds for readability
    for (let i = 0; i < nodes.length; i++) {
      ctx.fillStyle = i % 2 === 0 ? "rgba(255,255,255,0.02)" : "rgba(255,255,255,0.01)";
      ctx.fillRect(0, padTop + i * rowH, canvas.width, rowH);
    }

    const allStarts = nodes.map((n) => new Date(n.first_start).getTime());
    const allEnds = nodes.map((n) => {
      if (n.last_finish) return new Date(n.last_finish).getTime();
      if (n.has_running) return Date.now();
      return new Date(n.first_start).getTime() + 60000;
    });
    const tMin = Math.min(...allStarts) - 60000;
    const tMax = Math.max(...allEnds) + 60000;
    const chartW = canvas.width - labelW - padRight;

    function tToX(t: number) {
      const frac = (t - tMin) / (tMax - tMin);
      return labelW + (reversed ? (1 - frac) : frac) * chartW;
    }

    // Time axis — Canvas 2D API requires resolved color strings (not CSS vars)
    ctx.strokeStyle = "#30363d"; // var(--border)
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(labelW, h - padBottom);
    ctx.lineTo(canvas.width - padRight, h - padBottom);
    ctx.stroke();

    // Time ticks
    const range = tMax - tMin;
    let tickInterval: number;
    if (range < 600000) tickInterval = 60000;
    else if (range < 3600000) tickInterval = 300000;
    else if (range < 86400000) tickInterval = 3600000;
    else tickInterval = 86400000;

    const firstTick = Math.ceil(tMin / tickInterval) * tickInterval;
    ctx.fillStyle = "#8b949e"; // var(--text-muted)
    ctx.font = "10px SF Mono, Fira Code, Consolas, monospace";
    ctx.textAlign = "center";
    for (let t = firstTick; t <= tMax; t += tickInterval) {
      const x = tToX(t);
      ctx.strokeStyle = "#21262d"; // var(--bg-hi)
      ctx.beginPath();
      ctx.moveTo(x, padTop);
      ctx.lineTo(x, h - padBottom);
      ctx.stroke();
      const d = new Date(t);
      const label =
        tickInterval >= 86400000
          ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
          : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
      ctx.fillText(label, x, h - padBottom + 16);
    }

    // Dependency arrows
    for (const n of nodes) {
      for (const pid of n.parent_ids || []) {
        const fromIdx = nodes.findIndex((p) => p.id === pid);
        const toIdx = nodes.indexOf(n);
        if (fromIdx < 0) continue;
        const fromNode = nodes[fromIdx];
        const fromEnd = tToX(
          fromNode.last_finish ? new Date(fromNode.last_finish).getTime() : Date.now()
        );
        const fromY = padTop + fromIdx * rowH + rowH / 2;
        const toStart = tToX(new Date(n.first_start).getTime());
        const toY = padTop + toIdx * rowH + rowH / 2;
        ctx.save();
        ctx.strokeStyle = "#30363d"; // dimmer arrows, less visual noise
        ctx.lineWidth = 0.8;
        ctx.globalAlpha = 0.5;
        ctx.setLineDash([3, 5]);
        ctx.beginPath();
        ctx.moveTo(fromEnd, fromY);
        ctx.lineTo(toStart, toY);
        ctx.stroke();
        const angle = Math.atan2(toY - fromY, toStart - fromEnd);
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(toStart, toY);
        ctx.lineTo(toStart - 6 * Math.cos(angle - 0.4), toY - 6 * Math.sin(angle - 0.4));
        ctx.moveTo(toStart, toY);
        ctx.lineTo(toStart - 6 * Math.cos(angle + 0.4), toY - 6 * Math.sin(angle + 0.4));
        ctx.stroke();
        ctx.restore();
      }
    }

    // Bars and labels
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const y = padTop + i * rowH;
      const barH = rowH * 0.75;
      const barY = y + (rowH - barH) / 2;
      const start = new Date(n.first_start).getTime();
      const end = n.last_finish
        ? new Date(n.last_finish).getTime()
        : n.has_running
          ? Date.now()
          : start + 60000;
      const x1 = tToX(start);
      const x2 = tToX(end);
      const barW = Math.max(x2 - x1, 6);
      const status = n.has_running ? "running" : (n.has_queued ? "queued" : n.status);
      const color = STATUS_BAR_COLORS[status] || STATUS_BAR_COLORS.active;
      ctx.globalAlpha = 1;
      ctx.fillStyle = color + "cc";
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.roundRect(x1, barY, barW, barH, 3);
      ctx.fill();
      ctx.stroke();
      // Idea ID label inside bar if wide enough
      if (barW > 28) {
        ctx.fillStyle = "rgba(0,0,0,0.7)";
        ctx.font = "bold 9px SF Mono, Fira Code, monospace";
        ctx.textAlign = "left";
        ctx.fillText("#" + n.id, x1 + 4, barY + barH * 0.65);
      }
      ctx.fillStyle = "#c9d1d9";
      ctx.font = "11px system-ui, -apple-system, sans-serif";
      ctx.textAlign = "right";
      ctx.fillText("#" + n.id + ": " + truncate(n.description, 28), labelW - 8, y + rowH / 2 + 4);
    }
  }, [data, reversed]);

  function handleClick(e: MouseEvent) {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const nodes = nodesRef.current;
    const idx = Math.floor((y - 30) / 36);
    if (idx >= 0 && idx < nodes.length) {
      selectedIdea.value = nodes[idx].id;
    }
  }

  return (
    <div id="timeline-container">
      <div id="timeline-wrap" ref={wrapRef}>
        <canvas
          ref={canvasRef}
          onClick={handleClick}
          style={{ cursor: "pointer", display: "block" }}
          title="Click a row to load idea details"
        />
      </div>
    </div>
  );
}
