/**
 * 基金数据 Store
 * - 看板列表、分类/TAB 筛选、自选管理
 * - 历史数据、分时数据、篮子权重
 */
import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import * as api from '../api'

export interface FundItem {
  fund_code: string
  fund_name: string
  category: string
  price: number
  nav: number
  static_val: number
  static_premium: number
  rt_val: number | null
  rt_premium: number | null
  rt_frozen?: boolean
  rt_frozen_note?: string
  rt_frozen?: boolean
  volume: number
  shares: number
  shares_added: number
  turnover_rate: number
  price_change?: number | null
  prev_close?: number | null
  nav_date: string
  index_close: number
  index_pct: number
  purchase_status: string
  redemption_status: string
  purchase_fee: string
  redemption_fee: string
  idx_code: string
  idx_name: string
  related_index: string
  pos_ratio: number
  [key: string]: any
}

/**
 * [AI-2026-07-09] 分类已简化：数据库 category 值与主看板 TAB 名一一对应，无子分类映射。
 * 主看板 TAB 完全由数据库动态分类生成，不再需要 TAB→分类映射表。
 */
export const HIGH_FREQ_TABS = ['自选', '黄金原油', 'QDII欧美', 'QDII日本', '商品']

export const useFundStore = defineStore('fund', () => {
  // ---- state ----
  const tableData = ref<FundItem[]>([])
  const loading = ref(false)
  const dashboardMeta = ref({
    updated_at: null as string | null,
    stale: false,
    compute_ms: 0,
    error: null as string | null
  })
  let dashboardInFlight = false
  let dashboardController: AbortController | null = null
  let dashboardRequestSeq = 0
  // 始终默认"我的自选"TAB（持久化到 localStorage，从其他页面回来时恢复上次的 TAB）
  const savedTab = localStorage.getItem('dashboard_tab')
  const currentTab = ref(savedTab && savedTab !== 'null' ? savedTab : '自选')
  // [AI-2026-06-28] 自动持久化 TAB 选择
  watch(currentTab, (v) => {
    localStorage.setItem('dashboard_tab', v)
  })
  const searchKeyword = ref('')
  // [AI-2026-07-09] 动态 TAB：从数据库读取的真实分类列表（category 值与 TAB 名一致）
  const dbCategories = ref<string[]>([])

  // [AI-2026-07-09] 主看板 TAB 固定顺序（自选始终第一，其余按用户指定顺序，未知分类追加末尾）
  const DASHBOARD_TAB_ORDER = ['黄金原油', 'QDII欧美', 'QDII日本', 'QDII亚洲', '国内LOF', '现金管理', '商品']

  /** 主看板 TAB 列表：始终含"我的自选"，其余由数据库分类动态生成，按固定顺序排序 */
  const dashboardTabs = computed<string[]>(() => {
    const cats = dbCategories.value.filter((c) => c && c !== '自选')
    const ordered = DASHBOARD_TAB_ORDER.filter((c) => cats.includes(c))
    const extra = cats.filter((c) => !DASHBOARD_TAB_ORDER.includes(c))
    return ['自选', ...ordered, ...extra]
  })
  const fundHistory = ref<any[]>([])
  const fundHistoryLoading = ref(false)
  // [AI-2026-10-09 方案A] 历史接口附带的锚点信号：today=服务器当天日期、
  // trading_today=A股日历是否交易日（null=信号缺失/异常，前端锚点校验按无法判定保守处理）
  const fundHistoryToday = ref<string | null>(null)
  const fundHistoryTradingToday = ref<boolean | null>(null)
  const intradayData = ref<any[]>([])
  const basketData = ref<any[]>([])

  // ---- watchlist（DB 化为唯一真源；见 全站主备说明.md §1.1：B 类用户选择，本地权威上推 ARM）----
  const watchlist = ref<string[]>([])
  let watchlistInitialized = false
  async function loadWatchlist() {
    if (watchlistInitialized) return
    watchlistInitialized = true
    try {
      const res = await fetch('/api/watchlist')
      const data = await res.json()
      if (data?.status === 'ok' && Array.isArray(data.data)) {
        watchlist.value = data.data.map((r: any) => r.fund_code)
      }
    } catch (e) {
      console.error('加载自选失败', e)
    }
    // 一次性迁移：DB 空且 localStorage 仍有旧自选 → 推上 DB 后清 localStorage（避免丢历史自选）
    if (watchlist.value.length === 0) {
      try {
        const legacy = JSON.parse(localStorage.getItem('watchlist') || 'null')
        if (Array.isArray(legacy) && legacy.length > 0) {
          await Promise.all(legacy.map((code: string) =>
            fetch('/api/watchlist', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ fund_code: code })
            })))
          watchlist.value = legacy
        }
      } catch (e) { /* 迁移失败不影响后续 */ }
      localStorage.removeItem('watchlist')
    }
    // 若当前在「自选」TAB，加载完成后静默重拉一次（首屏时序保证有数据）
    if (currentTab.value === '自选') fetchDashboard(true)
  }

  // ---- getters ----
  /** [AI-2026-10-07] QDII欧美固定排序（东哥指定分组顺序，便于同组比较）。
   *  不在表内的基金（如未入库的 513519）自动落末尾，不影响其他 TAB。 */
  const QDII_OUMEI_FIXED_ORDER: Record<string, number> = {
    '162411': 1,  // 华宝油气
    '513350': 2,  // 标普油气ETF富国
    '159518': 3,  // 标普油气ETF嘉实（油气组，东哥原清单笔误 513519→159518）
    '161125': 4,  // 易方达标普500
    '161126': 5,  // 标普医疗保健
    '161127': 6,  // 标普生物科技
    '159502': 7,  // 标普生物科技ETF嘉实
    '161128': 8,  // 标普信息科技
    '161130': 9,  // 易方达纳100（东哥指定移至 161128 之下）
    '162415': 10, // 美国消费
    '164824': 11, // 交银印度
    '164906': 12, // 交银中证海外
    '513030': 13, // 德国ETF华安
    '159561': 14, // 德国ETF嘉实
    '513080': 15, // 法国ETF华安
    '501300': 16, // 海富通美元债
    '160644': 17, // 港美互联网
    '501225': 18, // 顺丰半导体芯片
    '501312': 19, // 海外科技
  }

  /** 按当前 TAB + 搜索关键词过滤后的数据 */
  const filteredTableData = computed(() => {
    let data = tableData.value || []

    if (currentTab.value === '自选') {
      data = data.filter((item) => watchlist.value.includes(item.fund_code))
    } else {
      // [AI-2026-07-09] 分类已简化，currentTab 即数据库 category 值，直接精确过滤
      data = data.filter((item) => item.category === currentTab.value)
    }

    if (searchKeyword.value) {
      const kw = searchKeyword.value.toLowerCase()
      data = data.filter(
        (item) =>
          (item.fund_code || '').toLowerCase().includes(kw) ||
          (item.fund_name || '').toLowerCase().includes(kw)
      )
    }

    // [AI-2026-10-07] QDII欧美：删表头排序后，改用固定分组顺序
    if (currentTab.value === 'QDII欧美' && data.length > 0) {
      const fallback = data.length + 1
      data = [...data].sort((a, b) => {
        const oa = QDII_OUMEI_FIXED_ORDER[a.fund_code] ?? fallback
        const ob = QDII_OUMEI_FIXED_ORDER[b.fund_code] ?? fallback
        return oa - ob
      })
    }

    return data
  })

  /** 刷新间隔（毫秒） */
  const refreshInterval = computed(() =>
    HIGH_FREQ_TABS.includes(currentTab.value) ? 3000 : 30000
  )

  // ---- actions ----
  // [2026-11-14] 写 DB（乐观更新 + 失败回滚），不再落 localStorage
  async function toggleWatchlist(code: string) {
    const idx = watchlist.value.indexOf(code)
    const adding = idx === -1
    if (adding) watchlist.value.push(code)
    else watchlist.value.splice(idx, 1)
    try {
      if (adding) {
        await fetch('/api/watchlist', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ fund_code: code })
        })
      } else {
        await fetch(`/api/watchlist/${code}`, { method: 'DELETE' })
      }
    } catch (e) {
      // 回滚
      const i = watchlist.value.indexOf(code)
      if (adding && i > -1) watchlist.value.splice(i, 1)
      else if (!adding && i === -1) watchlist.value.push(code)
      console.error('更新自选失败', e)
    }
  }

  function isInWatchlist(code: string) {
    return watchlist.value.includes(code)
  }

  async function fetchDashboard(isSilent = false, cancelPrevious = false) {
    if (dashboardInFlight && !cancelPrevious) return
    if (cancelPrevious && dashboardController) dashboardController.abort()
    dashboardInFlight = true
    dashboardController = new AbortController()
    const requestSeq = ++dashboardRequestSeq
    if (!isSilent && tableData.value.length === 0) loading.value = true
    try {
      const params: Record<string, string> = {}
      if (currentTab.value === '自选') {
        params.watchlist = watchlist.value.join(',')
      } else {
        params.category = currentTab.value
      }
      const res = await api.getDashboard(params, dashboardController.signal)
      if (requestSeq === dashboardRequestSeq && res.data?.status === 'ok') {
        tableData.value = res.data.data || []
        dashboardMeta.value = {
          updated_at: res.data.updated_at || null,
          stale: !!res.data.stale,
          compute_ms: Number(res.data.compute_ms || 0),
          error: res.data.error || null
        }
      }
    } catch (err: any) {
      if (err?.name !== 'CanceledError' && err?.code !== 'ERR_CANCELED') {
        console.error('获取看板数据失败', err)
      }
    } finally {
      dashboardInFlight = false
      loading.value = false
    }
  }

  // [AI-2026-07-09] 拉取数据库动态分类，驱动主看板 TAB 生成
  async function fetchCategories() {
    try {
      const res = await api.getCategories()
      if (res.data?.status === 'ok' && Array.isArray(res.data.data)) {
        dbCategories.value = res.data.data
      }
    } catch (err) {
      console.error('获取基金分类失败', err)
    }
  }

  async function fetchFundHistory(code: string) {
    fundHistoryLoading.value = true
    try {
      const res = await api.getFundHistory(code)
      if (res.data?.status === 'ok') {
        fundHistory.value = res.data.data || []
        // [AI-2026-10-09 方案A] 存锚点信号（后端 today/trading_today）
        fundHistoryToday.value = res.data.today ?? null
        fundHistoryTradingToday.value = typeof res.data.trading_today === 'boolean' ? res.data.trading_today : null
      }
    } catch (err) {
      console.error('获取历史数据失败', err)
      fundHistory.value = []
      fundHistoryToday.value = null
      fundHistoryTradingToday.value = null
    } finally {
      fundHistoryLoading.value = false
    }
  }

  async function fetchIntraday(code: string, date?: string) {
    try {
      const res = await api.getFundIntraday(code, date)
      if (res.data?.status === 'ok') {
        intradayData.value = res.data.data || []
      }
    } catch (err) {
      console.error('获取分时数据失败', err)
      intradayData.value = []
    }
  }

  async function fetchBasket(code: string) {
    try {
      const res = await api.getFundBasket(code)
      if (res.data?.status === 'ok') {
        basketData.value = res.data.data || []
      }
    } catch (err) {
      console.error('获取篮子权重失败', err)
      basketData.value = []
    }
  }

  function setTab(tab: string) {
    currentTab.value = tab
    localStorage.setItem('dashboard_tab', tab)
    // [V8.1] 不再清空 tableData — 保留旧数据让 filteredTableData 直接过滤，
    // 用户看到的是已缓存的分类数据而非空白转圈。
    // 后台静默刷新会在 watch(currentTab) 中触发 fetchData(true)
  }

  // 初始化：从 DB 加载自选（含一次性迁移旧 localStorage）
  loadWatchlist()

  return {
    tableData, loading, currentTab, searchKeyword,
    dashboardMeta,
    fundHistory, fundHistoryLoading, fundHistoryToday, fundHistoryTradingToday,
    intradayData, basketData,
    watchlist,
    filteredTableData, refreshInterval,
    dashboardTabs, fetchCategories,
    toggleWatchlist, isInWatchlist,
    fetchDashboard, fetchFundHistory, fetchIntraday, fetchBasket,
    setTab
  }
})
