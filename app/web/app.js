/* 小爱同学 · 大模型工作台 —— 前端。零构建，原生 JS。 */
(() => {
  "use strict";

  // 挂载到宿主子路径时（如 /xiaoai），所有接口与资源都要带前缀
  const BASE = window.XIAOAI_BASE || "";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const SECRET_FIELDS = new Set(["password", "api_key"]);

  let STATE = null;
  const LOGS = [];
  const SEEN = new Set();      // 日志去重：SSE 重连会重放
  let LAST_SEQ = 0;
  const DIRTY = new Set();     // 用户改过但还没保存的配置分区

  /* ------------------------------------------------ 基础请求 */
  async function api(path, body) {
    const opt = body === undefined
      ? { method: "GET" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const res = await fetch(BASE + path, opt);
    let data;
    try { data = await res.json(); } catch { data = { ok: false, error: "响应解析失败" }; }
    if (!res.ok && data.ok !== false) data = { ok: false, error: data.error || ("HTTP " + res.status) };
    return data;
  }

  let toastTimer = null;
  function toast(msg, bad) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.toggle("bad", !!bad);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  function tstr(ts) {
    const d = new Date((ts || 0) * 1000);
    return d.toTimeString().slice(0, 8);
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  /* ------------------------------------------------ 导航 */
  $$(".nav-item").forEach(btn => {
    btn.addEventListener("click", () => {
      $$(".nav-item").forEach(b => b.classList.remove("active"));
      $$(".panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      const tab = btn.dataset.tab;
      const panel = $(`[data-panel="${tab}"]`);
      if (panel) panel.classList.add("active");
      if (tab === "history") loadHistory();
      if (tab === "portable") runSelfcheck();
      if (tab === "miot") { loadMiotDevices(); loadScenes(false); }
    });
  });

  /* ------------------------------------------------ 配置表单 */
  // 程序化填值也要走事件，否则后台轮询会把刚填的内容冲掉
  function setField(el, value) {
    if (!el) return;
    el.value = value == null ? "" : value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function fillForm(section, cfg) {
    const box = $(`.form[data-section="${section}"]`);
    if (!box || !cfg) return;
    $$("input,select,textarea", box).forEach(el => {
      const name = el.name;
      if (!name || el.disabled || (el.hidden && el.tagName === "INPUT")) return;
      const v = cfg[name];
      if (el.type === "checkbox") {
        el.checked = !!v;
      } else if (name === "model") {
        setModelField(v || "");
      } else if (SECRET_FIELDS.has(name)) {
        el.value = "";
        el.placeholder = v ? "已配置（留空则不修改）" : (el.getAttribute("data-ph") || "");
      } else {
        el.value = v == null ? "" : v;
      }
    });
  }

  function collectForm(section) {
    const box = $(`.form[data-section="${section}"]`);
    const out = {};
    if (!box) return out;
    $$("input,select,textarea", box).forEach(el => {
      const name = el.name;
      if (!name || el.disabled || (el.hidden && el.tagName === "INPUT")) return;
      if (el.type === "checkbox") { out[name] = el.checked; return; }
      const raw = el.value;
      if (SECRET_FIELDS.has(name)) {
        if (raw.trim()) out[name] = raw.trim();     // 空 = 不修改
        return;
      }
      if (raw === "") return;
      out[name] = el.type === "number" ? Number(raw) : raw;
    });
    return out;
  }

  const SECTIONS = ["mi", "llm", "agent", "miot", "voice", "memory"];

  function fillAll(cfg, force) {
    SECTIONS.forEach(s => {
      if (force || !DIRTY.has(s)) fillForm(s, cfg[s]);
    });
  }

  // 用户一敲键盘就标记为「脏」，后台轮询不再回填，避免把正在输入的内容冲掉
  function bindDirtyTracking() {
    SECTIONS.forEach(sec => {
      const box = $(`.form[data-section="${sec}"]`);
      if (!box) return;
      const mark = () => DIRTY.add(sec);
      box.addEventListener("input", mark);
      box.addEventListener("change", mark);
    });
  }

  async function saveSection(section) {
    const patch = {};
    patch[section] = collectForm(section);
    const r = await api("/api/config", patch);
    if (!r.ok) { toast(r.error || "保存失败", true); return null; }
    STATE = STATE || {};
    STATE.config = r.config;
    DIRTY.delete(section);
    fillForm(section, r.config[section]);
    toast("配置已保存");
    return r.config;
  }

  /* ------------------------------------------------ 状态渲染 */
  function renderState(st) {
    STATE = st;
    const b = st.bridge || {};
    const stt = b.state || "stopped";
    const map = { stopped: "已停止", starting: "启动中", running: "运行中", error: "异常" };
    const cls = stt === "running" ? "ok" : stt === "error" ? "err" : stt === "starting" ? "warn" : "";

    const pb = $("#pill-bridge");
    pb.textContent = "桥接 " + (map[stt] || stt);
    pb.className = "pill " + cls + (stt === "running" ? " live" : "");

    const pm = $("#pill-mode");
    if (st.mock) {
      pm.textContent = "演示模式";
      pm.className = "pill warn";
    } else if (st.deps_ok === false) {
      pm.textContent = "真机·缺依赖";
      pm.className = "pill err";
      pm.title = "已取消演示模式，但当前 Python 环境缺 miservice，接不了真机";
    } else {
      pm.textContent = "真机模式";
      pm.className = "pill live";
      pm.title = "";
    }

    const pd = $("#pill-miservice");
    const depsOk = st.deps_ok !== undefined ? st.deps_ok : st.miservice;
    pd.textContent = "依赖 " + (depsOk ? "已安装" : "缺失");
    pd.className = "pill " + (depsOk ? "ok" : "err");

    const stats = (b.stats || {});
    $("#pill-turns").textContent = "语音回合 " + (stats.turns || 0);

    $("#ov-state").textContent = map[stt] || stt;
    $("#ov-uptime").textContent = b.uptime ? ("已运行 " + b.uptime + " 秒") : "未运行";
    $("#ov-turns").textContent = stats.turns || 0;
    $("#ov-polls").textContent = "轮询 " + (stats.polls || 0) + " 次 · 错误 " + (stats.errors || 0);
    $("#ov-errors").textContent = stats.errors || 0;
    $("#ov-errors").style.color = (stats.errors || 0) > 0 ? "var(--rd)" : "";
    $("#ov-lasterr").textContent = stats.last_error || "—";
    $("#ov-query").textContent = stats.last_query || "—";
    $("#ov-answer").textContent = stats.last_answer ? stats.last_answer.slice(0, 160) : "—";

    $("#btn-start").disabled = !!b.running;
    $("#btn-stop").disabled = !b.running;

    // 验证码：全局悬浮弹窗，任何面板下都能看到，不阻塞其它操作
    const otp = st.otp || {};
    const box = $("#otp-box");
    const wasHidden = box.hidden;
    box.hidden = !otp.waiting;
    if (otp.waiting && wasHidden) {
      setTimeout(() => { const i = $("#otp-input"); if (i) i.focus(); }, 60);
      $("#otp-hint").textContent = "小米已下发验证码，3 分钟内有效；填好点提交，或直接取消。";
    }

    renderMemory(st.memory);

    // 桥接没跑时 speaker 为空，退回配置里的 did/dids，别显示成 0 台
    let devs = b.devices || [];
    if (!devs.length) {
      const mi = ((st.config || {}).mi) || {};
      const ids = (mi.dids && mi.dids.length) ? mi.dids : (mi.did ? [mi.did] : []);
      devs = ids.map(did => ({ did, name: did, last: false }));
    }
    const ovd = $("#ov-devices");
    if (ovd) {
      ovd.textContent = devs.length;
      $("#ov-device-list").textContent = devs.length
        ? devs.map(d => (d.last ? "▶ " : "") + d.name).join("、")
        : "未指定";
    }
    const sel = $("#mock-device");
    if (sel) {
      const keep = sel.value;
      const opts = devs.map(d => `<option value="${esc(d.did)}">${esc(d.name)}</option>`).join("");
      if (sel.dataset.sig !== opts) {
        sel.innerHTML = opts || '<option value="">默认音箱</option>';
        sel.dataset.sig = opts;
        if (keep && Array.from(sel.options).some(o => o.value === keep)) sel.value = keep;
      }
    }

    // 播报列表
    const sp = b.spoken || [];
    $("#spoken-list").innerHTML = sp.length
      ? sp.slice().reverse().map(s => `<li><time>${tstr(s.ts)}</time><span>${esc(s.text)}</span></li>`).join("")
      : '<li class="muted">暂无</li>';
  }

  async function refresh() {
    const st = await api("/api/state");
    if (!st.ok) return;
    renderState(st);
    if (st.config) fillAll(st.config, false);
  }

  /* ------------------------------------------------ 桥接控制 */
  async function bridgeCmd(cmd) {
    const btn = $(cmd === "start" ? "#btn-start" : cmd === "stop" ? "#btn-stop" : "#btn-restart");
    btn.disabled = true;
    const r = await api("/api/bridge/" + cmd, {});
    btn.disabled = false;
    if (r.bridge) renderState({ ...(STATE || {}), bridge: r.bridge, config: (STATE || {}).config });
    toast(r.ok ? ("桥接" + ({ start: "已启动", stop: "已停止", restart: "已重启" }[cmd] || "已操作")) : (r.error || "操作失败"), !r.ok);
    setTimeout(refresh, 600);
  }
  $("#btn-start").addEventListener("click", () => bridgeCmd("start"));
  $("#btn-stop").addEventListener("click", () => bridgeCmd("stop"));
  $("#btn-restart").addEventListener("click", () => bridgeCmd("restart"));

  /* ------------------------------------------------ 手动对话 */
  ["#chat-input", "#mock-input", "#tts-input"].forEach(sel => {
    const el = $(sel);
    if (!el) return;
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        const target = sel === "#chat-input" ? "#btn-chat" : sel === "#mock-input" ? "#btn-mock" : "#btn-tts";
        $(target).click();
      }
    });
  });

  $("#btn-chat").addEventListener("click", async () => {
    const text = $("#chat-input").value.trim();
    if (!text) return toast("先说点什么", true);
    await streamChat(text);
  });

  /* ------------------------------------------------ 记忆 */
  function renderMemory(m) {
    const box = $("#mem-stats");
    if (!box) return;
    if (!m) { box.textContent = "—"; return; }
    const age = m.last_turn_age == null
      ? "—"
      : (m.last_turn_age < 60 ? m.last_turn_age + " 秒前" : Math.floor(m.last_turn_age / 60) + " 分钟前");
    box.innerHTML = [
      `已存 <b>${m.stored}</b> 条`,
      `本轮上下文 <b>${m.in_context}</b> 轮`,
      `上次对话 ${age}`,
      m.enabled ? '<span class="tag voice">已启用</span>' : '<span class="tag err">已关闭</span>',
    ].join("　·　");
  }

  /* ------------------------------------------------ 流式手动对话 */
  async function streamChat(text) {
    const box = $("#chat-result");
    box.className = "result";
    box.textContent = "";
    let acc = "";
    const res = await fetch(BASE + "/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok || !res.body) {
      box.className = "result err";
      box.textContent = "请求失败：HTTP " + res.status;
      return;
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let finalTurn = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = chunk.split("\n").find(l => l.startsWith("data: "));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.type === "delta") {
          acc += ev.text;
          box.textContent = acc;
        } else if (ev.type === "done") {
          finalTurn = ev.turn;
        } else if (ev.type === "error") {
          box.className = "result err";
          box.textContent = "出错：" + (ev.error || "未知");
          return;
        }
      }
    }
    if (!finalTurn) { box.className = "result err"; box.textContent = "没有拿到结果"; return; }
    box.className = "result ok";
    let out = `【回答】${finalTurn.answer}\n\n耗时 ${finalTurn.ms} ms`;
    if (finalTurn.tool_calls && finalTurn.tool_calls.length) {
      out += "\n\n" + finalTurn.tool_calls.map(c =>
        `【工具】${c.name} (${c.ms}ms)\n参数 ${JSON.stringify(c.args)}\n结果 ${c.result}`).join("\n\n");
    }
    if (finalTurn.spoken) out += "\n\n已播报到音箱";
    box.textContent = out;
    refresh();
  }

  $("#btn-save-memory").addEventListener("click", () => saveSection("memory").then(refresh));
  $("#btn-clear-memory").addEventListener("click", async () => {
    const r = await api("/api/memory/clear", {});
    toast(r.ok ? "记忆已清空" : "失败", !r.ok);
    if (r.ok) renderMemory(r);
  });

  /* ------------------------------------------------ 模拟发问 */
  $("#btn-mock").addEventListener("click", async () => {
    const text = $("#mock-input").value.trim();
    if (!text) return toast("填一句模拟语音", true);
    const did = $("#mock-device") ? $("#mock-device").value : "";
    const r = await api("/api/mock/say", { text, did });
    toast(r.ok ? `已注入给${r.device || "音箱"}，等待取走` : (r.error || "失败"), !r.ok);
  });

  /* ------------------------------------------------ 设备 / 诊断 */
  let MI_DIDS = [];      // 勾选接管的音箱，保存时写进 mi.dids

  function renderDevices(list, box) {
    const tbl = $("#dev-table");
    const tb = $("tbody", tbl);
    MI_DIDS = (((STATE || {}).config || {}).mi || {}).dids || [];
    tb.innerHTML = (list || []).map(d => {
      const on = MI_DIDS.includes(d.did) ? "checked" : "";
      return `<tr data-did="${esc(d.did)}" data-hw="${esc(d.hardware || "")}">
      <td><input type="checkbox" class="pick-did" data-did="${esc(d.did)}" ${on}></td>
      <td>${esc(d.name || "—")}</td><td>${esc(d.hardware || "—")}</td><td><code>${esc(d.did)}</code></td></tr>`;
    }).join("");
    tbl.hidden = !(list && list.length);
    const dn = $("#dev-note");
    if (dn) dn.hidden = !(list && list.length);
    if (list && list.length > 1) {
      box.textContent = `发现 ${list.length} 台设备：勾选「接管」可同时管理多台；只想接管一台就点那一行，DID 和型号会自动填好。`;
    }
    // 只有一台就直接把 DID 和型号填好，省掉手抄
    if (list && list.length === 1) {
      const d = list[0];
      const f = (n) => $(`.panel[data-panel="mi"] input[name="${n}"]`);
      setField(f("did"), d.did || "");
      if (d.hardware) setField(f("hardware"), d.hardware);
      box.textContent = `发现 1 台设备：${d.name || d.did}，DID 与型号已自动填入。`;
    }
    $$("#dev-table .pick-did").forEach(cb => {
      cb.addEventListener("change", () => {
        const did = cb.dataset.did;
        if (cb.checked) {
          if (!MI_DIDS.includes(did)) MI_DIDS.push(did);
        } else {
          MI_DIDS = MI_DIDS.filter(x => x !== did);
        }
      });
    });
    $$("#dev-table tbody tr").forEach(tr => {
      tr.addEventListener("click", (e) => {
        if (e.target.tagName === "INPUT") return;
        const did = tr.dataset.did || tr.querySelector("code").textContent;
        const hw = (tr.dataset.hw || "").trim();
        setField($('.panel[data-panel="mi"] input[name="did"]'), did);
        if (hw) setField($('.panel[data-panel="mi"] input[name="hardware"]'), hw);
        toast("已填入 DID" + (hw ? "（型号 " + hw + "）" : ""));
      });
    });
  }

  $("#btn-discover").addEventListener("click", async () => {
    const box = $("#mi-result");
    box.className = "result";
    box.textContent = "正在读取设备列表…";
    const r = await api("/api/devices");
    if (!r.ok) { box.className = "result err"; box.textContent = r.error || "失败"; return; }
    box.className = "result ok";
    renderDevices(r.devices, box);
  });

  $("#btn-diag-mi").addEventListener("click", async () => {
    const box = $("#mi-result");
    box.className = "result";
    box.textContent = "正在登录小米云…";
    const r = await api("/api/diag/mi");
    if (!r.ok) { box.className = "result err"; box.textContent = r.error || "登录失败"; return; }
    box.className = "result ok";
    box.textContent = `登录成功，用时 ${r.ms} ms。`;
    renderDevices(r.devices, box);
    refresh();
  });

  $("#btn-reset-fp").addEventListener("click", async () => {
    if (!confirm("将清除登录 token 与设备指纹，下次登录等于一台全新设备。确定？")) return;
    const box = $("#mi-result");
    box.className = "result";
    box.textContent = "正在重置…";
    const r = await api("/api/mi/reset-fingerprint", {});
    box.className = "result " + (r.ok ? "ok" : "err");
    box.textContent = r.ok ? (r.note || "已清除") : (r.error || "失败");
    toast(r.ok ? "已重置，请点一次「测试登录」" : (r.error || "失败"), !r.ok);
    refresh();
  });

  $("#btn-otp-cancel").addEventListener("click", async () => {
    await api("/api/mi/otp/cancel", {});
    $("#otp-box").hidden = true;
    toast("已取消本次登录");
  });
  $("#otp-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("#btn-otp").click(); }
  });

  $("#btn-otp").addEventListener("click", async () => {
    const code = $("#otp-input").value.trim();
    if (!code) return toast("请输入验证码", true);
    const r = await api("/api/mi/otp", { code });
    toast(r.ok ? "已提交验证码" : (r.error || "失败"), !r.ok);
    $("#otp-input").value = "";
  });


  /* ------------------------------------------------ 模型：下拉优先，手填兜底 */
  function setModelField(value) {
    const sel = $("#model-select");
    const inp = $("#model-input");
    if (!sel || !inp) return;
    if (value && !Array.from(sel.options).some(o => o.value === value)) {
      sel.insertAdjacentHTML("afterbegin", `<option value="${esc(value)}">${esc(value)}</option>`);
    }
    if (!inp.hidden) {          // 当前是手动模式
      inp.value = value || "";
      return;
    }
    sel.value = value || "";
  }

  function useManualModel(manual) {
    const sel = $("#model-select");
    const inp = $("#model-input");
    if (!sel || !inp) return;
    const cur = manual ? (sel.value || "") : (inp.value || "");
    sel.hidden = manual;
    sel.disabled = manual;
    inp.hidden = !manual;
    inp.disabled = !manual;
    $("#btn-model-manual").textContent = manual ? "用下拉选" : "手动输入";
    if (manual) inp.value = cur; else {
      if (cur && !Array.from(sel.options).some(o => o.value === cur)) {
        sel.insertAdjacentHTML("afterbegin", `<option value="${esc(cur)}">${esc(cur)}</option>`);
      }
      sel.value = cur;
    }
  }

  function fillModelOptions(models, current) {
    const sel = $("#model-select");
    if (!sel) return;
    sel.innerHTML = models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    if (current && !models.includes(current)) {
      sel.insertAdjacentHTML("afterbegin", `<option value="${esc(current)}">${esc(current)}（当前）</option>`);
    }
    sel.value = (current && (models.includes(current) || true)) ? current : models[0];
    if (sel.value !== current && current) sel.value = current;
  }

  async function persistLlmForm() {
    // 用户很可能填完 Key 直接点按钮，先把表单落库，否则服务端读到的还是旧配置
    const patch = collectForm("llm");
    if (!Object.keys(patch).length) return true;
    const r = await api("/api/config", { llm: patch });
    if (r.ok) { DIRTY.delete("llm"); if (r.config) fillForm("llm", r.config.llm); }
    return r.ok;
  }

  $("#btn-model-manual").addEventListener("click", () => {
    const inp = $("#model-input");
    useManualModel(inp.hidden);      // 当前是下拉就切手填，反之亦然
  });

  $("#btn-list-models").addEventListener("click", async () => {
    const btn = $("#btn-list-models");
    const box = $("#llm-result");
    btn.disabled = true;
    btn.textContent = "拉取中…";
    await persistLlmForm();
    box.className = "result";
    box.textContent = "正在向端点请求模型列表（会先用当前已保存的接口地址与 Key）…";
    try {
      const r = await api("/api/llm/models");
      if (!r.ok) {
        box.className = "result err";
        box.textContent = "拉取失败：" + (r.error || "未知") +
          "\n\n提示：先点「保存」把接口地址和 Key 存下来，再获取；个别端点不提供 /models，只能手动填。";
        return;
      }
      useManualModel(false);
      const pick = (r.current && r.models.includes(r.current)) ? r.current : r.models[0];
      fillModelOptions(r.models, pick);
      box.className = "result ok";
      box.textContent = `拉到 ${r.models.length} 个模型，已在下拉里，选一个再点「保存」：\n` +
        r.models.slice(0, 20).join("、") + (r.models.length > 20 ? " …" : "");
    } finally {
      btn.disabled = false;
      btn.textContent = "获取模型列表";
    }
  });

  $("#btn-diag-llm").addEventListener("click", async () => {
    const box = $("#llm-result");
    box.className = "result";
    box.textContent = "正在请求模型…";
    await persistLlmForm();
    const r = await api("/api/diag/llm");
    box.className = "result " + (r.ok ? "ok" : "err");
    box.textContent = r.ok
      ? `连通正常。模型 ${r.model} 回复：${r.reply}`
      : (r.error || "失败");
  });

  $("#btn-save-mi").addEventListener("click", async () => {
    const patch = collectForm("mi");
    patch.dids = MI_DIDS;
    const r = await api("/api/config", { mi: patch });
    if (!r.ok) return toast(r.error || "保存失败", true);
    DIRTY.delete("mi");
    fillForm("mi", r.config.mi);
    toast(MI_DIDS.length > 1 ? `已保存，接管 ${MI_DIDS.length} 台音箱` : "配置已保存");
    refresh();
  });
  $("#btn-save-llm").addEventListener("click", async () => {
    const cfg = await saveSection("llm");
    if (!cfg) return;
    const sel = $("#model-select");
    const hasList = sel && Array.from(sel.options).some(o => o.value);
    // 刚填完 Key 就该自动把模型列出来，不用再点一次按钮
    if (!hasList && (cfg.llm?.api_key || "").trim() !== "") {
      $("#btn-list-models").click();
    }
  });
  $("#btn-save-agent").addEventListener("click", () => saveSection("agent"));
  $("#btn-save-voice").addEventListener("click", () => saveSection("voice"));

  /* ------------------------------------------------ 试播 */
  $("#btn-tts").addEventListener("click", async () => {
    const text = $("#tts-input").value.trim();
    if (!text) return toast("填一句要播报的话", true);
    const box = $("#voice-result");
    box.className = "result";
    box.textContent = "播报中…";
    const r = await api("/api/tts", { text });
    box.className = "result " + (r.ok ? "ok" : "err");
    box.textContent = r.ok ? "已送到音箱播报。" : (r.error || "失败");
  });

  /* ------------------------------------------------ 会话记录 */
  function turnHtml(t) {
    const tags = [];
    tags.push(`<span class="tag ${t.source === "voice" ? "voice" : "manual"}">${t.source === "voice" ? "语音" : "手动"}</span>`);
    if (t.error) tags.push('<span class="tag err">出错</span>');
    if (t.spoken) tags.push('<span class="tag">已播报</span>');
    const tools = (t.tool_calls || []).map(c =>
      `<div class="tc"><span class="tc-name">${esc(c.name)}</span> · ${c.ms}ms · ${esc(JSON.stringify(c.args))}\n${esc(c.result)}</div>`
    ).join("");
    return `<div class="turn">
      <div class="turn-h">${tags.join("")}<span>${tstr(t.ts)}</span><span>${t.ms || 0}ms</span></div>
      <div class="turn-q"><b>问</b> ${esc(t.query)}</div>
      <div class="turn-a"><b>答</b> ${esc(t.answer || "")}</div>
      ${tools}
    </div>`;
  }

  let HIST_ITEMS = [];

  function renderHistory() {
    const kw = ($("#hist-search")?.value || "").trim().toLowerCase();
    const rows = HIST_ITEMS.filter(t =>
      !kw || (t.query || "").toLowerCase().includes(kw) || (t.answer || "").toLowerCase().includes(kw));
    const box = $("#turns");
    box.innerHTML = rows.length
      ? rows.slice().reverse().map(turnHtml).join("")
      : `<p class="muted">${HIST_ITEMS.length ? "没有匹配「" + esc(kw) + "」的记录。" : "暂无记录。"}</p>`;
  }

  async function loadHistory() {
    const r = await api("/api/history?limit=200");
    if (!r.ok) { $("#turns").innerHTML = '<p class="muted">读取失败</p>'; return; }
    HIST_ITEMS = r.items || [];
    renderHistory();
  }

  const histSearch = $("#hist-search");
  if (histSearch) histSearch.addEventListener("input", renderHistory);
  function downloadAs(url, fallbackName) {
    const a = document.createElement("a");
    a.href = url;
    a.download = fallbackName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("已开始下载");
  }
  $("#btn-hist-export-md").addEventListener("click", () => {
    downloadAs(BASE + "/api/history/export?format=md&limit=500", "xiaoai-history.md");
  });
  $("#btn-hist-export-json").addEventListener("click", () => {
    downloadAs(BASE + "/api/history/export?format=json&limit=500", "xiaoai-history.json");
  });

  $("#btn-hist-refresh").addEventListener("click", loadHistory);
  $("#btn-hist-clear").addEventListener("click", async () => {
    const r = await api("/api/history/clear", {});
    toast(r.ok ? "已清空" : "失败", !r.ok);
    loadHistory();
  });


  /* ------------------------------------------------ 迁移与环境 */
  const LEVEL_TXT = { ok: "正常", warn: "注意", fail: "异常" };

  function renderChecks(data) {
    const box = $("#checks");
    const rows = (data && data.rows) || [];
    if (!rows.length) { box.innerHTML = '<p class="muted">没有结果。</p>'; return; }
    box.innerHTML = rows.map(r => {
      const fix = r.fix
        ? `<div class="chk-fix">${r.name === "仓库路径" && r.level === "fail"
            ? '<button class="mini" id="btn-default-repo">用本机默认路径</button> '
            : ""}${esc(r.fix)}</div>`
        : "";
      return `<div class="chk-row ${r.level}">
        <span class="chk-badge">${LEVEL_TXT[r.level] || r.level}</span>
        <div class="chk-body"><div class="chk-name">${esc(r.name)}</div>
        <div class="chk-detail">${esc(r.detail)}</div>${fix}</div>
      </div>`;
    }).join("");
    const btn = $("#btn-default-repo");
    if (btn) btn.addEventListener("click", async () => {
      const r = await api("/api/portable/default-repo", {});
      toast(r.ok ? "已改为 " + r.repo : (r.error || "失败"), !r.ok);
      if (r.ok) runSelfcheck();
    });
  }

  function renderMachine(m) {
    if (!m) return;
    const rows = [
      ["主机名", m.host], ["系统", m.os], ["架构", m.arch],
      ["Python", m.python], ["虚拟环境", m.venv ? "已启用" : "未启用"],
      ["项目目录", m.project], ["数据目录", m.data],
    ];
    $("#machine-table tbody").innerHTML = rows.map(([k, v]) =>
      `<tr><td style="color:var(--tx3);width:110px">${esc(k)}</td><td><code>${esc(v)}</code></td></tr>`
    ).join("");
  }

  async function runSelfcheck() {
    const sum = $("#selfcheck-summary");
    sum.textContent = "检查中…";
    const r = await api("/api/portable/selfcheck");
    if (!r.ok) { sum.textContent = r.error || "失败"; return; }
    renderChecks(r);
    renderMachine(r.machine);
    const s = r.summary || {};
    sum.textContent = `通过 ${s.pass || 0} 项，注意 ${s.warn || 0} 项，异常 ${s.fail || 0} 项`;
    sum.style.color = (s.fail || 0) > 0 ? "var(--rd)" : (s.warn || 0) > 0 ? "var(--am)" : "var(--gr)";
  }
  $("#btn-selfcheck").addEventListener("click", runSelfcheck);

  $("#btn-netcheck").addEventListener("click", async () => {
    const box = $("#net-result");
    box.hidden = false;
    box.className = "result";
    box.textContent = "正在对比直连 / 代理 / 小米通道的出口…";
    const r = await api("/api/net/proxy");
    if (!r.ok) { box.className = "result err"; box.textContent = r.error || "失败"; return; }
    const lines = [
      "直连出口    ：" + (r.direct?.text || "-"),
      "系统代理出口：" + (r.proxied?.text || "未开启"),
      "小米实际通道：" + (r.miservice?.text || "-"),
      "强制直连    ：" + (r.bypass_proxy ? "已开启" : "已关闭"),
      "",
      ...(r.verdict || []),
    ];
    // 只有「小米实际通道在境外」才是硬问题；仅代理在境外只作提示
    const viaCountry = (r.miservice?.raw?.country || "").toUpperCase();
    const bad = viaCountry && viaCountry !== "CN";
    box.className = "result " + (bad ? "err" : "ok");
    box.textContent = lines.join("\n");
  });

  async function exportConfig(withSecrets) {
    const r = await api("/api/portable/export?secrets=" + (withSecrets ? "1" : "0"));
    if (!r.ok) return toast(r.error || "导出失败", true);
    const text = JSON.stringify(r.bundle, null, 2);
    const blob = new Blob([text], { type: "application/json" });
    const a = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    a.href = URL.createObjectURL(blob);
    a.download = `xiaoai-config-${stamp}${withSecrets ? "-with-secrets" : ""}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    $("#export-hint").textContent = withSecrets
      ? "已导出（含密钥）。文件里有明文密码和 Key，别外传。"
      : "已导出（不含密钥）。新机器导入后补填密码和 Key 即可。";
    toast("已导出");
  }
  $("#btn-export-safe").addEventListener("click", () => exportConfig(false));
  $("#btn-export-full").addEventListener("click", () => exportConfig(true));

  async function doImport(text) {
    if (!text || !text.trim()) return toast("先把配置粘进来", true);
    const r = await api("/api/portable/import", { bundle: text.trim() });
    if (!r.ok) return toast(r.error || "导入失败", true);
    toast("导入成功，路径已按本机重写");
    if (r.note) $("#export-hint").textContent = r.note;
    await refresh();
    runSelfcheck();
  }
  $("#btn-import").addEventListener("click", () => doImport($("#import-box").value));
  $("#btn-import-file").addEventListener("click", () => $("#import-file").click());
  $("#import-file").addEventListener("change", async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    const text = await f.text();
    $("#import-box").value = text;
    doImport(text);
    e.target.value = "";
  });
  // 支持直接拖拽 json 到文本框
  $("#import-box").addEventListener("dragover", (e) => e.preventDefault());
  $("#import-box").addEventListener("drop", async (e) => {
    e.preventDefault();
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    const text = await f.text();
    $("#import-box").value = text;
    doImport(text);
  });


  /* ------------------------------------------------ 米家设备 */
  let MIOT_DEVICES = [];

  async function loadMiotDevices() {
    const sum = $("#miot-summary");
    sum.textContent = "读取中…";
    const r = await api("/api/miot/devices");
    if (!r.ok) {
      sum.textContent = r.error || "读取失败";
      sum.style.color = "var(--rd)";
      $("#miot-table").hidden = true;
      $("#miot-empty").hidden = false;
      $("#miot-empty").textContent = "读取失败：" + (r.error || "");
      return;
    }
    MIOT_DEVICES = r.devices || [];
    sum.style.color = "";
    sum.textContent = `共 ${MIOT_DEVICES.length} 台${r.mock ? "（演示设备）" : ""}`;
    renderMiotTable();
  }

  function renderMiotTable() {
    const tbl = $("#miot-table");
    const tb = $("tbody", tbl);
    $("#miot-empty").hidden = MIOT_DEVICES.length > 0;
    tbl.hidden = MIOT_DEVICES.length === 0;
    tb.innerHTML = MIOT_DEVICES.map((d, i) => `
      <tr>
        <td>${esc(d.name)}${d.blocked ? ' <span class="tag err">高危·只读</span>' : ""}</td>
        <td><code>${esc(d.model)}</code></td>
        <td>
          <button class="mini" data-act="cap" data-i="${i}">查能力</button>
          <button class="mini" data-act="get" data-i="${i}">读开关</button>
          <button class="mini" data-act="toggle" data-i="${i}">切换开关</button>
        </td>
      </tr>`).join("");
    $$("#miot-table .mini").forEach(b => b.addEventListener("click", () => miotAction(b.dataset.act, +b.dataset.i)));
  }

  async function miotAction(act, i) {
    const dev = MIOT_DEVICES[i];
    if (!dev) return;
    const box = $("#miot-cap");
    box.hidden = false;
    box.className = "result";
    if (act === "cap") {
      box.textContent = "查询中…";
      const r = await api("/api/miot/spec?device=" + encodeURIComponent(dev.name));
      box.className = "result " + (r.ok ? "ok" : "err");
      box.textContent = r.ok ? r.text : (r.error || "失败");
      return;
    }
    const prop = "power";
    if (act === "get") {
      const r = await api("/api/miot/get", { device: dev.name, prop });
      box.className = "result " + (r.ok ? "ok" : "err");
      box.textContent = r.ok ? r.result : (r.error || "失败");
      return;
    }
    // toggle：先读再取反
    const cur = await api("/api/miot/get", { device: dev.name, prop });
    if (!cur.ok) { box.className = "result err"; box.textContent = cur.error || "读取失败"; return; }
    const isOn = /=\s*True/i.test(cur.result);
    const r = await api("/api/miot/set", { device: dev.name, prop, value: isOn ? "关" : "开" });
    box.className = "result " + (r.ok ? "ok" : "err");
    box.textContent = r.ok ? r.result : (r.error || "失败");
    toast(r.ok ? "已执行" : (r.error || "失败"), !r.ok);
  }

  $("#btn-miot-refresh").addEventListener("click", loadMiotDevices);
  $("#btn-save-miot").addEventListener("click", () => saveSection("miot").then(loadMiotDevices));

  /* ------------------------------------------------ 场景 */
  async function loadScenes(fillEditor) {
    const r = await api("/api/scenes");
    if (!r.ok) return toast(r.error || "读取失败", true);
    const scenes = r.scenes || {};
    $("#scene-pick").innerHTML = Object.keys(scenes)
      .map(k => `<option value="${esc(k)}">${esc(k)}（${((scenes[k] || {}).steps || []).length} 步）</option>`)
      .join("") || '<option value="">（还没有场景）</option>';
    // 只在「没被编辑过」时回填，避免冲掉用户正在写的内容
    if (fillEditor !== false || !$("#scene-json").value.trim()) {
      $("#scene-json").value = JSON.stringify(scenes, null, 2);
    }
  }

  $("#btn-scene-save").addEventListener("click", async () => {
    const r = await api("/api/scenes", { scenes: $("#scene-json").value });
    toast(r.ok ? "场景已保存" : (r.error || "保存失败"), !r.ok);
    if (r.ok) loadScenes(true);
  });
  $("#btn-scene-reload").addEventListener("click", () => loadScenes(true));
  $("#btn-scene-run").addEventListener("click", async () => {
    const name = $("#scene-pick").value;
    if (!name) return toast("先选一个场景", true);
    const box = $("#miot-cap");
    box.hidden = false;
    box.className = "result";
    box.textContent = "执行中…";
    const r = await api("/api/scenes/run", { name });
    box.className = "result " + (r.ok ? "ok" : "err");
    box.textContent = r.ok ? r.result : (r.error || "执行失败");
  });

  /* ------------------------------------------------ 日志 */
  let LOG_FILTER = "all";

  function logLine(rec) {
    return `<span class="l-time">${tstr(rec.ts)}</span> <span class="l-${rec.level}">${esc(rec.msg)}</span>`;
  }

  function redrawLogs() {
    const rows = LOGS.filter(r => LOG_FILTER === "all" || r.level === LOG_FILTER);
    const text = rows.map(logLine).join("\n");
    $("#log-mini").innerHTML = text;
    $("#log-full").innerHTML = text;
    const mini = $("#log-mini");
    mini.scrollTop = mini.scrollHeight;
    const full = $("#log-full");
    full.scrollTop = full.scrollHeight;
  }

  function appendLog(rec) {
    if (SEEN.has(rec.seq)) return;
    if (SEEN.size > 6000) SEEN.clear();
    SEEN.add(rec.seq);
    if (rec.seq > LAST_SEQ) LAST_SEQ = rec.seq;
    LOGS.push(rec);
    if (LOGS.length > 3000) LOGS.splice(0, LOGS.length - 3000);
    if (LOG_FILTER !== "all" && rec.level !== LOG_FILTER) return;
    const line = logLine(rec);
    const mini = $("#log-mini");
    mini.insertAdjacentHTML("beforeend", line + "\n");
    const full = $("#log-full");
    full.insertAdjacentHTML("beforeend", line + "\n");
    if (mini.scrollHeight - mini.scrollTop - mini.clientHeight < 60) mini.scrollTop = mini.scrollHeight;
    if ($("#log-autoscroll").checked && $('[data-panel="logs"]').classList.contains("active")) {
      full.scrollTop = full.scrollHeight;
    }
  }

  const logLevelSel = $("#log-level");
  if (logLevelSel) {
    logLevelSel.addEventListener("change", () => {
      LOG_FILTER = logLevelSel.value;
      redrawLogs();
    });
  }

  function connectSSE() {
    const es = new EventSource(BASE + "/api/logs/stream?after=" + LAST_SEQ);
    es.onopen = () => $("#sse-dot").classList.add("on");
    es.onerror = () => $("#sse-dot").classList.remove("on");
    es.onmessage = ev => {
      try { appendLog(JSON.parse(ev.data)); } catch { /* 忽略坏帧 */ }
    };
  }

  $("#btn-log-toggle").addEventListener("click", () => {
    const bar = $("#logbar");
    bar.classList.toggle("collapsed");
    $("#btn-log-toggle").textContent = bar.classList.contains("collapsed") ? "展开" : "收起";
  });

  /* ------------------------------------------------ 启动 */
  (async function boot() {
    // 只有挂在宿主子路径下才显示「返回工作台」，standalone 模式不显示
    if (BASE) {
      const back = $("#back-home");
      if (back) { back.hidden = false; back.href = "/"; }
    }
    // 记住初始 placeholder，secret 字段清空后回落
    $$(".form input").forEach(el => {
      if (SECRET_FIELDS.has(el.name)) el.setAttribute("data-ph", el.placeholder || "");
    });
    bindDirtyTracking();
    await refresh();
    const savedModel = (STATE && STATE.config && STATE.config.llm && STATE.config.llm.model) || "";
    if (savedModel) setModelField(savedModel);
    const last = await api("/api/logs?limit=200");
    if (last.ok) (last.items || []).forEach(appendLog);
    connectSSE();
    setInterval(refresh, 3000);
  })();
})();
