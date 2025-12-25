/* =========================================================
   主加载函数：同屏加载（左收益曲线 + 右最新记录）
========================================================= */
async function loadData() {
  const limit = Math.max(
    1,
    Math.min(300, parseInt(document.getElementById("limit")?.value || "20", 10))
  );

  try {
    // 并行请求：收益曲线 + 最新记录
		const [profitRes, latestRes, statsRes] = await Promise.all([
			fetch("/profit_curve").then(r => r.json()),
			fetch(`/latest?limit=${limit}`).then(r => r.json()),
			fetch("/stats").then(r => r.json())
		]);

    renderProfit(profitRes);
    renderLatest(latestRes, statsRes);

  } catch (err) {
    // 左侧报错
    const meta = document.getElementById("profit_meta");
    const chartEl = document.getElementById("profit_chart");
    if (meta) meta.innerHTML = `<span style="color:#ff5252">加载失败：${err}</span>`;
    if (chartEl) chartEl.innerHTML = `<div style="padding:14px;color:#ff5252;">${err}</div>`;

    // 右侧报错
    const statsWrap = document.getElementById("stats_wrap");
    if (statsWrap) statsWrap.innerHTML = "";

    const latestWrap = document.getElementById("latest_wrap");
    if (latestWrap) {
      latestWrap.innerHTML = `<div class="card"><b>加载失败：</b><br>${err}</div>`;
    }
  }
}

/* =========================================================
   左侧：收益曲线渲染
   后端 /profit_curve 返回：
   { count, initial_equity, data: curve }
   curve 通常是 [{ts, equity}, ...]
========================================================= */
function renderProfit(data) {
  const meta = document.getElementById("profit_meta");
  const chartWrap = document.getElementById("profit_chart");

  if (!meta || !chartWrap) return;

  const list = (data && Array.isArray(data.data)) ? data.data : [];
  const initialEquity = Number(data?.initial_equity || 0);

  if (!Array.isArray(list) || list.length === 0 || initialEquity <= 0) {
    meta.textContent = "暂无收益数据";
    chartWrap.innerHTML = `<div style="padding:14px;color:#b5b5b5;">暂无收益数据</div>`;
    return;
  }

  // 兼容：最后一个点可能是 {equity} 或 [ts, equity]
  const last = list[list.length - 1];
  const equity = Array.isArray(last) ? Number(last[1] || 0) : Number(last.equity || 0);

  const unrealizedProfit = equity - initialEquity;
  const profitPct = ((unrealizedProfit / initialEquity) * 100).toFixed(2);

  meta.innerHTML = `
    初始权益：<b>${initialEquity.toFixed(2)} USDT</b>
    &nbsp;&nbsp;
    当前权益：<b>${equity.toFixed(2)} USDT</b>
    &nbsp;&nbsp;
    <span style="color:${unrealizedProfit >= 0 ? '#00c853' : '#ff5252'}">
      未实现盈亏：
      ${unrealizedProfit >= 0 ? '+' : ''}${unrealizedProfit.toFixed(2)} USDT
      (${profitPct}%)
    </span>
  `;

  // 左侧容器里直接画图
  drawProfitChart(list, initialEquity, "profit_chart");
}

/* =========================================================
   右侧：统计条渲染（总交易数/盈利/亏损/总决策次数）
   当前：总决策次数 = 最新 response 的 signals 数量
   其它三项先占位（--），后续接交易明细接口再补
========================================================= */
function renderStatsFromLatest(latestData, statsData, nShown) {
  const statsWrap = document.getElementById("stats_wrap");
  if (!statsWrap) return;

	const decisionCount =
		typeof statsData?.total_decisions === "number"
			? statsData.total_decisions
			: "--";

  // 交易统计：暂时无数据来源，先占位
  const totalTrades = "--";
  const winCount = "--";
  const lossCount = "--";

  statsWrap.innerHTML = `
    <div class="card" style="padding:12px 14px;margin-bottom:14px;">
      <div class="title" style="margin-bottom:10px;">📊 统计</div>
      <div class="stats-grid-4" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;">
        <div style="background:#181c27;border-radius:10px;padding:12px;border:1px solid #1d2330;">
          <div style="font-size:13px;color:#b5b5b5;margin-bottom:6px;">总交易数</div>
          <div style="font-size:20px;font-weight:800;color:#ff5252;">${totalTrades}</div>
        </div>
        <div style="background:#181c27;border-radius:10px;padding:12px;border:1px solid #1d2330;">
          <div style="font-size:13px;color:#b5b5b5;margin-bottom:6px;">盈利次数</div>
          <div style="font-size:20px;font-weight:800;color:#ff5252;">${winCount}</div>
        </div>
        <div style="background:#181c27;border-radius:10px;padding:12px;border:1px solid #1d2330;">
          <div style="font-size:13px;color:#b5b5b5;margin-bottom:6px;">亏损次数</div>
          <div style="font-size:20px;font-weight:800;color:#ff5252;">${lossCount}</div>
        </div>
        <div style="background:#181c27;border-radius:10px;padding:12px;border:1px solid #1d2330;">
          <div style="font-size:13px;color:#b5b5b5;margin-bottom:6px;">总决策次数</div>
          <div style="font-size:20px;font-weight:800;color:#ff5252;">${decisionCount}</div>
        </div>
      </div>
      <div style="margin-top:10px;font-size:12px;color:#777;">
        当前展示：最新 ${nShown} 条（统计按“最新一条”计算）
      </div>
    </div>
  `;
}

