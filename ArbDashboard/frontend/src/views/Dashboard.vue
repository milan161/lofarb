<template>
  <div class="dashboard">
    <n-grid :cols="24" :x-gap="10" :y-gap="10">
      <!-- 时钟+汇率（引擎状态已移到侧边栏） -->
      <n-gi :span="8">
        <n-card size="small" :bordered="false" class="stat-card" content-style="padding: 4px 10px;">
          <div style="display: flex; flex-direction: column; gap: 2px; padding: 2px 0;">
            <div style="display: flex; align-items: baseline; gap: 12px; white-space: nowrap;">
              <span style="font-weight: 600; font-size: 14px;">{{ currentDate }} <span class="time">{{ currentTime }}</span></span>
              <span style="color: #64748b; font-size: 12px;">USD/CNY</span>
              <span style="font-weight: bold; font-size: 13px;">{{ rates.usd_cny_mid || '-' }}</span>
              <span :class="['rate-change', rates.usd_change >= 0 ? 'up' : 'down']" style="font-size: 11px;">{{ formatChange(rates.usd_change) }}</span>
            </div>
            <div style="display: flex; align-items: baseline; gap: 12px; white-space: nowrap; font-size: 12px;">
              <span style="color: #64748b; width: 86px; display: inline-block;">HKD/CNY</span>
              <span style="font-weight: bold; margin-right: 2px;">{{ rates.hkd_cny_mid || '-' }}</span>
              <span :class="['rate-change', rates.hkd_change >= 0 ? 'up' : 'down']" style="font-size: 11px; margin-right: 8px;">{{ formatChange(rates.hkd_change) }}</span>
              <span style="color: #64748b;">JPY/CNY</span>
              <span style="font-weight: bold; margin-right: 2px;">{{ rates.jpy_cny_mid || '-' }}</span>
              <span :class="['rate-change', rates.jpy_change >= 0 ? 'up' : 'down']" style="font-size: 11px;">{{ formatChange(rates.jpy_change) }}</span>
            </div>
          </div>
        </n-card>
      </n-gi>

      <!-- [AI-2026-10-06] 环球市场概览条（自有实现）：商品+期货，只显示最新价+涨跌幅，无"收盘/行情/行情时间"冗余 -->
      <!-- [AI-2026-10-06 二轮] 日志卡移至侧栏（东哥定调），概览条扩为全宽 16；过期指示器/手动刷新随迁到本卡 -->
      <n-gi :span="16">
        <n-card size="small" :bordered="false" class="stat-card ov-card" content-style="padding: 4px 10px; position: relative;">
          <!-- [AI-2026-10-06 五轮] 手动刷新按钮删除：数据本就自动轮询，按钮冗余（东哥确认"死的"） -->
          <div v-if="dashboardMeta.stale || dashboardMeta.error" style="position: absolute; left: 8px; top: 4px; z-index: 10;">
            <n-tag type="warning" size="tiny" round>
              {{ dashboardMeta.error ? '数据异常' : (tableData.length === 0 ? '估值计算中…' : '数据已延迟') }}
              <template v-if="dashboardMeta.compute_ms > 0"> ({{ dashboardMeta.compute_ms }}ms)</template>
            </n-tag>
          </div>
          <div v-if="overviewGroups.length" class="ov-strip">
            <div v-for="g in overviewGroups" :key="g.key" class="ov-group">
              <span class="ov-glabel">{{ g.label }}</span>
              <div class="ov-items">
                <div v-for="it in g.items" :key="it.symbol" class="ov-item">
                  <span class="ov-name">{{ it.name }}<span v-if="it.code" class="ov-code" :title="it.symbol">{{ it.code }}</span></span>
                  <span class="ov-price">{{ it.price.toFixed(2) }}</span>
                  <span class="ov-pct" :class="it.change_pct >= 0 ? 'ov-up' : 'ov-down'">
                    {{ it.change_pct >= 0 ? '+' : '' }}{{ it.change_pct.toFixed(2) }}%
                  </span>
                </div>
              </div>
            </div>
          </div>
          <div v-else class="ov-loading">{{ overviewError ? '行情通道未就绪（自动重试中）' : '行情加载中…' }}</div>
        </n-card>
      </n-gi>

      <!-- Main Table -->
      <n-gi :span="24">
        <n-card :bordered="false" class="main-card" content-style="padding: 0;">
          <div class="table-toolbar">
            <!-- [AI-2026-07-09] TAB 改为动态：从数据库分类生成，新增分类（如 QDII日本）自动出现 -->
            <n-tabs type="bar" v-model:value="currentTab" animated style="flex: 1;" class="custom-tabs">
              <n-tab-pane v-for="tab in dashboardTabs" :key="tab" :name="tab" :tab="tab === '自选' ? '我的自选' : tab" />
            </n-tabs>
            <n-input v-model:value="searchKeyword" placeholder="搜索代码/名称..." class="search-input" size="small" clearable />
          </div>

          <div class="table-scroll-wrapper">
            <n-data-table
              :columns="columns"
              :data="filteredTableData"
              :loading="loading"
              :pagination="pagination"
              style="min-width: 1500px;"
              size="small"
              bordered
              :row-props="rowProps"
            />
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <!-- [AI-2026-10-07] 历史记录弹窗抽为共享组件 FundHistoryModal：主看板与实时估值页（TradeWorkbench）共用 -->
    <FundHistoryModal v-model:show="showHistoryModal" :fund-code="selectedFund?.fund_code || ''" :fund="selectedFund" />

    <!-- 白银比价弹窗 -->
    <!-- [AI-2026-11-14 修 BUG] 真正原因不是高度/定位，而是 NModal 没 import（已补）。
         之前误判为"弹窗太高撑破视口"，加的 v-if 只是把它从"一进页面就有"
         变成"点击后才出现"，症状反而更明显 ⇒ 现已撤掉 v-if，恢复标准写法。
         height:85vh 保留：内容确实多（180+ 行 + 图表），限高后内部滚动更合用。 -->
    <n-modal v-model:show="showSilverRatioModal" preset="card" title="白银比价监控 (161226)" style="width: 90%; max-width: 1200px; height: 85vh;">
      <SilverRatio />
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, h, computed, watch, onUnmounted } from 'vue'
// [AI-2026-10-10] 移除 useRouter/router：名称列不再跳转持仓分析页，router 在本文件已无引用
import { storeToRefs } from 'pinia'
import {
  NGrid, NGi, NCard, NIcon, NText, NInput,
  // [AI-2026-11-14 修 BUG] 补 NModal：本文件第 89 行用了 <n-modal> 但从未 import，
  //   Vue 解析不到该组件 ⇒ 退化成未知 HTML 标签，slot 内容按普通文档流渲染，
  //   表现为「白银比价」直接显示在主看板下方、无遮罩、无关闭按钮，切 Tab 也一直挂着。
  //   （FundHistoryModal.vue 里一直正常，就是因为它自己 import 了 NModal。）
  NModal,
  NButton, NDataTable, NTag, NTabs, NTabPane, useMessage
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { Star, StarOff, History } from 'lucide-vue-next'

// --- 新架构导入 ---
import { useFundStore, useMarketStore, useAppStore } from '../store'
import { formatPrice, formatValuation, formatPercent, formatPremium,
         formatVolume, formatShares, formatSharesChange, formatTurnoverRate,
         formatIndexPrice, priceColor, shortDate, cleanFundName } from '../utils'
// [AI-2026-10-10] 主看板移除原油持仓口径小字/表头T-2净值改名：fundApi 实时持仓接口导入已删（迁往原油穿透页）
import SilverRatio from './SilverRatio.vue'
import FundHistoryModal from '../components/FundHistoryModal.vue'

// ===== Stores =====
// [AI-2026-10-10] 删除 const router = useRouter()：名称列跳转入口已移除，router 无其它引用
const fundStore = useFundStore()
const marketStore = useMarketStore()
const appStore = useAppStore()
const message = useMessage()

// [AI-2026-08-03] 主看板「现价」应显示盘中 LOF 实时价(realtime_price)，而非数据库收盘价(price)。
// 非交易时段 realtime_price 为空/0 时回退到收盘价，避免显示 0 或误导。
const curPrice = (row: any): number => {
  const rp = row?.realtime_price
  if (rp && rp > 0) return rp
  return row?.price || 0
}

// [AI-2026-10-10] 主看板移除原油持仓口径小字/表头T-2净值改名：原油三基金双估值整块（常量+轮询器）已删，数据源迁往原油穿透页

// ===== 从 Store 解构响应式状态（保持与模板同名的变量，避免改模板） =====
const { tableData, loading, currentTab, searchKeyword, watchlist,
        filteredTableData, fundHistory, dashboardMeta, dashboardTabs } = storeToRefs(fundStore)

// [AI-2026-10-06 二轮] 里程碑日志整体移至侧栏（MainLayout），Dashboard 不再渲染；
// milestones 由 fetchData 里的 appStore.fetchSystemStatus() 持续刷新，侧栏直接读 store。
// [AI-2026-10-06] 环球市场概览：商品(黄金/白银/WTI) + 期货(标普500/纳斯达克100/日经225)
// 后端 /api/market/global-overview 带 60s 缓存，随 fetchData 轮询即可
const overviewItems = ref<any[]>([])
const overviewError = ref(false)
const overviewGroups = computed(() => [
  { key: 'commodity', label: '商品', items: overviewItems.value.filter((x: any) => x.group === 'commodity') },
  { key: 'futures', label: '期货', items: overviewItems.value.filter((x: any) => x.group === 'futures') },
].filter(g => g.items.length > 0))

const fetchGlobalOverview = async () => {
  try {
    const res = await fetch('/api/market/global-overview')
    const data = await res.json()
    if (data.status === 'ok' && Array.isArray(data.data) && data.data.length) {
      overviewItems.value = data.data
      overviewError.value = false
    } else {
      // 后端正常但抓取为空（如数据源故障）：标记失败态，提示"自动重试中"而非永久"加载中"
      overviewError.value = true
    }
  } catch (e) {
    console.error('获取环球市场概览失败', e)
    overviewError.value = true
  }
}

// ===== 本地状态（无需进 Store） =====
const showHistoryModal = ref(false)
const showSilverRatioModal = ref(false)
const selectedFund = ref<any>(null)
const isCashManagementFund = computed(() => {
  return ['511880', '511360', '511520'].includes(selectedFund.value?.fund_code)
})
// [AI-2026-10-07] 历史记录弹窗（视图/分页/持仓明细/误差表/汇率行等）已抽为共享组件 FundHistoryModal

let refreshTimer: any = null

// [2026-11-14] 自选已 DB 化（见 全站主备说明.md §1.1），唯一真源 = fund_watchlist 表；
// 前端不再写 localStorage，原「Watch 自选持久化」监听器删除。

// ===== 方法 =====
// [AI-2026-10-07] 拉取 fundHistory 移入 FundHistoryModal（watch show 触发），此处只选中行并开弹窗
const openHistory = (fund: any) => {
  selectedFund.value = fund
  showHistoryModal.value = true
}

/**
 * 获取指数显示名称（中文名 + 代码）
 * 例如："中证新能源汽车指数 (399417)"
 */
const getIdxDisplayName = (fund: any) => {
  const idxName = fund.idx_name || ''
  const idxCode = fund.idx_code || fund.related_index || ''
  
  // 如果有中文名，显示"中文名 (代码)"
  if (idxName && idxName !== idxCode) {
    return `${idxName} (${idxCode})`
  }
  // 否则只显示代码
  return idxCode || '-'
}

const pagination = { pageSize: 100 }

const toggleWatchlist = (code: string) => fundStore.toggleWatchlist(code)

// [AI-2026-10-09 东哥拍板] 交互改为「点整行」打开基金详情弹窗（原先是点 T-1静态估值那一列）。
//   为什么改：列级点击要求用户瞄准 82px 宽的单元格，且与列底色/副标题一起构成过重视觉编码；
//   整行点击是看板类页面的通用直觉，也省掉列头副标题（「点击看详情」已删）。
//   ⚠️ 副作用需知：白银「实时估值(SI)」列自带 onClick 打开**白银比价**弹窗，
//      它是列级独立交互，与整行不冲突（点它仍开比价窗，点行其它位置才开基金详情）。
const rowProps = (row: any) => {
  return {
    onClick: () => openHistory(row),
    style: 'cursor: pointer'
  }
}

const fetchData = async (isSilent = false) => {
  if (!isSilent && filteredTableData.value.length === 0) loading.value = true
  try {
    // [AI-2026-07-07] fetchRates 加入 fetchData，确保汇率随定时刷新自动更新（原来只在 onMounted 取一次）
    // [AI-2026-10-06] fetchGlobalOverview 加入：环球市场概览随定时刷新更新（后端 60s 缓存）
    await Promise.all([
      fundStore.fetchDashboard(isSilent),
      marketStore.fetchOverview(),
      appStore.fetchSystemStatus(),
      fetchRates(),
      fetchGlobalOverview()
    ])
  } catch (err) { console.error('获取数据失败', err) } finally { loading.value = false }
}

const setupRefreshTimer = () => {
  if (refreshTimer) clearInterval(refreshTimer)
  const interval = fundStore.refreshInterval
  refreshTimer = setInterval(() => fetchData(true), interval)
}

watch(currentTab, () => {
  // [V8.1] 保留旧数据不闪白，后台静默刷新；filteredTableData 自动切换分类
  fetchData(true)
  setupRefreshTimer()
})

// [交换位置] 时钟 + 汇率：从侧边栏移入 Dashboard
const currentDate = ref('')
const currentTime = ref('')
const rates = ref({
  usd_cny_mid: '',
  hkd_cny_mid: '',
  jpy_cny_mid: '',
  usd_change: 0,
  hkd_change: 0,
  jpy_change: 0
})
let clockTimer: any = null

const updateTime = () => {
  const now = new Date()
  currentDate.value = now.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
  currentTime.value = now.toLocaleTimeString('zh-CN', { hour12: false })
}

const formatChange = (val: number) => {
  if (val === undefined || val === null) return '-'
  const sign = val >= 0 ? '+' : ''
  return `${sign}${val.toFixed(2)}%`
}

const fetchRates = async () => {
  try {
    const res = await fetch('/api/market/overview')
    const data = await res.json()
    if (data.status === 'ok') {
      rates.value.usd_cny_mid = data.data?.rates?.usd_cny_mid || '-'
      rates.value.hkd_cny_mid = data.data?.rates?.hkd_cny_mid || '-'
      rates.value.jpy_cny_mid = data.data?.rates?.jpy_cny_mid || '-'
      rates.value.usd_change = data.data?.usd_change || 0
      rates.value.hkd_change = data.data?.hkd_change || 0
      rates.value.jpy_change = data.data?.jpy_change || 0
    }
  } catch (e) {
    console.error('获取汇率失败', e)
  }
}

onMounted(() => {
  // [AI-2026-06-28] 取消重置 TAB，由 fundStore 从 localStorage 恢复上次浏览的 TAB
  // [AI-2026-07-09] 拉取数据库动态分类，生成主看板 TAB
  fundStore.fetchCategories()
  fetchData()
  setupRefreshTimer()
  // [交换位置] 时钟 + 汇率
  updateTime()
  fetchRates()
  clockTimer = setInterval(updateTime, 1000)
  // [AI-2026-10-10] 主看板移除原油持仓口径小字/表头T-2净值改名：onMounted 内原油持仓轮询调用已删
})
onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  if (clockTimer) clearInterval(clockTimer)
  // [AI-2026-10-10] 主看板移除原油持仓口径小字/表头T-2净值改名：onUnmounted 内原油轮询清除已删
})

