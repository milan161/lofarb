/**
 * 基金数据 API
 */
import client from './client'

/** 看板统一数据 */
export function getDashboard(
  params?: { watchlist?: string; category?: string },
  signal?: AbortSignal
) {
  return client.get('/api/dashboard', { params, signal })
}

/** 基金历史对账数据 */
export function getFundHistory(code: string) {
  return client.get(`/api/fund/${code}/history`)
}

/** [AI-2026-08-04] 单基金「核对静态估值」：补采近 days 个交易日价格/净值（级联底层ETF日价）并重算静态估值 */
export function reconcileStaticVal(code: string, days: number = 10) {
  return client.post(`/api/fund/${code}/reconcile_static_val?days=${days}`)
}

/** 动态基金分类（主看板 TAB 用） */
export function getCategories() {
  return client.get('/api/config/categories')
}

/** 基金分时数据（曲线图用，支持多日） */
export function getFundIntraday(code: string, date?: string, days?: number) {
  return client.get(`/api/fund/${code}/intraday`, { params: { date, days } })
}

/** 基金篮子权重 */
export function getFundBasket(code: string) {
  return client.get(`/api/fund/${code}/basket`)
}

/** 基金估值元数据（深度分析页用） */
export function getFundValuationMeta(code: string) {
  return client.get(`/api/fund/${code}/valuation_meta`)
}

/** 季报持仓分析：可用报告期列表 */
export function getFundHoldingPeriods(code: string) {
  return client.get(`/api/fund/${code}/holding-periods`)
}

/** 季报持仓分析：某报告期持仓明细、地区分布、变动 */
export function getFundHoldings(code: string, period: string) {
  return client.get(`/api/fund/${code}/holdings`, { params: { period } })
}

/** 季报持仓分析：季报持仓法实时估值 */
export function getFundHoldingValuation(code: string, period: string) {
  return client.get(`/api/fund/${code}/holding-valuation`, { params: { period } })
}

/** 季报持仓分析：持仓静态估值核心列（读本地缓存 holding_static_val，B方案） */
export function getFundHoldingRecalc(code: string, period: string = '2026H1', start: string = '2026-07-01') {
  return client.get(`/api/fund/${code}/holding-recalc`, { params: { period, start } })
}

/** 季报持仓分析：持仓静态估值全量诊断（etf_prices/fill_warning/note，按需从 ARM 取） */
export function getFundHoldingRecalcDetail(code: string, period: string = '2026H1', start: string = '2026-07-01') {
  return client.get(`/api/fund/${code}/holding-recalc-detail`, { params: { period, start } })
}

/** B方案：手动触发 本地←ARM 拉取原油三基金 holding_static_val */
export function syncOilStatic(code: string) {
  return client.post(`/api/fund/${code}/sync-oil-static`)
}

/** 季报持仓分析：持仓实时估值（Model B，季报持仓法 + CL 期货实时价） */
export function getFundHoldingRealtime(code: string) {
  return client.get(`/api/fund/${code}/holding-realtime`)
}

/** 对冲穿透：底层 ETF 实际持有合约月 + 归一化 CL 对冲分布（对冲页表1/表2） */
export function getFundHedgeExposure(code: string) {
  return client.get(`/api/fund/${code}/hedge-exposure`)
}

/** 从 ARM 拉 CL 冻结价到本地（盘前手动触发一次即可） */
export function syncFuturesFreeze() {
  return client.post(`/api/fund/sync-freeze`)
}

/** 从 ARM 拉美股/伦敦/港股 ETF 日 K（usa_etf_daily_prices）全表到本地（手动触发） */
export function syncUsaEtf() {
  return client.post(`/api/fund/sync-usa-etf`)
}

// [AI-2026-09-24 东哥需求] ARM 美股价新鲜度检测（只读）：ARM 是否已抓到新浪口径的最新美股收盘日
export function getOilPriceFreshness() {
  return client.get(`/api/fund/oil-price-freshness`)
}

// [AI-2026-09-24 东哥需求] 一键闭环：本地重抓新浪 → 推 ARM → ARM 重算 → 拉回本地
export function refetchOilPrices() {
  return client.post(`/api/fund/oil-refetch-prices`)
}

/** 市场概览（汇率、活跃数据源、统计） */
export function getMarketOverview() {
  return client.get('/api/market/overview')
}

/** 单只标的实时行情 */
export function getRealtimeQuote(code: string) {
  return client.get(`/api/market/realtime/${code}`)
}

/** 历史净值 */
export function getHistoricalNav(code: string, startDate?: string) {
  return client.get(`/api/market/historical/nav/${code}`, { params: { start_date: startDate } })
}

/** 历史价格 */
export function getHistoricalPrice(code: string, startDate?: string) {
  return client.get(`/api/market/historical/price/${code}`, { params: { start_date: startDate } })
}

/** 债券ETF - 设置手动BP覆盖 */
export function postBpOverride(code: string, bp7y: number, bp10y: number) {
  return client.post('/api/bond/bp-override', { code, bp_7y: bp7y, bp_10y: bp10y })
}

/** 债券ETF - 获取今日BP覆盖 */
export function getBpOverride(code: string) {
  return client.get('/api/bond/bp-override', { params: { code } })
}

/** 债券ETF - 清除BP覆盖 */
export function clearBpOverride(code: string) {
  return client.post('/api/bond/bp-override/clear', { code })
}

/** 白银比价数据（161226 沪银/SI 比价） */
export function getSilverRatio() {
  return client.get('/api/silver/ratio')
}

/** 底层资产穿透分析 */
export function getFundPenetration(code: string, period: string) {
  return client.get(`/api/penetration/${code}/${period}`)
}

/**
 * [AI-2026-08-05] 单基金实时估值封装入口（ETF/篮子），包后端 analyze_realtime。
 * 供沙盘估值计算器 / LazyMode 统一走 canonical 引擎。
 */
export function getRealtimeCalc(params: {
  code: string
  lof_price?: number
  fx?: number
  etfs?: string
  lof_qty?: number
}) {
  return client.get('/api/funds/realtime_calc', { params })
}

/**
 * [AI-2026-08-05] 单基金期货估值封装入口（期货校准 calib / 纯期货 pure），包后端
 * analyze_realtime_futures / analyze_realtime_pure_futures。
 */
export function getRealtimeFuturesCalc(params: {
  code: string
  mode?: string
  futures_price: number
  calibration?: number
  lof_price?: number
  fx?: number
  lof_qty?: number
}) {
  return client.get('/api/funds/realtime_futures_calc', { params })
}
