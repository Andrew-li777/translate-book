/* translate-web 前端逻辑 */
let BOOKS = [], CURRENT = null, CUR_CHUNK = null;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = n => n >= 1048576 ? (n/1048576).toFixed(1)+"MB" : n >= 1024 ? Math.round(n/1024)+"KB" : n+"B";

/* ===== 认证（游客/管理员） ===== */
const AUTH_KEY = 'tw-basic-auth';
function getAuth(){ try{ return localStorage.getItem(AUTH_KEY) || ''; }catch(e){ return ''; } }
function setAuth(v){ try{ v ? localStorage.setItem(AUTH_KEY, v) : localStorage.removeItem(AUTH_KEY); }catch(e){} }
function isAdmin(){ return !!getAuth(); }
function _authHeaders(h){ const a = getAuth(); if(a){ h = h || {}; h['Authorization'] = 'Basic ' + a; } return h || {}; }
function updateAuthBadge(){
  const badge = $('auth-badge'), btn = $('auth-btn'), lo = $('logout-btn');
  if(isAdmin()){
    badge.textContent = '已登录 · ADMIN'; badge.className = 'kicker auth-admin'; badge.onclick = null; badge.title = '';
    if(btn) btn.style.display = 'none';
    if(lo) lo.style.display = 'inline-block';
  } else {
    badge.textContent = '游客 · GUEST'; badge.className = 'kicker auth-guest'; badge.onclick = openLoginModal; badge.title = '点击登录';
    if(btn) btn.style.display = 'inline-block';
    if(lo) lo.style.display = 'none';
  }
}
function openLoginModal(){ $('login-msg').textContent = ''; $('login-modal').style.display = 'flex'; $('login-user').focus(); }
function closeLoginModal(){ $('login-modal').style.display = 'none'; }
async function doLogin(){
  const u = $('login-user').value.trim(), p = $('login-pass').value;
  if(!u || !p){ $('login-msg').textContent = '请输入账号和密码'; return; }
  const token = btoa(u + ':' + p);
  try{
    const r = await fetch('/api/jobs', { headers: { 'Authorization': 'Basic ' + token } });
    if(r.ok){ setAuth(token); updateAuthBadge(); closeLoginModal(); $('login-pass').value=''; location.reload(); }
    else { $('login-msg').textContent = r.status === 401 ? '账号或密码错误' : '登录失败(' + r.status + ')'; }
  }catch(e){ $('login-msg').textContent = '网络错误'; }
}
function logout(){ setAuth(''); updateAuthBadge(); location.reload(); }
function requireLogin(){
  if(isAdmin()) return true;
  openLoginModal(); return false;
}

async function api(url, opts){
  opts = opts || {};
  opts.headers = _authHeaders(opts.headers);
  const r = await fetch(url, opts);
  if(r.status === 401){ setAuth(''); updateAuthBadge(); openLoginModal(); throw new Error('登录已失效，请重新登录'); }
  if(r.status === 403){ openLoginModal(); throw new Error('游客只读：此操作需登录'); }
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}
/* 带认证头的裸 fetch（写操作用）；403/401 时弹登录 */
async function authFetch(url, opts){
  opts = opts || {};
  opts.headers = _authHeaders(opts.headers);
  const r = await fetch(url, opts);
  if(r.status === 401){ setAuth(''); updateAuthBadge(); openLoginModal(); }
  else if(r.status === 403){ openLoginModal(); }
  return r;
}
function statusInfo(s){
  if(s==='done') return {t:'已完成',c:'done'};
  if(s==='converting') return {t:'转换中',c:'converting'};
  if(s==='converted') return {t:'待翻译',c:'converted'};
  if(s && s.startsWith('translating')){ const [,a,b]=s.split(':'); return {t:`翻译中 ${a}/${b}`,c:'translating'}; }
  return {t:'空',c:'empty'};
}

/* ===== 进度：结构化进度取值 + 进度条渲染 ===== */
function progOf(b){
  if(b && b.progress) return b.progress;               // 后端已给
  const s = (b && b.status) || '';
  let done = 0, total = (b && b.chunk_count) || 0;     // 兜底：自己解析状态串
  if(s.startsWith('translating:')){
    const [a, t] = (s.split(':')[1] || '').split('/');
    done = parseInt(a, 10) || 0; total = parseInt(t, 10) || total;
  } else if(s === 'done'){ done = total; }
  const pct = total > 0 ? Math.min(100, Math.round(done * 100 / total)) : 0;
  return {done, total, pct, active: s.startsWith('translating') || s === 'converting'};
}
// 进度条 HTML。total 未知且 active → 无限循环的“不确定态”动画条
function barHtml(p, extraCls){
  const indet = p.active && !p.total;
  const cls = 'pbar' + (extraCls ? ' ' + extraCls : '') + (p.pct >= 100 ? ' done' : '') + (indet ? ' indet' : '');
  const w = indet ? '' : ` style="width:${p.pct}%"`;
  return `<div class="${cls}"><i${w}></i></div>`;
}
// 状态徽章 + 进度条 + 百分比 一行
function progLineHtml(p, st, cls){
  const label = p.active && !p.total ? '处理中' : `${p.done}/${p.total} · ${p.pct}%`;
  return `<div class="prog-line ${cls||''}">${barHtml(p)}<span class="prog-pct${p.pct>=100?' done':''}">${label}</span></div>`;
}

