<template>
  <div class="holding-analysis-page">
    <n-card :bordered="false" class="shadow-soft" size="small">
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
          <div style="display: flex; align-items: center; gap: 12px;">
            <!-- [AI-2026-10-12 重做 返回上一页] 原 router.push('/dashboard') 硬跳主看板，改为返回上一页（从原油穿透进入则回到原油穿透） -->
            <n-button text size="small" @click="router.back()" style="color: #64748b; padding: 0 4px;">
              ← 返回上一页
            </n-button>
            <n-icon size="20" color="#2563eb"><PieChart /></n-icon>
            <span style="font-size: 16px; font-weight: bold;">季报持仓分析</span>
            <n-tag size="small" type="info">{{ fundCode }}</n-tag>
            <span style="font-size: 14px; color: #475569;">{{ fundName || fundCode }}</span>
          </div>
          <div style="display: flex; align-items: center; gap: 8px;">
            <span style="font-size: 12px; color: #64748b;">T-1 持仓静态估值:</span>
            <n-tag v-if="staticSynced" size="small" :bordered="false" type="success">
              已同步（最新 {{ staticLatestDate }}）
            </n-tag>
            <n-tag v-else size="small" :bordered="false" type="warning">未同步</n-tag>
            <n-button size="small" :loading="staticSyncLoading" @click="syncStaticValuation">
              同步静态持仓估值
            </n-button>
          </div>
        </div>
      </template>

      <n-alert v-if="syncAlert" type="warning" :show-icon="true" style="margin-bottom: 12px;">
        {{ syncAlert }}
      </n-alert>
      <n-alert
        v-if="staticSyncMsg"
        :type="staticSyncMsg.startsWith('✅') ? 'success' : 'error'"
        :show-icon="true"
        style="margin-bottom: 12px;"
      >
        {{ staticSyncMsg }}
      </n-alert>

      <!-- [AI-2026-09-24 东哥需求] 美股价新鲜度：ARM 缺「新浪口径最新交易日」收盘价时提示 + 一键重抓推送 -->
      <n-alert
        v-if="priceFresh && priceFresh.stale"
        type="warning"
        :show-icon="true"
        style="margin-bottom: 12px;"
      >
        <div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap;">
          <div style="font-size: 12px; line-height: 1.7; color: #92400e;">
            新浪已更新 <strong>{{ priceFresh.reference_date }}</strong> 美股收盘价，但 ARM 最新只到
            <strong>{{ priceFresh.arm_clock || priceFresh.arm_latest || '-' }}</strong>
            → 静态估值不会起该日新行（宁缺毋假）。
            <span v-if="priceFresh.missing_auto && priceFresh.missing_auto.length">
              <br>参考日缺价：{{ priceFresh.missing_auto.join('、') }}
            </span>
          </div>
          <n-button size="small" type="warning" :loading="refetchLoading" @click="refetchUsPrices">
            重新从数据源抓取并推送到 ARM
          </n-button>
        </div>
      </n-alert>
      <n-alert
        v-if="refetchMsg"
        :type="refetchMsg.startsWith('✅') ? 'success' : 'error'"
        :show-icon="true"
        style="margin-bottom: 12px;"
      >
        {{ refetchMsg }}
      </n-alert>

      <div v-if="loading" style="text-align: center; padding: 40px; color: #999;">
        <n-spin size="small" />
        <span style="margin-left: 8px;">加载中...</span>
      </div>

      <n-empty v-else-if="error" :description="error" style="padding: 40px;" />

      <template v-else-if="holdings.length > 0">
        <!-- 顶部概览：报告日期 + 披露持仓数 + 地区分布 + 总权重 + 底层期货 -->
        <n-grid :cols="24" :x-gap="12" :y-gap="12" style="margin-bottom: 16px;">
          <n-gi :span="4">
            <n-card size="small" class="stat-card" content-style="padding: 10px;">
              <div style="font-size: 11px; color: #64748b;">报告截止日</div>
              <div style="font-size: 16px; font-weight: bold; color: #1e293b;">{{ reportDate || '-' }}</div>
            </n-card>
          </n-gi>
          <n-gi :span="4">
            <n-card size="small" class="stat-card" content-style="padding: 10px;">
              <div style="font-size: 11px; color: #64748b;">披露持仓数</div>
              <div style="font-size: 16px; font-weight: bold; color: #1e293b;">{{ holdings.length }} 只</div>
            </n-card>
          </n-gi>
          <n-gi :span="5">
            <n-card size="small" class="stat-card" content-style="padding: 10px;">
              <div style="font-size: 11px; color: #64748b; margin-bottom: 4px;">地区分布</div>
              <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                <div v-for="r in regionDistribution" :key="r.region" style="display: flex; align-items: center; gap: 3px;">
                  <span style="font-weight: 600; color: #1e293b; font-size: 11px;">{{ regionLabel(r.region) }}</span>
                  <span style="font-size: 13px; font-weight: bold; color: #2563eb;">{{ r.pct.toFixed(1) }}%</span>
                </div>
              </div>
            </n-card>
          </n-gi>
          <n-gi :span="5">
            <n-card size="small" class="stat-card" content-style="padding: 10px;">
              <div style="font-size: 11px; color: #64748b;">总权重（前十大）</div>
              <div style="font-size: 18px; font-weight: bold; color: #1e293b;">{{ totalWeight.toFixed(2) }}%</div>
            </n-card>
          </n-gi>
          <n-gi :span="6">
            <n-card
              size="small"
              class="stat-card"
              content-style="padding: 10px; cursor: pointer;"
              style="transition: background 0.2s;"
              :style="penetrationReady ? { background: '#f0f9ff', border: '1px solid #bae6fd' } : { background: '#f8fafc' }"
              @click="penetrationReady ? goToPenetration() : null"
              @mouseenter="penetrationReady = true"
              @mouseleave="penetrationReady = false"
            >
              <div style="font-size: 11px; color: #64748b; margin-bottom: 4px;">底层期货（点击穿透）</div>
              <div v-if="penetrationData" style="display: flex; gap: 12px; flex-wrap: wrap;">
                <div>
                  <div style="font-size: 15px; font-weight: bold; color: #ea580c;">{{ wtiPct.toFixed(1) }}%</div>
                  <div style="font-size: 10px; color: #64748b;">WTI (CL)</div>
                </div>
                <div>
                  <div style="font-size: 15px; font-weight: bold; color: #7c3aed;">{{ brentPct.toFixed(1) }}%</div>
                  <div style="font-size: 10px; color: #64748b;">Brent (B)</div>
                </div>
                <div>
                  <div style="font-size: 15px; font-weight: bold; color: #16a34a;">{{ penetratedPct.toFixed(1) }}%</div>
                  <div style="font-size: 10px; color: #64748b;">已穿透</div>
                </div>
              </div>
              <div v-else style="font-size: 13px; color: #94a3b8;">—</div>
            </n-card>
          </n-gi>
        </n-grid>

        <!-- 持仓实时估值 -->
        <n-card v-if="valuation" size="small" class="shadow-soft" style="margin-bottom: 16px; background: #f8fafc;">
          <template #header>
            <div style="font-size: 14px; font-weight: bold;">持仓实时估值（CL 合约月 · 近月±1）</div>
          </template>
          <n-grid :cols="24" :x-gap="12" :y-gap="12">
            <n-gi :span="4">
              <div style="font-size: 12px; color: #64748b;">
                最新净值<span v-if="latestNavDate" style="font-size: 11px; color: #94a3b8;">（{{ latestNavDate }}）</span>
              </div>
              <div style="font-size: 18px; font-weight: bold; color: #1e293b;">
                {{ latestNav != null ? latestNav.toFixed(4) : '-' }}
              </div>
            </n-gi>
            <!-- 实时估值：选中合约蓝字大 + 其他合约灰字小 -->
            <n-gi :span="4" style="cursor: pointer;" @click="openRealtimeModal">
              <div style="font-size: 12px; color: #64748b;">持仓实时估值</div>
              <template v-if="cSel">
                <div style="font-size: 18px; font-weight: bold; line-height: 1.25; color: #2563eb;">
                  {{ cSel.hrv != null ? cSel.hrv.toFixed(4) : '缺失' }}<span style="font-size: 11px; font-weight: normal; margin-left: 2px;">{{ contractMonthLabel(hedgeContract) }}</span>
                </div>
                <div v-for="oc in otherContracts" :key="oc" style="font-size: 13px; font-weight: bold; line-height: 1.25; color: #94a3b8;">
                  {{ cData(oc)?.hrv != null ? cData(oc)!.hrv.toFixed(4) : '-' }}<span style="font-size: 10px; font-weight: normal; margin-left: 2px;">{{ contractMonthLabel(oc) }}</span>
                </div>
              </template>
              <div v-else style="font-size: 18px; font-weight: bold; color: #94a3b8;">-</div>
              <div style="font-size: 10px; color: #2563eb; margin-top: 2px;">点击核对CL分母</div>
            </n-gi>
            <n-gi :span="4">
              <div style="font-size: 12px; color: #64748b;">实时ETF现价</div>
              <div style="font-size: 18px; font-weight: bold;" :style="{ color: cSel && cSel.lof_price != null ? '#dc2626' : '#94a3b8' }">
                {{ cSel && cSel.lof_price != null ? cSel.lof_price.toFixed(3) : '-' }}
              </div>
              <div style="font-size: 10px; color: #94a3b8; margin-top: 2px;">
                {{ lofSrcLabel(cSel && cSel.lof_price_source) }}
              </div>
            </n-gi>
            <!-- 实时持仓溢价：选中蓝 + 其他灰 -->
            <n-gi :span="4">
              <div style="font-size: 12px; color: #64748b;">实时持仓溢价</div>
              <template v-if="cSel">
                <div style="font-size: 18px; font-weight: bold; line-height: 1.25;" :style="{ color: priceColor((cSel.realtime_premium || 0) * 100) }">
                  {{ cSel.realtime_premium != null ? formatPercent(cSel.realtime_premium * 100, 3) : '-' }}<span style="font-size: 11px; font-weight: normal; margin-left: 2px;">{{ contractMonthLabel(hedgeContract) }}</span>
                </div>
                <div v-for="oc in otherContracts" :key="'p'+oc" style="font-size: 13px; font-weight: bold; line-height: 1.25;" :style="{ color: priceColor((cData(oc)?.realtime_premium || 0) * 100) }">
                  {{ cData(oc)?.realtime_premium != null ? formatPercent(cData(oc)!.realtime_premium * 100, 3) : '-' }}<span style="font-size: 10px; font-weight: normal; margin-left: 2px;">{{ contractMonthLabel(oc) }}</span>
                </div>
              </template>
              <div v-else style="font-size: 18px; font-weight: bold; color: #94a3b8;">-</div>
              <div style="font-size: 10px; color: #94a3b8; margin-top: 2px;">现价/实时估值-1</div>
            </n-gi>
            <!-- 累计涨跌贡献：选中蓝 + 其他灰 -->
            <n-gi :span="4">
              <div style="font-size: 12px; color: #64748b;">累计涨跌贡献</div>
              <template v-if="cSel">
                <div style="font-size: 18px; font-weight: bold; line-height: 1.25;" :style="{ color: priceColor((cSel.total_change_pct || 0) * 100) }">
                  {{ cSel.total_change_pct != null ? formatPercent(cSel.total_change_pct * 100, 2) : '-' }}<span style="font-size: 11px; font-weight: normal; margin-left: 2px;">{{ contractMonthLabel(hedgeContract) }}</span>
                </div>
                <div v-for="oc in otherContracts" :key="'t'+oc" style="font-size: 13px; font-weight: bold; line-height: 1.25;" :style="{ color: priceColor((cData(oc)?.total_change_pct || 0) * 100) }">
                  {{ cData(oc)?.total_change_pct != null ? formatPercent(cData(oc)!.total_change_pct * 100, 2) : '-' }}<span style="font-size: 10px; font-weight: normal; margin-left: 2px;">{{ contractMonthLabel(oc) }}</span>
                </div>
              </template>
              <div v-else style="font-size: 18px; font-weight: bold; color: #94a3b8;">-</div>
            </n-gi>
            <n-gi :span="4">
              <div style="font-size: 12px; color: #64748b;">有效权重覆盖</div>
              <div style="font-size: 18px; font-weight: bold; color: #1e293b;">
                {{ cSel && cSel.valid_weight_sum != null ? (cSel.valid_weight_sum * 100).toFixed(2) + '%' : '-' }}
              </div>
            </n-gi>
          </n-grid>
          <div v-if="cSel && cSel.components && cSel.components.some((c: any) => c.status !== 'ok')" style="margin-top: 10px; font-size: 11px; color: #64748b;">
            注：部分底层标的缺少报告日收盘价或实时行情，仅使用有效标的计算估值。
          </div>
        </n-card>

        <!-- [AI-2026-09-30 东哥需求] 持仓静态估值常驻主页面（原为「点击最新净值」弹窗）
             前十大持仓 / 本期退出·新进前十 三个月才变一次，已收进二级弹窗「季报仓位跟踪」 -->
        <n-card size="small" class="shadow-soft" style="margin-bottom: 16px;">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;">
              <div style="display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;">
                <div style="font-size: 14px; font-weight: bold; white-space: nowrap;">持仓静态估值（7-1 至今）</div>
                <div style="font-size: 11px; color: #94a3b8; line-height: 1.35;">
                  <div>T-1估值(D) = T-2净值 × (1 + Σᵢ 占净值ᵢ × 人民币口径涨跌ᵢ)</div>
                  <div>人民币口径涨跌ᵢ = (1 + 标的涨跌ᵢ[前复权]) × (1 + 汇率涨跌ᵢ) − 1</div>
                </div>
              </div>
              <div style="display: flex; align-items: center; gap: 8px;">
                <!-- [AI-2026-10-05 东哥需求] 导出人工核对用 CSV（汇率两日原值 + 底层 ETF 明细 + Excel 公式） -->
                <n-button
                  size="small"
                  type="primary"
                  ghost
                  :disabled="!recalcDisplayRows.length"
                  title="导出核对表：一行一日期，含各腿两日 ETF 收盘价/两日汇率原值/权重，派生列全为 Excel 公式"
                  @click="exportStaticValuationCsv"
                >
                  导出历史静态估值 CSV
                </n-button>
                <n-button size="small" @click="trackingModalShow = true">季报仓位跟踪</n-button>
              </div>
            </div>
          </template>
          <n-empty v-if="!recalcDisplayRows.length" description="该基金暂无持仓静态估值数据" />
          <n-data-table
            v-else
            :columns="recalcColumns"
            :data="recalcDisplayRows"
            :row-key="(row: any) => row.date"
            :pagination="{ pageSize: 15 }"
            :row-class-name="(row: any) => (row.pending ? 'recalc-pending-row' : (row.fill_warning ? 'recalc-warn-row' : (row.carried_forward ? 'recalc-carry-row' : '')))"
            size="small"
            bordered
          />
          <div v-if="recalcDetailLoading" style="font-size: 11px; color: #94a3b8; margin-top: 6px;">
            诊断细节加载中（底层 ETF / 缺价标注）…
          </div>
          <div
            v-if="staticCsvMsg"
            :style="{ fontSize: '11px', marginTop: '6px', color: staticCsvMsg.startsWith('✅') ? '#16a34a' : '#dc2626' }"
          >
            {{ staticCsvMsg }}
          </div>
        </n-card>
      </template>

      <n-empty v-else description="暂无持仓数据" style="padding: 40px;" />
    </n-card>

    <!-- [AI-2026-09-30 东哥需求] 季报仓位跟踪（二级弹窗）：前十大持仓 + 本期退出/新进前十。
         这些数据每季度才变一次，平时收起，不占用主页面空间。格式与数据维持原状。 -->
    <n-modal
      v-model:show="trackingModalShow"
      preset="card"
      :title="fundCode + ' 季报仓位跟踪'"
      style="width: 1000px; max-width: 94vw;"
    >
      <n-card size="small" class="shadow-soft" style="margin-bottom: 16px;">
        <template #header>
          <div style="display: flex; align-items: center; justify-content: space-between; gap: 12px;">
            <div style="font-size: 14px; font-weight: bold;">前十大持仓</div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; color: #64748b;">报告期:</span>
              <n-button
                v-for="p in periods"
                :key="p.period"
                size="small"
                type="default"
                :style="currentPeriod === p.period
                  ? { background: '#16a34a !important', borderColor: '#16a34a !important', color: '#fff !important', fontWeight: 'bold' }
                  : { background: 'transparent !important', borderColor: '#cbd5e1 !important', color: '#64748b !important' }"
                @click="switchPeriod(p.period)"
              >
                {{ p.period }}
              </n-button>
            </div>
          </div>
        </template>
        <n-data-table
          :columns="holdingColumns"
          :data="holdings"
          :summary="holdingSummary"
          size="small"
          bordered
          :pagination="false"
          style="max-height: 500px;"
        />
      </n-card>

      <!-- 退出 / 新进前十 — 并排（只要有上期就显示，列表为空则提示"无"，让用户看清朝季度持仓无变化） -->
      <n-grid :cols="24" :x-gap="12" :y-gap="12">
        <n-gi :span="12" v-if="prevPeriod">
          <n-card size="small" class="shadow-soft" style="background: #fff7ed;">
            <template #header>
              <div style="font-size: 13px; font-weight: bold; color: #9a3412;">本期已退出前十（上期 {{ prevPeriod }}）</div>
            </template>
            <div v-for="(item, idx) in exited" :key="idx" style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #ffedd5;">
              <span style="font-size: 12px;">
                <span v-if="item.symbol" style="font-weight: 600; margin-right: 6px;">{{ item.symbol }}</span>
                {{ item.name }}
              </span>
              <span style="font-size: 12px; color: #64748b;">{{ item.weight != null ? (item.weight * 100).toFixed(2) + '%' : '-' }}</span>
            </div>
            <div v-if="exited.length === 0" style="font-size: 12px; color: #94a3b8; padding: 6px 0;">无</div>
          </n-card>
        </n-gi>
        <n-gi :span="12" v-if="prevPeriod">
          <n-card size="small" class="shadow-soft" style="background: #f0fdf4;">
            <template #header>
              <div style="font-size: 13px; font-weight: bold; color: #166534;">本期新进前十（上期 {{ prevPeriod }}）</div>
            </template>
            <div v-for="(item, idx) in newIn" :key="idx" style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #dcfce7;">
              <span style="font-size: 12px;">
                <span v-if="item.symbol" style="font-weight: 600; margin-right: 6px;">{{ item.symbol }}</span>
                {{ item.name }}
              </span>
              <span style="font-size: 12px; color: #64748b;">{{ item.weight != null ? (item.weight * 100).toFixed(2) + '%' : '-' }}</span>
            </div>
            <div v-if="newIn.length === 0" style="font-size: 12px; color: #94a3b8; padding: 6px 0;">无</div>
          </n-card>
        </n-gi>
      </n-grid>
    </n-modal>

    <!-- 持仓实时估值 - CL 分母核对弹窗（全合约对比，不单选切换） -->
    <n-modal
      v-model:show="realtimeModalShow"
      preset="card"
      style="width: 900px; max-width: 96vw;"
    >
      <template #header>
        {{ fundCode }} 对冲（全合约对比）<span
          v-if="modalPriceSeg"
          style="color: #1d4ed8; font-weight: bold; margin-left: 4px;"
        > · 现价 {{ modalPriceSeg.price }}{{ modalPriceSeg.src ? `（${modalPriceSeg.src}）` : '' }}</span>
      </template>
      <div v-if="valuation" style="font-size: 13px;">
        <!-- [AI-2026-09-17] 实时估值 / 溢价 / 反算：全 CL 合约同屏对比（不再单选切换），便于手动 Excel 核对 -->
        <div style="font-size: 13px; font-weight: bold; margin: 4px 0 6px;">实时估值 · 溢价 · 反算对比（全 CL 合约同屏）</div>
        <n-grid :cols="24" :x-gap="12" :y-gap="8" style="margin-bottom: 8px;">
          <n-gi :span="9">
            <div style="font-size: 12px; color: #64748b;">做空 MCL 手数（统一用于下方反算）</div>
            <n-input-number v-model:value="mclLots" :min="0" :step="1" size="small" style="width: 100%;" />
          </n-gi>
          <n-gi :span="15" style="font-size: 11px; color: #94a3b8; align-self: end;">
            合约按 YYMM 排（2611/2612/2701）；Brent 2611/2701 敞口以同月 CL 价跨品种对冲，并入对应 CL 合约行（标「Brent同月」）。实时价弹窗打开期间每 20 秒自动刷新，关闭即停。
          </n-gi>
        </n-grid>
        <!-- [AI-2026-09-18] 基础数据条：β/基准日/基准静态估值 对所有合约相同，从对比表抽出只显示一次 -->
        <div style="font-size: 12px; color: #475569; background: #f1f5f9; border-radius: 4px; padding: 6px 10px; margin-bottom: 8px;">
          <span style="font-weight: bold; color: #1e293b;">基础数据：</span>
          β(仓位) <span style="font-weight: bold;">{{ firstContract?.valid_weight_sum != null ? (firstContract!.valid_weight_sum * 100).toFixed(2) + '%' : '-' }}</span>
          　｜　基准日 <span style="font-weight: bold;">{{ firstContract?.base_date || firstContract?.freeze_trade_date || '-' }}</span>
          　｜　基准静态估值 <span style="font-weight: bold;">{{ firstContract?.base_nav != null ? firstContract!.base_nav.toFixed(4) : '-' }}</span>
        </div>
        <n-table :single-line="false" size="small" style="margin-bottom: 8px;">
          <thead>
            <tr>
              <th>对冲合约</th>
              <th>CL 实时价</th>
              <th>持仓实时估值</th>
              <th>实时持仓溢价</th>
              <th>每手MCL→份数</th>
              <th>{{ mclLots }}手→份数</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="k in contractKeys" :key="k">
              <td style="font-weight: bold; white-space: nowrap;">
                <n-tooltip trigger="hover" placement="top-start">
                  <template #trigger>
                    <span style="color: #2563eb; cursor: pointer; text-decoration: underline dotted #93c5fd;" @click="openCompareModal(k)">CL {{ k }}（{{ contractMonthLabel(k) }}<span v-if="brentMonthSet.has(k)" style="color: #d85a30;"> · Brent同月</span>）</span>
                  </template>
                  3个原油LOF对比（160723 / 161129 / 501018 同合约月套利对比，点击进入）
                </n-tooltip>
              </td>
              <td>
                <div style="font-size: 14px; font-weight: bold;">{{ cData(k)?.cl_now != null ? cData(k)!.cl_now.toFixed(3) : '-' }}</div>
                <div style="font-size: 10px; color: #94a3b8;">{{ cData(k)?.cl_time || '' }}</div>
              </td>
              <td style="font-size: 14px; font-weight: bold; color: #2563eb;">{{ cData(k)?.hrv != null ? cData(k)!.hrv.toFixed(4) : '-' }}</td>
              <td :style="{ fontSize: '14px', fontWeight: 'bold', color: cData(k)?.realtime_premium != null ? (cData(k)!.realtime_premium >= 0 ? '#dc2626' : '#16a34a') : '#1e293b' }">{{ cData(k)?.realtime_premium != null ? (cData(k)!.realtime_premium * 100).toFixed(3) + '%' : '-' }}</td>
              <td>{{ lofSharesPerLot(k) != null ? lofSharesPerLot(k)!.toLocaleString() : '-' }}</td>
              <td style="font-size: 14px; font-weight: bold; color: #16a34a;">{{ lofSharesForMclOf(k) != null ? lofSharesForMclOf(k)!.toLocaleString() : '-' }}</td>
            </tr>
          </tbody>
        </n-table>
        <!-- [AI-2026-09-21] 混合估值（WTI 动态加权）：抹平"单月 vs 混合持仓"基差残余；与进阶对冲 68/32 在 2611/2612 两合约分仓配对即根治 WTI 残差（"两"指两个合约月，非2手；实盘整手下需放大规模或接受近似） -->
        <div v-if="hedgeExposure?.hedge_plan && mixedValuation != null" style="background: #ecfeff; border: 1px solid #67e8f9; border-radius: 6px; padding: 10px 12px; margin-bottom: 8px;">
          <div style="font-size: 13px; font-weight: bold; color: #0e7490;">
            混合估值（WTI {{ hedgeExposure.hedge_plan.months.join('+') }} 按 {{ hedgeExposure.hedge_plan.weights.join('/') }} 动态加权）＝
            <span style="font-size: 15px;">{{ mixedValuation.toFixed(4) }}</span>
            <span style="margin-left: 14px; color: {{ mixedPremium != null ? (mixedPremium >= 0 ? '#dc2626' : '#16a34a') : '#1e293b' }};">对应实时持仓溢价 {{ mixedPremium != null ? (mixedPremium * 100).toFixed(3) + '%' : '-' }}</span>
          </div>
          <div style="font-size: 11px; color: #64748b; margin-top: 4px;">
            把 WTI 内部 {{ hedgeExposure.hedge_plan.months.join('/') }} 比例还原，抹平"单月估值 vs 基金真实混合持仓"的基差残余（Brent 部分微扰可忽略）。与进阶对冲方案 68/32 在 2611/2612 两合约分仓配对即根治 WTI 基差残余（"两"指两个合约月、非2手；实盘整手约束下精确配比难落地，当前单月对冲仅作更接近真值的参考）。
          </div>
        </div>
        <div style="font-size: 11px; color: #94a3b8; margin-bottom: 10px;">
          公式：每手份数 = 100桶 × CL实时价 × 中间价(usd_cny_mid) ÷ (实时估值 × β)；N手 = 每手 × 手数。Brent 同月行与对应 WTI 行 CL 价/估值相同（同月跨品种对冲），仅对冲用途不同；Brent 2701 需 2701 冻结价（今夜 ARM 采样后生效，落地前该格显 -）。
        </div>

        <!-- [AI-2026-09-17] CL 冻结价（ARM 采样，分母）：矩阵，上移到穿透表前（每天变的数据集中顶部）
             [AI-2026-09-26] 新增 0230 = 东京 TSE 收盘 15:30 JST，仅 501018 日本腿（1671/1699 共 11.04%）
             用它做 Model B 分母；其余腿沿用 1130(Brent)/1430(CRUD)/1600(美股)，不受影响。 -->
        <div style="font-size: 13px; font-weight: bold; margin: 8px 0 6px;">CL 冻结价（ARM 采样，分母）</div>
        <div style="font-size: 11px; color: #94a3b8; margin-bottom: 6px;">
          0230 = 东京 TSE 收盘（15:30 JST，仅 501018 日本腿用）；1130 = LSE 收盘；1430 = CME 结算；1600 = NYSE 收盘。
        </div>
        <n-table :single-line="false" size="small" style="margin-bottom: 8px;">
          <thead>
            <tr><th>时点(NY)</th><th v-for="k in contractKeys" :key="k">CL {{ k }}（{{ contractMonthLabel(k) }}）</th></tr>
          </thead>
          <tbody>
            <tr v-for="pt in ['0230', '1130', '1430', '1600']" :key="pt">
              <td>{{ pt }}</td>
              <td v-for="k in contractKeys" :key="k">
                <div>{{ (cData(k)?.freeze_points && cData(k)!.freeze_points[pt]) ? cData(k)!.freeze_points[pt].price : '-' }}</div>
                <div style="font-size: 10px; color: #94a3b8;">{{ (cData(k)?.freeze_points && cData(k)!.freeze_points[pt]) ? cData(k)!.freeze_points[pt].trade_date : '' }}</div>
              </td>
            </tr>
          </tbody>
        </n-table>

        <!-- FX 实时（每天变，随冻结价上移） -->
        <div style="font-size: 12px; color: #64748b; margin-bottom: 10px;">
          今日中间价(usd_cny_mid, DB): {{ firstContract?.fx_now != null ? firstContract!.fx_now.toFixed(4) : '-' }} ｜
          汇率基准: {{ firstContract?.fx_point != null ? firstContract!.fx_point.toFixed(4) : '-' }} ｜
          状态: {{ firstContract?.fx_status }}
        </div>

        <!-- [AI-2026-09-17] 对冲穿透两表：静态估值(ETC市价)≠对冲(期货月份匹配)，两个独立问题 -->
        <template v-if="hedgeExposure && hedgeExposure.etfs && hedgeExposure.etfs.length">
          <div style="font-size: 13px; font-weight: bold; margin: 8px 0 6px;">底层 ETF → 实际持有合约月（穿透 · as-of {{ hedgeExposure.as_of }}）</div>
          <n-table :single-line="false" size="small" style="margin-bottom: 8px;">
            <thead>
              <tr><th>ETF</th><th>季报权重</th><th>结构 / 指数</th><th>当前持有合约</th><th>品种</th></tr>
            </thead>
            <tbody>
              <tr v-for="e in hedgeExposure.etfs" :key="e.etf">
                <td style="font-weight: bold;">{{ e.etf }}<span v-if="e.pending" style="color: #b45309;"> *</span></td>
                <td>{{ e.weight_pct }}%</td>
                <td style="font-size: 11px; color: #64748b;">{{ e.structure }}</td>
                <td>
                  <span v-if="e.pending" style="color: #b45309; font-weight: bold;">待核实（结构见左列）</span>
                  <span v-else>{{ e.contracts.map((c: any) => (e.variety === 'WTI' ? 'CL ' : 'Brent ') + c.month + (e.contracts.length > 1 ? ' ×' + c.pct + '%' : '')).join(' / ') }}</span>
                </td>
                <td :style="{ color: e.variety === 'WTI' ? '#2563eb' : '#d85a30', fontWeight: 'bold' }">{{ e.variety }}</td>
              </tr>
            </tbody>
          </n-table>
          <div v-if="hedgeExposure.pending_nav_pct > 0" style="font-size: 11px; color: #b45309; margin-bottom: 12px;">
            * 标 {{ hedgeExposure.pending_nav_pct }}% 净值的 ETF（DBO/OILUSA 等结构特殊：动态单月 / 曲线多期限分散）未计入下方月份敞口，待实测当前合约后补。
          </div>

          <div style="font-size: 13px; font-weight: bold; margin: 8px 0 6px;">归一化对冲分布：该空哪个月（WTI 书占净值 {{ hedgeExposure.wti_book_nav_pct }}%，MCL 唯一能盖的部分）</div>
          <n-table :single-line="false" size="small" style="margin-bottom: 8px;">
            <thead>
              <tr><th>月份</th><th>占WTI书</th><th>占净值</th><th>来自</th><th>MCL 可操作性</th></tr>
            </thead>
            <tbody>
              <tr v-for="m in hedgeExposure.wti_months" :key="m.month">
                <td>CL {{ m.month }}</td>
                <td>{{ m.book_pct }}%</td>
                <td>{{ m.exp_nav_pct }}%</td>
                <td>{{ m.from }}</td>
                <td>{{ m.mcl_ok ? '可交易' : 'MCL 远月流动性差' }}</td>
              </tr>
              <tr>
                <td style="font-weight: bold;">WTI 小计</td>
                <td>100%</td>
                <td style="font-weight: bold;">{{ hedgeExposure.wti_book_nav_pct }}%</td>
                <td colspan="2">可对冲部分（MCL）</td>
              </tr>
              <tr v-for="bm in hedgeExposure.brent.months" :key="'b' + bm.month" style="color: #993c1d;">
                <td>Brent {{ bm.month }}</td>
                <td>{{ bm.book_pct }}%</td>
                <td>{{ bm.exp_nav_pct }}%</td>
                <td>{{ bm.from }}</td>
                <td>用 CL{{ bm.month }} 同月对冲（跨品种，WTI-Brent 相关≈0.9）</td>
              </tr>
              <tr style="color: #993c1d;">
                <td style="font-weight: bold;">Brent 小计</td>
                <td>100%</td>
                <td style="font-weight: bold;">{{ hedgeExposure.brent.book_nav_pct }}%</td>
                <td colspan="2">跨品种同月对冲（CL 对应月），覆盖至 95%；非精确，承担 WTI-Brent 价差</td>
              </tr>
            </tbody>
          </n-table>
          <div v-if="hedgeExposure.hedge_plan" style="font-size: 11px; color: #64748b; margin-bottom: 12px;">
            进阶对冲方案：{{ hedgeExposure.hedge_plan.months.join(' + ') }} 按 {{ hedgeExposure.hedge_plan.weights.join('/') }} 分配手数（覆盖 WTI 书 {{ hedgeExposure.hedge_plan.coverage_book_pct }}%，占净值 {{ hedgeExposure.hedge_plan.coverage_nav_pct }}%）；远月（MCL 流动性差）放弃。指数每月滚动，上表 as-of {{ hedgeExposure.as_of }}，滚仓后需人工更新。
          </div>
          <div v-if="mixedValuation != null" style="font-size: 11px; color: #0e7490; margin-bottom: 12px; background: #ecfeff; border-left: 3px solid #22d3ee; padding: 6px 10px;">
            此进阶方案的「混合估值」＝上方高亮框的 <b>{{ mixedValuation.toFixed(4) }}</b>，即 WTI 部分真实混合 NAV。若按 {{ hedgeExposure.hedge_plan.months.join('/') }} 的 {{ hedgeExposure.hedge_plan.weights.join('/') }} 在 2611/2612 两合约分仓空单（而非单月对冲），WTI 基差残余≈0，混合估值即你的锁定折价基准——这是<b>根治 WTI 基差残余</b>的方案（"两合约"指 2611+2612、非"2手"；实盘整手约束下难精确落地，当前单月对冲仅作参考）。
          </div>
        </template>

      </div>
      <n-empty v-else description="暂无实时估值数据（进入页面会自动从 ARM 同步 CL 分母，若失败顶部会报警）" />
    </n-modal>

    <!-- [AI-2026-09-18] 三原油 LOF 同合约月套利对比（从对冲弹窗"对冲合约"列合约名点击进入） -->
    <n-modal
      v-model:show="compareModalShow"
      preset="card"
      :title="'3个原油LOF对比 · CL ' + compareContract + '（' + contractMonthLabel(compareContract) + '）' + (compareClNow != null ? ' · CL实时价 ' + compareClNow.toFixed(3) : '')"
      style="width: 880px; max-width: 96vw;"
    >
      <div style="font-size: 13px;">
        <div v-if="compareLoading" style="color: #64748b; padding: 8px 0;">加载三基金实时数据…</div>
        <n-table v-else :single-line="false" size="small">
          <thead>
            <tr>
              <th>基金</th>
              <th>β(仓位)</th>
              <th>持仓实时估值</th>
              <th>实时持仓溢价</th>
              <th>实时ETF现价</th>
              <th>每手MCL→份数</th>
              <th>赎回费</th>
              <th>净折溢价</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in compareRows" :key="row.code">
              <td style="font-weight: bold; white-space: nowrap;">{{ row.code }} {{ row.name }}</td>
              <td>{{ cmpBeta(row.data) }}</td>
              <td style="font-weight: bold; color: #2563eb;">{{ cmpNav(row.data) }}</td>
              <td :style="{ fontWeight: 'bold', color: row.data?.realtime_premium != null ? (row.data.realtime_premium >= 0 ? '#dc2626' : '#16a34a') : '#1e293b' }">{{ cmpPremium(row.data) }}</td>
              <td>
                <div>{{ cmpPrice(row.data) }}</div>
                <div style="font-size: 10px; color: #94a3b8;">{{ cmpPriceSrc(row.data) }}</div>
              </td>
              <td>{{ cmpShares(row.data) }}</td>
              <td>{{ (row.redeemFee * 100).toFixed(3) + '%' }}</td>
              <td :style="{ fontWeight: 'bold', color: cmpNet(row) != null ? (cmpNet(row)! >= 0 ? '#dc2626' : '#16a34a') : '#1e293b' }">
                {{ cmpNetLabel(row) }}
              </td>
            </tr>
          </tbody>
        </n-table>
        <div style="font-size: 11px; color: #94a3b8; margin-top: 8px;">
          净折溢价：实时持仓溢价 <b>&gt; 0</b>（场内卖出）时本列<b>留空</b>，看左侧「实时持仓溢价」；<b>&lt; 0</b>（场内买入 → 赎回）时 = <b>|实时持仓溢价| − 赎回费</b>。<span style="color: #dc2626;">红 = 正数</span>／<span style="color: #16a34a;">绿 = 负数</span>。三基金数据与各自"对冲"弹窗同源同口径，随主弹窗每 20 秒刷新。
        </div>
        <!-- [AI-2026-09-28 东哥需求] 导出 Model B 全部原始数据（2611/2612/2701 三个月），
             一腿一行长表，供每日在 Excel 里复算；不含「混合估值」（进阶对冲派生量，非原始输入）。 -->
        <div style="display: flex; align-items: center; justify-content: flex-end; gap: 10px; margin-top: 12px; padding-top: 10px; border-top: 1px solid #e2e8f0;">
          <span style="font-size: 11px; color: #94a3b8;">CSV 含 2611/2612/2701 三个月 · 一腿一行 · 不含混合估值</span>
          <n-button size="small" type="primary" :disabled="compareLoading || !compareRows.length" @click="exportCompareCsv">
            导出原始数据 CSV
          </n-button>
        </div>
      </div>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, h } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NCard, NTag, NIcon, NEmpty, NSpin, NButton, NDataTable, NGrid, NGi, NModal, NTable, NRadioGroup, NRadioButton, NInputNumber, NInput, NTooltip
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { PieChart } from 'lucide-vue-next'
import { getFundHoldingPeriods, getFundHoldings, getFundHoldingRealtime, getFundHedgeExposure, getFundPenetration, getFundHoldingRecalc, getFundHoldingRecalcDetail, syncOilStatic, syncFuturesFreeze, getOilPriceFreshness, refetchOilPrices } from '../api'
import { formatPercent, priceColor } from '../utils'

