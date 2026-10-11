# -*- coding: utf-8 -*-
"""技能表单页（8100 托管）—— 一份模板服务所有 skill，字段表来自各 SKILL.md 的 `form:` 段。

为什么单独一个文件：`model_admin.py` 是手写 BaseHTTPRequestHandler 的常驻服务（1500+ 行），
把 200 行 HTML/CSS/JS 再堆进去 = 下一个泥球；而"每条 skill 写一页"更是 40 页的灾难。
所以这里只有【一个模板 + query 参数】，字段表与校验判据仍然只在 model_admin 那一份。

🔴 三条一寸不许松的纪律（写进代码注释是因为它们容易被"顺手优化"掉）：
1. 页面**不发价格数字**（价格必须从 gateway resolve 现读，进页面就变成"写死的价"）。
2. 提交**只落 8100 的会话级载荷**：不 setDraft、不挂附件、不 submit、不代发 —— 不碰共享输入框。
3. `prefill`（agent 回填）**不等于 submitted**：回填只灌进输入框，额度与开工授权仍来自【人点提交】
   与 `127.0.0.1:8100/approval` 那一次点击。
"""

# ── 品牌 token：全站唯一一份（批准页与本表单页共用，谁也不许再抄一份 :root）──────────
WS_TOKENS = r""":root{--bg:#0a0d12;--panel:#12171f;--line:#222b38;--fg:#e8edf5;--mut:#8b98ab;
--ok:#3ddc97;--bad:#ff6b6b;--warn:#ffb454;--brand:#fa6f08;--on-brand:#1b1f27;
--radius:14px;--shadow:0 6px 24px rgba(0,0,0,.28)}
@media (prefers-color-scheme:light){:root{--bg:#f6f8fb;--panel:#fff;--line:#e2e8f0;
--fg:#12171f;--mut:#5b6b82;--shadow:0 6px 24px rgba(20,30,50,.08)}}
"""
# 注：--brand 用产品面已有的品牌橙（super-clone-ui/lib/client.js:16 的 --weeksir-accent-orange），
# 不是新发明的色 —— 同一屏里 dock 卡片就是这个色，表单跳色会显得是别家的东西。
# --on-brand 是"品牌色底上该用什么字色"：实测 #fa6f08 配白字对比度只有 2.87:1（WCAG AA 要 ≥4.5:1），
# 配 #1b1f27 是 7.3:1 ⇒ 按钮文字必须深色，这条不是审美而是可读性。