/* ===== 书库 ===== */
let SEL_BOOKS = new Set(), SEL_JOBS = new Set();  // 单项选择状态

async function loadLibrary(){
  try{
    const d = await api('/api/books'); BOOKS = d.books;
  }catch(e){ $('content').innerHTML = `<div class="empty-hint">加载失败：${esc(e.message)}</div>`; return; }
  renderNav(); renderLibrary();
  $('foot-meta').textContent = `${new Date().toISOString().slice(0,10)} · ${BOOKS.length} 本`;
  if(anyActive()) startProgPoll(); else stopProgPoll();
}
function renderNav(){
  $('book-nav').innerHTML = BOOKS.map(b=>{
    const st = statusInfo(b.status);
    const p = progOf(b);
    const mini = p.active ? barHtml(p, 'pbar-mini') : '';
    return `<div class="book-item ${CURRENT===b.name?'active':''}" onclick="openBook('${esc(b.name)}')">
      <input type="checkbox" class="chk" ${SEL_BOOKS.has(b.name)?'checked':''} onchange="toggleSelBook('${esc(b.name)}', this.checked)" onclick="event.stopPropagation()">
      <span class="dot ${st.c}"></span>
      <span class="book-col"><span class="book-name" title="${esc(b.title)}">${esc(b.title)}</span>${mini}</span></div>`;
  }).join('');
  $('nav-count').textContent = BOOKS.length;
  updateSelUI();
}
function renderLibrary(){
  const c = $('content');
  if(!BOOKS.length){ c.innerHTML = '<div class="empty-hint">暂无藏书 · 等待第一本书入库</div>'; return; }
  c.innerHTML = '<div class="grid">' + BOOKS.map(b=>{
    const st = statusInfo(b.status);
    const p = progOf(b);
    const files = Object.entries(b.files||{}).map(([k,v])=>`<span>${k} ${fmt(v)}</span>`).join('');
    const bar = p.active ? barHtml(p) : '';
    const pct = p.active ? `<span class="prog-pct${p.pct>=100?' done':''}">${p.total?p.pct+'%':'…'}</span>` : '';
    return `<div class="card${p.active?' card-busy':''}" onclick="openBook('${esc(b.name)}')">
      <div class="card-body">
        <div class="card-title">${esc(b.title)}</div>
        <div class="card-author">${esc(b.author||'—')}</div>
        <div class="card-meta">
          <span class="badge ${st.c}">${st.t}</span>
          <span>${b.chunk_count||0} chunks</span><span>→ ${esc(b.output_lang||'zh')}</span>${pct}
        </div>
        ${bar}
      </div>
      <div class="card-foot">${files||'<span>—</span>'}
        <span class="dl-btn" onclick="event.stopPropagation();dlFmt('${esc(b.name)}','epub')">EPUB</span>
      </div></div>`;
  }).join('') + '</div>';
  window.scrollTo(0,0);
}
function showLibrary(){
  CURRENT=null; CUR_ACTIVE=false;
  $('crumb-book').style.display='none'; $('crumb-sep').style.display='none'; $('page-title').textContent='';
  renderNav(); renderLibrary();
  if(anyActive()) startProgPoll();
}

/* ===== 进度自动刷新：仅在有书“翻译中/转换中”时开启，全部完成后自动停 ===== */
let PROG_TIMER = null;   // setInterval 句柄
let CUR_ACTIVE = false;  // 当前详情页的书是否在跑

function anyActive(){ return (BOOKS||[]).some(b=>progOf(b).active); }
function startProgPoll(){ if(!PROG_TIMER) PROG_TIMER = setInterval(pollTick, 4000); }
function stopProgPoll(){ if(PROG_TIMER){ clearInterval(PROG_TIMER); PROG_TIMER = null; } }

// 详情页顶部的进度条（done 的书也显示 100%，未开始则不显示）
function paintDetailProg(b){
  const el = $('detail-prog'); if(!el) return;
  const p = progOf(b);
  if(!p.total){ el.innerHTML = ''; return; }
  const flag = p.active ? ' <span style="color:var(--warn)">· 进行中</span>' : '';
  el.innerHTML = `<div class="kicker" style="margin-bottom:6px">翻译进度 · ${p.done}/${p.total} 块${flag}</div>` + progLineHtml(p);
}

