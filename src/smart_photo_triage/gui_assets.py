"""Self-contained v1.2.1 local GUI assets. No CDN or third-party browser code."""

# ruff: noqa: E501

HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Smart Photo Triage v1.2.1</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body data-page="home">
  <header class="hero">
    <div class="hero__brand">
      <div class="brand-mark" aria-hidden="true">SPT</div>
      <div><p class="eyebrow">本地照片整理工具</p><h1>Smart Photo Triage <span>v1.2.1</span></h1></div>
    </div>
    <div class="hero__right"><nav class="app-nav" aria-label="主导航"><a class="app-nav__link app-nav__link--active" href="/">开始整理</a><a class="app-nav__link" href="/settings">模型与隐私</a></nav><div class="hero__status"><span class="dot"></span>仅本机回环访问 · 默认不上传</div></div>
  </header>
  <main>
    <section class="panel workspace-panel" aria-labelledby="workspace-title">
      <div class="section-heading"><div><p class="eyebrow">01 · 工作位置</p><h2 id="workspace-title">选择本次整理的位置</h2></div><p>扫描与准备保持只读。真实复制前会要求二次确认。</p></div>
      <div class="directory-grid">
        <label class="field field--wide"><span>工作区</span><input id="workspace" readonly><small>保存索引、预览、审计和可回滚记录，不存原片。</small></label>
        <button class="browse" type="button" data-pick="workspace">选择工作区</button>
        <label class="field field--wide"><span>照片源</span><input id="source" placeholder="选择需要扫描的照片目录"><small>源目录只读，系统不会移动或删除这里的文件。</small></label>
        <button class="browse" type="button" data-pick="source">选择照片源</button>
        <label class="field field--wide"><span>输出目录</span><input id="output" placeholder="选择整理副本的输出目录"><small>仅第 8 步真实复制会在这里创建副本。</small></label>
        <button class="browse" type="button" data-pick="output">选择输出目录</button>
      </div>
      <p id="picker-note" class="hint">桌面版可使用 Windows 原生文件夹选择器。浏览器模式请手动输入完整路径。</p>
    </section>

    <section class="panel model-summary" aria-labelledby="model-summary-title">
      <div><p class="eyebrow">本次分析方式</p><h2 id="model-summary-title">已使用保存的模型设置</h2><p>通常不需要在每次整理时修改。只有更换模型或调整联网权限时才进入设置页。</p></div>
      <div class="model-summary__status"><div><span>单张照片</span><strong id="item-model-summary">正在读取设置…</strong></div><div><span>连拍照片</span><strong id="burst-model-summary">正在读取设置…</strong></div><div><span>联网状态</span><strong id="privacy-summary">正在读取设置…</strong></div></div>
      <a class="button button--secondary" href="/settings">打开模型与隐私设置</a>
    </section>

    <section class="panel workflow-panel" aria-labelledby="workflow-title">
      <div class="section-heading"><div><p class="eyebrow">03 · 安全流程</p><h2 id="workflow-title">执行步骤</h2></div><p>先审阅，后计划。最后一步才可能产生文件副本。</p></div>
      <div class="workflow-grid">
        <article class="flow-card"><span class="flow-number">01</span><h3>扫描</h3><p>建立只读索引并验证目录边界。</p><button class="button button--secondary" type="button" data-action="scan">扫描照片源</button></article>
        <article class="flow-card"><span class="flow-number">02</span><h3>准备与分析</h3><p>生成受控预览，并按上方的模型设置执行分析。</p><button class="button button--primary" type="button" data-action="prepare">准备并分析</button></article>
        <article class="flow-card"><span class="flow-number">03</span><h3>人工审阅</h3><p>在单独页面确认或调整 AI 建议。</p><button class="button button--secondary" type="button" data-action="review">打开审阅界面</button></article>
        <article class="flow-card"><span class="flow-number">04</span><h3>生成计划</h3><p>仅根据已审阅的决定生成不可变复制计划。</p><button class="button button--secondary" type="button" data-action="plan">生成复制计划</button></article>
      </div>
      <div class="plan-context"><div><p class="eyebrow">执行准备</p><h3>确认本次复制计划</h3><p>第 4 步生成后会自动填入。后续每一步都只针对这份计划执行。</p></div><label class="field"><span>计划编号</span><input id="plan_id" placeholder="生成计划后自动填入"></label></div>
      <div class="workflow-grid workflow-grid--execution">
        <article class="flow-card"><span class="flow-number">05</span><h3>批准计划</h3><p>确认这份计划可以进入执行准备阶段。</p><button class="button button--secondary" type="button" data-action="approve">批准计划</button></article>
        <article class="flow-card"><span class="flow-number">06</span><h3>开始前检查</h3><p>检查文件状态、空间和输出目录是否符合条件。</p><button class="button button--secondary" type="button" data-action="preflight">开始前检查</button></article>
        <article class="flow-card"><span class="flow-number">07</span><h3>模拟执行</h3><p>先验证将要创建的副本，不会实际写入文件。</p><button class="button button--secondary" type="button" data-action="dry-run">开始模拟</button></article>
        <article class="flow-card flow-card--danger"><span class="flow-number">08</span><h3>真实复制</h3><p>创建副本前会再次确认，原照片不会移动或删除。</p><button class="button button--danger" type="button" data-action="execute">真实复制</button></article>
      </div>
      <section class="copy-progress" aria-live="polite"><p class="eyebrow">复制进度</p><strong id="copy-progress-title">尚未开始复制</strong><p id="copy-progress-detail">真实复制会作为后台任务运行。此页面可持续显示文件、字节、速度和可恢复状态。</p></section>
      <div class="workflow-tools"><button class="button button--ghost" type="button" data-action="doctor">检查工作区</button><p class="hint">不确定下一步时，可先检查工作区状态。它不会修改任何文件。</p></div>
      <details class="rollback"><summary>需要撤回已执行的复制？</summary><div><label class="field"><span>复制记录编号</span><input id="transaction_id" placeholder="真实复制后自动填入"></label><button class="button button--ghost" type="button" data-action="rollback">安全回滚</button></div><p class="hint">点击回滚后会再次询问您，确认后才会删除本次创建的副本。</p></details>
    </section>
    <section class="result-panel" aria-label="操作结果"><div class="result-panel__header"><span class="dot"></span><span>本地操作记录</span></div><pre id="result">正在连接本地控制台…</pre></section>
  </main>
  <script src="/app.js"></script>
  <script src="/copy-progress.js"></script>
