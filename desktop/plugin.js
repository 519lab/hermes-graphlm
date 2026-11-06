// graphlm — live codebase map page for Hermes Desktop.
//
// One plain ES module (no build step, no bundled libraries): the layout is a
// small force simulation below, the drawing is React SVG. Data comes from the
// plugin's read-only API (dashboard/plugin_api.py). File activity arrives three
// ways, fastest first: the native `tool.start` event (the agent is about to read
// or edit a file), the plugin's own `activity` broadcast, and a poll of
// /activity — so the page stays live even where events cannot reach it.

import { ROUTES_AREA, SIDEBAR_NAV_AREA, host, useQuery, useValue, queryClient } from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'graphlm'
const PATH = '/graphlm'
const READ_TOOLS = new Set(['read_file', 'search_files'])
const WRITE_TOOLS = new Set(['write_file', 'patch'])
const PULSE_MS = 2600
const TRAIL = 14
const C = {
  text: 'var(--ui-text-primary, currentColor)',
  dim: 'var(--ui-text-tertiary)',
  faint: 'var(--ui-text-quaternary)',
  stroke: 'var(--ui-stroke-secondary)',
  read: 'var(--ui-accent)',
  write: 'var(--ui-warning, #f5a524)',
  cycle: 'var(--ui-danger, #e5484d)'
}

// ---------------------------------------------------------------- paths ----
function relPath(path, root, cwd) {
  if (!path || !root) return null
  let p = String(path).trim()
  if (!p.startsWith('/')) p = (cwd || root).replace(/\/+$/, '') + '/' + p
  const parts = []
  for (const seg of p.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') parts.pop()
    else parts.push(seg)
  }
  const abs = '/' + parts.join('/')
  const base = root.replace(/\/+$/, '') + '/'
  return abs.startsWith(base) ? abs.slice(base.length) : null
}

function pathsFromTool(name, args) {
  if (!args || typeof args !== 'object') return []
  const kind = WRITE_TOOLS.has(name) ? 'write' : READ_TOOLS.has(name) ? 'read' : null
  if (!kind) return []
  if (typeof args.path === 'string' && args.path.trim() && name !== 'search_files') {
    return [{ path: args.path, kind }]
  }
  if (name === 'patch' && typeof args.patch === 'string') {
    const out = []
    const re = /^\*\*\* (?:(?:Update|Add|Delete) File: (.+?)|Move File: (.+?) -> (.+?))\s*$/gm
    let m
    while ((m = re.exec(args.patch))) for (const p of [m[1], m[2], m[3]]) if (p) out.push({ path: p, kind })
    return out
  }
  return []
}

// ---------------------------------------------------------------- layout ---
// Deterministic force layout: pairwise repulsion, link springs, gravity.
// Positions persist across map versions so an edit nudges the picture
// instead of reshuffling it. ~400 nodes settle in a few hundred ms, run in
// small chunks so the graph visibly assembles.
function seedPositions(nodes, links, prev) {
  const pos = new Map()
  const nbr = new Map()
  for (const l of links) {
    if (!nbr.has(l.source)) nbr.set(l.source, [])
    if (!nbr.has(l.target)) nbr.set(l.target, [])
    nbr.get(l.source).push(l.target)
    nbr.get(l.target).push(l.source)
  }
  const R = 14 * Math.sqrt(nodes.length + 1)
  nodes.forEach((n, i) => {
    const old = prev.get(n.id)
    if (old) return pos.set(n.id, { x: old.x, y: old.y, vx: 0, vy: 0 })
    const anchor = (nbr.get(n.id) || []).map((id) => prev.get(id)).find(Boolean)
    const a = i * 2.39996323
    const r = anchor ? 18 : R * Math.sqrt((i + 0.5) / nodes.length)
    const cx = anchor ? anchor.x : 0
    const cy = anchor ? anchor.y : 0
    pos.set(n.id, { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a), vx: 0, vy: 0 })
  })
  return pos
}

