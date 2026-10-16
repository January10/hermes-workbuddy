/**
 * WorkBuddy 面板 —— 桌面 App 里的一页（`/workbuddy` + 侧边栏入口 + ⌘K 命令）。
 *
 * 数据走 `ctx.rest('/snapshot')`，也就是本插件自己的 Python 后端
 * `~/.hermes/plugins/hermes-workbuddy/dashboard/plugin_api.py`（`/api/plugins/hermes-workbuddy`）——
 * 那条路由再调同一统一包注册的两个提供方 profile。全程进程内，没有第二个进程。
 *
 * 页面内容：两类账户（国内版 / 国际版）各自的
 * 账号（昵称、企业、uid、类型、令牌到期、登录文件）/ 额度 / 模型表（倍率 × 上下文 × 能力 × 推理档位）。
 *
 * 纯 ESM、不编译：只用 jsx() 调用（没有 JSX 语法），只用内联样式 + 主题变量（不硬编码颜色）。
 */

import { host, MODEL_MENU_ROW_AREA, PALETTE_AREA, ROUTES_AREA, ScrollArea, SIDEBAR_NAV_AREA, useQuery, useQueryClient } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'hermes-workbuddy'
const PAGE_PATH = '/workbuddy'
const QUERY_KEY = [PLUGIN_ID, 'snapshot']

/** 绑定自 ctx.rest（register 时注入）。 */
let restRequest = null

// ── 模型选择器里的倍率徽标 ───────────────────────────────────────────────────
// 模型菜单（输入框那个模型胶囊的下拉）每行都会同步问一次 decorate()，所以这里只读内存缓存：
// provider -> Map(model -> row)，由后台定时（+ 刷新按钮）从 /snapshot 更新。

const RATE_CACHE = new Map()

function rateBadgeText(row) {
  if (!row) return null
  // 上游状态优先（限免 / 免费 / 试用 / 免费路由），没有就退回基础倍率。
  if (typeof row.status === 'string' && row.status.trim()) return row.status.trim()
  const value = row.effective_multiplier !== undefined ? row.effective_multiplier : row.multiplier
  if (value === 0) return '免费'
  if (value === null || value === undefined) {
    return row.credits ? String(row.credits) : null
  }
  return `×${value}`
}

async function refreshRateCache() {
  if (!restRequest) return
  try {
    const data = await restRequest('/snapshot', { timeoutMs: 30_000 })
    const next = new Map()
    for (const variant of (data && data.variants) || []) {
      const models = new Map()
      for (const row of variant.models || []) {
        models.set(row.id, row)
      }
      if (models.size) next.set(variant.provider, models)
    }
    if (next.size) {
      RATE_CACHE.clear()
      for (const [provider, models] of next) RATE_CACHE.set(provider, models)
    }
  } catch {
    // 保留上一份缓存：拿不到就只是不显示徽标，不能让菜单出错
  }
}

const MUTED = { color: 'var(--ui-text-quaternary)' }
const MUTED_SMALL = { color: 'var(--ui-text-quaternary)', fontSize: '11px' }
const LABEL = { color: 'var(--ui-text-secondary)', fontSize: '12px' }
const MONO = { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }
const CARD = {
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: '10px',
  padding: '14px',
  display: 'flex',
  flexDirection: 'column',
  gap: '10px',
  background: 'transparent'
}
const GRID = {
  display: 'grid',
  gridTemplateColumns: 'minmax(140px, 1.4fr) 76px 62px 66px minmax(100px, 1fr) minmax(100px, 1fr) 78px',
  gap: '6px 10px',
  alignItems: 'baseline'
}
const HEADER_CELL = { ...LABEL, fontWeight: 600 }

function fmtRate(row) {
  const value = row && (row.effective_multiplier !== undefined ? row.effective_multiplier : row.multiplier)
  if (value === null || value === undefined) return '—'
  if (value === 0) return '免费'
  return `×${value}`
}

/**
 * 总分那一行的分母：上游 `TotalDosage`（各包剩余之和）。
 * 上游偶尔比本地逐包求和多几分（口径可能是"可用分"含进行中的小数），差一点就直接用上游的数。
 */
function creditTotal(credits) {
  const total = Number(credits.total_remain)
  if (!Number.isFinite(total)) return '—'
  return total
}