</body>
</html>"""

SETTINGS_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>模型与隐私设置 · Smart Photo Triage v1.2.1</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body data-page="settings">
  <header class="hero">
    <div class="hero__brand">
      <div class="brand-mark" aria-hidden="true">SPT</div>
      <div><p class="eyebrow">本地照片整理工具</p><h1>Smart Photo Triage <span>v1.2.1</span></h1></div>
    </div>
    <div class="hero__right"><nav class="app-nav" aria-label="主导航"><a class="app-nav__link" href="/">开始整理</a><a class="app-nav__link app-nav__link--active" href="/settings">模型与隐私</a></nav><div class="hero__status"><span class="dot"></span>仅本机回环访问 · 默认不上传</div></div>
  </header>
  <main>
    <section class="panel ai-panel" aria-labelledby="ai-title">
      <div class="section-heading"><div><p class="eyebrow">模型与隐私</p><h2 id="ai-title">分析服务设置</h2></div><p>这些设置会保存到当前工作区，日常整理时无需重复配置。</p></div>
      <div class="privacy-strip"><span class="privacy-strip__icon">◎</span><p><strong>隐私提示：</strong>默认不会联网。启用互联网模型前需要您明确确认；API 密钥会使用当前 Windows 用户加密保存，不会以明文写入配置、数据库或日志。</p></div>
      <div class="ai-grid">
        <div class="subpanel">
          <div class="subpanel__heading"><h3>添加或修改分析服务</h3><span class="tag">可添加多个</span></div>
          <div class="two-col">
            <label class="field"><span>配置编号</span><input id="provider_id" value="primary_vision" placeholder="例如 qwen_fast"><small>用于系统区分不同服务。可使用英文、数字和下划线。</small></label>
            <label class="field"><span>页面显示名称（可选）</span><input id="display_name" placeholder="例如 我的主分析模型"><small>这是您在下方列表中看到的名称。</small></label>
          </div>
          <div class="two-col">
            <label class="field"><span>模型服务</span><select id="driver"><option value="fake">离线演示（不会联网或上传）</option><option value="gemini">Google Gemini</option><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option><option value="deepseek">DeepSeek（视觉模型）</option><option value="openai_compatible">其他兼容服务（Qwen / 豆包 / GLM / 本地模型等）</option></select><small>选择服务商后会自动填入官方服务地址。兼容服务与本地模型请自行填写地址。</small></label>
            <label class="field"><span>模型名称</span><input id="model" value="fake-v1" placeholder="例如 gpt-4.1-mini 或服务商提供的模型名称"><small>请以服务商控制台显示的模型名称为准。</small></label>
          </div>
          <label class="field"><span>服务地址（高级设置）</span><input id="base_url" placeholder="官方 Gemini、OpenAI、Anthropic 可留空；其他服务请填写地址"><small>仅使用其他兼容服务或本地模型时才需要填写。互联网地址必须使用 HTTPS。</small></label>
          <label class="field"><span>API 密钥</span><input id="api_key" type="password" autocomplete="new-password" placeholder="粘贴服务商提供的 API 密钥"><small>保存后会加密保存在当前 Windows 用户账户中。下次启动自动可用，页面不会再显示密钥。</small></label>
          <details class="provider-advanced"><summary>高级选项：从环境变量读取密钥</summary><label class="field"><span>环境变量名称</span><input id="api_key_env" placeholder="例如 OPENAI_API_KEY"><small>可选。只在您已自行设置环境变量时使用。</small></label></details>
          <div class="provider-actions"><button class="button button--primary" type="button" data-action="save-provider">保存分析服务</button><button class="button button--ghost" type="button" data-action="forget-provider-key">清除已保存密钥</button></div>
        </div>
        <div class="subpanel registry-panel">
          <div class="subpanel__heading"><h3>已添加的分析服务</h3><span id="provider-count" class="tag">0</span></div>
          <div id="provider-list" class="provider-list" aria-live="polite"></div>
          <p class="hint">“访问密钥已准备”表示已保存密钥或相应环境变量可用。页面不会显示密钥内容。</p>
        </div>
      </div>
      <div class="route-grid">
        <div class="subpanel">
          <div class="subpanel__heading"><h3>单张照片分析</h3><span class="tag">逐张判断</span></div>
          <label class="field"><span>优先使用的模型</span><select id="item_primary"></select></label>
          <label class="field"><span>备用模型（可选）</span><input id="item_fallbacks" placeholder="填写配置编号，用英文逗号隔开，例如 local_vlm,openai_strong"><small>只有当前模型临时不可用时，才会按从左到右的顺序尝试备用模型。</small></label>
          <div class="two-col"><label class="field"><span>把握不足的分数线（可选）</span><input id="item_confidence_below" inputmode="decimal" placeholder="例如 0.72"><small>介于 0 和 1。低于此值时可改用更强模型。</small></label><label class="field"><span>改用更强的模型（可选）</span><select id="item_escalate_to"></select><small>不设置则保留当前结果供您人工审阅。</small></label></div>
        </div>
        <div class="subpanel">
          <div class="subpanel__heading"><h3>连拍照片复核</h3><span class="tag">多张对比</span></div>
          <label class="field"><span>优先使用的模型</span><select id="burst_primary"></select></label>
          <label class="field"><span>备用模型（可选）</span><input id="burst_fallbacks" placeholder="填写配置编号，用英文逗号隔开，例如 local_vlm,openai_strong"><small>只有当前模型临时不可用时，才会按从左到右的顺序尝试备用模型。</small></label>
          <div class="two-col"><label class="field"><span>把握不足的分数线（可选）</span><input id="burst_confidence_below" inputmode="decimal" placeholder="例如 0.72"><small>介于 0 和 1。低于此值时可改用更强模型。</small></label><label class="field"><span>改用更强的模型（可选）</span><select id="burst_escalate_to"></select><small>不设置则保留当前结果供您人工审阅。</small></label></div>
        </div>
      </div>
      <div class="route-actions"><button class="button button--secondary" type="button" data-action="save-routes">保存模型使用方式</button><p class="hint">仅当模型出现临时错误时才会尝试备用模型。未经您的互联网授权，本地模型失败也不会上传预览图。</p></div>
      <details class="privacy-settings"><summary>允许连接模型服务（默认关闭）</summary><div class="privacy-settings__body"><label class="check"><input id="allow_cloud" type="checkbox">允许向互联网模型服务发送受控预览图和匿名质量指标</label><label class="field"><span>勾选后输入确认词</span><input id="cloud_confirmation" placeholder="ALLOW_CLOUD"><small>这是一次明确的联网确认，不是访问密钥。</small></label><label class="check"><input id="allow_lan" type="checkbox">允许连接局域网内的模型服务</label><label class="field"><span>勾选后输入确认词</span><input id="lan_confirmation" placeholder="ALLOW_LAN"><small>仅当您信任局域网内的模型服务时启用。</small></label><button class="button button--secondary" type="button" data-action="configure-privacy">保存联网设置</button></div></details>
    </section>
    <section class="result-panel" aria-label="操作结果"><div class="result-panel__header"><span class="dot"></span><span>设置结果</span></div><pre id="result">正在读取当前模型设置…</pre></section>
  </main>
  <script src="/app.js"></script>
</body>
</html>"""