const route = useRoute()
const router = useRouter()

const fundCode = computed(() => (route.query.code as string) || '')
const fundName = computed(() => (route.query.name as string) || '')
const currentPeriod = ref('')

const loading = ref(false)
const error = ref('')
const periods = ref<any[]>([])
const holdings = ref<any[]>([])
const regionDistribution = ref<any[]>([])
const prevPeriod = ref('')
const exited = ref<any[]>([])
const newIn = ref<any[]>([])
const reportDate = ref('')
const valuation = ref<any>(null)
// [AI-2026-09-17] 对冲穿透：底层 ETF 实际持有合约月 + 归一化 CL 对冲分布
const hedgeExposure = ref<any>(null)

// [AI-2026-09-15] 进页面自动同步 CL 冻结价（从 ARM），每天只拉一次；
// 拉取失败弹出黄色报警，提示东哥检查 ARM 连接/采样。
const syncAlert = ref('')

// [AI-2026-09-15] 有效近月±1 三合约对比：动态合约列表来自后端 active_contracts。
// [AI-2026-09-17] UI 只显示底层实际持有的 MCL 可操作月份（权威=etf_contract_exposure 穿透配置）：
// 穿透证实底层 WTI 敞口已无 2610（USO 滚至 2611 100%、CRUD 2611/2612/2701），主连 2610 估值对对冲无意义 → 不显示。
// 无穿透配置的基金（161129/501018 暂无）退回显示全部 active_contracts；滚仓后改配置表即自动跟随，无需改代码。
const shownContracts = computed(() => {
  const list: string[] = valuation.value?.active_contracts || []
  const months = hedgeExposure.value?.wti_months as any[] | undefined
  if (months && months.length) {
    const ok = new Set(months.filter((m) => m.mcl_ok).map((m) => m.month as string))
    const filtered = list.filter((c) => ok.has(c))
    if (filtered.length) return filtered
  }
  return list
})
// [AI-2026-09-17] 对冲合约选项：WTI 直接对冲（2611/2612）+ Brent 同月 CL 跨品种对冲（2611/2701）。
// 选 Brent 月即代表用同月 CL 跨品种对冲，反算工具（读 cSel）同步覆盖。
const hedgeOptions = computed(() => {
  const wti = shownContracts.value.map((c: string) => ({
    label: `CL ${c}（${contractMonthLabel(c)} · WTI 直接）`,
    value: c,
  }))
  const brent = (hedgeExposure.value?.brent?.months || []).map((b: any) => ({
    label: `Brent ${b.month}（→CL${b.month} 同月）`,
    value: b.month,
  }))
  return [...wti, ...brent]
})
const hedgeContract = ref<string>('')
// 默认 2611（穿透显示当前 WTI 敞口主力月）；选中项被过滤掉时自动纠正
watch(shownContracts, (list: string[]) => {
  if (list.length && !list.includes(hedgeContract.value)) {
    hedgeContract.value = list.includes('2611') ? '2611' : list[0]
  }
}, { immediate: true })