function step(nodes, links, pos, alpha) {
  const ps = nodes.map((n) => pos.get(n.id))
  for (let i = 0; i < ps.length; i++) {
    const a = ps[i]
    for (let j = i + 1; j < ps.length; j++) {
      const b = ps[j]
      let dx = a.x - b.x
      let dy = a.y - b.y
      let d2 = dx * dx + dy * dy
      if (d2 < 0.01) { dx = 0.1 * (i - j); dy = 0.1; d2 = 0.02 }
      if (d2 > 90000) continue
      const f = Math.min(900 / d2, 6) * alpha
      a.vx += dx * f * 0.05; a.vy += dy * f * 0.05
      b.vx -= dx * f * 0.05; b.vy -= dy * f * 0.05
    }
  }
  for (const l of links) {
    const a = pos.get(l.source)
    const b = pos.get(l.target)
    if (!a || !b) continue
    const dx = b.x - a.x
    const dy = b.y - a.y
    const d = Math.sqrt(dx * dx + dy * dy) || 1
    const f = ((d - 46) / d) * 0.04 * alpha
    a.vx += dx * f; a.vy += dy * f
    b.vx -= dx * f; b.vy -= dy * f
  }
  for (const p of ps) {
    p.vx -= p.x * 0.004 * alpha; p.vy -= p.y * 0.004 * alpha
    p.vx *= 0.62; p.vy *= 0.62
    p.x += p.vx; p.y += p.vy
  }
}

function useLayout(ctx, view) {
  const prev = useRef(new Map())
  const [frame, setFrame] = useState(0)
  const [pos, setPos] = useState(() => new Map())
  useEffect(() => {
    if (!view) return undefined
    const nodes = view.nodes
    const links = view.links.filter((l) => l.type !== 'data_flow')
    const p = seedPositions(nodes, links, prev.current)
    const fresh = nodes.filter((n) => !prev.current.has(n.id)).length
    const total = fresh > nodes.length / 3 ? 260 : 80
    let done = 0
    let cancelled = false
    setPos(p)
    const tick = () => {
      if (cancelled) return
      const n = Math.min(12, total - done)
      for (let k = 0; k < n; k++) step(nodes, links, p, 1 - (done + k) / total)
      done += n
      prev.current = p
      setFrame((f) => f + 1)
      if (done < total) ctx.setTimeout(tick, 16)
    }
    tick()
    return () => { cancelled = true }
  }, [view])
  return { pos, frame }
}

// ---------------------------------------------------------------- data -----
function pickMap(maps, cwd) {
  if (!maps || !maps.length) return null
  const hit = maps
    .filter((m) => cwd && (cwd === m.root || cwd.startsWith(m.root.replace(/\/+$/, '') + '/')))
    .sort((a, b) => b.root.length - a.root.length)[0]
  return hit || maps[0]
}

function graphFacts(view) {
  const fanIn = new Map()
  const out = new Map()
  const inc = new Map()
  for (const l of view.links) {
    if (l.type === 'data_flow') continue
    fanIn.set(l.target, (fanIn.get(l.target) || 0) + 1)
    if (!out.has(l.source)) out.set(l.source, [])
    if (!inc.has(l.target)) inc.set(l.target, [])
    out.get(l.source).push(l.target)
    inc.get(l.target).push(l.source)
  }
  const hubs = [...fanIn.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10).map(([id]) => id)
  return { fanIn, out, inc, hubs: new Set(hubs) }
}

// ---------------------------------------------------------------- views ----
function Stat({ label, value, tone }) {
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', minWidth: 64 },
    children: [
      jsx('span', { style: { fontSize: 18, fontWeight: 600, color: tone || C.text }, children: value }),
      jsx('span', { style: { fontSize: 11, color: C.dim }, children: label })
    ]
  })
}

function Header({ info, live }) {
  const sha = (info.commit || '').slice(0, 8) || 'working tree'
  const summaries = info.status === 'enriching'
    ? 'generating summaries…'
    : info.enriched_at ? `${info.modules} module summaries` : 'summaries: /graphlm enrich'
  return jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 24, padding: '12px 16px',
      borderBottom: `1px solid ${C.stroke}` },
    children: [
      jsxs('div', { style: { flex: 1, minWidth: 0 }, children: [
        jsx('div', { style: { fontSize: 15, fontWeight: 600 }, children: `Code map — ${info.name}` }),
        jsx('div', { style: { fontSize: 11, color: C.dim, overflow: 'hidden', textOverflow: 'ellipsis',
          whiteSpace: 'nowrap' }, children: `${info.root} · map of ${sha} · ${summaries}` })
      ] }),
      jsx(Stat, { label: 'files', value: info.files }),
      jsx(Stat, { label: 'import edges', value: info.ast_edges }),
      jsx(Stat, { label: 'cycles', value: info.cycles, tone: info.cycles ? C.cycle : undefined }),
      jsx(Stat, { label: 'touched', value: live, tone: live ? C.read : undefined })
    ]
  })
}