CSS = """:root{--ink:#17233b;--muted:#65728a;--line:#dbe3f0;--canvas:#f3f6fb;--surface:#fff;--blue:#2463eb;--blue-dark:#1748b4;--teal:#0f8575;--danger:#c83d45;--shadow:0 18px 48px rgba(30,52,89,.09)}*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);font:15px/1.5 Inter,"Microsoft YaHei",system-ui,sans-serif}.hero{max-width:1240px;margin:0 auto;padding:42px 42px 26px;display:flex;align-items:center;justify-content:space-between;gap:24px}.hero__brand{display:flex;align-items:center;gap:17px}.brand-mark{display:grid;place-items:center;width:52px;height:52px;border-radius:16px;background:linear-gradient(145deg,#2358d7,#35a7d5);color:#fff;font-weight:800;letter-spacing:.03em;box-shadow:0 10px 24px rgba(36,99,235,.25)}.eyebrow{margin:0 0 4px;font-size:11px;font-weight:750;letter-spacing:.12em;color:var(--blue)}h1,h2,h3,p{margin-top:0}h1{margin-bottom:0;font-size:clamp(26px,4vw,37px);letter-spacing:-.035em}h1 span{vertical-align:middle;font-size:13px;font-weight:700;letter-spacing:0;color:var(--blue);background:#e6efff;border-radius:999px;padding:4px 9px}.hero__right{display:flex;align-items:center;gap:16px}.app-nav{display:flex;gap:4px;padding:4px;background:#eaf0fb;border-radius:10px}.app-nav__link{padding:7px 10px;border-radius:7px;color:#48617f;font-size:13px;font-weight:700;text-decoration:none}.app-nav__link--active{background:#fff;color:var(--blue);box-shadow:0 1px 4px rgba(32,61,111,.13)}.hero__status{color:#406075;font-size:13px;white-space:nowrap}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#22a17f;box-shadow:0 0 0 4px rgba(34,161,127,.13);margin-right:8px}main{max-width:1240px;margin:0 auto;padding:0 30px 46px}.panel,.result-panel{background:var(--surface);border:1px solid rgba(211,222,238,.85);border-radius:18px;box-shadow:var(--shadow);padding:30px;margin:18px 0}.section-heading{display:flex;justify-content:space-between;gap:24px;border-bottom:1px solid var(--line);padding-bottom:19px;margin-bottom:23px}.section-heading h2{margin-bottom:0;font-size:22px;letter-spacing:-.025em}.section-heading>p{max-width:380px;margin:13px 0 0;color:var(--muted);font-size:13px;text-align:right}.directory-grid{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px 14px;align-items:start}.field{display:block;min-width:0}.field>span{display:block;margin:0 0 6px;font-size:13px;font-weight:700;color:#344159}.field small,.hint{display:block;margin-top:6px;color:var(--muted);font-size:12px}input,select{width:100%;height:42px;border:1px solid #c7d3e4;border-radius:9px;background:#fff;color:var(--ink);padding:0 12px;font:inherit;outline:0;transition:border .15s,box-shadow .15s}input:focus,select:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(36,99,235,.14)}input[readonly]{background:#f6f8fc;color:#536078}.browse,.button{border:0;border-radius:9px;min-height:42px;padding:0 15px;font:inherit;font-weight:700;cursor:pointer;transition:transform .12s,background .12s,box-shadow .12s}.browse{margin-top:25px;background:#eaf0fb;color:#315177}.browse:hover,.button:hover{transform:translateY(-1px)}.privacy-strip{display:flex;gap:10px;align-items:flex-start;background:#effaf8;border:1px solid #cbeee7;border-radius:11px;padding:12px 15px;margin:-3px 0 20px;color:#285f5a;font-size:13px}.privacy-strip p{margin:0}.privacy-strip__icon{font-size:18px;line-height:1}.model-summary{display:grid;grid-template-columns:minmax(230px,.85fr) minmax(420px,1.5fr) auto;align-items:center;gap:26px;padding-top:22px;padding-bottom:22px}.model-summary h2{margin:0 0 5px;font-size:18px}.model-summary>div>p:last-child{margin:0;color:var(--muted);font-size:12px}.model-summary__status{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.model-summary__status>div{min-width:0;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#fbfcff}.model-summary__status span{display:block;margin-bottom:3px;color:var(--muted);font-size:11px;font-weight:700}.model-summary__status strong{display:block;overflow:hidden;color:#2d4363;font-size:12px;white-space:nowrap;text-overflow:ellipsis}.ai-grid,.route-grid{display:grid;grid-template-columns:1.15fr .85fr;gap:18px}.route-grid{grid-template-columns:1fr 1fr;margin-top:18px}.subpanel{border:1px solid var(--line);border-radius:13px;padding:19px;background:#fbfcff}.subpanel__heading{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px}.subpanel__heading h3{margin:0;font-size:15px}.tag{color:#49617e;background:#eef2f8;border-radius:999px;padding:3px 8px;font-size:11px;font-weight:700}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:12px}.subpanel .field{margin:0 0 12px}.button--primary{background:var(--blue);color:#fff;box-shadow:0 8px 15px rgba(36,99,235,.18)}.button--primary:hover{background:var(--blue-dark)}.button--secondary{background:#eaf0fb;color:#24436d}.button--ghost{background:#f3f6fa;color:#455a77}.button--danger{background:var(--danger);color:#fff}.provider-list{display:grid;gap:9px;max-height:330px;overflow:auto}.provider-card{border:1px solid #dde5f1;border-radius:10px;padding:11px;background:#fff}.provider-card__head{display:flex;justify-content:space-between;gap:8px;align-items:baseline}.provider-card__title{font-weight:750;font-size:13px}.provider-card__meta{margin-top:5px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}.provider-card__state{font-size:11px;font-weight:700;color:#177464}.provider-card__state--warn{color:#a56b15}.provider-advanced{margin:0 0 12px}.provider-advanced summary{cursor:pointer;color:#49617e;font-size:13px;font-weight:700}.provider-advanced .field{margin:12px 0 0}.provider-actions,.route-actions{display:flex;align-items:center;gap:14px;margin-top:14px}.route-actions .hint{margin:0}.privacy-settings,.rollback{margin-top:20px;border:1px solid var(--line);border-radius:11px;padding:0 15px}.privacy-settings summary,.rollback summary{cursor:pointer;padding:13px 0;font-weight:750;color:#354966}.privacy-settings__body{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:4px 0 16px}.privacy-settings__body .button{justify-self:start;align-self:end}.check{display:flex;gap:9px;align-items:center;font-size:13px;color:#344159;font-weight:600}.check input{width:17px;height:17px;accent-color:var(--blue)}.workflow-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.flow-card{border:1px solid var(--line);border-radius:13px;padding:17px;background:linear-gradient(180deg,#fff,#f9fbfe)}.flow-card--danger{border-color:#efc8ca;background:linear-gradient(180deg,#fff,#fff7f7)}.flow-number{display:inline-block;color:var(--blue);font-size:12px;font-weight:800;letter-spacing:.08em}.flow-card--danger .flow-number{color:var(--danger)}.flow-card h3{margin:8px 0 4px;font-size:16px}.flow-card p{min-height:43px;margin-bottom:13px;color:var(--muted);font-size:12px}.plan-context{display:grid;grid-template-columns:minmax(0,.8fr) minmax(280px,1.2fr);gap:24px;align-items:end;margin:19px 0 14px;padding:18px 20px;border:1px solid #dbe5f3;border-radius:13px;background:#f6f9fd}.plan-context h3{margin:0 0 5px;font-size:16px}.plan-context>div>p:last-child{margin:0;color:var(--muted);font-size:12px}.plan-context .field{margin:0}.workflow-grid--execution{margin-top:0}.workflow-tools{display:flex;align-items:center;gap:12px;margin-top:14px}.workflow-tools .hint{margin:0}.rollback>div{display:flex;align-items:end;gap:12px;padding:3px 0 8px}.rollback .field{flex:1}.rollback .hint{margin:0 0 16px}.result-panel{overflow:hidden;padding:0;background:#111b2d;border:0}.result-panel__header{padding:12px 16px;color:#c9d8ef;background:#18263e;font-size:12px;font-weight:700}.result-panel__header .dot{width:7px;height:7px;background:#64d7a1}pre{margin:0;min-height:160px;max-height:410px;overflow:auto;padding:18px;color:#d4e4fb;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word}@media(max-width:850px){.hero,.section-heading{align-items:flex-start;flex-direction:column}.hero{padding:28px 22px 16px}.hero__right{align-items:flex-start;flex-direction:column;gap:10px}.hero__status{white-space:normal}.ai-grid,.route-grid,.workflow-grid,.plan-context,.model-summary{grid-template-columns:1fr}.model-summary{gap:16px}.model-summary__status{grid-template-columns:1fr}.privacy-settings__body{grid-template-columns:1fr}.section-heading>p{text-align:left;margin-top:0}main{padding:0 14px 30px}.panel{padding:21px}.directory-grid{grid-template-columns:1fr}.browse{margin-top:0}.workflow-grid{gap:10px}.flow-card p{min-height:0}.workflow-tools,.provider-actions{align-items:flex-start;flex-direction:column}.rollback>div{align-items:stretch;flex-direction:column}.two-col{grid-template-columns:1fr}}"""