/** 从 YYMM 取可读月份标签，如 '2610'→'10月', '2701'→'01月' */
function contractMonthLabel(yymm: string): string {
  if (!yymm || yymm.length < 4) return yymm
  const m = parseInt(yymm.slice(2), 10)
  return `${m}月`
}

/** 取某合约的估值对象 */
function cData(contract: string) {
  return valuation.value?.contracts?.[contract] || null
}
/** 当前选中合约的估值对象 */
const cSel = computed(() => cData(hedgeContract.value))
/** 所有非选中合约列表（用于灰字副行显示；同 shownContracts 口径，2610 不显示） */
const otherContracts = computed(() => {
  return shownContracts.value.filter((c: string) => c !== hedgeContract.value)
})
const penetrationData = ref<any>(null)
const penetrationReady = ref(false)

// [AI-2026-09-11] 持仓静态估值（季报持仓法全 USD 简化口径）
const recalcRows = ref<any[]>([])
const latestNav = ref<number | null>(null)
const latestNavDate = ref<string>('')
// [AI-2026-09-30 东哥需求] 持仓静态估值已常驻主页面（不再用弹窗）；此开关改为二级弹窗「季报仓位跟踪」
const trackingModalShow = ref(false)
// [AI-2026-09-23 B方案] 静态持仓估值"是否已从 ARM 同步到本地缓存"状态（读本地 holding_static_val）
const staticSynced = computed(() => recalcRows.value.length > 0)
const staticLatestDate = computed(() => recalcRows.value.length ? recalcRows.value[0].date : '')
const staticSyncLoading = ref(false)
const staticSyncMsg = ref('')

