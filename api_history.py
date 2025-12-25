import os
import json
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from typing import Optional
from database import redis_client
from fastapi.staticfiles import StaticFiles

KEY_REQ = "deepseek_analysis_request_history"
KEY_RES = "deepseek_analysis_response_history"

app = FastAPI(title="DeepSeek Analysis History API")

def _read_list(key: str, limit: int):
    # 从 Redis 获取最新 limit 条（最右侧为最新）
    items = redis_client.lrange(key, -limit, -1)
    result = []

    # 反转顺序，最新在前
    items = list(reversed(items))

    for item in items:
        try:
            obj = json.loads(item)
        except Exception:
            continue

        # 🔥 新结构：如果是 list，自动展开
        if isinstance(obj, list):
            result.extend(obj)
        else:
            result.append(obj)

    return result

@app.get("/latest")
async def get_latest_pair(limit: int = Query(1, ge=1, le=300)):
    reqs = redis_client.lrange(KEY_REQ, -limit, -1)
    ress = redis_client.lrange(KEY_RES, -limit, -1)
    reqs = list(reversed(reqs))
    ress = list(reversed(ress))

    def safe(x):
        if not x:
            return None
        try:
            return json.loads(x)
        except:
            return {"raw": x}

    return {
        "request": [safe(r) for r in reqs],
        "response": [safe(r) for r in ress]
    }

app.mount("/static", StaticFiles(directory="static"), name="static")
# ----------------- HTML 页面 -----------------
html_page = """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>AIBTC.VIP</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
body {
    background: #0b0c10;
    color: #e8e8e8;
    font-family: "Inter", "Consolas", sans-serif;
    margin: 0;
    padding: 20px;
}

.card {
    background: #111319;
    border: 1px solid #1d2330;
    border-radius: 10px;
    padding: 16px;
    margin-bottom: 22px;
    box-shadow: 0 0 18px rgba(0, 0, 0, 0.45);
}

.card .title {
    font-size: 18px;
    font-weight: bold;
    margin-bottom: 6px;
    color: #5ab2ff;
}

.card .time {
    font-size: 13px;
    margin-bottom: 10px;
    color: #b5b5b5;
}

.card .section {
    background: #181c27;
    border-radius: 8px;
    padding: 12px;
    margin-top: 12px;
    overflow-x: auto;
}

/* JSON 折叠区域 */
.section.collapsible .toggle,
.section.collapsible .copy {
    padding: 6px 12px;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    margin-right: 6px;
    font-size: 14px;
    margin-bottom: 8px;
}

.section.collapsible .toggle {
    background: #2b78ff;
    color: white;
}
.section.collapsible .copy {
    background: #00c853;
    color: white;
}

.section.collapsible .toggle:hover {
    background: #1f62d3;
}
.section.collapsible .copy:hover {
    background: #009842;
}

/* 🧠 分析内容换行（reasoning 区域） */
pre:not(.json) {
    white-space: pre-wrap;
    word-wrap: break-word;
    line-height: 1.55;
    font-size: 15px;
    max-height: 360px;
    overflow-y: auto;
}

/* 🔥 JSON 高亮（保持缩进格式，不换行） */
pre.json {
    background: #0f1118;
    padding: 14px;
    border-radius: 8px;
    font-family: Consolas, monospace;
    font-size: 14px;
    line-height: 1.45;
    white-space: pre;
    overflow-x: auto;
}

pre.json .key { color: #ffca5f; }
pre.json .string { color: #7cd6ff; }
pre.json .number { color: #9aff6b; }
pre.json .boolean { color: #ff9e52; }
pre.json .null { color: #ff6363; }

/* 顶部区域选择框美化 */
.controls {
    margin-bottom: 18px;
    display: flex;
    justify-content: flex-end; /* 🔥 推到最右 */
    align-items: center;
    gap: 8px;
}

.controls select, .controls input {
    background: #10131a;
    color: white;
    border: 1px solid #2a3143;
    border-radius: 6px;
    padding: 6px 10px;
    margin-right: 8px;
}

.controls button {
    background: #2b78ff;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
    cursor: pointer;
}
.controls button:hover {
    background: #1b5ecd;
}

/* 滚动条美化 */
::-webkit-scrollbar {
    width: 8px;
    height: 8px;
}
::-webkit-scrollbar-track {
    background: #0f1118;
}
::-webkit-scrollbar-thumb {
    background: #313748;
    border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
    background: #495168;
}

/* ===================== 新增：双栏布局 ===================== */
.layout {
    display: grid;
    grid-template-columns: 1.2fr 0.8fr; /* 左边收益曲线更宽，右边日志更窄 */
    gap: 18px;
    align-items: start;
}

.panel {
    min-width: 0;            /* 防止 grid 子项溢出导致横向滚动 */
    display: flex;           /* ✅ 关键：让 panel 变成 flex 容器 */
    flex-direction: column;  /* ✅ 关键：从上往下堆叠，顶部对齐 */
}

/* 小屏自动改成上下布局 */
@media (max-width: 1100px) {
    .layout {
        grid-template-columns: 1fr;
    }
    #profit_chart {
        height: 420px !important;
    }
}

/* 次数统计 */
.stats-grid{
    display: grid;
    grid-template-columns: repeat(2, 1fr); /* 右侧窄，用 2 列更合适 */
    gap: 12px;
    margin-bottom: 14px;
}

.stat-card{
    background: #111319;
    border: 1px solid #1d2330;
    border-radius: 10px;
    padding: 12px 14px;
    box-shadow: 0 0 18px rgba(0, 0, 0, 0.35);
}

.stat-card .k{
    font-size: 13px;
    color: #b5b5b5;
    margin-bottom: 6px;
}

.stat-card .v{
    font-size: 20px;
    font-weight: 800;
    color: #ff5252; /* 你红框是红色感受，这里用红 */
    letter-spacing: 0.5px;
}

@media (max-width: 1100px){
  #stats_wrap .stats-grid-4{
    grid-template-columns: repeat(2, 1fr) !important;
  }
}
</style>
</head>
<body>

<div class="controls">
    <label>AI 决策条数：</label>
    <input id="limit" type="number" value="1" min="1" max="300" style="width:60px;">
    <button onclick="loadData()">刷新</button>
</div>

<!-- 🔥 页面核心展示区域：左收益曲线 + 右最新请求 -->
<div class="layout">
    <!-- 左：收益曲线 -->
    <div class="panel left">
      <div class="card">
        <div class="title">账户收益曲线</div>
        <div class="time" id="profit_meta"></div>
        <div id="profit_chart" style="height:520px;"></div>
      </div>

      <!-- ✅ 统计放左侧最下面 -->
      <div id="stats_wrap"></div>
    </div>

    <!-- 右：最新一次 Request + Response -->
    <div class="panel right">
        <!-- 这里交给 history.js 渲染 -->
        <div id="latest_wrap"></div>
    </div>
</div>

<script src="/static/history.js"></script>
<script>
    window.onload = () => loadData();
</script>
</body>
</html>
"""