// [AI-2026-10-10] 主看板移除原油持仓口径小字/表头T-2净值改名：名称列不再使用原油白名单做特殊样式/点击跳转，常量已删

const allColumns: DataTableColumns<any> = [
  {
    title: '★', key: 'watchlist', width: 34, fixed: 'left', align: 'center',
    render(row: any) {
      const isSelected = watchlist.value.includes(row.fund_code)
      return h(NIcon, {
        size: 16, color: isSelected ? '#f1c40f' : '#ddd', style: 'cursor: pointer;',
        onClick: (e: MouseEvent) => { e.stopPropagation(); toggleWatchlist(row.fund_code) }
      }, { default: () => isSelected ? h(Star) : h(StarOff) })
    }
  },
  {
    title: '代码', key: 'fund_code', width: 65, fixed: 'left', align: 'center',
    render(row: any) { return h(NText, { code: true, class: 'code-cell' }, { default: () => row.fund_code || '-' }) }
  },
  {
    // [AI-2026-10-12 重做 名称列左对齐+压宽] 宽度 130→100、align center→left（dist 实测口径；全 TAB 共用此列定义）
    title: '名称', key: 'fund_name', width: 100, fixed: 'left', align: 'left', ellipsis: { tooltip: true },
    className: 'col-fund-name',
    render(row: any) {
      // [AI-2026-10-10] 主看板移除原油持仓口径小字：名称列去掉 canAnalyze 特殊样式/点击跳转，
      // 三个原油基金名称恢复与其它基金完全一样的普通格式
      return h('span', {}, cleanFundName(row.fund_name))
    }
  },
  {
    title: '现价', key: 'price', width: 62, align: 'center',
    render(row: any) { return h('span', { class: 'num-cell' }, formatPrice(curPrice(row))) }
  },
  {
    title: '涨跌幅', key: 'price_change', width: 66, align: 'center',
    render(row: any) {
      const chg = row.prev_close ? (curPrice(row) - row.prev_close) / row.prev_close * 100 : row.price_change
      if (chg === null || chg === undefined) return '-'
      if (chg === 0 && (!curPrice(row) || curPrice(row) === 0)) return '-'
      return h('span', { class: 'num-cell strong', style: { color: priceColor(chg) } }, formatPercent(chg, 2))
    }
  },
  {
    // [AI-2026-10-09 东哥拍板] 去掉「LOFTrader下单」副标题：
    //   本列已取消点击，副标题既占高度又指向别的系统，纯误导 ⇒ 只留主标题。
    title: () => h('div', { class: 'col-title-wrapper' }, [
      h('div', { style: 'font-size: 12px; font-weight: bold;' }, '实时估值')
    ]),
    key: 'rt_val_display', width: 80, align: 'center',
    // [AI-2026-10-09] className 'col-rt-val' 已随整列底色删除（无任何 css 引用）⇒ 一并移除
    render(row: any) {
      // [AI-2026-08-20] 盘后(≥15:00)实时估值无意义，优先显示"冻"标签
      // 🔴 [AI-2026-10-08 东哥拍板] 本列不再跳转 —— 「实时估值计算器」已整页迁到 LOFTrader，
      //    LOFarb 此处只剩看板/估值权威，交易操作一律在 LOFTrader(:5174) 做。
      const onClick = undefined
      const cellCls = 'num-cell strong'
      const tip = '实时估值 · 下单/测算请到 LOFTrader 实盘下单页'
      let top: any
      if (row.rt_frozen) {
        top = h('span', { class: cellCls + ' frozen-cell', onClick, title: tip || '收盘冻结估值' }, [
          row.rt_val?.toFixed(4) || '-',
          h('span', { class: 'freeze-badge', title: row.rt_frozen_note || '收盘冻结估值' }, '冻')
        ])
      } else if (row.rt_val && row.rt_val > 0) {
        top = h('span', { class: cellCls, onClick, title: tip }, row.rt_val.toFixed(4))
      } else if (row.rt_unavailable === 'FUTU') {
        top = h('span', { class: cellCls, style: 'color:#f59e0b;font-size:11px;', onClick, title: tip }, '缺FUTU')
      } else {
        top = h('span', { class: cellCls, onClick, title: tip }, '-')
      }
      // [AI-2026-10-10] 主看板移除原油持仓口径小字：rt_val 列不再追加"持仓实时估值(Model B)"小字
      return top
    }
  },
  {
    title: '实时溢价', key: 'rt_premium', width: 80, align: 'center',
    render(row: any) {
      const makeTop = () => {
        if (!row.rt_val || !curPrice(row)) return h('span', { class: 'num-cell muted' }, '-')
        const p = (curPrice(row) / row.rt_val - 1) * 100
        const children = [formatPremium(p)]
        if (row.rt_frozen) children.push(h('span', { class: 'freeze-badge sm', title: row.rt_frozen_note || '收盘冻结估值' }, '冻'))
        return h('span', { class: 'num-cell strong compact', style: { color: priceColor(p) } }, children)
      }
      const top = makeTop()
      // [AI-2026-10-10] 主看板移除原油持仓口径小字：rt_premium 列不再追加"÷持仓实时估值"小字
      return top
    }
  },
  {
    title: 'T-2/1日净值', key: 'nav', width: 66, align: 'center',
    render(row: any) { return h('span', { class: 'num-cell muted' }, formatValuation(row.nav)) }
  },
  {
    title: '净值日期', key: 'nav_date', width: 60, align: 'center',
    render(row: any) { return h(NText, { depth: 3, class: 'date-cell' }, { default: () => shortDate(row.nav_date) }) }
  },
  {
    // [AI-2026-10-09 东哥拍板] 列标题去掉「点击看详情」副标题：
    //   交互已从「点某一列」改成「点整行」，副标题写「点击…」既占高度又会让人以为是列级交互。
    //   保留 col-title-wrapper 单行居中样式，与其它列标题对齐。
    title: () => h('div', { class: 'col-title-wrapper' }, [
      h('div', { style: 'font-size: 12px; font-weight: bold;' }, 'T-1静态估值')
    ]),
    key: 'static_val_display', width: 82, align: 'center',
    // [AI-2026-10-09] className 'col-static-val' 已随整列底色删除（无任何 css 引用）⇒ 一并移除
    // [AI-2026-10-09 东哥拍板] 列级 onClick 已移除 —— 改由 rowProps 整行触发。
    //   保留 clickable-cell 让数值仍显示为可点样式（hover 下划线），视觉上引导「这行能点」。
    //   ⚠️ 历史背景：此前这里必须有 stopPropagation（LOFTrader 侧曾给 tr 加跳转），
    //      现两侧 rowProps 均为「打开基金详情」这一同一个动作，无需再阻断冒泡。
    render(row: any) {
      const val = formatValuation(row.static_val)
      const top = h('span', {
        class: 'num-cell strong clickable-cell',
        title: '点击该行查看基金详情'
      }, val)
      // [AI-2026-10-10] 主看板移除原油持仓口径小字：static_val 列不再追加"÷T-1持仓静态估值"小字
      return top
    }
  },
  {
    // [AI-2026-10-12 重做 T-1估值溢价压宽] 宽度 92→84（dist 实测口径），align 保持 center
    title: 'T-1估值溢价', key: 'static_premium', width: 84, align: 'center',
    render(row: any) {
      const top = !row.static_premium
        ? h('span', { class: 'num-cell compact' }, '-')
        : h('span', { class: 'num-cell compact', style: { color: priceColor(row.static_premium) } }, formatPremium(row.static_premium))
      // [AI-2026-10-10] 主看板移除原油持仓口径小字：static_premium 列不再追加"÷T-1持仓静态估值溢价"小字
      return top
    }
  },
  {
    // [AI-2026-10-12 重做 成交额居中+压宽] 宽度 68→60、align right→center（dist 实测口径；表头本就居中，数字改居中消除右偏错位）
    title: () => twoLineTitle('成交额', '(万)'), key: 'volume', width: 60, align: 'center',
    render(row: any) { return h('span', { class: 'num-cell muted' }, formatVolume(row.volume)) }
  },
  {
    // [AI-2026-10-12 重做 份额居中+压宽] 宽度 52→48、align right→center（dist 实测口径）
    title: () => twoLineTitle('份额', '(万)'), key: 'shares', width: 48, align: 'center',
    render(row: any) { return h('span', { class: 'num-cell muted' }, formatShares(row.shares)) }
  },
  {
    // [AI-2026-10-12 重做 新增列居中+压宽] 宽度 48→44、align right→center（dist 实测口径）
    title: () => twoLineTitle('新增', '(万)'), key: 'shares_added', width: 44, align: 'center',
    fixedHeader: true,
    render(row: any) {
      const added = row.shares_added || 0
      return h('span', { class: 'num-cell compact', style: { color: priceColor(added) } }, formatSharesChange(row.shares_added))
    }
  },
  {
      title: '换手率', key: 'turnover_rate', width: 48, align: 'center',
      render(row: any) { return h('span', { class: 'num-cell muted' }, formatTurnoverRate(row.turnover_rate)) }
  },
  {
    title: '指数价', key: 'index_close', width: 80, align: 'center',
    render(row: any) { return h('span', { class: 'num-cell muted' }, formatIndexPrice(row.index_close)) }
  },
  {
    title: '指数涨跌', key: 'index_pct', width: 66, align: 'center',
    render(row: any) {
      if (!row.index_pct) return '-'
      return h('span', { class: 'num-cell compact', style: { color: priceColor(Number(row.index_pct)) } }, formatPercent(Number(row.index_pct), 2))
    }
  },
  {
    // [AI-2026-10-12 重做 指数名称左对齐] align center→left（宽度 80 不变）
    title: '指数名称', key: 'index_name', width: 80, align: 'left',
    render(row: any) {
      const name = row.idx_name || row.related_index || '-'
      return h('span', { class: 'num-cell muted', style: 'font-size: 11px;' }, name)
    }
  },

  {
    title: '申购', key: 'purchase_status', width: 56, align: 'center',
    render(row: any) {
      const status = row.purchase_status || '未知'
      const limit = row.purchase_limit  // 日累计限定金额（元）
      let shortText = status
      let type = 'warning'
      // [AI-2026-06-26] 修复"限大额"不显示具体金额：原逻辑status.includes('开放')优先判断但"限大额"不包含"开放"
      if (status.includes('限大额') || status.includes('限制')) {
        // 限购 → 显示具体金额（如限50万），不再显示模糊的"限大额"
        shortText = formatPurchaseLimit(limit)
        type = 'warning'
      } else if (status.includes('开放')) {
        shortText = '开放'
        type = 'success'
      } else if (status.includes('暂停')) {
        shortText = '暂停'
        type = 'default'
      }
      return h(NTag, { type, size: 'small', round: true, class: 'status-pill' }, { default: () => shortText })
    }
  },
  {
    title: '赎回',
    key: 'redemption_status',
    width: 46,
    align: 'center',
    render(row: any) {
      const status = row.redemption_status || '未知'
      let shortText = status
      let type = 'warning'
      if (status.includes('开放')) {
        if (status.includes('限大额')) {
          shortText = '限大额'
          type = 'warning'
        } else {
          shortText = '开放'
          type = 'success'
        }
      } else {
        shortText = '暂停'
        type = 'warning'
      }
      return h(NTag, { type, size: 'small', round: true, class: 'status-pill' }, { default: () => shortText })
    }
  },
  ]