async function pollTick(){
  try{
    if(CURRENT){                                  // 详情页：只刷头部，不动内容
      let b; try{ b = await api(`/api/books/${CURRENT}`); }catch(e){ return; }
      CUR_ACTIVE = progOf(b).active;
      paintDetailProg(b);
      const st = statusInfo(b.status), badge = document.querySelector('#detail-head .badge');
      if(badge){ badge.className = 'badge ' + st.c; badge.textContent = st.t; }
      if(!CUR_ACTIVE) stopProgPoll();
    } else {                                      // 书库页：整体重绘（含书卡/侧栏进度条）
      await loadLibrary();
      if(!anyActive()) stopProgPoll();
    }
  }catch(e){}
}
async function dlFmt(name, ext){
  if(!isAdmin()){ openLoginModal(); return; }
  const file = `book.${ext}`;
  // 1) 先向后端要 R2 预签名链接（成品大文件走 R2，省 VPS 出网流量 + 不占浏览器内存）
  let d = null;
  try{
    const r = await fetch(`/api/download/${encodeURIComponent(name)}/${file}`, { headers: _authHeaders() });
    if(r.status === 401){ setAuth(''); updateAuthBadge(); openLoginModal(); return; }
    if(r.ok) d = await r.json();
  }catch(e){ /* 忽略，走本地回退 */ }

  // 2) 有 R2 链接 → 顶级导航直下（浏览器直接写盘，几十 MB 的 PDF 不进 JS 内存）
  if(d && d.url){ window.location.href = d.url; return; }

  // 3) 回退：带凭据的 fetch + Blob（本地直连端点，原可靠路径）
  const url = `/api/books/${encodeURIComponent(name)}/download/${file}`;
  try{
    const r = await fetch(url, { headers: _authHeaders() });
    if(r.status === 401){ setAuth(''); updateAuthBadge(); openLoginModal(); return; }
    if(r.status === 403){ openLoginModal(); return; }
    if(!r.ok){ alert('下载失败：' + r.status); return; }
    // 转 Blob 触发浏览器下载（保留文件名）
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `book.${ext}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
  }catch(e){ alert('下载失败：' + (e.message || e)); }
}

/* ===== P5: 全文搜索 ===== */
async function doSearch(){
  const q = $('search-input').value.trim();
  if(q.length < 2){ alert('关键词至少 2 个字符'); return; }
  const scope = $('search-scope').value;
  let d;
  try{ d = await api(`/api/search?q=${encodeURIComponent(q)}&scope=${scope}`); }
  catch(e){ $('content').innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`; return; }
  CURRENT = null;
  $('crumb-book').style.display='none'; $('crumb-sep').style.display='none';
  $('page-title').textContent = `搜索「${q}」`;
  renderNav();
  if(!d.count){ $('content').innerHTML = `<div class="empty-hint">无结果 · ${esc(q)}</div>`; return; }
  $('content').innerHTML = d.results.map(r=>`
    <div style="margin-bottom:22px">
      <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:6px">
        <span class="kicker" style="color:var(--accent)">${esc(r.title)}</span>
        <span class="kicker">${r.total} 处命中</span>
      </div>
      ${r.hits.map(h=>`
        <div class="search-hit" onclick="gotoChunk('${esc(r.book)}','${h.chunk}')">
          <span class="mono">${h.chunk}</span>
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.snippet)}</span>
        </div>`).join('')}
      ${r.total>r.hits.length?`<div class="kicker" style="margin-top:4px">… 共 ${r.total} 处，仅显示前 ${r.hits.length} 条</div>`:''}
    </div>`).join('');
}
async function gotoChunk(book, chunk){
  await openBook(book);
  switchTab('cmp');
  const sel = $('cmp-select');
  if(sel && [...sel.options].some(o=>o.value===chunk)){
    sel.value = chunk;
    cmpGo(chunk);
  }
}

