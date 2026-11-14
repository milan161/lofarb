<template>
  <div class="silver-ratio-page">
    <n-card :bordered="false" class="shadow-soft" size="small">
      <template #header>
        <div style="display: flex; align-items: center; gap: 12px;">
          <n-icon size="20" color="#d97706"><TrendingUp /></n-icon>
          <span style="font-size: 16px; font-weight: bold; color: #d97706;">白银比价监控 (161226)</span>
          <n-tag :type="silverRatioData.length > 0 ? 'success' : 'warning'" size="small" round>
            {{ silverRatioData.length }} 条数据
          </n-tag>
        </div>
      </template>

      <p style="font-size: 12px; color: #64748b; margin: 0 0 12px 0;">
        比价公式: (AG_settle ÷ 1000 × 31.1035 ÷ USDCNY) ÷ SI_close &nbsp;|&nbsp;
        31.1035 = 金衡盎司/克转换系数 &nbsp;|&nbsp;
        汇率：在岸价 (USDCNY)，同步 Woody 算法
      </p>

      <div v-if="silverRatioLoading" style="text-align: center; padding: 40px; color: #999;">
        <n-spin size="small" />
        <span style="margin-left: 8px;">加载中...</span>
      </div>

      <template v-else>
        <!-- 比价趋势图 -->
        <div v-if="chartData.dates.length > 0" style="height: 300px; margin-bottom: 16px;">
          <v-chart :option="chartOption" autoresize style="height: 100%; width: 100%;" />
        </div>

        <!-- 数据表格 -->
        <div style="overflow-x: auto; max-height: 400px; overflow-y: auto;">
          <n-empty v-if="silverRatioData.length === 0" description="暂无比价数据" />
          <!-- [AI-2026-10-12 重做 表头统一蓝底/字体token] 表头行背景 #f8fafc→#edf3fb、表头文字→#33629c（tr 上设 color 继承给全部 th）；内联裸 monospace 字体 → var(--co-font-mono)；padding 6px 8px 不变 -->
          <!-- [AI-2026-10-12 重做] 旧 dist 形态：tr 去掉 color 继承，7 个 th 各自内联表头蓝（「比价」列保留 color+bold） -->
          <table v-else style="width: 100%; border-collapse: collapse; font-size: 12px; font-family: var(--co-font-mono);">
            <thead>
              <tr style="background: #edf3fb; border-bottom: 2px solid #e2e8f0; position: sticky; top: 0; z-index: 1;">
                <th style="padding: 6px 8px; text-align: left; color: #33629c;">日期</th>
                <th style="padding: 6px 8px; text-align: right; color: #33629c;">AG收盘(¥/kg)</th>
                <th style="padding: 6px 8px; text-align: right; color: #33629c;">成交量(手)</th>
                <th style="padding: 6px 8px; text-align: right; color: #33629c;">AG结算(¥/kg)</th>
                <th style="padding: 6px 8px; text-align: right; color: #33629c;">SI($/oz)</th>
                <th style="padding: 6px 8px; text-align: right; color: #33629c;">USDCNY(在岸)</th>
                <th style="padding: 6px 8px; text-align: right; color: #33629c; font-weight: bold;">比价</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, idx) in silverRatioData" :key="idx"
                  :style="{ background: row.ratio ? '#fffbeb' : '#f9fafb', borderBottom: '1px solid #f1f5f9' }">
                <td style="padding: 4px 8px;">{{ row.date ? row.date.substring(5) : '-' }}</td>
                <td style="padding: 4px 8px; text-align: right;">{{ row.ag_close != null ? row.ag_close.toFixed(2) : '-' }}</td>
                <td style="padding: 4px 8px; text-align: right;">{{ row.ag_volume != null ? row.ag_volume.toLocaleString() : '-' }}</td>
                <td style="padding: 4px 8px; text-align: right;">{{ row.ag_settle != null ? row.ag_settle.toFixed(2) : '-' }}</td>
                <td style="padding: 4px 8px; text-align: right;">{{ row.si_close != null ? row.si_close.toFixed(2) : '-' }}</td>
                <td style="padding: 4px 8px; text-align: right;">{{ row.usd_cny_spot != null ? row.usd_cny_spot.toFixed(4) : '-' }}</td>
                <td style="padding: 4px 8px; text-align: right; font-weight: bold; color: #d97706;">
                  {{ row.ratio != null ? row.ratio.toFixed(4) : '-' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { NCard, NTag, NIcon, NEmpty, NSpin } from 'naive-ui'
import { TrendingUp } from 'lucide-vue-next'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, GridComponent, LegendComponent, DataZoomComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { getSilverRatio } from '../api'

use([CanvasRenderer, LineChart, TitleComponent, TooltipComponent, GridComponent, LegendComponent, DataZoomComponent])

const silverRatioData = ref<any[]>([])
const silverRatioLoading = ref(false)

const chartData = computed(() => {
  const dates: string[] = []
  const ratios: number[] = []
  const agPrices: number[] = []
  const siPrices: number[] = []

  // 倒序：从旧到新
  const sorted = [...silverRatioData.value].reverse()
  for (const row of sorted) {
    if (row.ratio != null) {
      dates.push(row.date ? row.date.substring(5) : '')
      ratios.push(row.ratio)
      agPrices.push(row.ag_settle ?? row.ag_close ?? 0)
      siPrices.push(row.si_close ?? 0)
    }
  }
  return { dates, ratios, agPrices, siPrices }
})

const chartOption = computed(() => ({
  tooltip: {
    trigger: 'axis',
    backgroundColor: 'rgba(255,255,255,0.95)',
    borderColor: '#e2e8f0',
    textStyle: { fontSize: 12 },
    formatter: (params: any[]) => {
      if (!params || params.length === 0) return ''
      const date = params[0].axisValue
      let html = `<strong>${date}</strong><br/>`
      for (const p of params) {
        html += `${p.marker} ${p.seriesName}: ${p.value}`
        if (p.seriesName === '比价') html += 'x'
        if (p.seriesName === 'SI') html += '$'
        html += '<br/>'
      }
      return html
    }
  },
  legend: {
    data: ['比价', 'AG(¥/kg)', 'SI($/oz)'],
    top: 0,
    textStyle: { fontSize: 11 }
  },
  grid: { left: 50, right: 20, top: 30, bottom: 30 },
  xAxis: {
    type: 'category',
    data: chartData.value.dates,
    axisLabel: { rotate: 30, fontSize: 10 }
  },
  yAxis: [
    {
      type: 'value',
      name: '比价',
      nameTextStyle: { fontSize: 10 },
      axisLabel: { fontSize: 10 },
      splitLine: { lineStyle: { type: 'dashed', color: '#e2e8f0' } }
    },
    {
      type: 'value',
      name: '价格',
      nameTextStyle: { fontSize: 10 },
      axisLabel: { fontSize: 10 },
      splitLine: { show: false }
    }
  ],
  dataZoom: [
    { type: 'inside', start: 0, end: 100 },
    { type: 'slider', start: 0, end: 100, height: 20, bottom: 0 }
  ],
  series: [
    {
      name: '比价',
      type: 'line',
      data: chartData.value.ratios,
      smooth: true,
      symbol: 'circle',
      symbolSize: 4,
      lineStyle: { width: 2, color: '#d97706' },
      itemStyle: { color: '#d97706' },
      areaStyle: {
        color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: 'rgba(217,119,6,0.25)' }, { offset: 1, color: 'rgba(217,119,6,0.02)' }] }
      }
    },
    {
      name: 'SI($/oz)',
      type: 'line',
      yAxisIndex: 1,
      data: chartData.value.siPrices,
      smooth: true,
      symbol: 'none',
      lineStyle: { width: 1, color: '#3b82f6', type: 'dashed' },
      itemStyle: { color: '#3b82f6' }
    },
    {
      name: 'AG(¥/kg)',
      type: 'line',
      yAxisIndex: 1,
      data: chartData.value.agPrices,
      smooth: true,
      symbol: 'none',
      lineStyle: { width: 1, color: '#10b981', type: 'dashed' },
      itemStyle: { color: '#10b981' }
    }
  ]
}))

const fetchSilverRatio = async () => {
  silverRatioLoading.value = true
  try {
    const res = await getSilverRatio()
    if (res.data.status === 'ok') {
      silverRatioData.value = res.data.data || []
    }
  } catch (e) {
    silverRatioData.value = []
  } finally {
    silverRatioLoading.value = false
  }
}

onMounted(() => {
  fetchSilverRatio()
})
</script>

<style scoped>
/* [AI-2026-11-14] 加 height/overflow：本组件既可能独立成页，也被塞进 n-modal（高 85vh）。
   固定高度 + 内部滚动，弹窗才不会撑出视口、看起来像常驻卡片。 */
.silver-ratio-page { padding: 12px; height: 100%; overflow-y: auto; box-sizing: border-box; }
.shadow-soft { box-shadow: 0 4px 20px rgba(0, 0, 0, 0.04); border-radius: 12px; }
</style>