// 通用数值渲染函数，historyColumns 和 columns 共享
const renderValWithChg = (val: number, chg: number, precision: number = 4) => {
    if (val == null) return h('span', { style: 'color: #999;' }, '-')
    const valStr = val.toFixed(precision)
    if (chg == null || chg === 0) {
        return h('span', { style: 'font-weight: 500;' }, valStr)
    }
    const color = chg > 0 ? 'var(--co-up)' : 'var(--co-down)'
    const chgStr = (chg > 0 ? '+' : '') + chg.toFixed(2) + '%'
    return h('span', {}, [
        h('span', { style: 'font-weight: 500;' }, valStr),
        h('span', { style: `color: ${color}; font-size: 11px; margin-left: 3px;` }, chgStr)
    ])
}

// [AI-2026-06-26] 新增：格式化申购限额，解决"限大额"无具体数量问题
const formatPurchaseLimit = (limit: number | null | undefined): string => {
  if (limit == null || limit === 0) return '限大额'
  if (limit >= 1e8 && limit % 1e8 === 0) return `限${limit / 1e8}亿`
  if (limit >= 1e4 && limit % 1e4 === 0) return `限${limit / 1e4}万`
  if (limit >= 1000) return `限${Math.round(limit / 100) / 10}千`
  return `限${limit}`
}