@app.get("/stats")
async def get_stats():
    try:
        total_decisions = redis_client.llen(KEY_RES)
    except Exception:
        total_decisions = 0

    return {
        "total_decisions": total_decisions
    }
    
@app.get("/", response_class=HTMLResponse)
async def history_page():
    return HTMLResponse(html_page)
# --------------------------------------------------

@app.get("/profit_curve")
async def get_profit_curve():
    raw_curve = redis_client.hget("profit:ultra_simple", "curve")
    raw_initial = redis_client.hget("profit:ultra_simple", "initial_equity")

    if not raw_curve:
        return {
            "count": 0,
            "initial_equity": None,
            "data": []
        }

    try:
        curve = json.loads(raw_curve)
    except Exception:
        curve = []

    try:
        initial_equity = float(raw_initial) if raw_initial else None
    except Exception:
        initial_equity = None

    return {
        "count": len(curve),
        "initial_equity": initial_equity,
        "data": curve
    }

if __name__ == "__main__":
    filename = os.path.basename(__file__).replace(".py", "")
    uvicorn.run(
        f"{filename}:app",
        host="0.0.0.0",
        port=8600,
        reload=True
    )

def run_api_server():
    uvicorn.run(
        "api_history:app",
        host="0.0.0.0",
        port=8600,
        reload=False,
        access_log=False,   # ✅ 关闭访问日志
        log_level="warning" # ✅ 可选：减少其它INFO
    )