// [AI-2026-09-23 B方案] 持仓静态估值弹窗诊断：本地核心行（recalcRows）只含 date/nav/hsv/err，
// 诊断细节（etf_prices/fill_warning/note 等）由弹窗打开时按需从 ARM 取全量，按 date 合并后展示。
// 前端不直接 SSH 代理：/holding-recalc-detail 端点本机侧已代理到 ARM（只读）。
const recalcDetailMap = ref<Record<string, any>>({})
const recalcDetailLoading = ref(false)
// [AI-2026-09-24 方案A] 美股价未入库的「待补日」：后端按 us_clock 口径给出（只读，不落库）。
// 这些日不在 recalcRows（读库）里，以灰色行插回表格，避免"少一行"被误判为程序丢行。
const recalcPending = ref<any[]>([])
const recalcDisplayRows = computed(() => {
  const m = recalcDetailMap.value
  let base = recalcRows.value
  if (m && Object.keys(m).length) {
    base = recalcRows.value.map((r: any) => {
      const d = m[r.date]
      return d ? { ...r, ...d } : r
    })
  }
  if (!recalcPending.value.length) return base
  const pendRows = recalcPending.value.map((p: any) => ({
    date: p.date,
    official_nav: p.official_nav ?? null,
    holding_static_val: null,
    err_pct: null,
    err_bp: null,
    pending: true,
    note: p.message || `美股价未入库（美股时钟 ${p.us_clock}），该行待补`,
    fill_warning: false,
    carried_forward: false,
  }))
  return [...pendRows, ...base].sort((a: any, b: any) => (a.date < b.date ? 1 : -1))
})
const fetchRecalcDetail = async () => {
  recalcDetailLoading.value = true
  try {
    const r = await getFundHoldingRecalcDetail(fundCode.value, currentPeriod.value, '2026-07-01')
    if (r.data?.status === 'ok') {
      const rows: any[] = (r.data.data?.rows) || []
      const m: Record<string, any> = {}
      for (const row of rows) m[row.date] = row
      recalcDetailMap.value = m
    }
  } catch (e: any) {
    // 诊断细节取不到时静默降级：核心行仍可显示，仅展开明细/缺价标红缺失
    console.warn('recalc 诊断细节获取失败(降级):', e?.message || e)
  } finally {
    recalcDetailLoading.value = false
  }
}

// [AI-2026-09-23 B方案] 手动触发 本地←ARM 拉取原油三基金 holding_static_val（pull_oil_static_from_arm）
const syncStaticValuation = async () => {
  staticSyncLoading.value = true
  staticSyncMsg.value = ''
  try {
    const r = await syncOilStatic(fundCode.value)
    const d = r?.data?.data || r?.data
    if (r?.data?.status === 'ok' || d?.status === 'ok') {
      staticSyncMsg.value = `✅ ${d?.message || '同步完成'}`
      // 重新读取本地缓存（holding-recalc 读本地），刷新 最新净值 / 同步状态
      await loadData()
    } else {
      staticSyncMsg.value = `❌ ${d?.message || '同步失败'}`
    }
  } catch (e: any) {
    staticSyncMsg.value = `❌ 同步失败: ${e?.message || e}`
  } finally {
    staticSyncLoading.value = false
  }
}

// [AI-2026-09-24 东哥需求] ARM 美股价新鲜度检测 + 一键重抓推送闭环：
// 新浪凌晨收盘价未到（ARM 07:30 采集抓空）→ 本页提示 → 点按钮 → 本机重抓 → 推 ARM → ARM 重算 → 拉回。
const priceFresh = ref<any>(null)
const refetchLoading = ref(false)
const refetchMsg = ref('')

const checkPriceFreshness = async () => {
  try {
    const r = await getOilPriceFreshness()
    priceFresh.value = r?.data?.status === 'ok' ? (r.data.data || null) : null
  } catch (e: any) {
    // 非关键检测：失败静默，不打扰主流程
    priceFresh.value = null
  }
}