/* ===== 详情 ===== */
async function openBook(name){
  CURRENT = name; renderNav();
  $('crumb-book').style.display='inline'; $('crumb-sep').style.display='inline';
  $('crumb-book').textContent = name;
  let b;
  try{ b = await api(`/api/books/${name}`); }catch(e){ $('content').innerHTML=`<div class="empty-hint">${esc(e.message)}</div>`; return; }
  $('page-title').textContent = b.meta.title || name;
  const st0 = statusInfo(b.status), p0 = progOf(b);
  CUR_ACTIVE = p0.active;
  $('content').innerHTML = `
    <div id="detail-head" style="display:flex;align-items:center;gap:14px;margin-bottom:14px;flex-wrap:wrap">
      <span class="badge ${st0.c}">${st0.t}</span>
      <span class="kicker">${esc(b.meta.author||'')} · ${b.chunk_count||0} chunks · → ${esc(b.meta.output_lang||'zh')}</span>
    </div>
    <div id="detail-prog" style="margin-bottom:18px"></div>
    <div class="tabs">
      <div class="tab active" data-tab="out" onclick="switchTab('out')">成品</div>
      <div class="tab" data-tab="trans" onclick="switchTab('trans')">译文</div>
      <div class="tab" data-tab="src" onclick="switchTab('src')">原文</div>
      <div class="tab" data-tab="cmp" onclick="switchTab('cmp')">对照</div>
      <div class="tab" data-tab="meta" onclick="switchTab('meta')">元数据</div>
      <div class="tab" data-tab="gloss" onclick="switchTab('gloss')">术语表</div>
      ${b.images?`<div class="tab" data-tab="img" onclick="switchTab('img')">插图 (${b.images})</div>`:''}
    </div>
    <div class="panel active" id="p-out"></div>
    <div class="panel" id="p-trans"></div>
    <div class="panel" id="p-src"></div>
    <div class="panel" id="p-cmp"></div>
    <div class="panel" id="p-meta"></div>
    <div class="panel" id="p-gloss"></div>
    <div class="panel" id="p-img"></div>`;
  const base = `/api/books/${name}`;
  paintDetailProg(b);
  if(CUR_ACTIVE) startProgPoll();
  const fmts = [['pdf','PDF'],['epub','EPUB'],['docx','DOCX'],['html','HTML']];
  const has = k => b.files && (b.files[k] ?? b.files[`book.${k}`]);
  $('p-out').innerHTML = `<div class="dl-row">` + fmts.filter(([k])=>has(k)).map(([k,label])=>{
    const sz = fmt(b.files[`book.${k}`]);
    return `<div class="dl-cell"><span class="fmt">${label}</span><span class="sz">${sz}</span>
      <button class="dl-btn" onclick="dlFmt('${esc(name)}','${k}')">下载</button></div>`;
  }).join('') + `</div>` +
    (has('book.html') ? `<div style="margin-top:16px"><div class="kicker" style="margin-bottom:8px">网页版预览</div>
      <iframe class="doc-preview" src="${base}/file/book.html?raw=1"></iframe></div>` : '');
  if(has('output.md')) loadMd('trans','output.md',name); else $('p-trans').innerHTML='<div class="empty-hint">尚无译文</div>';
  if(has('input.md')) loadMd('src','input.md',name); else $('p-src').innerHTML='<div class="empty-hint">无原文 markdown</div>';
  initCompare(name, b);
  loadMeta(name);
  loadGlossary(name);
  if(b.images) loadImages(name);
}
async function loadMd(panel, path, name){
  const p = $(`p-${panel}`);
  try{
    const d = await api(`/api/books/${name}/render/${path}`);
    p.innerHTML = `<div class="markdown">${d.html}</div>`;
  }catch(e){ p.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`; }
}
function switchTab(t){
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active', x.dataset.tab===t));
  document.querySelectorAll('.panel').forEach(x=>x.classList.toggle('active', x.id===`p-${t}`));
}

/* ===== 对照阅读 ===== */
let CUR_NAME=null, CUR_VIEW='render';
async function mdHtml(name, file){
  try{ const d = await api(`/api/books/${name}/render/${file}`); return `<div class="markdown">${d.html}</div>`; }
  catch(e){ return '<div class="empty-hint">渲染失败</div>'; }
}
function initCompare(name, b){
  CUR_NAME = name;
  const p = $('p-cmp');
  p.innerHTML = `<div class="chunk-nav">
      <span class="kicker">分块对照 · CHUNK</span>
      <button onclick="cmpStep(-1)">← 上一块</button>
      <select id="cmp-select" onchange="cmpGo(this.value)"></select>
      <button onclick="cmpStep(1)">下一块 →</button>
      <span id="cmp-pos" class="kicker"></span>
      <button id="cmp-view-btn" onclick="toggleCmpView()">📖 阅读视图</button>
    </div>
    <div class="compare">
      <div class="compare-pane"><div class="pane-head"><span>原文 · SOURCE</span><span id="cmp-src-size"></span></div><div class="cmp-body" id="cmp-src">—</div></div>
      <div class="compare-pane"><div class="pane-head"><span>译文 · TARGET</span><span id="cmp-trans-size"></span></div><div class="cmp-body" id="cmp-trans">—</div></div>
    </div>`;
  $('cmp-select').innerHTML = b.chunks.map(c=>`<option value="${c.id}">${c.id}${c.translated?'':' (未译)'}</option>`).join('');
  if(b.chunks.length){ $('cmp-select').value = b.chunks[0].id; cmpGo(b.chunks[0].id); }
  // C: 左右 pane 同步滚动（比例映射 + 防抖防循环）
  bindSyncScroll($('cmp-src'), $('cmp-trans'));
}
function bindSyncScroll(a, b){
  let busy = false;
  const sync = (from, to) => {
    if(busy) return;
    busy = true;
    const maxF = from.scrollHeight - from.clientHeight;
    const maxT = to.scrollHeight - to.clientHeight;
    if(maxF > 0 && maxT > 0) to.scrollTop = (from.scrollTop / maxF) * maxT;
    requestAnimationFrame(()=>{ busy = false; });
  };
  a.addEventListener('scroll', ()=>sync(a, b));
  b.addEventListener('scroll', ()=>sync(b, a));
}
function toggleCmpView(){
  CUR_VIEW = CUR_VIEW==='render' ? 'src' : 'render';
  $('cmp-view-btn').textContent = CUR_VIEW==='render' ? '📖 阅读视图' : '📄 源码视图';
  renderCmp();
}
async function cmpGo(cid){
  if(!cid || !CUR_NAME) return;
  let d;
  try{ d = await api(`/api/books/${CUR_NAME}/chunk/${cid}`); }catch(e){ return; }
  CUR_CHUNK = d;
  $('cmp-pos').textContent = `第 ${d.index}/${d.total} 块`;
  if(d.prev){ $('cmp-select').value = d.id; }
  await renderCmp();
}
async function renderCmp(){
  if(!CUR_CHUNK || !CUR_NAME) return;
  const d = CUR_CHUNK, src = $('cmp-src'), trans = $('cmp-trans');
  if(CUR_VIEW === 'render'){
    src.innerHTML = d.src ? await mdHtml(CUR_NAME, `${d.id}.md`) : '<div class="empty-hint">无原文</div>';
    trans.innerHTML = d.trans ? await mdHtml(CUR_NAME, `output_${d.id}.md`) : '<div class="empty-hint">（未翻译）</div>';
  } else {
    src.innerHTML = `<pre>${esc(d.src)}</pre>`;
    trans.innerHTML = `<pre>${esc(d.trans ?? '（未翻译）')}</pre>`;
  }
  $('cmp-src-size').textContent = `${d.src.length} 字符`;
  $('cmp-trans-size').textContent = d.trans ? `${d.trans.length} 字符` : '';
}
function cmpStep(dir){
  if(!CUR_CHUNK) return;
  const target = dir<0 ? CUR_CHUNK.prev : CUR_CHUNK.next;
  if(target){ $('cmp-select').value = target; cmpGo(target); }
}

/* ===== 元数据 ===== */
async function loadMeta(name){
  const p = $('p-meta');
  let cfg={}, man={};
  try{ const d = await api(`/api/books/${name}/file/config.txt`); cfg = parseCfg(d.content); }catch(e){}
  try{ const d = await api(`/api/books/${name}/file/manifest.json`); man = JSON.parse(d.content); }catch(e){}
  const rows = [['书名', cfg.original_title],['作者', cfg.creator],['原语言', cfg.source_language||cfg.input_lang],
    ['目标语言', cfg.output_lang],['分块数', man.chunk_count]] 
    .filter(([,v])=>v).map(([k,v])=>`<tr><th>${k}</th><td>${esc(v)}</td></tr>`).join('');
  p.innerHTML = `<table class="meta-table">${rows||'<tr><td>无元数据</td></tr>'}</table>`;
}
function parseCfg(t){ const o={}; (t||'').split(/\n/).forEach(l=>{ const i=l.indexOf('='); if(l&&l[0]!=='#'&&i>0) o[l.slice(0,i).trim()]=l.slice(i+1).trim(); }); return o; }

/* ===== P4: 术语表 ===== */
const CONF_OPTS = ['low','medium','high'];
function confSel(cur){ return '<select class="g-in" data-f="confidence">' + CONF_OPTS.map(c=>`<option value="${c}"${c===cur?' selected':''}>${c}</option>`).join('') + '</select>'; }
function glossRowHtml(t){
  t = t || {};
  return `<tr>
    <td><input class="g-in" data-f="source" value="${esc(t.source||'')}" placeholder="原文"></td>
    <td><input class="g-in" data-f="target" value="${esc(t.target||'')}" placeholder="译文"></td>
    <td><input class="g-in" data-f="category" value="${esc(t.category||'')}" placeholder="类别"></td>
    <td>${confSel(t.confidence||'medium')}</td>
    <td><input class="g-in" data-f="aliases" value="${esc((t.aliases||[]).join(', '))}" placeholder="别名,逗号分隔"></td>
    <td style="text-align:center">${t.frequency||0}</td>
    <td><button class="g-del" onclick="this.closest('tr').remove()">✕</button></td></tr>`;
}
async function loadGlossary(name){
  const p = $('p-gloss');
  let d;
  try{ d = await api(`/api/books/${name}/glossary`); }
  catch(e){ p.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`; return; }
  const terms = (d.glossary&&d.glossary.terms)||[];
  const rows = terms.map(glossRowHtml).join('');
  p.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:8px">
      <span class="kicker">TERMS · ${terms.length} 条${d.has_glossary?'':'（尚未创建）'}</span>
      <span>
        <button class="dl-btn" onclick="addGlossRow()">＋ 新增</button>
        <button class="dl-btn" onclick="saveGlossary('${esc(name)}')">💾 保存并触发重译</button>
      </span>
    </div>
    <div class="kicker" style="margin-bottom:8px;color:var(--ink-soft)">术语表保证专有名词/人名/地名全书统一译法；保存后 cron 自动重译含这些术语的章节。</div>
    <table class="meta-table gloss-table"><thead><tr>
      <th>原文 source</th><th>译文 target</th><th>类别</th><th>置信度</th><th>别名</th><th>频次</th><th></th>
    </tr></thead><tbody id="gloss-body">${rows||'<tr><td colspan="7" style="text-align:center;color:var(--ink-soft)">暂无术语 · 点击「＋ 新增」添加</td></tr>'}</tbody></table>`;
}
function addGlossRow(){
  const tb = $('gloss-body');
  if(!tb) return;
  if(tb.querySelector('.g-in[placeholder="原文"]') && !tb.querySelector('.g-in[placeholder="原文"]').value){
    alert('请先填写当前空行'); return;
  }
  tb.insertAdjacentHTML('beforeend', glossRowHtml(null));
}
async function saveGlossary(name){
  if(!requireLogin()) return;
  const rows = [...document.querySelectorAll('#gloss-body tr')];
  const terms = [];
  for(const tr of rows){
    const q = f => (tr.querySelector(`[data-f="${f}"]`)||{}).value||'';
    const source = q('source').trim(), target = q('target').trim();
    if(!source && !target) continue;
    if(!source || !target){ alert('术语 source 与 target 均必填'); return; }
    terms.push({
      id: source, source, target,
      category: q('category').trim() || undefined,
      confidence: q('confidence'),
      aliases: q('aliases').split(',').map(s=>s.trim()).filter(Boolean),
      frequency: 0, evidence_refs: [], notes: '',
    });
  }
  if(!terms.length){ alert('没有可保存的术语'); return; }
  try{
    const r = await authFetch(`/api/books/${name}/glossary`, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify({terms})});
    const d = await r.json().catch(()=>({}));
    if(!r.ok){ alert((d.detail||('HTTP '+r.status))); return; }
    alert(`已保存 ${d.terms_count} 条术语 · 将重译 ${d.affected_chunks} 块`);
    loadGlossary(name);
  }catch(e){ alert('保存失败: '+e.message); }
}

/* ===== 插图 ===== */
async function loadImages(name){
  const p = $('p-img');
  const imgs = await api(`/api/books/${name}/file/images/.`).catch(()=>null);
  // 简版：逐个尝试常见编号（images/000001.png ...），未知数量时用占位提示
  const list = await api(`/api/books/${name}`).catch(()=>null);
  const n = list && list.images ? list.images : 0;
  if(!n){ p.innerHTML='<div class="empty-hint">无插图</div>'; return; }
  const items = [];
  for(let i=1;i<=n && i<=200;i++){
    items.push(`<img loading="lazy" onclick="window.open(this.src)" src="/api/books/${name}/file/images/${String(i).padStart(6,'0')}.png" onerror="this.style.display='none'">`);
  }
  p.innerHTML = `<div class="img-grid">${items.join('')}</div>`;
}

/* ===== P3a: 上传 + 任务 ===== */
async function doUpload(){
  const f = $('up-file').files[0];
  const msg = $('up-msg');
  if(!f){ msg.textContent = '请先选择文件'; return; }
  const fd = new FormData();
  fd.append('file', f);
  fd.append('title', $('up-title').value);
  fd.append('target_lang', $('up-lang').value);
  msg.textContent = '上传中…';
  try{
    const r = await authFetch('/api/upload', {method:'POST', body:fd});
    const d = await r.json().catch(()=>({}));
    if(!r.ok){ msg.textContent = (d.detail||('HTTP '+r.status)); return; }
    msg.textContent = '已提交任务 ' + (d.job?d.job.id:'');
    $('up-file').value = ''; $('up-title').value = '';
    loadJobs(); watchJob(d.job); setTimeout(loadJobs, 5000);
  }catch(e){ msg.textContent = '上传失败: '+e.message; }
}
async function loadJobs(){
  let jobs = [];
  try{ const d = await api('/api/jobs'); jobs = d.jobs||[]; }catch(e){ return; }
  window._JOBS = jobs;
  renderJobs(jobs);
}
function renderJobs(jobs){
  const el = $('job-list');
  if(!jobs.length){ el.innerHTML = '<div class="kicker" style="color:var(--ink-soft)">无任务</div>'; return; }
  el.innerHTML = jobs.map(j=>{
    const p = j.progress||{};
    const pct = p.chunks_total ? Math.round((p.chunks_done||0)/p.chunks_total*100) : null;
    const jp = {done:p.chunks_done||0, total:p.chunks_total||0, pct:pct||0,
                active:(j.status!=='done'&&j.status!=='failed'&&j.status!=='cancelled')};
    const bar = (jp.active || jp.pct) ? barHtml(jp, 'pbar-mini') : '';
    const cancel = (j.status!=='done'&&j.status!=='failed'&&j.status!=='cancelled')
      ? `<span style="margin-left:6px;cursor:pointer;color:#c62828" onclick="event.stopPropagation();cancelJob('${j.id}')">✕</span>` : '';
    return `<div class="job-item" id="job-${j.id}" onclick="toggleJobDetail(this)">
      <div style="display:flex;justify-content:space-between;gap:6px">
        <span title="${esc(j.title)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">${esc(j.title)}</span>
        <span class="st st-${esc(j.status)}">${esc(j.status)}</span>
      </div>
      <div class="j-main">
        <input type="checkbox" class="chk" ${SEL_JOBS.has(j.id)?'checked':''} onchange="toggleSelJob('${j.id}', this.checked)" onclick="event.stopPropagation()">
        <span class="kicker" style="flex:1">${esc(p.message||'')}${pct!==null?' · '+pct+'%':''}</span>
        ${cancel}
      </div>
      ${bar}
      <div class="j-detail" style="display:none"><b>ID</b> ${esc(j.id)}\n<b>状态</b> ${esc(j.status)}${j.error?`\n<b>错误</b> <span class="err">${esc(j.error)}</span>`:''}</div>
    </div>`;
  }).join('');
  jobs.forEach(watchJob);  // 为每个非终态任务建立 SSE
  updateSelUI();
}
async function cancelJob(id){
  if(!requireLogin()) return;
  if(!confirm('取消任务 '+id+'？')) return;
  try{ await authFetch('/api/jobs/'+id+'/cancel', {method:'POST'}); loadJobs(); }catch(e){}
}
function toggleJobDetail(el){
  const d = el.querySelector('.j-detail');
  if(d) d.style.display = d.style.display==='none' ? 'block' : 'none';
}

/* ===== P3c: SSE 实时进度 ===== */
const TERMINAL_SSE = new Set(['done','failed','cancelled','notfound']);
const SSE = {};  // job_id -> EventSource

function closeSse(id){ const es = SSE[id]; if(es){ es.close(); delete SSE[id]; } }
function watchJob(job){
  const id = job && job.id;
  if(!id) return;
  if(TERMINAL_SSE.has(job.status)){ closeSse(id); return; }
  if(SSE[id]) return;  // 已连接
  const es = new EventSource(`/api/jobs/${id}/stream`);
  SSE[id] = es;
  es.addEventListener('progress', e => {
    try{
      const d = JSON.parse(e.data);
      const row = document.getElementById('job-'+id);
      if(row) row.dataset.status = d.status;  // 触发样式刷新（简单方案：整体重拉）
    }catch(_){}
    loadJobs();  // 简单可靠：进度变化就重拉列表
  });
  es.addEventListener('done', e => {
    closeSse(id);
    loadJobs();
    loadLibrary();  // 书可能完成入库
  });
  // 连接错误：EventSource 自动重连，无需处理
}

/* ===== 选择管理（单项 + 全选 + 删除所选） ===== */
function toggleSelBook(name, checked){
  if(checked) SEL_BOOKS.add(name); else SEL_BOOKS.delete(name);
  updateSelUI();
}
function toggleSelJob(id, checked){
  if(checked) SEL_JOBS.add(id); else SEL_JOBS.delete(id);
  updateSelUI();
}
function toggleSelAllBooks(){
  const on = $('sel-all-books').checked;
  SEL_BOOKS = on ? new Set(BOOKS.map(b=>b.name)) : new Set();
  updateSelUI();
  document.querySelectorAll('#book-nav .book-item .chk').forEach(c=>c.checked = on);
}
function toggleSelAllJobs(){
  const on = $('sel-all-jobs').checked;
  SEL_JOBS = on ? new Set((window._JOBS||[]).map(j=>j.id)) : new Set();
  updateSelUI();
  document.querySelectorAll('#job-list .job-item .chk').forEach(c=>c.checked = on);
}
function updateSelUI(){
  const bc = $('sel-count'); if(bc) bc.textContent = SEL_BOOKS.size;
  const jc = $('sel-jobs-count'); if(jc) jc.textContent = SEL_JOBS.size;
  const bb = $('btn-del-books'); if(bb) bb.disabled = SEL_BOOKS.size===0;
  const jb = $('btn-del-jobs'); if(jb) jb.disabled = SEL_JOBS.size===0;
  const ba = $('sel-all-books'); if(ba){ ba.checked = BOOKS.length>0 && SEL_BOOKS.size===BOOKS.length; ba.indeterminate = SEL_BOOKS.size>0 && SEL_BOOKS.size<BOOKS.length; }
  const ja = $('sel-all-jobs'); if(ja){ const n=(window._JOBS||[]).length; ja.checked = n>0 && SEL_JOBS.size===n; ja.indeterminate = SEL_JOBS.size>0 && SEL_JOBS.size<n; }
}
async function delSelectedBooks(){
  if(!requireLogin()) return;
  const names = [...SEL_BOOKS];
  if(!names.length) return;
  if(!confirm(`将删除所选 ${names.length} 本书（移入回收站，可恢复）？`)) return;
  let ok=0, fail=[];
  for(const n of names){
    try{
      const r = await authFetch(`/api/books/${encodeURIComponent(n)}/trash`, {method:'POST'});
      if(r.ok) ok++; else { const d = await r.json().catch(()=>({})); fail.push(`${n}：${d.detail||r.status}`); }
    }catch(e){ fail.push(n); }
  }
  SEL_BOOKS = new Set();
  const msg = `已删除 ${ok} 本` + (fail.length?`；失败 ${fail.length} 本（${fail.join('；')}）`:'');
  alert(msg);
  loadLibrary(); loadTrash();
}
async function delSelectedJobs(){
  if(!requireLogin()) return;
  const ids = [...SEL_JOBS];
  if(!ids.length) return;
  if(!confirm(`将删除所选 ${ids.length} 个任务（移入回收站，可恢复）？`)) return;
  let ok=0, fail=[];
  for(const id of ids){
    try{
      const r = await authFetch(`/api/jobs/${id}/trash`, {method:'POST'});
      if(r.ok) ok++; else { const d = await r.json().catch(()=>({})); fail.push(`${id}：${d.detail||r.status}`); }
    }catch(e){ fail.push(id); }
  }
  SEL_JOBS = new Set();
  const msg = `已删除 ${ok} 个任务` + (fail.length?`；失败 ${fail.length} 个（${fail.join('；')}）`:'');
  alert(msg);
  loadJobs(); loadTrash();
}

/* ===== 上传模态框 ===== */
function openUploadModal(){ if(!requireLogin()) return; $('upload-modal').style.display='flex'; }
function closeUploadModal(){ $('upload-modal').style.display='none'; $('up-msg').textContent=''; }

/* ===== 折叠 ===== */
function toggleJobs(){
  const body = $('jobs-body'), tri = $('jobs-collapse');
  const open = body.style.display !== 'none';
  body.style.display = open ? 'none' : 'block';
  tri.textContent = open ? '▸ 任务 · JOBS' : '▾ 任务 · JOBS';
}
function toggleTrash(){
  const body = $('trash-body'), tri = $('trash-collapse');
  const open = body.style.display !== 'none';
  body.style.display = open ? 'none' : 'block';
  tri.textContent = open ? '▸ 回收站 · TRASH' : '▾ 回收站 · TRASH';
  if(!open) loadTrash();
}

/* ===== 回收站 ===== */
async function loadTrash(){
  let d = {books:[], jobs:[]};
  try{ d = await api('/api/trash'); }catch(e){ return; }
  const nb = $('trash-count'); if(nb) nb.textContent = d.books.length + d.jobs.length;
  $('trash-books').innerHTML = d.books.length
    ? `<div class="kicker" style="margin-bottom:2px">藏书</div>` + d.books.map(b=>`
      <div class="trash-item"><span class="t-title">${esc(b.title)}</span>
      <span class="t-meta">${fmt(b.size)} · ${esc(b.trashed_at||'')}</span>
      <button class="mini-btn" onclick="restoreBook('${esc(b.name)}')">恢复</button></div>`).join('')
    : '<div class="kicker" style="margin-bottom:2px">藏书 · 空</div>';
  $('trash-jobs').innerHTML = d.jobs.length
    ? `<div class="kicker" style="margin-bottom:2px">任务</div>` + d.jobs.map(j=>`
      <div class="trash-item"><span class="t-title">${esc(j.title)}</span>
      <span class="t-meta">${esc(j.status)} · ${esc(j.trashed_at||'')}</span>
      <button class="mini-btn" onclick="restoreJob('${j.id}')">恢复</button></div>`).join('')
    : '<div class="kicker" style="margin-bottom:2px">任务 · 空</div>';
}
async function restoreBook(name){
  try{ const r = await authFetch(`/api/trash/books/${encodeURIComponent(name)}/restore`, {method:'POST'});
    if(r.ok){ loadTrash(); loadLibrary(); } else { const d = await r.json().catch(()=>({})); alert(d.detail||'恢复失败'); }
  }catch(e){ alert('恢复失败'); }
}
async function restoreJob(id){
  try{ const r = await authFetch(`/api/trash/jobs/${id}/restore`, {method:'POST'});
    if(r.ok){ loadTrash(); loadJobs(); } else { const d = await r.json().catch(()=>({})); alert(d.detail||'恢复失败'); }
  }catch(e){ alert('恢复失败'); }
}
async function emptyTrash(){
  if(!requireLogin()) return;
  if(!confirm('清空回收站？此操作不可恢复！')) return;
  await authFetch('/api/trash/books/empty', {method:'POST'}).catch(()=>{});
  await authFetch('/api/trash/jobs/empty', {method:'POST'}).catch(()=>{});
  loadTrash();
}

/* ===== 侧栏折叠 ===== */
function toggleSidebar(){
  const app = document.querySelector('.app');
  const on = app.classList.toggle('collapsed');
  $('side-toggle-btn').textContent = on ? '▸' : '☰';
  try{ localStorage.setItem('tw-side-collapsed', on ? '1' : '0'); }catch(e){}
}

/* ===== 启动 ===== */
updateAuthBadge();
loadLibrary();
loadJobs();
loadTrash();
(function(){
  let collapsed = false;
  try{ collapsed = localStorage.getItem('tw-side-collapsed') === '1'; }catch(e){}
  if(collapsed){ document.querySelector('.app').classList.add('collapsed'); $('side-toggle-btn').textContent = '▸'; }
})();
setInterval(()=>{ if(!document.hidden) loadJobs(); }, 15000);
