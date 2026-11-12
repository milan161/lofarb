<!--
  [AI-2026-10-10] 「原油穿透」三基金持仓口径页（Model B 季报持仓口径）
  - 行数据：getDashboard({ category: '黄金原油' }) 前端过滤 501018/160723/161129，约 5s 轮询
  - 持仓口径：getFundHoldingRealtime(code) 取 { hrv, base_nav }，约 20s 轮询
  - SUPREME 铁律：任何字段缺失显示 --，严禁用 0/最近值兜底
  - 行点击跳 /holding-analysis?code=&name=（与 HoldingAnalysis 页对接）
-->
<template>
  <div class="oil-page">
    <n-card :bordered="false" class="main-card" content-style="padding: 0;">
      <div class="page-header">
        <h1 class="page-title">原油穿透</h1>
        <p class="page-subtitle">
          季报持仓口径（Model B，与主看板标准估值口径不同）·
          仅 501018 / 160723 / 161129 三只原油 LOF · 点击行查看季报持仓分析
        </p>
      </div>

      <n-data-table
        :columns="columns"
        :data="rows"
        :loading="loading"
        :row-props="rowProps"
        size="small"
        bordered
      />
    </n-card>
  </div>
</template>

<script setup lang="ts">
// [AI-2026-10-10] 新建「原油穿透」页：三基金持仓口径（Model B）表格 + 路由/菜单接线
import { ref, h, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { NCard, NDataTable, NText } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { getDashboard, getFundHoldingRealtime } from '../api/fundApi'
import {
  formatPrice, formatValuation, formatPercent, formatPremium,
  priceColor, shortDate, cleanFundName
} from '../utils'

const router = useRouter()

// 固定顺序：501018 → 160723 → 161129（东哥口径）
const OIL_FUNDS = ['501018', '160723', '161129']

// 行数据：getDashboard({ category: '黄金原油' }) 后前端过滤出三个代码
// [AI-2026-10-10] 列改为与主看板完全相同前 10 列后，补充行类型字段：
//   price（收盘价）/ realtime_price（盘中实时价，优先）/ prev_close / price_change（涨跌幅）
//   / nav（T-2净值）/ nav_date（净值日期）—— 均与主看板同源同字段
type DashboardRow = {
  fund_code: string
  fund_name: string
  price?: number | null
  realtime_price?: number | null
  prev_close?: number | null
  price_change?: number | null
  nav?: number | null
  nav_date?: string | null
}

// 持仓口径（Model B）：hrv=实时估值，base_nav=T-1静态估值
type HoldingData = { hrv: number | null; base_nav: number | null }

const rows = ref<DashboardRow[]>([])
const holdingMap = ref<Record<string, HoldingData>>({})
const loading = ref(false)

// [SUPREME] 现价口径与主看板一致：盘中取 realtime_price，缺失/<=0 才退回收盘价 price；
// 两者都缺则 null（溢价显示 --，绝不兜底）
const curPrice = (row: DashboardRow): number | null => {
  const rp = row?.realtime_price
  if (rp && rp > 0) return rp
  const p = row?.price
  if (p && p > 0) return p
  return null
}

// ---- 行数据轮询（约 5s）----
let rowTimer: ReturnType<typeof setInterval> | null = null
const fetchRows = async () => {
  try {
    const res: any = await getDashboard({ category: '黄金原油' })
    const list: DashboardRow[] = res?.data?.data || []
    // 前端过滤出三只原油代码，按固定顺序 501018 → 160723 → 161129 排列
    rows.value = OIL_FUNDS
      .map((code) => list.find((r) => r.fund_code === code))
      .filter((r): r is DashboardRow => r != null)
  } catch (e) {
    console.error('获取黄金原油看板数据失败', e)
  }
}

// ---- 持仓口径轮询（约 20s）----
// 端点返回 {data:{contracts:{合约:{hrv, base_nav, ...}}}}，取首个合约（当前近月）
// 与 Dashboard.vue / HoldingAnalysis.vue 的取法对齐（读 getFundHoldingRealtime 的 data.data）
let holdingTimer: ReturnType<typeof setInterval> | null = null
const fetchHoldings = async () => {
  await Promise.all(OIL_FUNDS.map(async (code) => {
    try {
      const res: any = await getFundHoldingRealtime(code)
      const d = res?.data?.data
      if (d && d.contracts) {
        const keys = Object.keys(d.contracts)
        const c = keys.length > 0 ? d.contracts[keys[0]] : null
        // [SUPREME] 缺失就置 null（?? null），绝不用 0/最近值兜底
        holdingMap.value[code] = {
          hrv: c?.hrv ?? null,
          base_nav: c?.base_nav ?? null
        }
      }
    } catch (e) {
      // [SUPREME] 请求失败保留上次成功值，不清除、不兜底
    }
  }))
}

// ---- 轮询管理（独立于 fundStore.currentTab，不碰主看板轮询）----
onMounted(() => {
  loading.value = true
  fetchRows().finally(() => { loading.value = false })
  fetchHoldings()
  rowTimer = setInterval(fetchRows, 5000)
  holdingTimer = setInterval(fetchHoldings, 20000)
})
onUnmounted(() => {
  if (rowTimer) clearInterval(rowTimer)
  if (holdingTimer) clearInterval(holdingTimer)
})

// ---- 表格列 ----
// [AI-2026-10-10] 东哥指令：列名/顺序/格式化/涨跌颜色/列宽与主看板完全一致，只保留到「T-1估值溢价」为止
// （主看板右侧「成交额」起的列全部不要）。列名大体与主看板一致，但按东哥 2026-10-10 指令：
// 估值两列加「持仓」前缀（实时估值→持仓实时估值、T-1静态估值→T-1持仓静态估值）防口径歧义，溢价列名不变；
// 但 实时估值=T-2/实时估值 等 4 列的值仍是持仓口径（Model B：holding.hrv / holding.base_nav），
// 与主看板标准估值口径不同 —— 副标题口径提示必须保留。
// 涨跌幅列字段口径照抄主看板：有 prev_close 用 (现价-prev_close)/prev_close 实时算，否则退回行数据 price_change。
const columns: DataTableColumns<DashboardRow> = [
  {
    // [AI-2026-10-10] 主看板列 2「代码」：NText code 样式 + .code-cell，列宽 65 对齐主看板
    title: '代码',
    key: 'fund_code',
    width: 65,
    align: 'center',
    render(row) {
      return h(NText, { code: true, class: 'code-cell' }, { default: () => row.fund_code || '-' })
    }
  },
  {
    // [AI-2026-10-10] 主看板列 3「名称」：cleanFundName + ellipsis tooltip，列宽 130 对齐主看板
    // [AI-2026-10-12 重做 名称列左对齐] align center→left（与主看板名称列一致；宽度 130 不变）
    title: '名称',
    key: 'fund_name',
    width: 130,
    align: 'left',
    ellipsis: { tooltip: true },
    render(row) {
      return h('span', {}, cleanFundName(row.fund_name))
    }
  },
  {
    // [AI-2026-10-10] 主看板列 4「现价」：curPrice（盘中 realtime_price 优先，缺失/<=0 退回收盘价 price），
    // formatPrice(3位小数)；两者都缺 → '--'（SUPREME，不用 0）
    title: '现价',
    key: 'price',
    width: 62,
    align: 'center',
    render(row) {
      const p = curPrice(row)
      return p == null
        ? h('span', { class: 'num-cell' }, '--')
        : h('span', { class: 'num-cell' }, formatPrice(p))
    }
  },
  {
    // [AI-2026-10-10] 主看板列 5「涨跌幅」：照抄主看板 render —— 有 prev_close 用现价实时算，
    // 否则取行数据 price_change；缺失 → '-'（SUPREME 占位，不用 0）
    title: '涨跌幅',
    key: 'price_change',
    width: 66,
    align: 'center',
    render(row) {
      // 有 prev_close 时用现价实时算；现价双缺（cp=null）→ 退回行数据 price_change（缺失则 '-'）
      const cp = curPrice(row)
      // [SUPREME] 盘中双缺时 cp=null → 显示 '-'（主看板同场景也是 '-'），不用 0 兜底
      const chg = row.prev_close && cp != null
        ? (cp - row.prev_close) / row.prev_close * 100
        : row.price_change
      if (chg == null || (chg === 0 && cp == null)) return '-'
      return h('span', {
        class: 'num-cell strong',
        style: { color: priceColor(chg) }
      }, formatPercent(chg, 2))
    }
  },
  {
    // [AI-2026-10-10] 东哥指令：列名「实时估值」→「持仓实时估值」（与主看板标准口径列区分防歧义），值 = holding.hrv（Model B）
    // 标题用主看板同款 col-title-wrapper 单行居中写法；缺失 → '--'；列宽 80→100 适配 6 字表头不折行
    title: () => h('div', { class: 'col-title-wrapper' }, [
      h('div', { style: 'font-size: 12px; font-weight: bold;' }, '持仓实时估值')
    ]),
    key: 'rt_val_display',
    width: 100,
    align: 'center',
    render(row) {
      const nav = holdingMap.value[row.fund_code]?.hrv
      // [SUPREME] hrv 为 null → 显示 --，绝不用 0/最近值兜底
      return nav == null
        ? h('span', { class: 'num-cell strong' }, '--')
        : h('span', { class: 'num-cell strong' }, formatValuation(nav))
    }
  },
  {
    // [AI-2026-10-10] 主看板列 7「实时溢价」：列名照抄主看板，值 = (现价/hrv−1)×100（Model B）
    title: '实时溢价',
    key: 'rt_premium',
    width: 80,
    align: 'center',
    render(row) {
      const nav = holdingMap.value[row.fund_code]?.hrv
      const p = curPrice(row)
      // [SUPREME] hrv 或现价为 null → 溢价显示 --，绝不兜底
      if (nav == null || p == null) {
        return h('span', { class: 'num-cell muted' }, '--')
      }
      const pct = (p / nav - 1) * 100
      return h('span', {
        class: 'num-cell strong compact',
        style: { color: priceColor(pct) }
      }, formatPremium(pct))
    }
  },
  {
    // [AI-2026-10-10] 主看板列 8：黄金原油属 t2Tabs → 主看板表头即 'T-2净值'（不带「日」，2026-10-10 改名），照抄
    title: 'T-2净值',
    key: 'nav',
    width: 66,
    align: 'center',
    render(row) {
      // 行数据 nav 缺失/为 0 → formatValuation 返回 '-'（主看板同源口径，非造数）
      return h('span', { class: 'num-cell muted' }, formatValuation(row.nav ?? null))
    }
  },
  {
    // [AI-2026-10-10] 主看板列 9「净值日期」：NText depth=3 + .date-cell + shortDate(MM-DD)，缺失显示 '-'
    title: '净值日期',
    key: 'nav_date',
    width: 60,
    align: 'center',
    render(row) {
      return h(NText, { depth: 3, class: 'date-cell' }, { default: () => shortDate(row.nav_date || '') })
    }
  },
  {
    // [AI-2026-10-10] 东哥指令：列名「T-1静态估值」→「T-1持仓静态估值」防口径歧义，值 = holding.base_nav（Model B）
    // 列宽 82→118 适配 8 字表头不折行
    title: () => h('div', { class: 'col-title-wrapper' }, [
      h('div', { style: 'font-size: 12px; font-weight: bold;' }, 'T-1持仓静态估值')
    ]),
    key: 'static_val_display',
    width: 118,
    align: 'center',
    render(row) {
      const nav = holdingMap.value[row.fund_code]?.base_nav
      // [SUPREME] base_nav 为 null → 显示 --，绝不兜底
      return nav == null
        ? h('span', { class: 'num-cell strong' }, '--')
        : h('span', { class: 'num-cell strong' }, formatValuation(nav))
    }
  },
  {
    // [AI-2026-10-10] 主看板列 11「T-1估值溢价」：列名照抄主看板，值 = (现价/base_nav−1)×100（Model B）
    title: 'T-1估值溢价',
    key: 'static_premium',
    width: 92,
    align: 'center',
    render(row) {
      const nav = holdingMap.value[row.fund_code]?.base_nav
      const p = curPrice(row)
      // [SUPREME] base_nav 或现价为 null → 溢价显示 --，绝不兜底
      if (nav == null || p == null) {
        return h('span', { class: 'num-cell muted' }, '--')
      }
      const pct = (p / nav - 1) * 100
      return h('span', {
        class: 'num-cell compact',
        style: { color: priceColor(pct) }
      }, formatPremium(pct))
    }
  }
]

// 行点击 → 跳转季报持仓分析页（HoldingAnalysis 读 query.code / query.name）
const rowProps = (row: DashboardRow) => ({
  onClick: () => {
    router.push({
      path: '/holding-analysis',
      query: { code: row.fund_code, name: row.fund_name }
    })
  },
  style: 'cursor: pointer'
})
</script>

<style scoped>
/* [AI-2026-10-10] 页面容器 + 标题区 */
.oil-page {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.page-header {
  padding: 4px 12px 8px;
}
.page-title {
  font-size: 18px;
  font-weight: 800;
  color: #1f2937;
  margin: 0;
}
.page-subtitle {
  font-size: 12px;
  color: #64748b;
  margin: 2px 0 0;
}

/* [AI-2026-10-10] 主卡片（复制自 Dashboard.vue .main-card 风格） */
.main-card {
  border-radius: 8px;
  background-color: #fff;
  border: 1px solid #e5edf7;
  box-shadow: 0 2px 10px rgba(15, 23, 42, 0.05);
  overflow: hidden;
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
}

/* [AI-2026-10-10] 代码小字（.code-cell）：对齐主看板代码列 NText code 样式 */
.code-cell {
  /* [AI-2026-10-12 重做 字体统一token] Fira Code/Consolas/monospace 旧栈 → var(--co-font-mono) */
  font-family: var(--co-font-mono);
  font-size: 12px;
  font-weight: 700;
  color: #1f2937;
}
/* 净值日期（.date-cell，对齐主看板） */
.date-cell { font-size: 11px; color: #64748b; }
/* 数值单元格（.num-cell）及修饰类 .strong/.muted/.compact */
.num-cell {
  /* [AI-2026-10-12 重做 字体统一token] Inter/Fira Code/Consolas/sans 旧栈 → var(--co-font-sans) */
  font-family: var(--co-font-sans);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  color: #1f2937;
}
.num-cell.strong { font-weight: 750; }
.num-cell.muted { color: #64748b; }
.num-cell.compact { font-size: 12px; }
/* 状态小标签（.status-pill，保留以对齐主看板） */
.status-pill { font-size: 10px; padding-inline: 5px !important; }
/* 列头居中（对齐主看板 .col-title-wrapper） */
/* [AI-2026-10-12 重做 表头统一蓝底/文字] 背景 #eef5ff→#edf3fb、文字 #21395c→#33629c */
:deep(.n-data-table-th) {
  background-color: #edf3fb !important;
  color: #33629c !important;
  font-size: 12px;
  font-weight: 800 !important;
  border-bottom: 1px solid #dfe8f4 !important;
  text-align: center !important;
}
:deep(.n-data-table-th__title-container) {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  width: 100% !important;
}
</style>