const historyColumns = computed<DataTableColumns<any>>(() => {
    const isCash = isCashManagementFund.value
    const is511360 = computed(() => selectedFund.value?.fund_code === '511360')
    const is511520 = computed(() => selectedFund.value?.fund_code === '511520')

    // [现金管理] 计算每日增加（nav - 昨日nav）
    const dailyIncrements = computed(() => {
        const history = fundHistory.value
        if (history.length === 0) return {}
        const result: Record<string, number> = {}
        // history是按日期降序排列的，[0]是最新
        for (let i = 0; i < history.length - 1; i++) {
            const curr = history[i]
            const next = history[i + 1]
            if (curr.nav != null && next.nav != null) {
                result[curr.date] = curr.nav - next.nav
            }
        }
        return result
    })

    // [现金管理] 日期列：511880标记周五, 511360标记周一
    const getWeekendLabel = (dateStr: string, fundCode: string) => {
        try {
            const d = new Date(dateStr)
            if (fundCode === '511360' && d.getDay() === 1) return ' 周一'
            if (fundCode !== '511360' && d.getDay() === 5) return ' 周五'
        } catch {}
        return ''
    }

    // [AI-2026-07-04] 判断是否有 hedge 数据（单ETF基金使用魔法公式需要）
    const hasHedge = fundHistory.value.some(r => r.hedge != null && r.hedge > 0)
    // [AI-2026-08-21] 商品基金（白银/豆粕）：历史弹窗"对冲值"列改为"结算价"（ag0/m0_settle 由后端注入）
    const isSilver = selectedFund.value?.category === '商品'

    const baseCols: DataTableColumns<any> = [
        { title: '日期', key: 'date', width: 85, align: 'center', render(row: any) {
            const d = shortDate(row.date)
            const label = isCash ? getWeekendLabel(row.date, selectedFund.value?.fund_code || '') : ''
            if (label) {
                return h('span', { style: 'color: #d97706; font-weight: 600;' }, d + label)
            }
            return d
        }},
        // 现金管理及国内LOF不显示汇率（国内LOF为纯A股，无汇率影响）
        ...(isCash || selectedFund.value?.category === '国内LOF' ? [] : [
            { title: '汇率', key: 'usd_cny_mid', width: 95, align: 'center', render(row: any) { return renderValWithChg(row.usd_cny_mid, row.usd_cny_mid_chg) } },
        ]),
        { title: '净值', key: 'nav', width: 95, align: 'center', render(row: any) { return renderValWithChg(row.nav, row.nav_chg) } },
        // [现金管理] 在净值后插入"每日增加"列
        ...(isCash ? [{
            title: '每日增加', key: 'daily_inc', width: 78, align: 'center',
            render(row: any) {
                const inc = dailyIncrements.value[row.date]
                if (inc == null) return '-'
                // 份额增减非涨跌语义，用中性深色（东哥10-07：仅跌幅可用绿）
                return h('span', { class: 'num-cell compact', style: { color: 'var(--co-text)' } }, (inc >= 0 ? '+' : '') + inc.toFixed(4))
            }
        }] : []),
        { title: '收盘价', key: 'price', width: 95, align: 'center', render(row: any) { return renderValWithChg(row.price, row.price_chg, 3) } },
        // 现金管理：折价几根毛/溢价在静态估值左侧；否则放静态估值右侧
        ...isCash
            ? [
                { title: '折价几根毛', key: 'yield_per_wan', width: 90, align: 'center', render(row: any) { const v = ((row.nav || 0) - (row.price || 0)) * 100; if (v === 0) return '-'; return h('span', { style: { color: priceColor(v), fontWeight: '500' } }, v.toFixed(2)) } },
                { title: '溢价', key: 'rt_premium', width: 90, align: 'center', render(row: any) { const nav = row.nav || 0; if (nav === 0) return '-'; const v = ((row.price || 0) / nav - 1); return h('span', { style: { color: priceColor(v), fontWeight: '500' } }, (v * 100).toFixed(3) + '%') } },
                // 511360 专属: 国债指数 + 涨幅
                ...(is511360.value ? [
                    { title: '指数', key: 'idx_close', width: 78, align: 'center', render(row: any) { return row.idx_close ? h('span', { class: 'num-cell' }, row.idx_close.toFixed(2)) : '-' } },
                    { title: '指数涨幅', key: 'idx_pct', width: 78, align: 'center', render(row: any) { if (row.idx_pct == null) return '-'; return h('span', { style: { color: priceColor(row.idx_pct), fontWeight: '500' } }, row.idx_pct.toFixed(3) + '%') } },
                ] : []),
                // 511520 专属: 国债期货 + 涨幅
                ...(is511520.value ? [
                    { title: '期货', key: 'futures_close', width: 78, align: 'center', render(row: any) { return row.futures_close ? h('span', { class: 'num-cell' }, row.futures_close.toFixed(3)) : '-' } },
                    { title: '期货涨幅', key: 'futures_pct', width: 78, align: 'center', render(row: any) { if (row.futures_pct == null) return '-'; return h('span', { style: { color: priceColor(row.futures_pct), fontWeight: '500' } }, row.futures_pct.toFixed(3) + '%') } },
                ] : []),
                { title: 'T-1静态估值', key: 'static_val', width: 105, align: 'center', render(row: any) { return renderValWithChg(row.static_val, row.static_val_chg) } },
                // [AI-2026-07-07] 修复：static_val为null时不显示-100%，直接返回'-'
                { title: '估值误差', key: 'val_error_pct', width: 85, align: 'center', render(row: any) { if (row.static_val == null || row.nav == null) return h('span', { class: 'num-cell muted' }, '-'); const v = row.static_val - row.nav; return h('span', { class: 'num-cell', style: { color: priceColor(v), fontWeight: 'bold' } }, v.toFixed(4)) } },
                { title: '误差率', key: 'val_error_rate', width: 78, align: 'center', render(row: any) { if (row.static_val == null || row.nav == null || row.nav === 0) return '-'; const v = (row.static_val - row.nav) / row.nav * 100; return h('span', { class: 'num-cell', style: { color: priceColor(v), fontWeight: '500' } }, v.toFixed(3) + '%') } },
              ]
            : [
                { title: 'T-1静态估值', key: 'static_val', width: 105, align: 'center', render(row: any) { return renderValWithChg(row.static_val, row.static_val_chg) } },
                // [AI-2026-07-07] 修复同上：static_val为null时显示'-'
                { title: '估值误差', key: 'val_error_pct', width: 85, align: 'center', render(row: any) { if (row.static_val == null || row.nav == null) return h('span', { class: 'num-cell muted' }, '-'); const v = row.static_val - row.nav; return h('span', { style: { color: priceColor(v), fontWeight: 'bold' } }, v.toFixed(4)) } },
                { title: '误差率', key: 'val_error_rate', width: 78, align: 'center', render(row: any) { if (row.static_val == null || row.nav == null || row.nav === 0) return '-'; const v = (row.static_val - row.nav) / row.nav * 100; return h('span', { style: { color: priceColor(v), fontWeight: '500' } }, v.toFixed(3) + '%') } },
                { title: '溢价', key: 'static_premium', width: 85, align: 'center', render(row: any) { const v = row.static_premium; if (v == null) return '-'; return h('span', { style: { color: priceColor(v) } }, formatPremium(v)) } },
                // [AI-2026-08-21] 商品（白银/豆粕）：历史弹窗"对冲值"列改为"结算价"（数据来自后端 ag0/m0_settle）
                ...(isSilver ? [{ title: '结算价', key: 'ag0_settle', width: 95, align: 'center', render(row: any) { const v = row.ag0_settle != null ? row.ag0_settle : row.m0_settle; const chg = row.ag0_settle != null ? row.ag0_settle_chg : row.m0_settle_chg; return v != null ? renderValWithChg(v, chg, 2) : '-' } }] : []),
              ],
        // QDII亚洲 / QDII日本 / 国内LOF / 指数型基金 专属：指数价 + 指数涨跌
        ...(['QDII亚洲', 'QDII日本', '国内LOF'].includes(selectedFund.value?.category || '') || selectedFund.value?.sub_category?.includes('指数') ? [
            { title: '指数价', key: 'index_close', width: 95, align: 'center', render(row: any) { return renderValWithChg(row.index_close, row.index_close_chg) } },
            { title: '指数涨跌', key: 'index_pct', width: 85, align: 'center', render(row: any) { if (row.index_pct == null) return '-'; return h('span', { style: { color: priceColor(row.index_pct), fontWeight: '500' } }, row.index_pct.toFixed(2) + '%') } },
        ] : []),
        // [2026-07-30] 份额/新增/换手率 不再内联，改为最右侧（见下方 shareCols）
    ]

    // [2026-07-30] 份额/新增/换手率 列（移到最右侧）
    const shareCols: DataTableColumns<any> = isCash ? [] : [
        { title: '份额(万)', key: 'shares', width: 85, align: 'center', render(row: any) { const v = row.shares; if (v == null) return '-'; return h('span', { style: 'font-size: 12px;' }, Number(v).toFixed(0)) } },
        { title: '新增(万)', key: 'shares_added', width: 80, align: 'center', render(row: any) { const v = row.shares_added; if (v == null) return '-'; const n = Number(v); return h('span', { style: { color: priceColor(n), fontSize: '11px' } }, (n >= 0 ? '+' : '') + n.toFixed(2)) } },
        { title: '换手率', key: 'turnover_rate', width: 80, align: 'center', render(row: any) { const v = row.turnover_rate; if (v == null) return '-'; return h('span', { style: 'font-size: 12px;' }, Number(v).toFixed(2) + '%') } },
    ]

    // [2026-07-30] 动态底层标的列：价格 + 紧随其后的权重列（权重紧贴价格右侧）
    const dynCols: DataTableColumns<any> = []
    if (fundHistory.value.length > 0) {
        // position/is_single_etf 为已知字段，避免被当作动态数值列
        const knownKeys = ['date', 'price', 'nav', 'static_val', 'static_premium', 'calibration', 'usd_cny_mid', 'turnover_amt', 'price_change', 'price_chg', 'nav_chg', 'static_val_chg', 'usd_cny_mid_chg', 'index_close', 'index_pct', 'idx_close', 'idx_pct', 'val_error_pct', 'shares', 'shares_added', 'turnover_rate', 'trade_volume', 'volume', 'valuation_error', 'hkd_cny_mid', 'jpy_cny_mid', 'latest_nav', 'futures_close', 'futures_pct', 'hedge', 'position', 'is_single_etf']
        // 扫描所有行收集动态键（首行可能缺数据，如 06-19 无 XOP_price）
        const dynamicKeys = new Set<string>()
        for (const row of fundHistory.value) {
            for (const key of Object.keys(row)) {
                if (!knownKeys.includes(key) && !key.endsWith('_chg') && typeof row[key] === 'number') {
                    dynamicKeys.add(key)
                }
            }
        }

        // [AI] 指数类基金不显示底层 SPY/QQQ 动态列，只看指数
        const isIndexFund = selectedFund.value?.sub_category?.includes('指数')
        const firstRow = fundHistory.value[0]
        const isSingleEtf = firstRow && 'is_single_etf' in firstRow ? (firstRow as any).is_single_etf : true

        // 归集：每个底层标的生成 价格列 + (若有)权重列，权重列紧贴价格列右侧
        const priceKeys: string[] = []
        const weightOf: Record<string, string> = {}
        dynamicKeys.forEach(key => {
            if (isIndexFund && key.endsWith('_price')) return
            const pm = key.match(/^(.+)_price$/)
            if (pm) {
                const wkey = pm[1] + '_weight'
                if (dynamicKeys.has(wkey)) weightOf[key] = wkey
                priceKeys.push(key)
            }
        })
        priceKeys.forEach(key => {
            const pm = key.match(/^(.+)_price$/)!
            const sym = pm[1]
            // [AI-2026-07-21] 单主ETF（如162411→XOP）显示净值；多篮子（如161116→GLD+^GLD-EU）显示价格
            const title = sym + (isSingleEtf ? '净值' : '价格')
            dynCols.push({
                title: title, key: key, width: 95, align: 'center',
                render(row: any) { return renderValWithChg(row[key], row[`${key}_chg`], 2) }
            })
            if (weightOf[key]) {
                const wkey = weightOf[key]
                dynCols.push({
                    title: sym + '权重', key: wkey, width: 85, align: 'center',
                    render(row: any) { const v = row[wkey]; if (v == null) return '-'; return h('span', { style: 'font-size: 12px;' }, Number(v).toFixed(2) + '%') }
                })
            }
        })
    }

    return [...baseCols, ...dynCols, ...shareCols]
})

