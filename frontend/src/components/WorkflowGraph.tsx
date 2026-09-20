/** SVG renderer for a workflow step graph. */

import {
  GRAPH_COLUMN_GAP,
  GRAPH_NODE_HEIGHT,
  GRAPH_NODE_WIDTH,
  GRAPH_PAD,
  GRAPH_ROW_GAP,
} from '../constants'
import { estimateLabelWidth } from '../lib/format'
import type { WorkflowGraphNode } from '../types'
import { useId } from 'react'


export function fitSvgLabel(text: string, maxWidth: number, fallback: string): string {
  const value = text || fallback
  if (estimateLabelWidth(value) <= maxWidth) return value
  const chars = [...value]
  for (let take = chars.length - 1; take > 0; take -= 1) {
    const candidate = `${chars.slice(0, take).join('')}…`
    if (estimateLabelWidth(candidate) <= maxWidth) return candidate
  }
  return '…'
}


/**
 * Place each step in the column its depth puts it in.
 *
 * Depth is the longest path from an entry step, so a step that waits for two
 * others sits one column past whichever of them is further along -- drawing it
 * any earlier would mean drawing an edge backwards. Steps that already ran
 * (or failed) keep their place: the picture is of the graph, not of how far
 * along it got.
 */
export function workflowGraphLayout(nodes: WorkflowGraphNode[]) {
  const known = new Set(nodes.map(node => node.key.trim()))
  const depths = new Map<string, number>()
  const depthOf = (key: string, guard: Set<string>): number => {
    const cached = depths.get(key)
    if (cached !== undefined) return cached
    if (guard.has(key)) return 0
    const node = nodes.find(item => item.key.trim() === key)
    const upstreams = (node?.depends_on || [])
      .map(item => item.trim())
      .filter(item => item && known.has(item) && item !== key)
    // Written before recursing so a graph that somehow holds a cycle stops
    // here instead of overflowing the stack.
    depths.set(key, 0)
    const depth = upstreams.length
      ? 1 + Math.max(...upstreams.map(item => depthOf(item, new Set([...guard, key]))))
      : 0
    depths.set(key, depth)
    return depth
  }

  const columns: string[][] = []
  nodes.forEach(node => {
    const key = node.key.trim()
    const depth = Math.max(0, depthOf(key, new Set()))
    while (columns.length <= depth) columns.push([])
    columns[depth].push(key)
  })

  const placed = new Map<string, { x: number; y: number; node: WorkflowGraphNode }>()
  columns.forEach((keys, column) => {
    keys.forEach((key, row) => {
      const node = nodes.find(item => item.key.trim() === key)!
      placed.set(key, {
        x: GRAPH_PAD + column * (GRAPH_NODE_WIDTH + GRAPH_COLUMN_GAP),
        y: GRAPH_PAD + row * (GRAPH_NODE_HEIGHT + GRAPH_ROW_GAP),
        node,
      })
    })
  })

  const widest = Math.max(1, ...columns.map(keys => keys.length))
  return {
    placed,
    columns,
    width: GRAPH_PAD * 2 + columns.length * GRAPH_NODE_WIDTH
      + Math.max(0, columns.length - 1) * GRAPH_COLUMN_GAP,
    height: GRAPH_PAD * 2 + widest * GRAPH_NODE_HEIGHT
      + Math.max(0, widest - 1) * GRAPH_ROW_GAP,
  }
}


/**
 * The steps of a workflow as a picture, because a list of names cannot show
 * that one step waits for two others and not the other way round.
 */
export function WorkflowGraph({
  nodes,
  title,
  selectedKey,
  onSelect,
}: {
  nodes: WorkflowGraphNode[]
  title: string
  selectedKey?: string
  onSelect?: (node: WorkflowGraphNode) => void
}) {
  const arrowId = `wf-arrow-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const layout = workflowGraphLayout(nodes)
  const edges: { from: { x: number; y: number }; to: { x: number; y: number } }[] = []
  layout.placed.forEach(({ x, y, node }, key) => {
    ;(node.depends_on || []).map(item => item.trim()).forEach(upstream => {
      const source = layout.placed.get(upstream)
      if (!source || upstream === key) return
      edges.push({
        from: { x: source.x + GRAPH_NODE_WIDTH, y: source.y + GRAPH_NODE_HEIGHT / 2 },
        to: { x, y: y + GRAPH_NODE_HEIGHT / 2 },
      })
    })
  })

  return (
    <div className="workflow-graph-scroll">
      <svg
        className="workflow-graph"
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        style={{ width: `${layout.width}px`, height: `${layout.height}px` }}
        role="img"
        aria-label={title}
      >
        <defs>
          <marker
            id={arrowId}
            viewBox="0 0 8 8"
            refX="7.2"
            refY="4"
            markerWidth="6.5"
            markerHeight="6.5"
            orient="auto-start-reverse"
          >
            <path d="M0,0.6 L8,4 L0,7.4 z" style={{ fill: 'var(--text-muted)' }} />
          </marker>
        </defs>
        {edges.map((edge, index) => {
          const midX = (edge.from.x + edge.to.x) / 2
          return (
            <path
              key={`edge-${index}`}
              className="workflow-edge"
              d={`M ${edge.from.x} ${edge.from.y} C ${midX} ${edge.from.y}, ${midX} ${edge.to.y}, ${edge.to.x - 1} ${edge.to.y}`}
              markerEnd={`url(#${arrowId})`}
            />
          )
        })}
        {[...layout.placed.entries()].map(([key, { x, y, node }]) => (
          <g
            key={key}
            className={`workflow-node status-${node.status || 'pending'}${selectedKey === key ? ' active' : ''}`}
            tabIndex={0}
            role="button"
            aria-label={`步骤 ${node.name || key}`}
            onClick={() => onSelect?.(node)}
            onKeyDown={event => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                onSelect?.(node)
              }
            }}
          >
            <rect
              x={x}
              y={y}
              width={GRAPH_NODE_WIDTH}
              height={GRAPH_NODE_HEIGHT}
              rx="9"
            />
            <circle cx={x + 15} cy={y + 20} r="3.5" className="workflow-node-dot" />
            <text x={x + 26} y={y + 24} className="workflow-node-name">
              {fitSvgLabel(node.name || node.key, GRAPH_NODE_WIDTH - 40, node.key)}
            </text>
            <text x={x + 15} y={y + 42} className="workflow-node-key">
              {fitSvgLabel(
                `${node.key}${node.kind === 'message' ? ' · 提醒' : ''}`,
                GRAPH_NODE_WIDTH - 30,
                node.key,
              )}
            </text>
          </g>
        ))}
      </svg>
    </div>
  )
}