const refetchUsPrices = async () => {
  refetchLoading.value = true
  refetchMsg.value = ''
  try {
    const r = await refetchOilPrices()
    const d = r?.data?.data
    if (r?.data?.status === 'ok') {
      refetchMsg.value = `✅ ${d?.summary || '重抓并推送完成'}`
      await checkPriceFreshness()
      await loadData()
    } else {
      refetchMsg.value = `❌ ${d?.summary || r?.data?.message || '重抓失败'}`
    }
  } catch (e: any) {
    refetchMsg.value = `❌ 重抓失败: ${e?.message || e}`
  } finally {
    refetchLoading.value = false
  }
}

// [AI-2026-09-12] 持仓实时估值弹窗：核对 CL 各腿分母（1130/1430/1600，日本腿 0230）+ 实时 CL 价
const realtimeModalShow = ref(false)
const rtRefreshing = ref(false)
const openRealtimeModal = () => { realtimeModalShow.value = true }
const refreshRealtime = async () => {
  rtRefreshing.value = true
  try {
    const r = await getFundHoldingRealtime(fundCode.value)
    // error 也覆盖旧值，避免残留
    valuation.value = r?.data?.data || null
    // [AI-2026-09-18] 三基金对比弹窗若开着，随同一 20s 定时器同步刷新
    if (compareModalShow.value && compareContract.value) fetchCompareRows()
  } catch (e: any) {
    error.value = `刷新CL实时价失败: ${e?.message || e}`
  } finally {
    rtRefreshing.value = false
  }
}
// 弹窗打开期间每 20s 刷新一次 CL 实时价（新浪），关闭即停
let rtTimer: ReturnType<typeof setInterval> | null = null
watch(realtimeModalShow, (v) => {
  if (v) {
    refreshRealtime()
    rtTimer = setInterval(() => { refreshRealtime() }, 20000)
  } else if (rtTimer) {
    clearInterval(rtTimer)
    rtTimer = null
  }
})

// [AI-2026-09-15] 对冲手数 → 应买 LOF 份数：纯前端实时反算。
// 用当刻实时估值(hrv) / 选中合约 CL 实时价(cl_now) / 中间价(fx_now, usd_cny_mid) / 季报篮子 β(valid_weight_sum)。
// 物理含义：LOF 每份油价敞口 = NAV × β(仓位)；1 张 MCL = 100 桶 WTI 敞口 = 100 × CL × fx(CNY)。
// 对冲平衡：shares × hrv × β = n × 100 × cl_now × fx → shares = n×100×cl_now×fx / (hrv × β)。
const mclLots = ref(1)
const lofSharesForMcl = computed(() => {
  const c = cSel.value
  if (!c || c.hrv == null || c.valid_weight_sum == null || c.valid_weight_sum <= 0) return null
  const cl = c.cl_now
  const fx = c.fx_now
  if (cl == null || fx == null || fx <= 0) return null
  const shares = mclLots.value * 100 * cl * fx / (c.hrv * c.valid_weight_sum)
  return Math.round(shares)
})
const lofMarketValueForMcl = computed(() => {
  const c = cSel.value
  if (lofSharesForMcl.value == null || !c || c.hrv == null) return null
  return Math.round(lofSharesForMcl.value * c.hrv)
})

// [AI-2026-09-17] 全合约对比（弹窗不再单选切换）：以下函数按合约 key 直接计算，不依赖选中。
// 合约 key 按 YYMM 数值排序（2611/2612/2701），保证展示顺序稳定。
const contractKeys = computed(() => {
  const ks = Object.keys(valuation.value?.contracts || {})
  return ks.sort((a: string, b: string) => parseInt(a) - parseInt(b))
})
const firstContract = computed(() => (contractKeys.value.length ? cData(contractKeys.value[0]) : null))
// Brent 同月 CL 合约集合（从穿透配置派生；用于对比表标注跨品种对冲）
const brentMonthSet = computed(() => new Set((hedgeExposure.value?.brent?.months || []).map((m: any) => m.month as string)))
/** 每合约：N 手 MCL → 应买 LOF 份数（用当刻估值/CL/汇率/β 实时算） */
function lofSharesForMclOf(contract: string): number | null {
  const c = cData(contract)
  if (!c || c.hrv == null || c.valid_weight_sum == null || c.valid_weight_sum <= 0) return null
  const cl = c.cl_now, fx = c.fx_now
  if (cl == null || fx == null || fx <= 0) return null
  return Math.round(mclLots.value * 100 * cl * fx / (c.hrv * c.valid_weight_sum))
}
/** 每合约：每 1 手 MCL → 应买 LOF 份数（mclLots=1，便于东哥 Excel 按任意手数乘） */
function lofSharesPerLot(contract: string): number | null {
  return perLotShares(cData(contract))
}
/** 每 1 手 MCL → 应买 LOF 份数（任意基金/合约通用；三基金对比弹窗复用） */
function perLotShares(c: any): number | null {
  if (!c || c.hrv == null || c.valid_weight_sum == null || c.valid_weight_sum <= 0) return null
  const cl = c.cl_now, fx = c.fx_now
  if (cl == null || fx == null || fx <= 0) return null
  return Math.round(100 * cl * fx / (c.hrv * c.valid_weight_sum))
}

// [AI-2026-09-21] 混合估值（WTI 动态加权）：用进阶对冲方案的真实月份占比，把 WTI 内部 2611/2612 比例误差抹平，更接近基金真实混合持仓 NAV。
// V_mix = Σ(weights[i]% × cData(months[i]).hrv)；仅当 hedge_plan 存在且每个月都能取到估值时返回，否则 null（不兜底）。
const mixedValuation = computed<number | null>(() => {
  const hp = hedgeExposure.value?.hedge_plan
  if (!hp || !hp.months || !hp.weights || hp.months.length !== hp.weights.length) return null
  let sum = 0
  for (let i = 0; i < hp.months.length; i++) {
    const c = cData(hp.months[i])
    if (!c || c.hrv == null) return null
    sum += (hp.weights[i] / 100) * c.hrv
  }
  return sum
})
// 混合估值对应的实时持仓溢价（用同一 LOF 市价 firstContract.lof_price；与三合约行同口径）
const mixedPremium = computed<number | null>(() => {
  if (mixedValuation.value == null) return null
  const price = firstContract.value?.lof_price
  if (price == null || price <= 0) return null
  return price / mixedValuation.value - 1
})

// [AI-2026-09-18] 弹窗标题带实时现价（复用"实时ETF现价"卡的 lof_price/数据源；表格不动，只在标题后追加）
// [AI-2026-09-18] 现价段改蓝色醒目（东哥要求）：header 插槽渲染，现价段独立上色
const modalPriceSeg = computed(() => {
  const c = firstContract.value
  if (c?.lof_price == null) return null
  const src = lofSrcLabel(c.lof_price_source)
  return { price: c.lof_price.toFixed(3), src }
})

// [AI-2026-09-18] 三原油 LOF 同合约月套利对比（点对冲弹窗"对冲合约"列的合约名进入）。
// 赎回费口径（东哥 2026-09-18）：160723/501018 = 0.5%，161129 = 0.365%。
// [AI-2026-09-28 东哥定口径·终2] 净折溢价：>0（场内卖出）留空；<0（买入→赎回）用「|溢价| − 赎回费」。
// [AI-2026-09-28 东哥点名] 只呈报数值，不标"最优"、不标绿底 —— 选标由东哥自行判断。
const OIL_FUNDS = [
  { code: '160723', name: '嘉实原油', redeemFee: 0.005 },
  { code: '161129', name: '易方达原油', redeemFee: 0.00365 },
  { code: '501018', name: '南方原油', redeemFee: 0.005 },
]
const compareModalShow = ref(false)
const compareContract = ref('')
const compareLoading = ref(false)
const compareRows = ref<any[]>([])
const fetchCompareRows = async () => {
  compareLoading.value = true
  try {
    compareRows.value = await Promise.all(OIL_FUNDS.map(async (f) => {
      let contracts: any = null
      try {
        const r = await getFundHoldingRealtime(f.code)
        // [AI-2026-09-28 东哥需求] 保留整份 contracts（2611/2612/2701 全月），供 CSV 导出；
        // data 仍是当前对比合约，喂表格。兼容两种壳：本机 {status,data} / ARM 外层多包一层。
        const payload = r?.data?.data?.data ?? r?.data?.data ?? null
        contracts = payload?.contracts || null
      } catch { contracts = null }
      return { ...f, contracts, data: contracts?.[compareContract.value] || null }
    }))
  } finally { compareLoading.value = false }
}
const openCompareModal = (contract: string) => {
  compareContract.value = contract
  compareModalShow.value = true
  fetchCompareRows()
}
/** [AI-2026-09-28 东哥定口径·终2] 净折溢价 —— 两条路各算各的，用不上的那支留空。
 *  东哥 10:52 理清现金流：「实时持仓溢价 > 0 时我不会折价去买入，也不会去赎回，
 *  只会考虑场内卖出，那就和赎回费没关系了」；10:59「可以让 >0 时留空」。
 *    · 溢价 > 0 → 场内卖出：本列【留空】（值 = 实时持仓溢价，与赎回费无关，看左侧列即可）
 *    · 溢价 < 0 → 场内买入 → 赎回：net = |溢价| − 赎回费（赎回费是这条路上唯一的成本）
 *  ⚠️ 赎回费在程序里是【正数】（160723/501018=0.005，161129=0.00365）。
 *  不做排序推荐（东哥自行判断，无"最优"标记）。
 *  ⚠️ 史（同日六次改动，以本式为准）：
 *    ① 09-18 起 `溢价 + 赎回费`（配"越负越优"）；② 09:45 我擅自改 `(1−费)/(1+溢价) − 1`（错）；
 *    ③ 09:47 统一 `溢价 − 赎回费`（错）；④ 10:37 统一 `溢价 + 赎回费`；
 *    ⑤ 10:45 分两支（>0 `溢价 − 费` / <0 `溢价 ＋ 费`）；
 *    ⑥ 10:52·10:59 东哥理清现金流 ⇒ >0 留空、<0 用 `|溢价| − 费`。 */
function cmpNet(row: any): number | null {
  const p = row?.data?.realtime_premium
  if (p == null) return null
  return p > 0 ? null : Math.abs(p) - row.redeemFee
}
// [AI-2026-09-28 东哥点名] 已删除 bestNetCode（自动标"最优"）：不做选标推荐，由东哥自行判断。
// —— 对比弹窗展示 helper（吸收 TS 断言噪音）——
const cmpBeta = (c: any) => c?.valid_weight_sum != null ? (c.valid_weight_sum * 100).toFixed(2) + '%' : '-'
const cmpNav = (c: any) => c?.hrv != null ? c.hrv.toFixed(4) : '-'
const cmpPremium = (c: any) => c?.realtime_premium != null ? (c.realtime_premium * 100).toFixed(3) + '%' : '-'
const cmpPrice = (c: any) => c?.lof_price != null ? c.lof_price.toFixed(3) : '-'
// [AI-2026-09-23] LOF 价格来源标签：非交易时段腾讯返回的是最近收盘价，必须区分标"昨收"/"收盘(今日)"，避免把昨收误显成实时误导决策
const lofSrcLabel = (src: string | null | undefined) => {
  if (!src) return ''
  if (src === 'close') return '收盘(盘后)'
  if (src === '昨收') return '昨收'
  if (src === '收盘') return '收盘(今日)'
  return src.replace('realtime:', '')
}
const cmpPriceSrc = (c: any) => lofSrcLabel(c?.lof_price_source)
const cmpShares = (c: any) => { const s = perLotShares(c); return s != null ? s.toLocaleString() : '-' }
const cmpNetLabel = (row: any) => { const n = cmpNet(row); return n != null ? (n * 100).toFixed(3) + '%' : '-' }