function Legend() {
  const dot = (color, label, ring) => jsxs('span', {
    style: { display: 'inline-flex', alignItems: 'center', gap: 6, marginRight: 14 },
    children: [
      jsx('span', { style: { width: 10, height: 10, borderRadius: 5, display: 'inline-block',
        background: ring ? 'transparent' : color, border: `2px solid ${color}` } }),
      label
    ]
  })
  return jsxs('div', {
    style: { position: 'absolute', left: 12, bottom: 10, fontSize: 11, color: C.dim },
    children: [dot(C.read, 'reading'), dot(C.write, 'editing'), dot(C.cycle, 'import cycle', true),
      'scroll to zoom · drag to pan · click a file']
  })
}

function Panel({ node, facts, touches, onPick }) {
  if (!node) {
    return jsxs('div', { style: { padding: 14, fontSize: 12, color: C.dim }, children: [
      jsx('div', { style: { fontWeight: 600, color: C.text, marginBottom: 8 }, children: 'Agent activity' }),
      touches.length === 0 && 'Files the agent reads or edits light up here as it works.',
      ...touches.slice(-18).reverse().map((t, i) => jsxs('div', {
        onClick: () => onPick(t.path),
        style: { display: 'flex', gap: 8, padding: '3px 0', cursor: 'pointer' },
        children: [
          jsx('span', { style: { color: t.kind === 'write' ? C.write : C.read, width: 34 },
            children: t.kind === 'write' ? 'edit' : 'read' }),
          jsx('span', { style: { color: C.text, overflow: 'hidden', textOverflow: 'ellipsis',
            whiteSpace: 'nowrap' }, children: t.path })
        ]
      }, `${t.path}-${t.ts}-${i}`))
    ] })
  }
  const list = (title, ids) => ids && ids.length ? jsxs('div', { style: { marginTop: 10 }, children: [
    jsx('div', { style: { color: C.dim, marginBottom: 4 }, children: `${title} (${ids.length})` }),
    ...ids.slice(0, 14).map((id) => jsx('div', {
      onClick: () => onPick(id),
      style: { cursor: 'pointer', padding: '2px 0', overflow: 'hidden', textOverflow: 'ellipsis',
        whiteSpace: 'nowrap' },
      children: id
    }, id))
  ] }) : null
  return jsxs('div', { style: { padding: 14, fontSize: 12 }, children: [
    jsx('div', { style: { fontWeight: 600, wordBreak: 'break-all' }, children: node.id }),
    node.in_cycle && jsx('div', { style: { color: C.cycle, marginTop: 6 }, children: 'In an import cycle' }),
    node.description && jsx('div', { style: { color: C.dim, marginTop: 8, lineHeight: 1.45 },
      children: node.description }),
    list('Imported by', facts.inc.get(node.id)),
    list('Imports', facts.out.get(node.id)),
    jsx('div', { onClick: () => onPick(null),
      style: { marginTop: 14, color: C.read, cursor: 'pointer' }, children: '← activity' })
  ] })
}