/* =========================================================
   右侧：最新一次(Request+Response) 渲染
   /latest 返回：{ request: [], response: [] }
========================================================= */
function renderLatest(data, statsData) {
  const wrap = document.getElementById("latest_wrap");
  if (!wrap) return;

  const reqs = Array.isArray(data?.request) ? data.request : [];
  const ress = Array.isArray(data?.response) ? data.response : [];

  if (!reqs.length || !ress.length) {
    wrap.innerHTML = `<div class="card"><b>无最新记录</b></div>`;
    const statsWrap = document.getElementById("stats_wrap");
    if (statsWrap) statsWrap.innerHTML = "";
    return;
  }

  const n = Math.min(reqs.length, ress.length);

  // ✅ 先渲染统计（右侧最上方）
  renderStatsFromLatest(data, statsData, n);

  wrap.innerHTML = ""; // 清空

  for (let i = 0; i < n; i++) {
    const r = reqs[i] || {};
    const s = ress[i] || {};

    const ts = s.timestamp ? new Date(s.timestamp * 1000).toLocaleString() : "（无时间）";
    const reasoning = s.reasoning || "（无分析内容）";
    const signals = s.signals || [];
    const prettySignals = JSON.stringify(signals, null, 2);

    // r.request 可能不存在，做兼容
    const requestText = (typeof r.request === "string")
      ? r.request
      : JSON.stringify(r, null, 2);

    wrap.innerHTML += `
      <div class="card">
        <div class="title">🧠 AIBTC.VIP 决策</div>
        <div class="time">时间：${ts}</div>

        <div class="section collapsible">
          <button class="toggle">📌 展开/折叠投喂内容</button>
          <div class="content" style="display:none;">
            <pre>${escapeHtml(requestText)}</pre>
          </div>
        </div>

        <div class="section collapsible">
          <button class="toggle">📌 展开/折叠推理内容</button>
          <div class="content" style="display:none;">
            <pre>${escapeHtml(reasoning)}</pre>
          </div>
        </div>

        <div class="section collapsible">
          <button class="toggle">🚨 展开/折叠 AI 最终交易信号</button>
          <button class="copy" data-json="${encodeURIComponent(prettySignals)}">📋 复制 JSON</button>
          <div class="content" style="display:block;">
            <pre class="json">${syntaxHighlight(prettySignals)}</pre>
          </div>
        </div>
      </div>
    `;
  }

  bindButtons();
}

/* =========================================================
   折叠 + 复制绑定
========================================================= */
function bindButtons() {
  // 折叠
  document.querySelectorAll(".section.collapsible .toggle").forEach(btn => {
    btn.onclick = () => {
      const content = btn.closest(".section.collapsible").querySelector(".content");
      content.style.display =
        (content.style.display === "none" || !content.style.display)
          ? "block"
          : "none";
    };
  });

  // 复制 JSON
  document.querySelectorAll(".section.collapsible .copy").forEach(btn => {
    btn.onclick = () => {
      const raw = decodeURIComponent(btn.getAttribute("data-json") || "");
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(raw);
      } else {
        const ta = document.createElement("textarea");
        ta.value = raw;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      alert("📋 JSON 已复制");
    };
  });
}

/* =========================================================
   JSON 代码高亮
========================================================= */
function syntaxHighlight(json) {
  json = json
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  return json.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(\.\d+)?)/g,
    match => {
      let cls = "number";
      if (/^"/.test(match)) cls = /:$/.test(match) ? "key" : "string";
      else if (/true|false/.test(match)) cls = "boolean";
      else if (/null/.test(match)) cls = "null";
      return `<span class="${cls}">${match}</span>`;
    }
  );
}

/* =========================================================
   画收益曲线：支持指定容器 id
========================================================= */
function drawProfitChart(data, initialEquity, containerId) {
  if (!initialEquity || initialEquity <= 0) {
    console.warn("initialEquity invalid:", initialEquity);
    return;
  }

  const el = document.getElementById(containerId || "profit_chart");
  if (!el) return;

  const chart = echarts.init(el);

  // 兼容两种结构：
  // 1) [{ts, equity}, ...]
  // 2) [[ts, equity], ...]
  const x = data.map(i => {
    const ts = Array.isArray(i) ? i[0] : i.ts;
    const d = new Date(ts);
    return isNaN(d.getTime()) ? String(ts) : d.toLocaleTimeString();
  });

  const y = data.map(i => {
    const eq = Array.isArray(i) ? i[1] : i.equity;
    return Number(eq);
  });

  const baseLine = data.map(() => initialEquity);

  chart.setOption({
    backgroundColor: "#111319",
    tooltip: {
      trigger: "axis",
      formatter: params => {
        const equity = Number(params[0].value);
        const profit = equity - initialEquity;
        const pct = ((profit / initialEquity) * 100).toFixed(2);

        return `
          <b>权益：</b>${equity.toFixed(2)} USDT<br/>
          <b>盈亏：</b>
          <span style="color:${profit >= 0 ? '#00c853' : '#ff5252'}">
            ${profit >= 0 ? '+' : ''}${profit.toFixed(2)} USDT (${pct}%)
          </span>
        `;
      }
    },
    grid: { left: 55, right: 20, top: 30, bottom: 55 },
    xAxis: {
      type: "category",
      data: x,
      axisLabel: { color: "#aaa" }
    },
    yAxis: {
      type: "value",
      axisLabel: { color: "#aaa" },
      scale: true
    },
    series: [
      {
        name: "账户权益",
        type: "line",
        data: y,
        smooth: true,
        symbol: "circle",
        symbolSize: 6,
        lineStyle: { width: 3 },
        areaStyle: { opacity: 0.15 }
      },
      {
        name: "初始资金",
        type: "line",
        data: baseLine,
        symbol: "none",
        lineStyle: {
          type: "dashed",
          width: 2,
          color: "#888"
        }
      }
    ]
  });

  window.addEventListener("resize", () => chart.resize());
}

/* =========================================================
   防 XSS：把文本转义
========================================================= */
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