// ============================================================================
// [AI-2026-09-28 东哥需求] 导出三只原油 LOF 的 Model B 全部原始数据
//   · 范围：2611 / 2612 / 2701 三个月全导（一次性成一份文件，带「合约月」列区分）
//   · 结构：一腿一行长表 —— 基金级字段每腿重复，便于 Excel 直接复算
//   · 刻意不含「混合估值（WTI 2611/2612 加权）」：那是进阶对冲方案的派生量，
//     不是 Model B 的原始输入，混进来会污染要核对的那条链。
// Excel 可原样复核：
//   Σ腿贡献 × 100 ÷ β(pos_pct)                        = 篮子变动%
//   β/100 × ((1+篮子变动%) × (1+r_fx%) − 1)            = 总变动%
//   base_nav × (1 + 总变动%)                           = 实时估值
//   LOF现价 ÷ 实时估值 − 1                             = 实时持仓溢价%
//   净折溢价% = 溢价>0 留空 ／ <0 → |溢价| − 赎回费
// ============================================================================
const CSV_HEADERS = [
  '导出时间(北京)', '合约月', '基金代码', '基金名称',
  '基准日', '基准静态估值', 'β仓位%(pos_pct)', '有效权重和%', '覆盖率%',
  '腿序号', '腿标的', '腿权重%', '采样时点', '冻结价', '冻结日',
  'CL实时价', 'CL时间', '腿涨跌%', '腿贡献',
  '篮子变动%', '汇率基准fx_point', '今日中间价fx_now', 'r_fx%', 'fx状态',
  '总变动%', '实时估值', 'LOF现价', '现价来源', '实时持仓溢价%', '赎回费%', '净折溢价%', '备注',
]
const csvNum = (v: any, d = 6) => (v == null || v === '' || isNaN(Number(v))) ? '' : Number(v).toFixed(d)
const csvPct = (v: any, d = 6) => (v == null || v === '' || isNaN(Number(v))) ? '' : (Number(v) * 100).toFixed(d)
/** CSV 单元转义（含逗号/引号/换行 → 双引号包裹）。两个导出功能共用，勿各自实现一份。 */
const csvEsc = (v: any) => {
  const s = (v == null) ? '' : String(v)
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s
}
const exportCompareCsv = () => {
  const now = new Date()
  const p2 = (n: number) => String(n).padStart(2, '0')
  const stampStr = `${now.getFullYear()}-${p2(now.getMonth() + 1)}-${p2(now.getDate())} ${p2(now.getHours())}:${p2(now.getMinutes())}`
  // 合约月 = 三只基金 contracts 键的并集，按 YYMM 数值排序（2611/2612/2701）
  const months = Array.from(new Set(
    compareRows.value.flatMap((r: any) => Object.keys(r.contracts || {}))
  )).sort((a: any, b: any) => parseInt(a) - parseInt(b))
  const out: Record<string, any>[] = []
  for (const row of compareRows.value) {
    for (const m of months) {
      const c: any = row.contracts?.[m] || null
      const p = c?.realtime_premium
      const base: Record<string, any> = {
        '导出时间(北京)': stampStr,
        '合约月': m,
        '基金代码': row.code,
        '基金名称': row.name,
        '基准日': c?.base_date ?? '',
        '基准静态估值': csvNum(c?.base_nav, 6),
        'β仓位%(pos_pct)': csvNum(c?.pos_pct, 2),
        '有效权重和%': csvPct(c?.valid_weight_sum, 2),
        '覆盖率%': csvPct(c?.coverage, 2),
        '篮子变动%': csvPct(c?.basket_change_pct, 6),
        '汇率基准fx_point': csvNum(c?.fx_point, 4),
        '今日中间价fx_now': csvNum(c?.fx_now, 4),
        'r_fx%': csvPct(c?.fx_change_pct, 6),
        'fx状态': c?.fx_status ?? '',
        '总变动%': csvPct(c?.total_change_pct, 6),
        '实时估值': csvNum(c?.hrv, 6),
        'LOF现价': csvNum(c?.lof_price, 4),
        '现价来源': lofSrcLabel(c?.lof_price_source),
        '实时持仓溢价%': csvPct(p, 6),
        '赎回费%': csvNum(row.redeemFee * 100, 3),
        // 与页面同口径：>0 留空；<0 → |溢价| − 赎回费
        '净折溢价%': (p == null) ? '' : (p > 0 ? '' : csvPct(Math.abs(p) - row.redeemFee, 3)),
        '备注': (c?.status && c.status !== 'ok') ? (c.message || c.status) : '',
      }
      const legs: any[] = c?.components || []
      if (!legs.length) { out.push(base); continue }
      legs.forEach((g: any, i: number) => {
        out.push({
          ...base,
          '腿序号': i + 1,
          '腿标的': g.symbol ?? '',
          '腿权重%': csvNum(g.weight_pct, 4),
          '采样时点': g.point ?? '',
          '冻结价': csvNum(g.freeze_price, 4),
          '冻结日': g.freeze_date ?? '',
          'CL实时价': csvNum(g.cl_now, 4),
          'CL时间': c?.cl_time ?? '',
          '腿涨跌%': csvPct(g.ratio, 6),
          '腿贡献': csvNum(g.contrib, 8),
          '备注': (g.status && g.status !== 'ok') ? g.status : base['备注'],
        })
      })
    }
  }
  // \uFEFF BOM：保证 Excel 双击打开中文不乱码
  const csv = '\uFEFF' + [CSV_HEADERS.join(',')]
    .concat(out.map(r => CSV_HEADERS.map(h => csvEsc(r[h])).join(',')))
    .join('\r\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `原油LOF实时估值原始数据_${now.getFullYear()}${p2(now.getMonth() + 1)}${p2(now.getDate())}_${p2(now.getHours())}${p2(now.getMinutes())}.csv`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 3000)
}
// ============================================================================
// [AI-2026-10-05 东哥需求] 导出「持仓静态估值」人工核对 CSV
//
// 东哥原话：「我的主要目的，不是要导出静态估值本身，而是要手工来计算核对静态估值，
// 所以需要导出汇率和底层 ETF 明细，并且最好 CSV 里面能带计算公式」。
// ⇒ 设计原则：**凡派生量一律写成 Excel 公式**，表里只保留原始输入（两日 ETF 收盘价、
//   两日汇率原值、各腿原始权重、T-1 官方净值）。这样打开 CSV 拖一下就验完，
//   而不是拿程序算好的数字去验证程序自己（自证＝没验）。
//
// 形状：宽表 —— 一行一日期；一腿一个 9 列块；尾部 4 个复算列。选宽表不选长表的理由：
//   宽表所有公式都是**同一行内横向引用**，拖拽即整列生效；长表要靠 SUMIF 跨行捞腿，
//   漏写一行汇总会静默算错 —— 不适合「肉眼核对」这个用途。
//
// 复算链（与后端 holding_service.get_recalc_history 严格同式，两边不得各自发挥）：
//   r_local_i = 当日价/前日价 − 1
//   r_fx_i    = 汇率当日/汇率前日 − 1
//   r_i       = (1 + r_local_i) × (1 + r_fx_i) − 1
//   篮子变动% = Σ (权重_i / pos_pct) × r_i × 100
//   总变动%   = pos_pct × 篮子变动% / 100        （= pos_pct/100 × r_basket，百分数计）
//   静态估值  = T-1官方净值 × (1 + 总变动%/100)
//   误差%     = (静态估值 / 当日官方净值 − 1) × 100
// ⚠️ 分母 pos_pct（有效权重和）与各腿原始权重分开两列：有腿被剔除时二者不等；
//    权重列必须保持「季报原始值」才可核对，归一化交给公式里的 /pos_pct。
// ============================================================================
/** 1-based 列号 → Excel 列名（A/B/…/Z/AA…） */
const csvColLetter = (n: number): string => {
  let s = ''
  while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26) }
  return s
}
/** 腿列块内 9 列的顺序（表头与公式定位共用同一常量，防止两处错位） */
const STATIC_LEG_COLS = ['权重%', '前日价', '当日价', '币种', '汇率前日', '汇率当日', '本地涨跌%', '汇率涨跌%', '腿贡献%']
const STATIC_HEAD_COLS = [
  '导出时间(北京)', '基金代码', '基金名称', '日期', '报告期', '覆盖率%',
  'T-1官方净值(基数)', '官方净值(T日)', '有效权重和%(pos_pct)',
  '篮子变动%(程序)', '静态估值(程序)', '误差%(程序)', 'USD/CNY前日', 'USD/CNY当日',
]
const STATIC_TAIL_COLS = ['篮子变动%(公式)', '总变动%(公式)', '静态估值(公式)', '误差%(公式)', '备注']
const staticCsvMsg = ref('')
const exportStaticValuationCsv = () => {
  staticCsvMsg.value = ''
  // 待补行（美股价未入库）本就没有估值；无诊断明细的行缺腿、无法复算 —— 两者都不进核对表。
  const all = recalcDisplayRows.value
  const rows = all.filter((r: any) => !r.pending && r.weights && Object.keys(r.weights).length)
  if (!rows.length) {
    staticCsvMsg.value = '❌ 暂无可导出的行（诊断明细未加载，或全部为「待补」），请刷新页面后重试'
    return
  }
  const skipped = all.length - rows.length
  // 腿并集：按首次出现顺序（后端 weights 顺序 = 季报持仓权重序），跨报告期取并集
  const legs: string[] = []
  for (const r of rows) for (const s of Object.keys(r.weights)) if (!legs.includes(s)) legs.push(s)

  const header = [
    ...STATIC_HEAD_COLS,
    ...legs.flatMap((s) => STATIC_LEG_COLS.map((c) => `${s}${c}`)),
    ...STATIC_TAIL_COLS,
  ]
  // 关键列字母（公式拼接用），从表头位置反推，避免手写数字与表头脱钩
  const cPrevNav = csvColLetter(STATIC_HEAD_COLS.indexOf('T-1官方净值(基数)') + 1)
  const cNav = csvColLetter(STATIC_HEAD_COLS.indexOf('官方净值(T日)') + 1)
  const cPos = csvColLetter(STATIC_HEAD_COLS.indexOf('有效权重和%(pos_pct)') + 1)
  const legStart = STATIC_HEAD_COLS.length + 1
  const tailStart = legStart + legs.length * STATIC_LEG_COLS.length
  const cBasket = csvColLetter(tailStart)
  const cTotal = csvColLetter(tailStart + 1)
  const cEst = csvColLetter(tailStart + 2)

  const now = new Date()
  const p2 = (n: number) => String(n).padStart(2, '0')
  const stampStr = `${now.getFullYear()}-${p2(now.getMonth() + 1)}-${p2(now.getDate())} ${p2(now.getHours())}:${p2(now.getMinutes())}`

  const out: any[][] = [header]
  rows.forEach((r: any, i: number) => {
    const R = i + 2  // 第 1 行是表头
    const row: any[] = [
      stampStr, fundCode.value, fundName.value || fundCode.value, r.date, r.report_period ?? '',
      csvPct(r.coverage, 2),
      csvNum(r.prev_official_nav, 6),
      csvNum(r.official_nav, 6),
      csvNum(r.pos_pct, 4),
      csvNum(r.basket_change_pct, 6),
      csvNum(r.holding_static_val, 6),
      csvNum(r.err_pct, 4),
      csvNum(r.usd_cny_prev, 4),
      csvNum(r.usd_cny, 4),
    ]
    const contribCells: string[] = []
    legs.forEach((s, j) => {
      const b = legStart + j * STATIC_LEG_COLS.length
      const L = { w: csvColLetter(b), p0: csvColLetter(b + 1), p1: csvColLetter(b + 2), f0: csvColLetter(b + 4), f1: csvColLetter(b + 5), rl: csvColLetter(b + 6), rf: csvColLetter(b + 7), rc: csvColLetter(b + 8) }
      const inBasket = r.weights?.[s] != null
      const fx: any = r.fx_detail?.[s] || {}
      row.push(
        inBasket ? csvNum(r.weights[s], 4) : '',
        csvNum(r.etf_prev?.[s], 4),
        csvNum(r.etf_prices?.[s], 4),
        fx.cur ?? (inBasket ? 'USD' : ''),
        csvNum(fx.fx0, 6),
        csvNum(fx.fx1, 6),
        // 以下三列一律用公式，不写程序算好的数 —— 这三列就是"给人自己算"的
        `=IF(OR(${L.p0}${R}="",${L.p1}${R}=""),"",ROUND((${L.p1}${R}/${L.p0}${R}-1)*100,6))`,
        `=IF(OR(${L.f0}${R}="",${L.f1}${R}=""),"",ROUND((${L.f1}${R}/${L.f0}${R}-1)*100,6))`,
        `=IF(${L.w}${R}="","",ROUND(${L.w}${R}/$${cPos}${R}*((1+${L.rl}${R}/100)*(1+${L.rf}${R}/100)-1)*100,6))`,
      )
      contribCells.push(`${L.rc}${R}`)
    })
    row.push(
      `=ROUND(SUM(${contribCells.join(',')}),6)`,
      `=ROUND($${cPos}${R}*${cBasket}${R}/100,6)`,
      `=ROUND($${cPrevNav}${R}*(1+${cTotal}${R}/100),6)`,
      `=IF($${cNav}${R}="","",ROUND((${cEst}${R}/$${cNav}${R}-1)*100,4))`,
      [r.fill_warning ? '⚠️' : '', r.carried_forward ? '🔸T日沿用上一收盘' : '', r.note || ''].filter(Boolean).join(' '),
    )
    out.push(row)
  })

  const csv = '\uFEFF' + out.map((r) => r.map(csvEsc).join(',')).join('\r\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `持仓静态估值核对_${fundCode.value}_${now.getFullYear()}${p2(now.getMonth() + 1)}${p2(now.getDate())}_${p2(now.getHours())}${p2(now.getMinutes())}.csv`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 3000)
  staticCsvMsg.value = `✅ 已导出 ${rows.length} 行 × ${legs.length} 条腿${skipped ? `（跳过 ${skipped} 行待补/无明细）` : ''}`
}

/** 对比弹窗标题用：取三基金同一合约月的 CL 实时价（任一非空即可，三基金值相同） */
const compareClNow = computed(() => {
  for (const r of compareRows.value) {
    if (r?.data?.cl_now != null) return r.data.cl_now
  }
  return null
})

// [AI-2026-09-03] 地区列直接显示英文缩写（US/UK/HK…），与东哥口径一致
const regionLabel = (region: string) => region || '其他'

// 总权重
const totalWeight = computed(() => {
  return holdings.value.reduce((s: number, r: any) => s + (typeof r.weight === 'number' ? r.weight : 0), 0) * 100
})

// 穿透数据展示
const wtiPct = computed(() => {
  if (!penetrationData.value?.summary?.by_variety?.WTI) return 0
  return Number(penetrationData.value.summary.by_variety.WTI)
})
const brentPct = computed(() => {
  if (!penetrationData.value?.summary?.by_variety?.Brent) return 0
  return Number(penetrationData.value.summary.by_variety.Brent)
})
const penetratedPct = computed(() => {
  if (!penetrationData.value?.summary?.total_penetrated) return 0
  return Number(penetrationData.value.summary.total_penetrated)
})

// 跳转到穿透分析页
// [AI-2026-10-10] /penetration 已改挂「原油穿透」三基金页，单基金底层资产穿透页挪到 /penetration-detail
const goToPenetration = () => {
  if (!fundCode.value || !currentPeriod.value) return
  router.push({
    path: '/penetration-detail',
    query: { fund_code: fundCode.value, period: currentPeriod.value }
  })
}

// [AI-2026-09-03] 持仓表合计行：前 6 列合并为"总权重"，权重列求和，市值列求和
const holdingSummary = (pageData: any[]) => {
  const w = pageData.reduce((s: number, r: any) => s + (typeof r.weight === 'number' ? r.weight : 0), 0)
  const mvVals = pageData.map((r: any) => r.market_value).filter((v: any) => typeof v === 'number')
  const mv = mvVals.length > 0 ? mvVals.reduce((a: number, b: number) => a + b, 0) : null
  return {
    display_order: { colSpan: 6, value: h('strong', '总权重') },
    market_value: { value: h('strong', mv != null ? Math.round(mv).toLocaleString() : '-') },
    weight: { value: h('strong', (w * 100).toFixed(2) + '%') },
  }
}

const holdingColumns: DataTableColumns<any> = [
  { title: '序号', key: 'display_order', width: 50, align: 'center' },
  { title: '代码', key: 'symbol', width: 80, align: 'center', render(row: any) {
    // [AI-2026-10-12 重做 字体统一token] 裸 monospace 字体 → var(--co-font-mono)
    return h('span', { style: 'font-family: var(--co-font-mono); font-weight: 600;' }, row.symbol || '-')
  }},
  { title: '名称', key: 'name', ellipsis: { tooltip: true } },
  { title: '地区', key: 'region', width: 80, align: 'center', render(row: any) {
    return regionLabel(row.region)
  }},
  { title: '货币', key: 'currency', width: 60, align: 'center' },
  { title: '管理人', key: 'manager', ellipsis: { tooltip: true }, render(row: any) {
    return row.manager || '-'
  }},
  { title: '市值(元)', key: 'market_value', width: 140, align: 'right', render(row: any) {
    return row.market_value != null ? Math.round(row.market_value).toLocaleString() : '-'
  }},
  { title: '权重', key: 'weight', width: 90, align: 'right', render(row: any) {
    return h('span', { style: 'font-weight: 600;' }, row.weight != null ? (row.weight * 100).toFixed(2) + '%' : '-')
  }},
  { title: '相比上期变动', key: 'change', width: 110, align: 'right', render(row: any) {
    if (row.prev_weight == null) return '-'
    const delta = (row.weight || 0) - row.prev_weight
    const sign = delta >= 0 ? '+' : ''
    // 红涨绿跌（中国股市惯例）
    const color = delta >= 0 ? '#dc2626' : '#16a34a'
    return h('span', { style: `font-weight: 600; color: ${color};` }, `${sign}${(delta * 100).toFixed(2)}%`)
  }},
]

// [AI-2026-09-11] 持仓静态估值弹窗表：列 = 日期 / 官方净值 / 静态估值 / 误差%，涨跌幅等移入展开行
// [AI-2026-10-06] 展开行升级为「季报持仓还原六腿表」（见 renderExpand）；数据取自 row.weights/etf_prev/etf_prices/fx_detail。

const recalcColumns: DataTableColumns<any> = [
  {
    type: 'expand',
      renderExpand: (row: any) => {
      // [AI-2026-09-24 方案A] 待补行无底层明细可展开，直接说明原因
      if (row.pending) {
        return h('div', { style: 'padding: 8px 12px; font-size: 12px; color: #92400e;' }, row.note || '美股价未入库，该行待补')
      }
      // [AI-2026-10-06 东哥需求] 展开行由「底层 ETF 明细」flex 列表升级为「季报持仓还原六腿表」
      // （eye-catching 呈现，但自有口径/配色；并公开估值公式，契合开放人设）。
      const syms = Array.from(new Set([
        ...Object.keys(row.weights || {}),
        ...Object.keys(row.etf_prev || {}),
        ...Object.keys(row.etf_prices || {}),
      ]))
      const legs = syms.map((s) => {
        const w = row.weights?.[s]
        const p0 = row.etf_prev?.[s]
        const p1 = row.etf_prices?.[s]
        const rLocal = (p0 != null && p1 != null && p0 > 0) ? p1 / p0 - 1 : null
        const fx = row.fx_detail?.[s]
        const rFx = fx?.r_fx
        const rI = (rLocal != null && rFx != null) ? (1 + rLocal) * (1 + rFx) - 1 : null
        // 估值影响(%)= 占净值(%) × 人民币口径涨跌(小数)；Σ 即总变动%
        const contrib = (w != null && rI != null) ? w * rI : null
        return { s, cur: fx?.cur || 'USD', w, p0, p1, rLocal, rFx, fx0: fx?.fx0, fx1: fx?.fx1, rI, contrib }
      })
      const sumW = legs.reduce((a, l) => a + (l.w || 0), 0)
      const sumContrib = legs.reduce((a, l) => a + (l.contrib || 0), 0)
      const residual = 100 - sumW
      // [AI-2026-10-06 东哥需求] 清淡风重刷（自有浅色系，绝不用他三色）；样式类见 <style scoped> .leg-table
      const UP = '#cc4a3c'   // 红涨（柔化陶红）
      const DOWN = '#1e9e63' // 绿跌（柔化）
      const pctStr = (v: number | null) => v != null ? `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%` : '-'
      const ppStr = (v: number | null) => v != null ? `${v >= 0 ? '+' : ''}${v.toFixed(2)}%` : '-'
      const colColor = (v: number | null) => v == null ? '#cbd5e1' : (v >= 0 ? UP : DOWN)
      const th = (t: string, align = 'right') => h('th', { class: align === 'left' ? 'lt-left' : (align === 'center' ? 'lt-center' : '') }, t)
      const td = (children: any, cls = '', style = '') => h('td', { class: cls, style }, children)
      const dash = () => td('—', 'lt-center lt-muted')
      const legRows = legs.map((l) => h('tr', {}, [
        td([h('span', { style: 'font-weight:600; color:#1e293b;' }, l.s), h('span', { class: 'cur-pill' }, l.cur)], 'lt-left'),
        td(l.w != null ? `${l.w.toFixed(2)}%` : '-'),
        td([
          h('span', {}, l.p0 != null ? l.p0.toFixed(2) : '-'),
          h('span', { class: 'fx-arrow' }, '→'),
          h('span', { style: 'color:#334155;' }, l.p1 != null ? l.p1.toFixed(2) : '-'),
        ], 'lt-center'),
        td(pctStr(l.rLocal), '', `color:${colColor(l.rLocal)}; font-weight:600;`),
        td(l.fx0 != null || l.fx1 != null ? [
          h('span', { class: 'fx-pair' }, `${l.fx0 != null ? l.fx0.toFixed(4) : '-'} → ${l.fx1 != null ? l.fx1.toFixed(4) : '-'}`),
          h('span', { style: `margin-left:8px; color:${colColor(l.rFx)};` }, pctStr(l.rFx)),
        ] : '-'),
        td(ppStr(l.contrib), '', `color:${colColor(l.contrib)}; font-weight:600;`),
      ]))
      const sumRow = h('tr', { class: 'lt-sum' }, [
        td('合计', 'lt-left'),
        td(`${sumW.toFixed(2)}%`),
        dash(), dash(), dash(),
        td(ppStr(sumContrib), '', 'color:#0f172a;'),
      ])
      const residualRow = h('tr', { class: 'lt-residual' }, [
        td('未披露持仓（按 0% 变化）', 'lt-left'),
        td(`${residual.toFixed(2)}%`),
        dash(), dash(), dash(),
        td('0.00%'),
      ])
      const table = h('table', { class: 'leg-table' }, [
        h('thead', {}, h('tr', {}, [
          th('腿', 'left'), th('占净值'),
          // [AI-2026-10-08 东哥需求·锚点透明化] T-2 起点 = 基数所对应的交易日（anchor_date）。
          // 长假/停牌期间它会早于本行日期数天，加小字标注，避免把「自锚点累计涨跌」误读成"当日涨跌"。
          // 动态取 row.anchor_date（非硬编码日期）：普通行显示"自锚点 <前一交易日>"，长假显示"自锚点 09-29"。
          h('th', { class: 'lt-center' }, [
            h('div', {}, 'T-2→T-1 收盘'),
            row.anchor_date
              ? h('div', { style: 'font-size:10px; font-weight:400; color:#94a3b8; white-space:nowrap;' },
                  `（自锚点 ${String(row.anchor_date).slice(5)} 累计）`)
              : null,
          ]),
          th('标的涨跌'), th('汇率(前日→当日)'), th('估值影响'),
        ])),
        h('tbody', {}, [...legRows, sumRow, residualRow]),
      ])
      return h('div', { style: 'margin:2px 0 4px; background:#ffffff; border:1px solid #eef1f4; border-radius:10px; padding:2px 10px 8px; box-shadow:0 1px 2px rgba(15,23,42,.04);' }, [
        table,
        h('div', {
          style: `margin-top: 6px; font-size:11px; font-weight: ${row.fill_warning || row.carried_forward ? 600 : 400}; color: ${row.fill_warning ? '#cc4a3c' : (row.carried_forward ? '#d97706' : '#94a3b8')};`,
        }, `备注: ${row.note || '数据齐全'}${row.fill_warning ? '（⚠️ 非假期缺价，前填推算，请核实）' : ''}${row.carried_forward ? '（🔸 部分标的沿用上一交易日收盘价）' : ''}`),
      ])
    },
  },
  { title: '日期', key: 'date', width: 110, align: 'center' },
  { title: '官方净值', key: 'official_nav', width: 110, align: 'right', render(row: any) {
    return row.official_nav != null ? row.official_nav.toFixed(4) : '-'
  }},
  { title: 'T-1 持仓静态估值', key: 'holding_static_val', width: 110, align: 'right', render(row: any) {
    // [AI-2026-09-24 方案A] 美股价未入库的待补行：显式标「待补」，不留空让人误判丢行
    if (row.pending) return h('span', { style: 'color: #d97706; font-weight: 600;', title: row.note || '美股价未入库，该行待补' }, '待补')
    if (row.holding_static_val == null) return '-'
    const mark = row.fill_warning ? ' ⚠️' : (row.carried_forward ? ' 🔸' : '')
    return h('span', null, `${row.holding_static_val.toFixed(4)}${mark}`)
  }},
  { title: '误差%', key: 'err_pct', width: 100, align: 'right', render(row: any) {
    if (row.pending) return h('span', { style: 'color: #d97706;' }, '—')
    if (row.err_pct == null) {
      if (row.fill_warning) return h('span', { style: 'color: #dc2626; font-weight: 600;' }, '⚠️')
      if (row.carried_forward) return h('span', { style: 'color: #d97706; font-weight: 600;' }, '🔸')
      return '-'
    }
    const color = row.err_pct >= 0 ? '#dc2626' : '#16a34a'
    const mark = row.fill_warning ? ' ⚠️' : (row.carried_forward ? ' 🔸' : '')
    return h('span', { style: `font-weight: 600; color: ${color};` }, `${row.err_pct >= 0 ? '+' : ''}${row.err_pct.toFixed(2)}%${mark}`)
  }},
]

const loadPeriods = async () => {
  if (!fundCode.value) return
  try {
    const res = await getFundHoldingPeriods(fundCode.value)
    if (res.data?.status === 'ok') {
      periods.value = res.data.data || []
      // 默认选中最新一期；若 URL 已带 period 则优先
      const urlPeriod = route.query.period as string
      if (urlPeriod && periods.value.some((p: any) => p.period === urlPeriod)) {
        currentPeriod.value = urlPeriod
      } else if (periods.value.length > 0) {
        currentPeriod.value = periods.value[0].period
      }
    }
  } catch (e: any) {
    error.value = `获取报告期失败: ${e?.message || e}`
  }
}

const loadData = async () => {
  if (!fundCode.value || !currentPeriod.value) return
  loading.value = true
  error.value = ''
  // 切换基金/报告期时先清空旧估值，避免 freeze_incomplete 时残留上一个基金的显示
  valuation.value = null
  try {
    const [holdingsRes, valuationRes, penetrationRes, recalcRes, exposureRes] = await Promise.all([
      getFundHoldings(fundCode.value, currentPeriod.value),
      getFundHoldingRealtime(fundCode.value),
      getFundPenetration(fundCode.value, currentPeriod.value),
      getFundHoldingRecalc(fundCode.value, currentPeriod.value, '2026-07-01'),
      getFundHedgeExposure(fundCode.value),
    ])

    if (holdingsRes.data?.status === 'ok') {
      const d = holdingsRes.data.data || {}
      holdings.value = d.holdings || []
      regionDistribution.value = d.region_distribution || []
      prevPeriod.value = d.prev_period || ''
      exited.value = d.exited || []
      newIn.value = d.new_in || []
      reportDate.value = d.report_date || ''
    } else {
      error.value = holdingsRes.data?.message || '获取持仓失败'
    }

    // 无论 ok/error 都覆盖：error 时让旧值清空，避免切换基金后看到上一个基金的残留
    valuation.value = valuationRes.data?.data || null

    // [AI-2026-09-17] 对冲穿透（etf_contract_exposure）：底层 ETF 实际持有合约月
    hedgeExposure.value = exposureRes.data?.data || null

    if (penetrationRes.data?.status === 'ok') {
      penetrationData.value = penetrationRes.data.data || null
    } else if (penetrationRes.data?.fund_code) {
      // 穿透API直接返回数据（无status包装）
      penetrationData.value = penetrationRes.data || null
    } else {
      penetrationData.value = null
    }

    if (recalcRes.data?.status === 'ok') {
      const d = recalcRes.data.data || {}
      recalcRows.value = d.rows || []
      // [AI-2026-09-24 方案A] 待补日（美股价未入库）：随核心行一起刷新
      recalcPending.value = d.pending || []
      // 重新载入核心行后清空诊断 map（诊断细节仅在弹窗打开时按需取，避免陈旧合并）
      recalcDetailMap.value = {}
      // QDII 净值 T+2 公布：今天及最近未公布日 official_nav 为 null，
      // 取第一个 official_nav 非空的行作为"最新净值"
      const navRow = recalcRows.value.find((r: any) => r.official_nav != null)
      latestNav.value = navRow ? navRow.official_nav : null
      latestNavDate.value = navRow ? navRow.date : ''
      // [AI-2026-09-30 东哥需求] 持仓静态估值表已常驻主页面：载入核心行后立即取诊断明细
      // （etf_prices/fill_warning/note，展开行要用），不再等弹窗打开才取。
      if (recalcRows.value.length) void fetchRecalcDetail()
    } else {
      recalcRows.value = []
      recalcPending.value = []
      recalcDetailMap.value = {}
      latestNav.value = null
      latestNavDate.value = ''
    }
  } catch (e: any) {
    error.value = `加载失败: ${e?.message || e}`
  } finally {
    loading.value = false
  }
  // [AI-2026-09-24 东哥需求] 加载完成后异步检测 ARM 美股价新鲜度（失败静默，不挡页面）
  void checkPriceFreshness()
}

const switchPeriod = (period: string) => {
  currentPeriod.value = period
  router.replace({ query: { ...route.query, period } })
  loadData()
}

watch(fundCode, () => {
  loadPeriods().then(() => loadData())
})

watch(currentPeriod, () => {
  if (currentPeriod.value) loadData()
})

// [AI-2026-09-15] 自动同步 CL 分母：每天仅一次（localStorage 记当天日期），失败报警。
const autoSyncFreeze = async () => {
  const todayKey = new Date().toISOString().slice(0, 10)
  const lsKey = `cl_freeze_synced_${fundCode.value}_${todayKey}`
  if (localStorage.getItem(lsKey) === 'ok') {
    syncAlert.value = ''  // 今日已成功同步，清报警
    return
  }
  try {
    await syncFuturesFreeze()
    localStorage.setItem(lsKey, 'ok')
    syncAlert.value = ''
  } catch (e: any) {
    syncAlert.value = `⚠️ CL 分母（从 ARM）同步失败：${e?.message || e}。请检查 ARM 连接与采样，刷新页面可重试。`
  }
}

onMounted(() => {
  autoSyncFreeze()  // 进页面先自动拉 CL 分母（每天一次）
  loadPeriods().then(() => loadData())
})
</script>

<style scoped>
.holding-analysis-page {
  padding: 12px;
  color: #1f2937;
}
.shadow-soft {
  box-shadow: 0 2px 10px rgba(15, 23, 42, 0.05);
  border-radius: 8px;
  border: 1px solid #e5edf7;
}
.stat-card {
  background: #ffffff;
  border-radius: 8px;
  border: 1px solid #e5edf7;
}
/* 非假期缺价前填行：整行淡红醒目提示，便于人工核实数据漏抓 */
:deep(.recalc-warn-row td) {
  background-color: #fef2f2 !important;
}
:deep(.recalc-warn-row:hover td) {
  background-color: #fee2e2 !important;
}
/* 合法前填/沿用上一交易日行：整行淡琥珀提示（区别于红色真缺价报警） */
:deep(.recalc-carry-row td) {
  background-color: #fffbeb !important;
}
:deep(.recalc-carry-row:hover td) {
  background-color: #fef3c7 !important;
}
/* [AI-2026-09-24 方案A] 美股价未入库的「待补日」行：淡灰斜体，明示该日尚未生成（非程序丢行） */
:deep(.recalc-pending-row td) {
  background-color: #f8fafc !important;
  color: #94a3b8 !important;
  font-style: italic;
}
:deep(.recalc-pending-row:hover td) {
  background-color: #f1f5f9 !important;
}
</style>

<style>
/* [AI-2026-10-06] 季报持仓还原六腿表·清淡表格风：白卡+浅竖线分列+浅底表头+hover，涨跌色柔化（红#cc4a3c/绿#1e9e63）
   ⚠️ 必须放普通 <style>（非 scoped）：renderExpand 是传给 naive-ui DataTable 的渲染函数，
   表格 DOM 在 naive-ui 内部渲染、没有本组件的 data-v 属性，scoped 选择器（.leg-table[data-v-xxx]）永远不匹配。
   类名 .leg-table/.cur-pill 足够独特，全局块无泄漏风险。 */
.leg-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.leg-table th {
  text-align: right;
  font-weight: 600;
  font-size: 11px;
  /* [AI-2026-10-12 重做 表头统一蓝底/文字] 文字 #64748b→#33629c、背景 #f6f8fa→#edf3fb（padding 6px 10px 不动） */
  color: #33629c;
  padding: 6px 10px;
  background: #edf3fb;
  border-bottom: 1px solid #e8eaed;
  border-right: 1px solid #eef1f4;
  white-space: nowrap;
}
.leg-table th:last-child { border-right: none; }
.leg-table td {
  padding: 6px 10px;
  border-bottom: 1px solid #f1f5f9;
  border-right: 1px solid #f6f8fa;
  color: #475569;
  text-align: right;
  white-space: nowrap;
}
.leg-table td:last-child { border-right: none; }
.leg-table tbody tr:hover {
  background: #f8fafc;
}
.leg-table .lt-left { text-align: left; }
.leg-table .lt-center { text-align: center; }
.leg-table .lt-muted { color: #cbd5e1; }
.leg-table .lt-sum td {
  border-top: 1px solid #e8eaed;
  border-bottom: none;
  background: #fafbfc;
  font-weight: 600;
  color: #0f172a;
}
.leg-table .lt-residual td {
  border-bottom: none;
  color: #94a3b8;
  font-style: italic;
}
.cur-pill {
  display: inline-block;
  margin-left: 6px;
  padding: 0 4px;
  border-radius: 3px;
  background: #eef3f8;
  color: #5b7fa6;
  font-size: 9px;
  font-weight: 500;
  line-height: 14px;
  vertical-align: 1px;
}
.fx-pair { color: #64748b; }
.fx-arrow { color: #cbd5e1; margin: 0 4px; }
</style>