// [2026-07-30] 历史弹窗表头"仓位"展示（取最新非空 position，按百分比显示；position 以分数存储，如 0.9599 → 95.99%）
const fundHistoryPositionDisplay = computed(() => {
    for (const r of fundHistory.value) {
        const p = r.position
        if (p != null && p !== '' && !isNaN(Number(p))) {
            return (Number(p) * 100).toFixed(2) + '%'
        }
    }
    return '-'
})

// [AI-2026-10-06 东哥需求] 表头两行显示：主名 + 小字灰色单位行（如 成交额/(万)），压列宽不折行挤爆
// [AI-2026-10-12 重做] 恢复 3 参 align（旧 dist: text-align: ${align}），模板字符串形式；旧 dist 无 3 参调用点，默认 center
const twoLineTitle = (main: string, sub: string, align: string = 'center') =>
  h('div', { style: `line-height: 1.25; text-align: ${align};` }, [
    h('div', {}, main),
    h('div', { style: 'font-size: 9px; color: #64748b; font-weight: 400;' }, sub),
  ])

const columns = computed<DataTableColumns<any>>(() => {
  // 深拷贝以便动态修改表头
  let cols = allColumns.map(c => ({...c}))

  // 1. 动态重命名净值日期列
  const t1Tabs = ['QDII亚洲', '国内LOF', '商品']
  const t2Tabs = ['QDII欧美', 'QDII日本', '黄金原油']  // [AI-2026-07-09] 混合跨境已并入QDII欧美；QDII日本同属T-2
  const navCol = cols.find(c => c.key === 'nav')
  if (navCol) {
    if (t1Tabs.includes(currentTab.value)) navCol.title = 'T-1日净值'
    // [AI-2026-10-10] 主看板移除原油持仓口径小字/表头T-2净值改名：t2Tabs 分支表头去掉「日」字（t1Tabs/默认分支不动）
    else if (t2Tabs.includes(currentTab.value)) navCol.title = 'T-2净值'
    else navCol.title = 'T-2/1日净值'
  }

  // [V7.0] 商品 TAB 专属列与重命名（161226 白银 AG0/SI + 159985 豆粕 M0）
  if (currentTab.value === '商品') {
    cols.forEach(col => {
      if (col.key === 'rt_val_display') col.title = '参考估值'
      if (col.key === 'rt_premium') col.title = '参考溢价'
      if (col.key === 'static_val_display') col.title = '官方估值'
      if (col.key === 'static_premium') col.title = '官方溢价'
    })
    
    const staticPremIndex = cols.findIndex(c => c.key === 'static_premium')
    cols.splice(staticPremIndex + 1, 0, 
      { title: () => h('div', { style: 'line-height: 1.3; text-align: center;' }, [h('div', {}, '实时成交价'), h('div', { style: 'font-size: 10px; color: #64748b;' }, '(AG0/M0)')]), key: 'futures_price', width: 100, align: 'center', render(row: any) { const v = row.ag0_price != null ? row.ag0_price : row.m0_price; return h('span', { class: 'num-cell' }, v != null ? Number(v).toFixed(0) : '-') } },
      // [AI-2026-10-12 重做] 盘中涨跌列（旧 dist futures_chg）：成交价/昨结算-1；无数据输出 '-' 不兜底
      // [AI-2026-10-12 重做] 与旧 dist 逐字等价：- 分支也包 span.num-cell；结果为单 span class=num-cell strong + style color（内层不包 strong）
      { title: '盘中涨跌', key: 'futures_chg', width: 78, align: 'center', render(row: any) {
        const p = row.ag0_price != null ? row.ag0_price : row.m0_price
        const s = row.ag0_settlement != null ? row.ag0_settlement : row.m0_settlement
        if (p == null || s == null || s <= 0) return h('span', { class: 'num-cell' }, '-')
        const r = (Number(p) / Number(s) - 1) * 100
        return h('span', { class: 'num-cell strong', style: { color: priceColor(r) } }, formatPercent(r, 2))
      } },
      { title: () => h('div', { style: 'line-height: 1.3; text-align: center;' }, [h('div', {}, '昨结算价'), h('div', { style: 'font-size: 10px; color: #64748b;' }, '(AG0/M0)')]), key: 'futures_settle', width: 100, align: 'center', render(row: any) { const v = row.ag0_settlement != null ? row.ag0_settlement : row.m0_settlement; return h('span', { class: 'num-cell muted' }, v != null ? Number(v).toFixed(0) : '-') } },
      // [AI-2026-10-12 重做] 当日VWAP列（旧 dist futures_vwap）：无数据输出 '-' 不兜底（禁填 0）
      // [AI-2026-10-12 重做] 与旧 dist 逐字等价：标题内联 line-height 1.3 + 副行 font-size:10px/#64748b（非 twoLineTitle 助手）；'-' 包在 span.num-cell 内
      { title: () => h('div', { style: 'line-height: 1.3; text-align: center;' }, [h('div', {}, '当日VWAP'), h('div', { style: 'font-size: 10px; color: #64748b;' }, '(结算价)')]), key: 'futures_vwap', width: 100, align: 'center', render(row: any) {
        const v = row.ag0_vwap != null ? row.ag0_vwap : row.m0_vwap
        return h('span', { class: 'num-cell' }, v == null ? '-' : Number(v).toFixed(0))
      } },
      // [AI-2026-10-09 东哥拍板] 白银比价弹窗记录为待修项。
      //   本列是**列级独立交互**，整行点击上线后必须 stopPropagation，
      //   否则点它会同时弹「白银比价」+「基金详情」两个窗（叠在一起）。
      { title: () => h('div', { style: 'line-height: 1.3; text-align: center;' }, [h('div', {}, '实时估值'), h('div', { style: 'font-size: 10px; color: #64748b;' }, '(SI)')]), key: 'si_val', width: 100, align: 'center', render(row: any) { return h('span', { class: 'num-cell clickable-cell', style: { cursor: 'pointer', color: '#2563eb' }, onClick: (e: MouseEvent) => { e.stopPropagation(); showSilverRatioModal.value = true } }, row.si_val != null ? row.si_val.toFixed(4) : '-') } },
      { title: () => h('div', { style: 'line-height: 1.3; text-align: center;' }, [h('div', {}, '实时溢价'), h('div', { style: 'font-size: 10px; color: #64748b;' }, '(SI)')]), key: 'si_premium', width: 95, align: 'center', render(row: any) { const v = row.si_premium; if (v == null) return '-'; return h('span', { class: 'num-cell', style: { color: priceColor(v) } }, formatPremium(v)) } }
    )
  }

  const hideIndexTabs = ['黄金原油', 'QDII欧美', '商品']
  if (hideIndexTabs.includes(currentTab.value)) {
    return cols.filter(c => c.key !== 'related_index' && c.key !== 'index_close' && c.key !== 'index_pct' && c.key !== 'index_name')
  }

  return cols
})