FORM_PAGE = r"""<!doctype html><html lang=zh><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>填参数 · WeekSir</title>
<style>
__TOKENS__
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 Inder,Inter,"Noto Sans SC",-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:18px 16px 96px}
/* 顶部品牌带 */
.brand{display:flex;align-items:center;gap:12px;padding-bottom:14px;
border-bottom:1px solid var(--line);margin-bottom:16px}
.brand img{height:30px;width:auto;object-fit:contain}
.brand .slogan{color:var(--mut);font-size:12px;border-left:1px solid var(--line);padding-left:12px}
.brand .chip{margin-left:auto;font-size:11px;color:var(--brand);border:1px solid var(--brand);
border-radius:999px;padding:2px 10px;white-space:nowrap}
h1{font-size:18px;margin:0 0 2px;font-weight:600}
.sub{color:var(--mut);font-size:13px;margin:0 0 18px}
/* 分区 */
.sec{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
padding:16px;margin:0 0 12px;box-shadow:var(--shadow)}
.sec>h2{font-size:13px;margin:0 0 12px;color:var(--mut);font-weight:600;letter-spacing:.4px}
.adv{border:1px dashed var(--line);background:transparent;box-shadow:none}
.adv>h2{cursor:pointer;display:flex;align-items:center;gap:8px;margin:0}
.adv>h2 .t{transition:transform .15s}
.adv[data-open="1"]>h2 .t{transform:rotate(90deg)}
.adv[data-open="0"] .body{display:none}
/* 字段 */
.f{margin:0 0 14px}
.f:last-child{margin-bottom:0}
.f label{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--mut);margin-bottom:5px}
.f label .req{color:var(--bad);font-size:11px;font-weight:600}
.f label .opt{color:var(--mut);font-size:11px;font-weight:400}
/* 🔒 只读行：只有一个合法值的东西不摆成能点的控件（摆了就是"看着能选、选了必失败"的假选项） */
.lock{display:flex;align-items:center;gap:10px;background:var(--bg);border:1px solid var(--line);
border-left:3px solid var(--brand);border-radius:10px;padding:10px 12px;font-size:13px;color:var(--fg)}
.lock b{font-weight:600}
.lock .mut{color:var(--mut);font-size:12px}
.f .tip{font-size:11.5px;color:var(--mut);margin-top:6px}
.f input[type=text],.f input[type=number],.f textarea,.f select{width:100%;font:inherit;font-size:14px;
padding:9px 11px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--fg)}
.f textarea{min-height:78px;resize:vertical}
.f input:focus,.f textarea:focus,.f select:focus{outline:none;border-color:var(--brand)}
.f[data-miss="1"] label,.f[data-miss="1"] .dz{color:var(--bad);border-color:var(--bad)}
.f[data-miss="1"] input,.f[data-miss="1"] textarea,.f[data-miss="1"] select{border-color:var(--bad)}
/* agent 回填标记 */
.ai{font-size:10px;color:var(--brand);border:1px solid var(--brand);border-radius:999px;
padding:0 7px;cursor:help}
/* chip 单选组（选项 ≤4 用它，长清单才退回原生 select）*/
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:10px;
padding:7px 12px;font-size:13px;cursor:pointer;transition:transform .12s,border-color .12s}
.chip:hover{border-color:var(--brand);transform:translateY(-2px)}
.chip[data-on="1"]{border-color:var(--brand);color:var(--brand);background:rgba(250,111,8,.10);font-weight:600}
/* 步进数字 */
.step{display:flex;align-items:center;gap:8px}
.step button{width:30px;height:30px;border-radius:8px;border:1px solid var(--line);
background:var(--bg);color:var(--fg);font-size:15px;cursor:pointer}
.step input{width:78px;text-align:center}
.step .rng{font-size:11px;color:var(--mut)}
/* 拖拽上传区（🔴 不是裸 <input type=file>）*/
.dz{border:1px dashed var(--line);border-radius:12px;padding:14px;text-align:center;
color:var(--mut);font-size:12px;cursor:pointer;transition:border-color .12s,background .12s}
.dz:hover,.dz[data-over="1"]{border-color:var(--brand);color:var(--fg);background:rgba(250,111,8,.06)}
.dz input[type=file]{display:none}
.thumbs{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.th{position:relative;width:86px}
.th img,.th .vid{width:86px;height:86px;object-fit:cover;border-radius:10px;border:1px solid var(--line);
background:#000;cursor:grab;display:block}
.th[data-main="1"] img{border-color:var(--brand)}
.th .mn{position:absolute;top:4px;left:4px;font-size:10px;background:var(--brand);color:var(--on-brand);
border-radius:5px;padding:1px 5px}
.th .rm,.th .up,.th .dn{position:absolute;width:18px;height:18px;line-height:16px;text-align:center;
border-radius:50%;border:1px solid var(--line);background:var(--panel);color:var(--fg);
font-size:11px;cursor:pointer;padding:0}
.th .rm{top:-6px;right:-6px}.th .up{bottom:-6px;left:-6px}.th .dn{bottom:-6px;left:16px}
.th .nm{font-size:10px;color:var(--mut);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.th[data-src="agent"] img,.th[data-src="agent"] .vid{border-color:var(--brand);border-style:dashed}
.th .agent{position:absolute;top:4px;left:4px;font-size:10px;color:var(--brand);cursor:help}
/* 吸底操作条 */
.bar{position:fixed;left:0;right:0;bottom:0;background:var(--panel);border-top:1px solid var(--line);
padding:12px 16px;display:flex;flex-direction:column;gap:6px}
.bar .inner{max-width:720px;margin:0 auto;display:flex;gap:10px;align-items:center;width:100%}
.go{flex:1;padding:12px 14px;border-radius:10px;border:1px solid var(--line);font-size:14px;
font-weight:600;cursor:pointer;background:var(--line);color:var(--mut)}
.go[data-on="1"]{background:var(--brand);border-color:var(--brand);color:var(--on-brand)}
.go[disabled]{cursor:progress;opacity:.75}
.hint{font-size:12px;color:var(--mut);min-height:16px}
.hint.bad{color:var(--bad)}
.hint.ok{color:var(--ok)}
.done .row{display:flex;justify-content:space-between;gap:12px;font-size:13px;
padding:5px 0;border-bottom:1px solid var(--line)}
.done .row .k{color:var(--mut)}
.foot{font-size:11px;color:var(--mut);text-align:center;margin:14px 0 0}
</style>
<body><div class=wrap>
<div class=brand><img src=/assets/weeksir-logo.png alt=WeekSir>
<div class=slogan>专注电商AI落地</div><div class=chip id=skill></div></div>
<h1 id=title>请您仔细填写下面表单</h1>
<p class=sub id=sub>填完点右下角提交，我按这些参数直接开跑 —— 这张表只收输入，花钱那一下仍要点批准页。</p>
<div id=empty class=sec style=display:none><b>这张会话还没表过意图</b>
<p class=sub>没有登记意图就不该出现任何界面。回对话里点一下你要用的那个技能，表就出来了。</p></div>
<div id=silent class=sec style=display:none><b>8100 没回话</b>
<p class=sub>这是【读不到】，不是【没有表单】。别当成技能没配 —— 8100 起了就有。</p></div>
<div id=form></div>
<div class=bar id=bar style=display:none><div class=inner>
<button class=go id=go type=button>提交给 agent 分析</button></div>
<div class=hint id=hint></div></div>
<p class=foot id=foot style=display:none>参数只落这台机器的工作区，提交不等于批准。</p>
</div>
<script>
(function(){
var QS = new URLSearchParams(location.search), SID = QS.get("session") || "";
var API = "/api/skills/form";        // 同源相对路径：页面由 8100 自己发，绝不跨口（端口收敛的根据）
var gate = null, vals = {}, files = {}, aiDefaults = {}, busy = false, sent = false, notes = {};
/* 🔴 editing = 人从【已提交摘要卡】点了「改参数」回来。它是"别再拿 done 覆盖这张表"的旗子：
   服务端 submitted 一直是 true（载荷在那儿摆着），轮询每 8 秒就会把整页换回只读卡 ——
   没有这面旗，客户点【改参数】等于什么都不会发生（2026-10-11 现网那句"ai 帮我选了我没法选"）。 */
var editing = false;
/* 🔴 客户一旦动过任何一格，就不再拿回填覆盖他（回填是建议，不是命令）。
   touched 只记"人改过的 key"；vals 里来自回填的 key 不算。 */
var touched = {}, lastPrefill = "";
function mark(k){ touched[k] = 1; }
function userDirty(){ return Object.keys(touched).length > 0; }

/* ── 完成判据：纯函数，规则与服务端 form_submit 同源（字段表下发），这里不抄第二份 ──
   返回缺失字段的 label 数组；空数组 = 可以提交。                                   */
/* 哪些类型走文件槽：🔴 判据只这一份，与 model_admin._FORM_MEDIA 同口径（闸 C 组连这个函数一起抠出去跑）。
   写成 `type==="images"||type==="video"` 散在四处 = 加一种媒体类型就漏一处，漏的那处会把音频当文本渲染。 */
/* 下拉/chip 的选项写成 `值 显示名`：存和提交都存整串（后端取第一个 token 当值），
   但【显示只给中文】—— 直接把整串印出来会变成客户看见 `preserve_original 原声保留`。 */
function optLabel(op){ var s = String(op || ""); var i = s.indexOf(" "); return i > 0 ? s.slice(i + 1) : s; }
/* 角标：字段表写了 `mark=` 就用那句话（"选填 · 换人时必给"这种约束要能写出来），
   没写就按必填回退成「必填」。以前是一个 `*` —— 客户分不清"必填"和"只在某种情况下必填"。 */
function markOf(f){
  if (f.marker) return {text: f.marker, cls: f.required ? "req" : "opt"};
  return f.required ? {text: "必填", cls: "req"} : null;
}
function putMark(lab, f){
  var m = markOf(f);
  if (m) lab.appendChild(h("span", {class: m.cls, text: m.text}));
  return lab;
}
function isMedia(f){ return f.type === "images" || f.type === "video" || f.type === "audio"; }
/* 条件显隐 + 只读行的判据，和 model_admin._field_hidden 同一条规则（字段表里那个 show=）。
   🔴 隐藏字段【既不占必填、也不进提交载荷】——漏了这条就会出现"前端藏起来了、后端还报必填没填"。 */
function isShown(f, values){
  var c = f.show_if; if (!c) return true;
  var cur = String((values || {})[c.key] || "").split(" ")[0];
  return (c.values || []).indexOf(cur) >= 0;
}
function isLocked(f){ return f.type === "locked"; }
function formIsComplete(g, values, picked, defaults){
  var miss = [];
  ((g && g.fields) || []).forEach(function(f){
    if (isLocked(f) || !isShown(f, values)) return;
    var ok = false;
    if (isMedia(f)){
      var n = (picked[f.key] || []).length;
      var d = ((g.default_files || {})[f.param] || []);
      // 🔴 选填的媒体槽【不选也算填完】。漏了这条的后果很实在：商品图设成选填，
      //    客户永远点不动提交按钮 —— 而按钮上写的还是"还差 1 项"，看着像我们的 bug 又像他的错。
      ok = n > 0 || (Array.isArray(d) ? d.length > 0 : !!d) || !f.required;
    } else if (f.type === "number"){
      var raw = (values[f.key] !== undefined && values[f.key] !== "" ? values[f.key]
                 : ((defaults[f.key] || "") + ""));
      if (String(raw).trim() === "") ok = !f.required;
      else { var n2 = Number(raw); ok = !isNaN(n2) && (f.min == null || n2 >= f.min)
                                      && (f.max == null || n2 <= f.max); }
    } else {
      var v = (values[f.key] !== undefined && values[f.key] !== "" ? values[f.key]
               : (defaults[f.key] || "")).toString().trim();
      ok = v !== "" || !f.required;
    }
    if (!ok) miss.push(f);
  });
  return miss;
}

function h(t, a){ var e = document.createElement(t); for (var k in (a||{})) {
  if (k === "text") e.textContent = a[k]; else if (k === "html") e.innerHTML = a[k];
  else if (k.indexOf("on") === 0) e.addEventListener(k.slice(2).toLowerCase(), a[k]);
  else if (a[k] !== null && a[k] !== undefined) e.setAttribute(k, a[k]); }
  // 🔴 textarea 的 value【不是属性】—— setAttribute("value") 灌不进内容，
  //    表现就是"回填明明有值，框里却是空的"（浏览器实测抓出来的）。
  if (t === "textarea" && a && a.value) e.value = a.value;
  return e; }
/* 回填值是【选项码】(all / gpt)，而 select/chip 的 value 是整条选项文案("all 三变体")。
   直接塞代码匹配不上 ⇒ 下拉显示"（不选）"、chip 一个都不亮 = 客户看不见已经替他填好了。 */
function matchOption(fd, v){
  var code = String(v || "").trim().split(" ")[0];
  var hit = (fd.options || []).filter(function(o){ return o.split(" ")[0] === code; })[0];
  if (hit) return hit;
  return (fd.options || []).indexOf(String(v || "")) >= 0 ? String(v || "") : "";
}

function thumbUrl(item){ return API + "/file?session=" + encodeURIComponent(SID)
  + "&param=" + encodeURIComponent(item.param || "") + "&name=" + encodeURIComponent(item.name || ""); }

/* 图片/视频槽：拖拽 + 点选 + 缩略图 + 主图标记 + 可删可排序（不是裸 file input） */
/* 三种文件槽的措辞与占位。🔴 一处表，别在渲染分支里再各写一遍三元 ——
   原来 `video ? 视频 : 图` 这种写法加第三种媒体类型时，会静默把音频显示成"把图拖进来"。 */
var MEDIA_WORD = {images:"把图拖进来，或点击选", video:"把视频拖进来，或点击选",
                  audio:"把音频拖进来，或点击选"};
/* 盘上名带 sha8（抗跨会话同名覆盖），但界面要能认出是哪张 ⇒ 显示前剥掉哈希段。
   不剥的后果实测出现过：缩略图下面只剩 `01-40fd465c-`，客户看不出那是自己的哪个文件。
   🔴 抽成纯函数给闸跑值表（和 formIsComplete 同一套写法），别只让人 grep 源码。 */
function dispName(p){
  return String(p || "").split(/[\\/]/).pop().replace(/^(\d\d)-[0-9a-f]{8}-/, "$1-");
}
function mediaField(f, sec){
  files[f.key] = files[f.key] || [];
  var box = h("div", {class:"f", "data-key":f.key});
  var lab = putMark(h("label", {text:f.label}), f);
  box.appendChild(lab);
  /* 媒体槽的说明放【槽内】当占位文案（mock 就是这个位置）：槽本来就大,再在下面重复一句是两处说同一件事。 */
  var dz = h("div", {class:"dz", tabindex:0,
    text:f.hint || ((MEDIA_WORD[f.type] || MEDIA_WORD.images) + "（" + (f.max || 1) + " 个上限）")});
  var inp = h("input", {type:"file", accept:(f.accept || []).join(","),
                        multiple:(f.max || 1) > 1 ? "1" : null});
  dz.appendChild(inp); box.appendChild(dz);
  var row = h("div", {class:"thumbs"}); box.appendChild(row);

  function painted(){                      // 已选（本地）+ 已回填（服务端路径）合并渲染
    var out = files[f.key].map(function(it, i){ return {it:it, i:i, local:true}; });
    var pref = ((gate && gate.default_files || {})[f.param] || []);
    if (!Array.isArray(pref)) pref = pref ? [pref] : [];
    pref.slice(0, (f.max || 1)).forEach(function(p, i){ out.push({path:p, i:i, local:false}); });
    return out;
  }
  function draw(){
    row.innerHTML = "";
    painted().forEach(function(e, idx){
      var it = e.it || {}, th = h("div", {class:"th", "data-main":idx === 0 ? "1":"0",
                                    "data-src":e.local ? "user":"agent", draggable:"1"});
      var base = String(e.path || "").split(/[\/\\]/).pop();
      var media = f.type === "audio"
        ? h("div", {class:"vid", text:"♪ " + (e.local ? "音频" : "已选音频"),
                    title:String(base || "")})
        : (e.local
            ? (f.type === "video" ? h("video", {class:"vid", src:it.url || URL.createObjectURL(it.blob)})
                                  : h("img", {src:it.url || URL.createObjectURL(it.blob), alt:""}) )
            : (f.type === "video" ? h("div", {class:"vid", text:"已选视频"})
                                  : h("img", {src:thumbUrl({param:f.param, name:base}), alt:""})));
      th.appendChild(media);
      if (idx === 0 && f.type === "images") th.appendChild(h("span", {class:"mn", text:"主图"}));
      if (!e.local) th.appendChild(h("span", {class:"agent", text:"AI",
        title:(notes[f.key] || "agent 从你在对话里给的素材/要求预填的 · 可改")}));
      var nm = dispName(e.local ? (it.name || "") : base).slice(0, 24);
      th.appendChild(h("div", {class:"nm", text:nm}));
      th.appendChild(h("button", {class:"rm", title:"移除", type:"button", text:"×", onclick:function(){
        mark(f.key);                 // 客户删过的槽不再被回填覆盖（否则刷新就把他的操作吃回去）
        if (e.local) files[f.key].splice(it._i, 1);
        else { var l = gate.default_files[f.param]; if (!Array.isArray(l)) delete gate.default_files[f.param];
               else { var j = l.indexOf(e.path); if (j >= 0) l.splice(j, 1); } }
        refresh(); }}));
      if (idx > 0) th.appendChild(h("button", {class:"up", title:"前移", type:"button", text:"↑", onclick:function(){
        if (e.local && idx === 1){ files[f.key].splice(0, 0, files[f.key].splice(it._i, 1)[0]); refresh(); } }}));
      th.addEventListener("dragstart", function(ev){ try{ ev.dataTransfer.setData("text/th", String(idx)); }catch(x){} });
      th.addEventListener("dragover", function(ev){ ev.preventDefault(); });
      th.addEventListener("drop", function(ev){ ev.preventDefault(); ev.stopPropagation();
        var from = (ev.dataTransfer || {}).getData ? ev.dataTransfer.getData("text/th") : "";
        if (from === "" || !e.local) return;
        var arr = files[f.key], a = Number(from), b = it._i;
        if (a === b) return; arr.splice(b, 0, arr.splice(a, 1)[0]); refresh(); });
      row.appendChild(th);
    });
  }
  function add(list){
    var cap = f.max || 1, room = cap - painted().length;
    Array.prototype.slice.call(list || []).forEach(function(file){
      if (room <= 0) return;
      var okMime = (f.accept || []).some(function(m){ return file.type === m; });
      if (!okMime) { hint(f.label + "：不收 " + (file.type || "未知类型"), "bad"); return; }
      if ((f.max_bytes || 0) && file.size > f.max_bytes) { hint(f.label + "：超过 " + Math.round(f.max_bytes/1048576) + "MB", "bad"); return; }
      room--; files[f.key].push({name:file.name, type:file.type, blob:file, _i:files[f.key].length});
    });
    refresh();
  }
  dz.addEventListener("click", function(){ inp.click(); });
  dz.addEventListener("keydown", function(e){ if (e.key === "Enter" || e.key === " ") inp.click(); });
  inp.addEventListener("change", function(){ add(inp.files); inp.value = ""; });
  dz.addEventListener("dragover", function(e){ e.preventDefault(); dz.setAttribute("data-over", "1"); });
  dz.addEventListener("dragleave", function(){ dz.removeAttribute("data-over"); });
  dz.addEventListener("drop", function(e){ e.preventDefault(); dz.removeAttribute("data-over");
    add((e.dataTransfer || {}).files); });
  sec.appendChild(box);
  return {draw:draw, clear:function(){ files[f.key] = []; }};
}

function hint(t, kind){ var el = document.getElementById("hint");
  el.className = "hint" + (kind ? " " + kind : ""); el.textContent = t || ""; }
function refresh(){ applyVis(); drawAll(); paint(); }

/* 🔴 条件显隐要【在每次值变化后重算】。以前 render() 只在打开页面时算一次，
   结果点了 chip 也不会把「参考音频」放出来 —— 表现就是"这格根本不存在"，比假选项更坏。
   这里只翻 display，不重建 DOM：重建会丢掉客户正在输入的焦点和已选文件。 */
var VIS = [], SECS = [];
function applyVis(){
  VIS.forEach(function(v){
    v.box.style.display = (isLocked(v.f) || isShown(v.f, vals)) ? "" : "none";
  });
  SECS.forEach(function(s){
    var any = s.items.some(function(f){
      return isLocked(f) || isShown(f, vals); });
    s.sec.style.display = any ? "" : "none";
  });
}

function textField(f, sec, kind){
  var box = h("div", {class:"f", "data-key":f.key});
  var lab = putMark(h("label", {text:f.label}), f);
  if ((aiDefaults[f.key] || "") !== "") lab.appendChild(h("span", {class:"ai",
    title:(notes[f.key] || "agent 预填 · 可改"), text:"AI 已填 · 可改"}));
  box.appendChild(lab);
  var el;
  if (kind === "select"){
    var opts = f.options || [];
    if (opts.length && opts.length <= 8){
      /* 一律用 chip：以前"选项 ≤4 才给 chip"，结果 6 档画面比例退化成原生下拉 ——
         互斥单选就该一眼看见全部选项，而不是点开才知道有什么。 */
      var chipOn = matchOption(f, vals[f.key] !== undefined ? vals[f.key] : aiDefaults[f.key]);
      var wrapChips = h("div", {class:"chips"});
      opts.forEach(function(op){
        var c = h("div", {class:"chip", text:optLabel(op), "data-on":(chipOn === op) ? "1":"0",
          onclick:function(){ vals[f.key] = (vals[f.key] === op ? "" : op); mark(f.key); refresh(); }});
        wrapChips.appendChild(c);
      });
      el = wrapChips;
    } else {
      var cur = matchOption(f, vals[f.key] !== undefined ? vals[f.key] : aiDefaults[f.key]);
      el = h("select");
      el.appendChild(h("option", {value:"", text:"（不选）"}));
      opts.forEach(function(op){ el.appendChild(h("option", {value:op, text:optLabel(op)})); });
      el.value = cur;
      el.addEventListener("change", function(){ vals[f.key] = el.value; mark(f.key); refresh(); });
    }
  } else if (kind === "number"){
    var s = h("div", {class:"step"});
    var inp2 = h("input", {type:"number", value:(vals[f.key] === undefined ? "" : vals[f.key]),
      min:(f.min == null ? null : f.min), max:(f.max == null ? null : f.max)});
    inp2.addEventListener("input", function(){ vals[f.key] = inp2.value; mark(f.key); refresh(); });
    s.appendChild(h("button", {type:"button", text:"−", onclick:function(){
      vals[f.key] = String((Number(inp2.value || 0) - 1)); mark(f.key); refresh(); }}));
    s.appendChild(inp2);
    s.appendChild(h("button", {type:"button", text:"+", onclick:function(){
      vals[f.key] = String((Number(inp2.value || 0) + 1)); mark(f.key); refresh(); }}));
    if (f.min != null || f.max != null) s.appendChild(h("span", {class:"rng",
      text:"可填 " + (f.min == null ? "不限" : f.min) + "~" + (f.max == null ? "不限" : f.max)}));
    el = s;
  } else {
    el = h(kind === "textarea" ? "textarea" : "input",
      {type:kind === "textarea" ? null : "text", placeholder:f.placeholder || "",
       value:(vals[f.key] !== undefined ? vals[f.key] : (aiDefaults[f.key] || ""))});
    el.addEventListener("input", function(){ vals[f.key] = el.value; mark(f.key); refresh(); });
  }
  box.appendChild(el);
  if (f.hint) box.appendChild(h("div", {class:"tip", text:f.hint}));
  sec.appendChild(box);
}

/* 🔒 只读行：只有一个合法值、或由 agent 论证且另有批准关口的事，不摆成能填的控件。
   摆了就是假选项 —— 客户点了、填了，到引擎那儿被拒，最后看起来是我们的 bug。 */
function lockedField(f, sec){
  var box = h("div", {class:"f", "data-key":f.key});
  box.appendChild(putMark(h("label", {text:f.label}), f));
  var row = h("div", {class:"lock"});
  row.appendChild(h("span", {text:"🔒"}));
  row.appendChild(h("b", {text:f.value || f.label}));
  if (f.note) row.appendChild(h("span", {class:"mut", text:f.note}));
  box.appendChild(row);
  if (f.hint) box.appendChild(h("div", {class:"tip", text:f.hint}));
  sec.appendChild(box);
}

var medias = [];
function drawAll(){ medias.forEach(function(m){ m.draw(); }); }

/* 分区：字段表写了 `group=` 就【按表分组】（组名、组内顺序都由那条线自己定）；
   一条都没写就退回老的按类型自动分（素材/内容/风格/高级）⇒ 没改字段表的线，界面一个像素都不变。 */
function render(){
  var host = document.getElementById("form"); host.innerHTML = ""; medias = []; VIS = []; SECS = [];
  var F = gate.fields || [];
  var useGroups = F.some(function(f){ return f.group; });
  var groups = [];
  if (useGroups){
    F.forEach(function(f){
      var name = f.group || "其他";
      var g = null;
      for (var i = 0; i < groups.length; i++) if (groups[i][0] === name) { g = groups[i]; break; }
      if (!g){ g = [name, []]; groups.push(g); }
      g[1].push(f);
    });
  } else {
    groups = [["素材", []], ["内容", []], ["风格", []], ["高级", []]];
    F.forEach(function(f){
      var g = isMedia(f) ? 0
            : f.type === "number" ? 3
            : f.type === "select" ? 2 : 1;
      if (f.type === "select" && f.required) g = 2;
      groups[g][1].push(f);
    });
  }
  groups.forEach(function(gr, gi){
    if (!gr[1].length) return;
    var adv = !useGroups && gi === 3;
    var sec = h("div", {class:"sec" + (adv ? " adv":""), "data-open":adv ? "0":"1"});
    var t = h("h2");
    if (adv){ t.appendChild(h("span", {class:"t", text:"▸"})); t.appendChild(document.createTextNode("高级（可不动）")); }
    else t.textContent = gr[0];
    sec.appendChild(t);
    var body = h("div", {class:"body"}); sec.appendChild(body);
    SECS.push({sec: sec, items: gr[1]});
    gr[1].forEach(function(f){
      var into = adv ? body : sec;
      if (isLocked(f)) lockedField(f, into);
      else if (isMedia(f)) medias.push(mediaField(f, into));
      else if (f.type === "number") textField(f, into, "number");
      else if (f.type === "select") textField(f, into, "select");
      else textField(f, into, f.type === "textarea" ? "textarea" : "text");
    });
    if (adv) t.addEventListener("click", function(){
      sec.setAttribute("data-open", sec.getAttribute("data-open") === "1" ? "0" : "1"); });
    host.appendChild(sec);
  });
  // 每个字段一个 [data-key] 盒子 ⇒ 显隐按 key 找回来，不用改三个渲染函数
  VIS = F.map(function(f){
    return {f: f, box: host.querySelector('[data-key="' + f.key + '"]')};
  }).filter(function(v){ return v.box; });
  applyVis();
  document.getElementById("bar").style.display = F.length ? "flex" : "none";
  document.getElementById("foot").style.display = F.length ? "block" : "none";
  drawAll();
}

function paint(){
  var go = document.getElementById("go");
  if (sent){ go.textContent = "已提交 · 改参数要重新提交"; go.setAttribute("data-on", "0"); return; }
  var miss = formIsComplete(gate, vals, files, aiDefaults);
  go.textContent = busy ? "提交中…" : (miss.length ? "还差 " + miss.length + " 项" : "提交给 agent 分析");
  go.setAttribute("data-on", miss.length ? "0" : "1");
  if (!busy) hint(miss.length ? "还差：" + miss.map(function(f){ return f.label; }).join("、") : "",
                 miss.length ? "" : "");
}

function reportAttempt(missing, reason){
  /* 🔴 点了提交但没成交 ⇒ 给 8100 记一次现场（缺哪格 / 哪条会话 / 拒因）。
     为什么值得多发一个请求：客户说"点了没反应"时，载荷文件里那一行【根本没有】，
     服务端就永远答不出是没点、被前端拦了、还是 fetch 没送到（2026-10-10 就是这么靠猜的）。
     失败不打扰客户：台账是诊断面，不是第二道关口，所以吞掉异常继续原流程。 */
  try{
    fetch(API, {method:"POST", headers:{"Content-Type":"text/plain;charset=UTF-8"},
      body:JSON.stringify({stage:"attempt", session:SID, skill:(gate && gate.skill) || "",
                           task:(gate && gate.task) || "", missing:missing || [],
                           reason:reason || ""})}).catch(function(){});
  }catch(e){}
}

function submit(){
  if (busy) return;
  var miss = formIsComplete(gate, vals, files, aiDefaults);
  if (miss.length){                 // 🔴 灰态仍可点：不静默、不提交，只指着缺的那一格
    miss.forEach(function(f){ var el = document.querySelector('[data-key="' + f.key + '"]');
      if (el) { el.setAttribute("data-miss", "1"); } });
    var first = document.querySelector('[data-miss="1"]');
    if (first && first.scrollIntoView) first.scrollIntoView({behavior:"smooth", block:"center"});
    hint("还差：" + miss.map(function(f){ return f.label; }).join("、"), "bad");
    reportAttempt(miss.map(function(f){ return f.label; }), "前端判缺项，未发提交");
    return;
  }
  busy = true; paint();
  var payload = {session:SID, skill:gate.skill, values:{}, files:[]};
  (gate.fields || []).forEach(function(f){
    if (isMedia(f) || isLocked(f) || !isShown(f, vals)) return;
    var v = (vals[f.key] !== undefined && vals[f.key] !== "" ? vals[f.key] : aiDefaults[f.key] || "");
    if (String(v).trim() !== "") payload.values[f.key] = String(v).trim();
  });
  var jobs = [];
  (gate.fields || []).forEach(function(f){
    if (!isMedia(f)) return;
    var picked = files[f.key] || [], prefilled = ((gate.default_files || {})[f.param] || []);
    prefilled.forEach(function(p){ jobs.push(Promise.resolve({field:f.key, path:p})); });
    picked.forEach(function(it){
      jobs.push(new Promise(function(res, rej){
        var r = new FileReader();
        r.onload = function(){ var s = String(r.result || ""); var i = s.indexOf(",");
          res({field:f.key, name:it.name, type:it.type, data:i >= 0 ? s.slice(i + 1) : s}); };
        r.onerror = function(){ rej(new Error("读不出 " + it.name)); };
        r.readAsDataURL(it.blob);
      }));
    });
  });
  Promise.all(jobs).then(function(items){
    payload.files = items.filter(function(x){ return !!x.path || !!x.data; });
    // 🔴 简单请求：8100 没有 do_OPTIONS，application/json 会触发预检撞 501
    return fetch(API, {method:"POST", headers:{"Content-Type":"text/plain;charset=UTF-8"},
                       body:JSON.stringify(payload)});
  }).then(function(r){ return r.json(); }).then(function(j){
    busy = false;
    if (!j || !j.ok){ hint((j && j.error) || "提交失败（8100 没接受）", "bad");
                      reportAttempt([], (j && j.error) || "8100 没接受"); paint(); return; }
    sent = true; editing = false; showDone(j.params || {}); paint();
  }).catch(function(e){ busy = false; hint("提交失败：" + ((e && e.message) || e), "bad");
                        reportAttempt([], "fetch/解析失败：" + ((e && e.message) || e)); paint(); });
}

/* 已提交 = 整卡转确认态，把最终参数列出来供回看（客户看得见"你到底交了我什么"）*/
function showDone(params){
  var host = document.getElementById("form");
  var card = h("div", {class:"sec done"});
  card.appendChild(h("h2", {text:"参数已收到 · 我按这些跑"}));
  Object.keys(params).forEach(function(k){
    var v = params[k];
    card.appendChild(h("div", {class:"row"}, ));
    var row = card.lastChild;
    row.appendChild(h("span", {class:"k", text:(labelOf(k))}));
    var f = fieldOf(k);
    var shown = (!Array.isArray(v) && f && isMedia(f) && v)
      ? String(v).split(/[\/\\]/).pop() : displayValue(k, v);
    row.appendChild(h("span", {text:shown}));
  });
  /* 🔴 这里原来只有一句"要改就回这一栏重新提交一次"，而那一栏已经被本函数 innerHTML="" 清空、
     提交条也 display:none 了 —— 文案指的路代码里不存在，客户只能干看着。改成真的按钮：
     点它 = 退出只读卡、把已交的非素材值灌回格子、恢复提交条（素材格由 default_files 自己长回来）。 */
  var back = h("div", {class:"chip", text:"改参数 · 改完重新提交一次", style:"margin-top:12px",
    title:"改完点【提交给 agent 分析】；这一版交上去之前，agent 用的还是下面这份参数。",
    onclick:function(){
      editing = true; sent = false;
      Object.keys(params).forEach(function(k){
        var f = fieldOf(k);
        if (Array.isArray(params[k]) || (f && isMedia(f))) return;   // 素材走 default_files，别把路径当文本塞进去
        vals[k] = String(params[k]);
      });
      render(); paint();
      hint("已放开编辑：改完点下面的【提交给 agent 分析】。", "");
    }});
  card.appendChild(back);
  card.appendChild(h("p", {class:"sub", style:"margin:12px 0 0",
    text:"生视频那一下仍要在批准页点一次，我才开始扣费跑。"}));
  host.innerHTML = ""; host.appendChild(card);
  document.getElementById("bar").style.display = "none";
}
function labelOf(param){ var hit = null;
  (gate.fields || []).forEach(function(f){ if (f.param === param) hit = f.label; });
  return hit || param; }

function fieldOf(k){ var hit = null;
  ((gate && gate.fields) || []).forEach(function(f){ if (f.key === k || f.param === k) hit = f; });
  return hit; }

/* 🔴 摘要卡上印的必须是【人话】。服务端回的是引擎值（preserve_original / adaptive），
   直接 String(v) 印出来，客户看到的就是一行 token —— 读不懂，还更像"这格被锁死了"。
   select 走 matchOption→optLabel：显示「原声保留（preserve_original）」，值也留着，便于和 agent 对口令。
   对不上选项就【原样印】，不猜一个中文上去（猜错的中文比英文 token 更坑）。 */
function displayValue(k, v){
  if (Array.isArray(v)) return v.length + " 个文件";
  var f = fieldOf(k);
  if (f && f.type === "select"){
    var op = matchOption(f, v);
    if (op){ var code = String(op).split(" ")[0], lab = optLabel(op);
             return lab === code ? String(v) : lab + "（" + code + "）"; }
  }
  return String(v);
}

function boot(){
  document.getElementById("go").addEventListener("click", submit);
  document.getElementById("skill").textContent = SID ? "" : "缺会话参数";
  if (!SID){ document.getElementById("empty").style.display = "block";
    document.getElementById("sub").textContent = "地址里没带 ?session= —— 右栏那档必须把会话 id 传进来。";
    return; }
  poll(); setInterval(poll, 8000);
}

function poll(){
  fetch(API + "?session=" + encodeURIComponent(SID)).then(function(r){ return r.json(); })
  .then(function(j){
    document.getElementById("silent").style.display = "none";
    if (!j || !j.ok || !j.show){
      if (!sent) { document.getElementById("empty").style.display = "block";
        document.getElementById("form").innerHTML = "";
        document.getElementById("bar").style.display = "none"; }
      return;
    }
    if (!j.fields || !j.fields.length){ document.getElementById("empty").style.display = "block"; return; }
    document.getElementById("empty").style.display = "none";
    var fieldsChanged = !gate || gate.skill !== j.skill || JSON.stringify(gate.fields) !== JSON.stringify(j.fields);
    if (fieldsChanged){                       // 换技能 = 整张表重来（残留上一线的值最坑人）
      vals = {}; files = {}; touched = {}; sent = false; editing = false; lastPrefill = "";
    }
    gate = j;
    // 🔴 页头要带会话尾号：这台机器多会话并发、任务名还可能重名，客户点的到底是哪条会话的表
    //    以前看不出来（只有 skill · task）。"我点了提交啊"查不到现场时，第一个要问的就是这个。
    document.getElementById("skill").textContent = j.skill + (j.task ? " · " + j.task : "")
      + " · 会话 " + String(SID).replace(/^session-/, "").slice(0, 8);
    /* 2026-10-11 改判：表【登记完就弹】，不再等 agent 回填 —— 现网实测那段空窗是 38 分钟，
       客户对着右栏什么也看不见，也不知道 agent 卡在哪。但空表不能长得像"没人管"：
       agent 还没看过素材时这一行要明说，客户才分得清"是我先填"还是"等它填完我再改"。
       （回填仍然是报价/建批准条子的硬前置 —— 压力没取消，只是不再拿它为难客户。） */
    var subEl = document.getElementById("sub");
    if (subEl) subEl.textContent = (!j.prefilled && !j.submitted)
      ? "agent 还没分析这批素材 —— 你可以直接填，也可以等它回填之后再改（回填会进同样的格子）。" : "";
    aiDefaults = j.defaults || {}; notes = j.prefill_notes || {};
    // 🔴 回填要真的灌进输入框，不能只让按钮变黄 —— 只变黄 = 客户看不见我们替他填了什么。
    //    但【客户动过任何一个字段之后就不再覆盖】：回填是建议不是命令，也不许吃掉他刚打的字。
    var pre = JSON.stringify([aiDefaults, j.default_files || {}]);
    if (fieldsChanged || pre !== lastPrefill){
      lastPrefill = pre;
      if (!userDirty()){
        Object.keys(aiDefaults).forEach(function(k){ vals[k] = aiDefaults[k]; });
        render();
      } else { drawAll(); paint(); return; }
    }
    if (j.submitted && !(editing || userDirty())){ sent = true; showDone(j.params || {}); paint(); return; }
    paint();
  }).catch(function(){
    gate = null;
    document.getElementById("silent").style.display = "block";
    document.getElementById("bar").style.display = "none";
  });
}
document.addEventListener("DOMContentLoaded", boot);
})();
</script>
</body></html>
"""


def render_form_page() -> str:
    """一个模板服务所有 skill：字段表由前端问 8100 要，页面里【不出现任何 skill 专有的东西】。"""
    return FORM_PAGE.replace("__TOKENS__", WS_TOKENS)