/** 状态列的短标签：上游活动/试用优先，其次基础倍率。 */
function rowStatus(row) {
  if (row.status) return row.status
  return fmtRate(row)
}

/** 实际会发出的思考档位：没声明档位的模型不发参数（用官方默认）。 */
function fmtActiveEffort(row) {
  const declared = row.reasoning_efforts || []
  if (declared.length) return declared[declared.length - 1]
  return row.supports_reasoning ? '官方默认' : '不支持'
}

/** 目录声明的档位（含当前会选中的那一档）。 */
function fmtEfforts(row) {
  const declared = row.reasoning_efforts || []
  if (!declared.length) return '—'
  const active = declared[declared.length - 1]
  return declared.map((level) => (level === active ? `[${level}]` : level)).join('/')
}

function statusTone(row) {
  if (row.status === '限免' || row.status === '免费' || row.free === true) {
    return { color: 'var(--ui-accent)', fontWeight: 600 }
  }
  if (row.status === '试用') return { color: 'var(--ui-text-secondary)', fontWeight: 600 }
  return undefined
}

function PromotionsLegend({ variant }) {
  const promotions = (variant.promotions || []).filter((promo) => promo.active)
  const usage = variant.usage
  const notes = []
  ;(variant.models || []).forEach((row) => {
    if (row.status_detail && row.status_detail.indexOf('试用') >= 0) {
      notes.push(`${row.id}：${row.status_detail}`)
    }
  })
  if (!promotions.length && !notes.length) return null
  const rows = []
  promotions.forEach((promo, index) => {
    const label = promo.label_zh || promo.label || '限时活动'
    const factor = promo.factor === 0 || promo.factor === 0.0 ? '免费' : promo.factor !== null && promo.factor !== undefined ? `×${promo.factor}` : ''
    const window = promo.valid_until ? `至 ${String(promo.valid_until).slice(0, 10)}` : ''
    rows.push(jsxs('div', {
      key: `promo-${index}`,
      style: { ...MUTED_SMALL, display: 'flex', gap: '6px', flexWrap: 'wrap' },
      children: [
        jsx('span', {
          style: {
            color: promo.color && /^#/.test(promo.color) ? promo.color : 'var(--ui-accent)',
            fontWeight: 600
          },
          children: `${label}${factor ? ' ' + factor : ''}`
        }),
        jsx('span', { children: `${(promo.models || []).join('、')}${window ? ' · ' + window : ''}` }),
        promo.text ? jsx('span', { children: `· ${promo.text}` }) : null
      ]
    }))
  })
  notes.forEach((note, index) => {
    rows.push(jsx('div', { key: `note-${index}`, style: MUTED_SMALL, children: note }))
  })
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: '3px', padding: '6px 8px', border: '1px solid var(--ui-stroke-secondary)', borderRadius: '8px' },
    children: [
      jsx('div', { style: { ...LABEL, fontWeight: 600 }, children: '活动状态（上游）' }),
      ...rows
    ]
  })
}

function fmtContext(tokens) {
  if (!tokens) return '—'
  if (tokens >= 1000000) return `${Math.round(tokens / 100000) / 10}M`
  if (tokens >= 1000) return `${Math.round(tokens / 1000)}k`
  return String(tokens)
}

function fmtFlags(row) {
  const flags = []
  if (row.supports_tools) flags.push('工具')
  if (row.supports_vision) flags.push('图像')
  if (row.supports_reasoning) flags.push('推理')
  return flags.length ? flags.join('+') : '—'
}

function AccountLine({ variant }) {
  const account = variant.account || {}
  if (!account.signed_in) {
    return jsx('div', { style: MUTED, children: '未登录（打开对应桌面客户端登录一次）' })
  }
  const who = account.nickname || account.display_name || '—'
  const facts = []
  if (account.enterprise_name) facts.push(account.enterprise_name)
  if (account.enterprise_id) facts.push(`企业 id ${account.enterprise_id}`)
  if (account.account_type) facts.push(account.account_type)
  if (account.domain) facts.push(account.domain)
  if (account.uid) facts.push(`uid ${account.uid}`)
  if (account.expires_at) facts.push(`令牌到期 ${account.expires_at}`)
  return jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: '2px' }, children: [
    jsx('div', { style: { fontSize: '14px', fontWeight: 600 }, children: who }),
    jsx('div', { style: LABEL, children: facts.join(' · ') }),
    account.auth_file
      ? jsx('div', { style: { ...MUTED_SMALL, ...MONO, overflowWrap: 'anywhere' }, children: account.auth_file })
      : null
  ] })
}