JS = """let csrf = "";
const page = document.body.dataset.page;
const byId = id => document.getElementById(id);
const result = byId("result");
const providerProfiles = {fake:{baseUrl:"http://127.0.0.1:9999/v1",apiKeyEnv:"",model:"fake-v1"},gemini:{baseUrl:"https://generativelanguage.googleapis.com/v1beta",apiKeyEnv:"GEMINI_API_KEY",model:""},openai:{baseUrl:"https://api.openai.com/v1",apiKeyEnv:"OPENAI_API_KEY",model:""},anthropic:{baseUrl:"https://api.anthropic.com/v1",apiKeyEnv:"ANTHROPIC_API_KEY",model:""},deepseek:{baseUrl:"https://api.deepseek.com",apiKeyEnv:"DEEPSEEK_API_KEY",model:""}};
const serviceNames = {fake:"离线演示",gemini:"Google Gemini",openai:"OpenAI",anthropic:"Anthropic",openai_compatible:"兼容模型服务"};
const networkNames = {loopback:"本机",lan:"局域网",remote:"互联网"};
let previousService = "fake";
function text(id){ return byId(id)?.value.trim() || ""; }
function choice(id){ return byId(id)?.value || ""; }
function checked(id){ return Boolean(byId(id)?.checked); }
function setText(id,value){ const node=byId(id); if(node)node.value=value || ""; }
function status(message){ if(result)result.textContent=message; }
function routeFields(prefix, route){ setText(prefix+"_fallbacks", (route.fallbacks || []).join(",")); setText(prefix+"_confidence_below", route.confidence_below ?? ""); setSelect(prefix+"_primary", route.primary); setSelect(prefix+"_escalate_to", route.escalate_to || ""); }
function setSelect(id,value){ const node=byId(id); if(node&&[...node.options].some(option=>option.value===value))node.value=value; }
function option(label,value){ const node=document.createElement("option"); node.value=value; node.textContent=label; return node; }
function providerTitle(provider){ return provider.display_name || (provider.driver === "fake" ? "离线演示模型" : provider.provider_id); }
function providerLabel(provider){ return `${providerTitle(provider)} · ${serviceNames[provider.driver] || provider.driver} / ${provider.model}`; }
function populateProviderSelects(providers){ ["item_primary","burst_primary","item_escalate_to","burst_escalate_to"].forEach(id=>{ const node=byId(id); if(!node)return; const previous=node.value, empty=id.endsWith("escalate_to"); node.replaceChildren(); if(empty)node.append(option("不启用升级", "")); providers.forEach(provider=>node.append(option(providerLabel(provider),provider.provider_id))); setSelect(id,previous); }); }
function renderProviders(providers){ const list=byId("provider-list"), count=byId("provider-count"); if(!list||!count)return; list.replaceChildren(); count.textContent=`${providers.length} 个`; providers.forEach(provider=>{ const card=document.createElement("article"); card.className="provider-card"; const head=document.createElement("div"); head.className="provider-card__head"; const title=document.createElement("span"); title.className="provider-card__title"; title.textContent=providerTitle(provider); const state=document.createElement("span"); state.className="provider-card__state"+(provider.api_key_configured||provider.driver==="fake"?"":" provider-card__state--warn"); state.textContent=provider.driver==="fake"?"离线使用":"访问密钥"+(provider.api_key_configured?"已准备":"未准备"); head.append(title,state); const meta=document.createElement("div"); meta.className="provider-card__meta"; meta.textContent=`${serviceNames[provider.driver] || provider.driver} · ${provider.model} · ${networkNames[provider.network_scope] || provider.network_scope}`; const endpoint=document.createElement("div"); endpoint.className="provider-card__meta"; endpoint.textContent=`服务地址：${provider.base_url}`; card.append(head,meta,endpoint); list.append(card); }); }
function setSummary(id,route,providers){ const node=byId(id), provider=providers.find(item=>item.provider_id===route?.primary); if(node)node.textContent=provider?`${providerTitle(provider)} · ${provider.model}`:"未设置"; }
function renderHomeSummary(data){ const providers=data.providers || [], routes=data.routes || {}; setSummary("item-model-summary",routes.item_analysis,providers); setSummary("burst-model-summary",routes.burst_review,providers); const privacy=byId("privacy-summary"); if(privacy)privacy.textContent=data.allow_cloud?"允许互联网模型":data.allow_lan?"允许局域网模型":"仅使用本机模型"; }
function applyBootstrap(data){ csrf=data.csrf_token || csrf; setText("workspace",data.workspace); const providers=data.providers || []; if(page==="settings"){const cloud=byId("allow_cloud"), lan=byId("allow_lan"); if(cloud)cloud.checked=Boolean(data.allow_cloud); if(lan)lan.checked=Boolean(data.allow_lan); renderProviders(providers); populateProviderSelects(providers); const routes=data.routes || {}; if(routes.item_analysis)routeFields("item",routes.item_analysis); if(routes.burst_review)routeFields("burst",routes.burst_review); status(`模型设置已读取。当前共 ${providers.length} 个分析服务。`);}else{renderHomeSummary(data);status(`Smart Photo Triage v${data.version || "1.2.1"} 已就绪。选择照片源和输出目录后，从第 1 步开始。`);} }
function payload(){ const selectedService=choice("driver"), inferredName=selectedService==="deepseek"&&!text("display_name")?"DeepSeek":text("display_name"); return {source:text("source"),output:text("output"),plan_id:text("plan_id"),transaction_id:text("transaction_id"),confirmation:"",provider_id:text("provider_id"),display_name:inferredName,driver:selectedService==="deepseek"?"openai_compatible":selectedService,model:text("model"),base_url:text("base_url"),api_key:text("api_key"),api_key_env:text("api_key_env"),item_primary:choice("item_primary"),item_fallbacks:text("item_fallbacks"),item_confidence_below:text("item_confidence_below"),item_escalate_to:choice("item_escalate_to"),burst_primary:choice("burst_primary"),burst_fallbacks:text("burst_fallbacks"),burst_confidence_below:text("burst_confidence_below"),burst_escalate_to:choice("burst_escalate_to"),allow_cloud:checked("allow_cloud"),allow_lan:checked("allow_lan"),cloud_confirmation:text("cloud_confirmation"),lan_confirmation:text("lan_confirmation")}; }
function providerDraftProblem(body){ if(!body.provider_id)return ["请填写配置编号，例如 my_gemini。", "provider_id"]; if(!/^[a-z][a-z0-9_]{0,63}$/.test(body.provider_id))return ["配置编号只能使用小写英文、数字和下划线，并且必须以英文开头。", "provider_id"]; if(!body.model)return ["请填写模型名称。请复制您在服务商控制台实际可用的模型 ID。", "model"]; if(body.driver==="openai_compatible"&&!body.base_url)return ["兼容模型服务需要填写服务地址。", "base_url"]; if(body.api_key_env&&!/^[A-Z_][A-Z0-9_]*$/.test(body.api_key_env))return ["访问密钥变量名应类似 GEMINI_API_KEY。这里不要填写实际 API Key。", "api_key_env"]; return null; }
function readableError(detail){ const messages={"provider model must not be empty":"请填写模型名称。","provider_id must be a stable lowercase identifier":"配置编号只能使用小写英文、数字和下划线。","api_key_env must be an environment variable name":"访问密钥变量名格式不正确。请填写变量名，不要填写实际 API Key。","OpenAI-compatible providers require a base URL":"兼容模型服务需要填写服务地址。"}; return messages[detail] || detail || "本地操作未完成"; }
async function api(action, body){ const response=await fetch("/api/"+action,{method:"POST",headers:{"Content-Type":"application/json","X-SPT-CSRF":csrf},body:JSON.stringify(body)}); const data=await response.json(); if(!response.ok)throw new Error(readableError(data.detail || data.error)); return data; }
async function call(action){ try{ const body=payload(); if(action==="save-provider"){const problem=providerDraftProblem(body);if(problem){status("尚未保存："+problem[0]);byId(problem[1])?.focus();return;}}if(action==="forget-provider-key"&&!body.provider_id){status("请先填写要清除密钥的配置编号。");byId("provider_id")?.focus();return;}if(action==="execute"){const approved=window.confirm("即将根据已批准的计划创建照片副本。原照片不会移动或删除。是否继续真实复制？");if(!approved){status("已取消真实复制，未创建任何文件副本。");return;}body.confirmation="EXECUTE";}if(action==="rollback"){const approved=window.confirm("即将撤回本次真实复制创建的文件副本。原照片不会受到影响。是否继续安全回滚？");if(!approved){status("已取消安全回滚，现有副本保持不变。");return;}body.confirmation="ROLLBACK";}status("正在执行本地操作…");const data=await api(action,body);setText("api_key","");if(data.plan_id)setText("plan_id",data.plan_id);if(data.applied&&data.applied.transaction_id)setText("transaction_id",data.applied.transaction_id);if(data.ai)applyBootstrap({...data.ai,workspace:byId("workspace")?.value || ""});status(JSON.stringify(data,null,2));if(data.url)window.open(data.url,"_blank","noopener");}catch(error){status("操作未完成："+error.message);} }
function desktopApi(){return window.pywebview?.api || null;}
function configureDirectoryPicker(){const nativePicker=desktopApi();const note=byId("picker-note");document.querySelectorAll("[data-pick]").forEach(button=>{button.disabled=!nativePicker;button.title=nativePicker?"打开 Windows 原生文件夹选择器":"浏览器模式请直接在左侧输入完整路径";});if(note)note.textContent=nativePicker?"桌面版：点击按钮打开 Windows 原生文件夹选择器。取消不会改变当前路径。":"浏览器模式：请在左侧手动输入完整路径。Windows 原生文件夹选择器仅在桌面版提供。";}
async function choose(field){const nativePicker=desktopApi();if(!nativePicker){status("浏览器模式不提供原生目录选择器。请手动输入完整路径。");return;}try{status("正在打开 Windows 文件夹选择器…");const data=await nativePicker.choose_directory(field);if(data?.selected)setText(field,data.selected);if(field==="workspace"&&data?.workspace){setText("workspace",data.workspace);if(data.ai)applyBootstrap({...data.ai,workspace:data.workspace});}if(!data?.selected)status("已取消选择，当前内容保持不变。");}catch(error){status("无法打开目录选择器："+(error?.message || error));}}
function updateProviderInputs(){ const service=choice("driver"), profile=providerProfiles[service], previousProfile=providerProfiles[previousService], baseUrl=byId("base_url"), model=byId("model"), apiKeyEnv=byId("api_key_env"); if(!baseUrl)return; if(profile){baseUrl.value=profile.baseUrl;baseUrl.placeholder="已按所选服务商自动填写，可按需修改";if(model&&(!model.value||model.value===previousProfile?.model))model.value=profile.model;if(apiKeyEnv&&(!apiKeyEnv.value||apiKeyEnv.value===previousProfile?.apiKeyEnv))apiKeyEnv.value=profile.apiKeyEnv;}else{baseUrl.value="";baseUrl.placeholder="例如 http://127.0.0.1:1234/v1 或 https://provider.example/v1";if(model&&model.value===previousProfile?.model)model.value="";if(apiKeyEnv&&apiKeyEnv.value===previousProfile?.apiKeyEnv)apiKeyEnv.value="";}previousService=service; }
async function boot(){ const response=await fetch("/api/bootstrap"); if(!response.ok)throw new Error("无法读取本地状态"); applyBootstrap(await response.json()); const driver=byId("driver"); if(driver){previousService=driver.value;driver.addEventListener("change",updateProviderInputs);}document.querySelectorAll("[data-action]").forEach(button=>button.addEventListener("click",()=>call(button.dataset.action))); document.querySelectorAll("[data-pick]").forEach(button=>button.addEventListener("click",()=>choose(button.dataset.pick))); configureDirectoryPicker();window.addEventListener("pywebviewready",configureDirectoryPicker,{once:true}); }
boot().catch(error=>status("控制台启动失败："+error.message));"""