const tableScrollX = computed(() => {
  const minRequired = columns.value.reduce((total, col: any) => total + Number(col.width || 80), 0)
  // 深度压缩后列宽总和约900px，设置底线确保水平滚动在窄屏下可用
  return Math.max(1500, minRequired)
})
</script>

<style scoped>
.dashboard { 
  color: #1f2937;
  width: 100%;
  max-width: 100vw;
  min-width: 0;
  box-sizing: border-box;
}
:deep(.n-data-table),
:deep(.n-data-table-wrapper),
:deep(.n-data-table-base-table),
:deep(.n-data-table-base-table-body),
:deep(.n-data-table-table),
:deep(.n-data-table-tbody),
:deep(.n-scrollbar-container),
:deep(.n-scrollbar-content) {
  background: #ffffff !important;
  border-spacing: 0 !important;
}
:deep(.n-data-table-tr) {
  background-color: #ffffff !important;
}
:deep(.n-data-table-td) {
  padding: 2px 2px !important;
  color: #1f2937 !important;
  background-color: #ffffff !important;
  border-color: #f0f4f8 !important;
  border-width: 1px 0 !important;
}
:deep(.n-data-table-th) {
  padding: 3px 2px !important;
  /* [AI-2026-10-12 重做 表头统一蓝底] #eef5ff → #edf3fb（对齐基金详情·模拟持仓估值表参照色） */
  background-color: #edf3fb !important;
  border-width: 1px 0 !important;
}
:deep(.n-data-table-tr:nth-child(even) .n-data-table-td) { background-color: #fbfdff !important; }
:deep(.n-data-table-tr:hover .n-data-table-td) { background-color: #f6faff !important; }
:deep(.n-data-table-cell) {
  padding: 0 !important;
}
/* [AI-2026-10-12 重做 名称列左贴边] 基线只有 className 无 CSS 规则；dist 实测有 padding-left:6px。
   单元格全局 padding:0（见上），左对齐后文字会贴边，补 6px 内缩让名称不贴住固定列分隔线 */
.col-fund-name {
  padding-left: 6px !important;
}

.table-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  /* [AI-2026-10-06 东哥需求] TAB排与表格间距压缩：padding 8→4，底边0（tab自身有高度） */
  padding: 4px 12px 0;
  border-bottom: 1px solid #e5edf7;
  background: #ffffff;
}
.search-input { width: 170px; margin-left: 12px; flex-shrink: 0; }
.stat-card {
  background: #ffffff;
  border: 1px solid #e5edf7;
  border-radius: 8px;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
  box-shadow: 0 2px 10px rgba(15, 23, 42, 0.05);
  /* [AI-2026-10-06 五轮] 删旧布局固定 height:84px，卡片高度随内容自适应（压缩顶部留白，空间让给表格） */
}
/* [AI-2026-10-06 二轮] 日志卡已移至侧栏（MainLayout），相关样式随之删除 */
/* [AI-2026-10-06] 环球市场概览条：清淡风（涨红/跌绿统一走 --co-up/--co-down token） */
.ov-card { overflow: hidden; border: 1px solid #e5edf7; }
/* [AI-2026-10-06 四轮·东哥定调] 去掉嵌套底盒，商品/期货直接平铺在卡内（与左侧汇率卡同构），节约纵向空间 */
.ov-strip {
  display: flex; flex-direction: column; gap: 8px;
}
.ov-group { display: flex; align-items: center; gap: 10px; }
.ov-glabel { font-size: 12px; color: #1e293b; width: 32px; flex-shrink: 0; font-weight: 800; }
.ov-items { display: flex; flex: 1; justify-content: space-around; gap: 6px; }
.ov-item { display: flex; align-items: baseline; gap: 5px; white-space: nowrap; }
.ov-name { font-size: 12px; color: #64748b; }
/* [AI-2026-10-08 东哥需求] 名称后跟合约代码小字（GC/ES/NQ/NK…），悬停显示新浪原始代码（hf_GC） */
.ov-code { font-size: 10px; color: #94a3b8; font-weight: 600; margin-left: 2px; letter-spacing: 0.2px; cursor: help; }
.ov-price { font-size: 13px; font-weight: 700; color: #1e293b; font-variant-numeric: tabular-nums; }
.ov-pct { font-size: 11px; font-weight: 700; font-variant-numeric: tabular-nums; }
.ov-up { color: var(--co-up); }
.ov-down { color: var(--co-down); }
.ov-loading { font-size: 10px; color: #94a3b8; }
.stat-card:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(15, 23, 42, 0.08); }
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
.code-cell {
  /* [AI-2026-10-12 重做 字体统一token] Fira Code/Consolas/monospace 旧栈 → var(--co-font-mono) */
  font-family: var(--co-font-mono);
  font-size: 12px;
  font-weight: 700;
  color: #1f2937;
}
/* [AI-2026-10-10] 删除 .fund-name-cell：名称列已改回普通 span，该 class 无引用 */
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
.date-cell, .index-cell { font-size: 11px; color: #64748b; }
.clickable-cell { cursor: pointer; text-decoration: underline; color: #2563eb !important; }
.clickable-cell:hover { color: #1d4ed8 !important; }
/* [2026-07-31] 收盘冻结估值标签 — [AI-2026-08-04] 东哥硬刷看不到效果，进一步加大对比
   ⚠️ 必须放在 <style scoped> 外（见底部非 scoped 块），因 n-data-table slot 不继承父 data-v */
.col-title-wrapper { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 2px 0; width: 100%; }
.bg-blue-light { background-color: #dbeafe; }
.bg-orange-light { background-color: #ffedd5; }
.status-pill { font-size: 10px; padding-inline: 5px !important; }
:deep(.n-tabs .n-tabs-tab) {
  /* [AI-2026-10-06 东哥需求] 压缩TAB行高：6px→3px→2px+13px字 */
  padding: 2px 8px;
  font-size: 13px;
  color: #526173;
  font-weight: 650;
}
/* [AI-2026-10-06 五轮] bar型tabs导航自带上下留白压掉，TAB行整体再矮一截 */
:deep(.n-tabs .n-tabs-nav) { padding: 0; line-height: 1.3; }
:deep(.n-tabs .n-tabs-tab__label) { line-height: 1.3; }
:deep(.n-tabs .n-tabs-tab--active) { color: #2563eb !important; background-color: #eef6ff !important; border-radius: 6px 6px 0 0; }
:deep(.n-tabs .n-tabs-bar) { background-color: #2563eb !important; }
/* [AI-2026-10-12 重做 表头统一蓝底/文字] 背景 #eef5ff→#edf3fb、文字 #21395c→#33629c（padding/字重/边框不动） */
:deep(.n-data-table-th) {
  background-color: #edf3fb !important;
  color: #33629c !important;
  font-size: 12px;
  font-weight: 800 !important;
  border-bottom: 1px solid #dfe8f4 !important;
  text-align: center !important;
}
:deep(.n-data-table-th__title-container) { display: inline-flex !important; align-items: center !important; justify-content: center !important; width: 100% !important; }
:deep(.n-data-table-sorter) { margin-left: 2px !important; display: inline-flex !important; }
:deep(.n-data-table .n-data-table-td--fixed-left),
:deep(.n-data-table .n-data-table-th--fixed-left),
:deep(.n-data-table .n-data-table-td--fixed-right),
:deep(.n-data-table .n-data-table-th--fixed-right) {
  background-color: #ffffff !important;
  box-shadow: none !important;
}
:deep(.n-data-table .n-data-table-th--fixed-left) {
  /* [AI-2026-10-12 重做 表头统一蓝底] 固定左列表头同步 #edf3fb（右侧固定列粉色 #fff1f2/#e11d48 为语义告警色，保持不动） */
  background-color: #edf3fb !important;
}
:deep(.n-data-table .n-data-table-th--fixed-right) {
  background-color: #fff1f2 !important; /* 粉红色背景 */
  color: #e11d48 !important; /* 玫瑰红文字 */
}
/* 滚动条加粗：方便鼠标点击（水平+垂直） */
:deep(.n-scrollbar-rail) {
  width: 14px !important;
  height: 14px !important;
  right: 1px;
  bottom: 1px;
  opacity: 0.8;
}
:deep(.n-scrollbar-rail--vertical) {
  width: 14px !important;
}
:deep(.n-scrollbar-rail--horizontal) {
  height: 14px !important;
}
:deep(.n-scrollbar-thumb) {
  width: 10px !important;
  height: 10px !important;
  border-radius: 5px !important;
  background-color: #cbd5e1 !important;
  cursor: pointer !important;
}
:deep(.n-scrollbar-thumb:hover) {
  width: 10px !important;
  height: 10px !important;
  background-color: #94a3b8 !important;
}
:deep(.n-scrollbar-rail:hover .n-scrollbar-thumb) {
  background-color: #94a3b8 !important;
}

/* 表格外层容器：固定高度，同时处理水平和垂直滚动 */
.table-scroll-wrapper {
  width: 100%;
  /* [AI-2026-10-06 五轮] 顶部卡瘦身(84→~50)+TAB行压缩，220px→180px，省出高度全给表格 */
  height: calc(100vh - 180px);
  overflow: auto;
}

/* 历史弹窗：单一滚动容器方案（与 017 主表同原则，根治"横条被推到表底"）
   - 弹窗近全屏(96vw/90vh)：横向容纳更多列，160719 等典型基金整表直接放下
   - 卡片内容设为 flex 列；body flex:1 + min-height:0 撑满，表格外层再 flex:1 + overflow:auto
   - 移除了 n-data-table 的 flex-height / scroll-x，避免内层再生成独立横向滚动容器
   - 水平+垂直滚动统一由 .history-table-wrapper 接管，横条恒在弹窗底部、始终可见
*/
:deep(.n-card__content) {
  display: flex;
  flex-direction: column;
}
/* [AI-2026-10-07] 历史弹窗样式（.history-modal-body / .hd-*）已随组件抽走 → FundHistoryModal.vue */
.history-info {
  flex: 0 0 auto;
  margin-bottom: 16px;
  display: flex;
  gap: 24px;
  font-size: var(--co-fs-title);
  background: var(--co-surface-2);
  padding: 12px;
  border-radius: var(--co-radius);
}
.history-table-wrapper {
  flex: 1 1 auto;
  min-height: 0;
  width: 100%;
  overflow: auto;
  /* Firefox 滚动条配色（宽度受浏览器限制，无法自定义像素） */
  scrollbar-width: auto;
  scrollbar-color: #94a3b8 #f1f5f9;
}

/* 加大水平滚动条拖拽块：高度 16px，圆角滑块，hover 变深 */
.history-table-wrapper::-webkit-scrollbar {
  width: 12px;
  height: 16px;
}
.history-table-wrapper::-webkit-scrollbar-track {
  background: #f1f5f9;
  border-radius: 8px;
}
.history-table-wrapper::-webkit-scrollbar-thumb {
  background: #94a3b8;
  border-radius: 8px;
  border: 3px solid #f1f5f9;
}
.history-table-wrapper::-webkit-scrollbar-thumb:hover {
  background: #64748b;
}

/* [2026-07-30] 历史弹窗表格字体：改用截图同款 sans-serif 栈
   数字优先 Tahoma（与 woody 网页/截图里数字风格最接近），
   中文回退 Microsoft YaHei，等宽数字 tabular-nums 让列对齐更整齐 */
.history-table-wrapper :deep(.n-data-table),
.history-table-wrapper :deep(.n-data-table-td),
.history-table-wrapper :deep(.n-data-table-th) {
  font-family: var(--co-font-sans) !important;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum";
}
.history-table-wrapper :deep(.n-data-table-th) {
  font-weight: 600;
}

/* 表头吸顶：即便某页行数多导致纵向滚动，表头也始终可见 */
.history-table-wrapper :deep(.n-data-table-thead) {
  position: sticky;
  top: 0;
  z-index: 2;
}
.history-table-wrapper :deep(.n-data-table-thead .n-data-table-th) {
  background: var(--co-surface-2);
}

/* 分页栏：固定在表格下方，不随表格滚动 */
.history-pagination {
  flex: 0 0 auto;
  margin-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.history-page-info {
  font-size: var(--co-fs-base);
  color: var(--co-text-3);
}

/* [AI-2026-10-09 东哥拍板] 删除「实时估值 / T-1静态估值 / 雪山机构估值」三列的整列底色。
   取证结论（不是猜的）：这三个 className + 对应 CSS 变量注释都写着「历史误差·…列底」，
   是 2026-10-07 抽 FundHistoryModal 组件之前的旧设计残留 —— 弹窗抽走后
   `grep col-rt-val|col-static-val|col-si-val` 只剩本文件，**再无第二处使用**。
   即：底色刷的是主看板，却沿用着「弹窗内区分列」的过期意图。
   保留三色本意是让读者一眼区分三类估值口径，但这层视觉编码过重、反而像"这几列可以点"
   （实时估值列其实早已取消点击，只剩静态估值列可点）。
   ⇒ 三列底色 + 表头底色一并删除，回到 naive-ui 默认斑马纹。 */

/* [交换位置] 时钟 + 汇率样式 */
.date { font-size: 11px; color: #7b8a9b; }
/* [AI-2026-10-12 重做 字体统一token] 裸 monospace → var(--co-font-mono) */
.time { font-size: 14px; font-weight: bold; color: #2563eb; font-family: var(--co-font-mono); }
.exchange-rates { margin-top: 15px; padding: 12px; background: #f8fafc; border-radius: 8px; border: 1px solid #e2e8f0; }
.rate-group { display: flex; flex-direction: column; }
.rate-label { font-size: 11px; color: #64748b; margin-bottom: 4px; white-space: nowrap; font-weight: 500; }
.rate-row { display: flex; align-items: baseline; justify-content: space-between; gap: 4px; }
/* [AI-2026-10-12 重做 字体统一token] Inter+系统默认旧栈 → var(--co-font-sans) */
.rate-value { font-size: 16px; font-weight: 800; color: #1e293b; font-family: var(--co-font-sans); }
.rate-change { font-size: 12px; font-weight: 700; }
.rate-change.up { color: var(--co-up); }
.rate-change.down { color: var(--co-down); }
</style>

<!-- [AI-2026-08-04] freeze-badge / frozen-cell 必须在 <style scoped> 外
     原因：n-data-table (Naive UI) 渲染 slot 时，slot vnode 不继承父组件的 data-v-xxx 属性，
     所以 .freeze-badge[data-v-xxx] 选择器无法匹配 cell 内的 span。改为全局（不 scoped）即可生效。-->
<style>
.frozen-cell { display: inline-flex; align-items: center; gap: 8px; }
.freeze-badge {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 5px;
  font-size: 10px;
  line-height: 1.3;
  font-weight: 700;
  color: #64748b;
  background: #f1f5f9;
  border: 1px solid #cbd5e1;
  border-radius: 3px;
  vertical-align: middle;
}
.freeze-badge.sm { font-size: 9px; padding: 0 4px; }
</style>