function UsageLines({ variant }) {
  // 规则（用户定的，两轮收敛后）：**只显示两个数字** —— 总分 + 3 天内到期的可用分数。
  // 不列包名、不列包明细、不列"共有几个包"。后端已不再下发 soon 明细，这里也只算数字。
  const credits = variant.credits
  if (!credits || (!credits.unlimited && !credits.total_remain && !credits.total_size)) {
    // 没有额度数据：显示后端给的原因（例如上游账单接口要企业上下文），别只说"取不到"。
    const usage = variant.usage
    const note = (usage && (usage.details || []).find((line) => line.indexOf('账单') >= 0)) || '上游没给额度数据'
    return jsx('div', { style: MUTED, children: `额度：${note}` })
  }
  const days = credits.soon_days || 3
  const rows = []
  if (credits.unlimited) {
    rows.push(jsx('div', {
      key: 'total',
      style: { fontSize: '13px', fontWeight: 600 },
      children: '额度：不限量（企业版）' + (credits.cycle_end ? ` · 本周期至 ${credits.cycle_end}` : '')
    }))
  } else {
    rows.push(jsx('div', {
      key: 'total',
      style: { fontSize: '13px', fontWeight: 600 },
      children: `总分：${creditTotal(credits)} 分`
    }))
    rows.push(jsx('div', {
      key: 'soon',
      style: { fontSize: '13px', fontWeight: 600 },
      children: `${days} 天内到期：${Number(credits.soon_remain || 0)} 分`
    }))
  }
  return jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: '3px' }, children: rows })
}

function ModelTable({ variant }) {
  const models = variant.models || []
  if (!models.length) {
    return jsx('div', { style: MUTED, children: '没有模型（目录拉取失败或该账号未登录）' })
  }
  const rows = [
    jsxs('div', { key: 'head', style: GRID, children: [
      jsx('div', { style: HEADER_CELL, children: '模型' }),
      jsx('div', { style: HEADER_CELL, children: '状态' }),
      jsx('div', { style: HEADER_CELL, children: '倍率' }),
      jsx('div', { style: HEADER_CELL, children: '上下文' }),
      jsx('div', { style: HEADER_CELL, children: '能力' }),
      jsx('div', { style: HEADER_CELL, children: '推理档位' }),
      jsx('div', { style: HEADER_CELL, children: '当前强度' })
    ] })
  ]
  models.forEach((row) => {
    rows.push(jsxs('div', { key: row.id, style: GRID, children: [
      jsx('div', { style: { ...MONO, overflowWrap: 'anywhere' }, children: row.id }),
      jsx('div', { title: row.status_detail || '', style: statusTone(row), children: rowStatus(row) }),
      jsx('div', {
        style: row.multiplier === 0 ? { color: 'var(--ui-accent)', fontWeight: 600 } : undefined,
        children: fmtRate(row)
      }),
      jsx('div', { style: LABEL, children: fmtContext(row.context_window) }),
      jsx('div', { style: LABEL, children: fmtFlags(row) }),
      jsx('div', { style: MUTED_SMALL, children: fmtEfforts(row) }),
      jsx('div', {
        style: row.reasoning_efforts && row.reasoning_efforts.length
          ? { color: 'var(--ui-text-secondary)', fontWeight: 600 }
          : MUTED_SMALL,
        children: fmtActiveEffort(row)
      })
    ] }))
  })
  return jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: '4px' }, children: rows })
}

function VariantCard({ variant }) {
  return jsxs('section', { style: CARD, children: [
    jsxs('header', { style: { display: 'flex', alignItems: 'baseline', gap: '8px' }, children: [
      jsx('div', { style: { fontSize: '15px', fontWeight: 600 }, children: variant.label || variant.provider }),
      jsx('div', { style: MUTED_SMALL, children: variant.provider }),
      variant.live === false ? jsx('div', { style: { ...MUTED_SMALL, color: 'var(--ui-accent)' }, children: '（回退清单）' }) : null
    ] }),
    jsx(AccountLine, { variant }),
    jsx('div', { style: { height: '1px', background: 'var(--ui-stroke-secondary)' } }),
    jsx(UsageLines, { variant }),
    jsx(PromotionsLegend, { variant }),
    jsx('div', { style: { height: '1px', background: 'var(--ui-stroke-secondary)' } }),
    jsx(ModelTable, { variant })
  ] })
}