function Graph({ ctx, view, facts, pulses, trail, selected, onSelect }) {
  const { pos, frame } = useLayout(ctx, view)
  const box = useRef(null)
  const [size, setSize] = useState({ w: 800, h: 600 })
  const [tf, setTf] = useState(null)
  const drag = useRef(null)
  useEffect(() => {
    const el = box.current
    if (!el) return undefined
    const ro = new ResizeObserver(([e]) => setSize({ w: e.contentRect.width, h: e.contentRect.height }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const fit = useMemo(() => {
    let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity
    for (const p of pos.values()) { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y) }
    if (!isFinite(x0)) return { x: size.w / 2, y: size.h / 2, k: 1 }
    const k = Math.min(size.w / (x1 - x0 + 80), size.h / (y1 - y0 + 80), 2.5)
    return { x: size.w / 2 - k * (x0 + x1) / 2, y: size.h / 2 - k * (y0 + y1) / 2, k }
  }, [frame, size])
  const t = tf || fit
  const now = Date.now()
  const onWheel = (e) => {
    const r = box.current.getBoundingClientRect()
    const mx = e.clientX - r.left; const my = e.clientY - r.top
    const k = Math.max(0.15, Math.min(8, t.k * (e.deltaY < 0 ? 1.12 : 0.89)))
    setTf({ k, x: mx - (mx - t.x) * (k / t.k), y: my - (my - t.y) * (k / t.k) })
  }
  const onDown = (e) => { drag.current = { x: e.clientX, y: e.clientY, t } }
  const onMove = (e) => {
    if (!drag.current) return
    const d = drag.current
    setTf({ k: d.t.k, x: d.t.x + e.clientX - d.x, y: d.t.y + e.clientY - d.y })
  }
  const onUp = () => { drag.current = null }
  const trailPts = trail.map((id) => pos.get(id)).filter(Boolean)
  const nodes = view.nodes
  return jsx('div', {
    ref: box, style: { position: 'absolute', inset: 0, cursor: drag.current ? 'grabbing' : 'grab' },
    onWheel, onMouseDown: onDown, onMouseMove: onMove, onMouseUp: onUp, onMouseLeave: onUp,
    onDoubleClick: () => setTf(null),
    children: jsxs('svg', { width: size.w, height: size.h, children: [
      jsx('style', { children: '@keyframes glmPulse{0%{r:6px;opacity:.9}100%{r:26px;opacity:0}}' +
        '.glm-pulse{animation:glmPulse 1.3s ease-out 2}' }),
      jsxs('g', { transform: `translate(${t.x},${t.y}) scale(${t.k})`, children: [
        ...view.links.filter((l) => l.type !== 'data_flow').map((l, i) => {
          const a = pos.get(l.source); const b = pos.get(l.target)
          if (!a || !b) return null
          const hot = selected && (l.source === selected || l.target === selected)
          return jsx('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y,
            stroke: hot ? C.read : C.stroke, strokeOpacity: hot ? 0.9 : 0.45,
            strokeWidth: (hot ? 1.6 : 0.7) / Math.sqrt(t.k) }, `l${i}`)
        }),
        trailPts.length > 1 && jsx('polyline', {
          points: trailPts.map((p) => `${p.x},${p.y}`).join(' '), fill: 'none', stroke: C.read,
          strokeOpacity: 0.55, strokeWidth: 2 / t.k, strokeDasharray: `${6 / t.k} ${4 / t.k}` }),
        ...nodes.map((n) => {
          const p = pos.get(n.id)
          if (!p) return null
          const r = 3 + Math.sqrt(facts.fanIn.get(n.id) || 0) * 1.7
          const pulse = pulses.get(n.id)
          const active = pulse && now - pulse.at < PULSE_MS
          const color = active ? (pulse.kind === 'write' ? C.write : C.read) : n.color
          const label = active || n.id === selected || (facts.hubs.has(n.id) && t.k > 0.5)
          return jsxs('g', {
            onClick: (e) => { e.stopPropagation(); onSelect(n.id) },
            style: { cursor: 'pointer' },
            children: [
              active && jsx('circle', { className: 'glm-pulse', cx: p.x, cy: p.y, r: 6, fill: 'none',
                stroke: color, strokeWidth: 2 }, `${n.id}-${pulse.at}`),
              jsx('circle', { cx: p.x, cy: p.y, r: active ? r + 2 : r, fill: color,
                fillOpacity: n.is_test && !active ? 0.35 : 0.9,
                stroke: n.in_cycle ? C.cycle : n.id === selected ? C.text : 'none',
                strokeWidth: (n.in_cycle ? 2 : 1.5) / Math.sqrt(t.k) }),
              label && jsx('text', { x: p.x + r + 3, y: p.y + 3, fontSize: 11 / Math.sqrt(t.k),
                fill: active ? color : C.text, style: { pointerEvents: 'none' },
                children: n.id.split('/').pop() })
            ]
          }, n.id)
        })
      ] })
    ] })
  })
}

function makePage(ctx) {
  return function CodeMapPage() {
    const cwd = useValue(host.state.cwd)
    const maps = useQuery({ queryKey: [ID, 'maps'], queryFn: () => ctx.rest('/maps'),
      refetchInterval: 4000 })
    const current = pickMap(maps.data && maps.data.maps, cwd)
    const key = current && current.key
    const graph = useQuery({ queryKey: [ID, 'graph', key, current && current.version],
      queryFn: () => ctx.rest(`/graph?key=${key}`), enabled: !!key, staleTime: Infinity })
    const activity = useQuery({ queryKey: [ID, 'activity', key],
      queryFn: () => ctx.rest(`/activity?key=${key}&limit=60`), enabled: !!key,
      refetchInterval: 2500 })
    const [pulses, setPulses] = useState(() => new Map())
    const [trail, setTrail] = useState([])
    const [touches, setTouches] = useState([])
    const [selected, setSelected] = useState(null)
    const [, setNow] = useState(0)
    const seen = useRef(0)
    const root = current && current.root

    const lastPulse = useRef(new Map())
    const touch = (path, kind, ts) => {
      const at = Date.now()
      // tool.start and the plugin's own activity event report the same touch
      // moments apart; one pulse per file per second is enough.
      if (at - (lastPulse.current.get(path) || 0) < 1000) return
      lastPulse.current.set(path, at)
      setPulses((m) => new Map(m).set(path, { kind, at }))
      setTrail((tr) => (tr[tr.length - 1] === path ? tr : [...tr, path].slice(-TRAIL)))
      setTouches((xs) => [...xs, { path, kind, ts: ts || at / 1000 }].slice(-60))
    }

    useEffect(() => {
      const offStart = ctx.onEvent('tool.start', ({ payload }) => {
        if (!payload || !root) return
        for (const t of pathsFromTool(payload.name, payload.args)) {
          const rel = relPath(t.path, root, host.state.cwd.get())
          if (rel) touch(rel, t.kind)
        }
      })
      const offAct = ctx.onEvent(`plugin.${ID}.activity`, ({ payload }) => {
        if (!payload || payload.key !== key) return
        for (const t of payload.touches || []) {
          seen.current = Math.max(seen.current, t.ts || 0)
          touch(t.path, t.kind, t.ts)
        }
      })
      const offMap = ctx.onEvent(`plugin.${ID}.map.updated`, ({ payload }) => {
        queryClient.invalidateQueries({ queryKey: [ID, 'maps'] })
        const fresh = payload && payload.impact && payload.impact.new_cycles
        if (fresh && fresh.length) {
          host.notify({ kind: 'warning', title: 'graphlm: new import cycle',
            message: fresh[0].concat(fresh[0][0]).join(' → ') })
        }
      })
      return () => { offStart(); offAct(); offMap() }
    }, [root, key])

    useEffect(() => {
      const rows = (activity.data && activity.data.touches) || []
      const fresh = rows.filter((r) => (r.ts || 0) > seen.current)
      if (!fresh.length) return
      seen.current = Math.max(...fresh.map((r) => r.ts || 0))
      const recent = Date.now() / 1000 - 20
      for (const r of fresh) if (r.ts > recent) touch(r.path, r.kind, r.ts)
      setTouches((xs) => (xs.length ? xs : rows.map((r) => ({ path: r.path, kind: r.kind, ts: r.ts }))))
    }, [activity.data])

    useEffect(() => ctx.setInterval(() => setNow((n) => n + 1), 400), [])  // returns its disposer

    const view = graph.data && graph.data.view
    const info = (graph.data && graph.data.info) || current
    const facts = useMemo(() => (view ? graphFacts(view) : null), [view])
    const nodeById = useMemo(() => new Map((view ? view.nodes : []).map((n) => [n.id, n])), [view])
    const live = [...pulses.values()].filter((p) => Date.now() - p.at < 60000).length

    if (!current) {
      return jsx('div', { style: { padding: 32, color: C.dim, fontSize: 13 },
        children: 'No codebase mapped yet. Start a chat in a project folder — graphlm maps it automatically.' })
    }
    return jsxs('div', { style: { display: 'flex', flexDirection: 'column', height: '100%' }, children: [
      jsx(Header, { info, live }),
      jsxs('div', { style: { flex: 1, display: 'flex', minHeight: 0 }, children: [
        jsxs('div', { style: { flex: 1, position: 'relative', overflow: 'hidden' }, children: [
          view && facts
            ? jsx(Graph, { ctx, view, facts, pulses, trail, selected, onSelect: setSelected })
            : jsx('div', { style: { padding: 24, color: C.dim }, children: 'Building the map…' }),
          jsx(Legend, {})
        ] }),
        jsx('div', { style: { width: 280, borderLeft: `1px solid ${C.stroke}`, overflowY: 'auto' },
          children: jsx(Panel, { node: selected && nodeById.get(selected), facts: facts || graphFacts({ links: [] }),
            touches, onPick: setSelected }) })
      ] })
    ] })
  }
}

export default {
  id: ID,
  // The Plugins page titles a unified package by its desktop half's name, so
  // keep the package name in it — users look for "graphlm" after installing.
  name: 'graphlm — Code Map',
  description: 'Live codebase map: watch the agent move through your repository.',
  defaultEnabled: true,
  register(ctx) {
    const Page = makePage(ctx)
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: 'Code Map', codicon: 'type-hierarchy' } }
    ])
  }
}