function WorkBuddyPage() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => restRequest('/snapshot', { timeoutMs: 30_000 }),
    refetchInterval: 60_000
  })

  const refresh = () => {
    refreshRateCache()
    restRequest('/snapshot?force=1', { timeoutMs: 30_000 })
      .catch(() => null)
      .then(() => queryClient.invalidateQueries({ queryKey: QUERY_KEY }))
  }

  const data = query.data
  const variants = (data && data.variants) || []
  const stamp = data && data.fetched_at ? new Date(data.fetched_at * 1000).toLocaleTimeString() : ''

  const body = query.isPending
    ? jsx('div', { style: MUTED, children: '读取 WorkBuddy 账号与模型…' })
    : query.error
      ? jsx('div', { style: { color: 'var(--ui-accent)' }, children: `读取失败：${String(query.error.message || query.error)}` })
      : variants.length
        ? jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: '14px' }, children: variants.map((variant) => jsx(VariantCard, { key: variant.provider, variant })) })
        : jsx('div', { style: MUTED, children: '没有数据。' })

  // 整页套 ScrollArea：页面内容比面板高，没有它就只能看到表头（旧版的坑）。
  return jsx(ScrollArea, {
    style: { height: '100%' },
    children: jsxs('div', { style: { padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: '14px', fontSize: '13px' }, children: [
    jsxs('header', { style: { display: 'flex', alignItems: 'center', gap: '10px' }, children: [
      jsx('div', { style: { fontSize: '16px', fontWeight: 600 }, children: 'WorkBuddy' }),
      jsx('div', { style: MUTED_SMALL, children: stamp ? `更新于 ${stamp}${query.isFetching ? ' · 刷新中' : ''}` : '' }),
      jsxs('button', {
        type: 'button',
        onClick: refresh,
        style: {
          marginLeft: 'auto',
          padding: '4px 10px',
          fontSize: '12px',
          borderRadius: '6px',
          border: '1px solid var(--ui-stroke-secondary)',
          background: 'transparent',
          color: 'var(--ui-text-secondary)',
          cursor: 'pointer'
        },
        children: ['刷新']
      })
    ] }),
    jsx('div', { style: MUTED_SMALL, children: '状态 = 上游给的活动/试用标记（限免 = 活动期内免费；免费 = 基础倍率 ×0；试用 = 有免费试用期）；倍率 = 每次调用消耗积分的倍数，活动价优先；推理档位里的 [方括号] 那档，就是「当前强度」列实际会发出去的档位（你设过 /reasoning 或 config 就按你的来；显示"官方默认"= 该模型没声明档位，不发参数）；额度只列总计与 3 天内到期的包；60 秒自动刷新一次。' }),
    body
    ] })
  })
}

export default {
  id: PLUGIN_ID,
  name: 'WorkBuddy',
  register(ctx) {
    restRequest = ctx.rest
    ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: PAGE_PATH }, render: () => jsx(WorkBuddyPage, {}) })
    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      order: 60,
      data: { path: PAGE_PATH, label: 'WorkBuddy', codicon: 'account' }
    })
    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: `${PLUGIN_ID}.open`,
        label: '打开 WorkBuddy 面板',
        keywords: ['workbuddy', 'credits', '积分', '倍率', '模型'],
        run: () => host.navigate(PAGE_PATH)
      }
    })
    // 模型选择器（输入框的模型胶囊下拉）里给每个 WorkBuddy 模型加一个倍率徽标。
    // core 保留整行，只让插件填"行尾一个 chip"，所以这里必须是同步的纯查表。
    ctx.register({
      id: 'row-badge',
      area: MODEL_MENU_ROW_AREA,
      data: {
        decorate({ provider, model }) {
          const models = RATE_CACHE.get(provider)
          if (!models) return null
          const text = rateBadgeText(models.get(model))
          return text ? { badge: text } : null
        }
      }
    })
    refreshRateCache()
    const timer = setInterval(refreshRateCache, 5 * 60_000)
    if (typeof ctx.onDispose === 'function') {
      ctx.onDispose(() => clearInterval(timer))
    }
  }
}
