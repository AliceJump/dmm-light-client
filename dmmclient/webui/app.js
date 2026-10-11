/* DMM Light webview 前端：所有数据经 QWebChannel 从 Python 后端来，渲染在此完成。 */
'use strict';

let bridge = null;
let state = null;              // 最近一次快照
let detail = null;             // 当前详情 {product,...}
let filterMode = 0;
let query = '';
let coverBase = {};            // product -> 封面 URI（文件可能还没下载，先记住地址）

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* ---------- 事件入口 ---------- */
function onEvent(payload) {
  const data = typeof payload === 'string' ? JSON.parse(payload) : payload;
  switch (data.type) {
    case 'snapshot': applySnapshot(data); break;
    case 'detail':   applyDetail(data); break;
    case 'status':   setStatus(data.message); break;
    case 'progress': setProgress(data); break;
    case 'busy':     $('busy').classList.toggle('hidden', !data.show);
                     if (data.text) $('busytext').textContent = data.text; break;
    case 'covers':   refreshCovers(data.products); break;
    case 'theme':    document.documentElement.dataset.theme = data.theme; break;
  }
}

function call(action, payload) {
  if (!bridge) return;
  bridge.action(JSON.stringify(Object.assign({action}, payload || {})));
}

/* ---------- 快照渲染 ---------- */
function applySnapshot(data) {
  state = data;
  coverBase = {};
  [...data.library, ...data.store].forEach(c => { coverBase[c.product] = c.coverBase; });
  if (data.hero) coverBase[data.hero.product] = data.hero.coverBase;
  document.documentElement.dataset.theme = data.theme;
  $('libcount').textContent = data.library.length ? data.library.length + '' : '';
  $('storecount').textContent = data.store.length ? data.store.length + '' : '';
  $('acct-chip').textContent = data.accountChip;
  $('acct-dot').classList.toggle('on', !!data.loggedIn);
  $('node-chip').textContent = data.nodeChip;
  $('node-dot').classList.toggle('on', data.nodeReady);
  renderHero(data.hero);
  renderLibrary(data.library);
  renderStore();
  renderAccount(data.account);
  renderNetwork(data.network);
  $('pause').disabled = !data.busy;
  if (currentPage === 'gamelib') renderGameLib();   // 安装状态可能变了
}

function buttonHtml(b, product) {
  const cls = b.pri ? 'btn pri' : (b.danger ? 'btn danger link-like' : 'btn');
  return `<button class="${cls}" data-act="${esc(b.a)}" data-product="${esc(product)}"
           ${b.danger ? 'data-danger="1"' : ''}>${esc(b.t)}</button>`;
}
/* 卡片里的次要按钮走 links 样式 */
function linkHtml(b, product) {
  return `<button class="${b.danger ? 'danger' : ''}" data-act="${esc(b.a)}"
           data-product="${esc(product)}">${esc(b.t)}</button>`;
}

function cardHtml(card) {
  const primary = card.buttons.find(b => b.pri);
  const rest = card.buttons.filter(b => b !== primary);
  const badges = card.badges.map(b => `<span class="badge ${b.k}">${esc(b.t)}</span>`).join('');
  return `<article class="card" data-detail="${esc(card.product)}">
    <div class="cover">${card.cover ? `<img loading="lazy" src="${esc(card.cover)}">` : '<span class="ph">暂无封面</span>'}${badges}</div>
    <h5>${esc(card.title)}</h5>
    <p class="v">${esc(card.state)}</p>
    <div class="go">${primary ? buttonHtml(primary, card.product) : ''}
      ${rest.length ? '<div class="links">' + rest.map(b => linkHtml(b, card.product)).join('') + '</div>' : ''}
    </div></article>`;
}

/* ---------- 增量更新：按 key 复用 DOM 节点，图片不重建就不闪 ---------- */
const sigs = {};   // region -> 上次内容签名，没变化就完全不碰 DOM

function patchGrid(container, cards) {
  const existing = new Map([...container.querySelectorAll(':scope > .card')]
    .map(el => [el.dataset.detail, el]));
  const seen = new Set();
  cards.forEach(card => {
    const sig = JSON.stringify([card.title, card.state, card.buttons, !!card.cover]);
    let el = existing.get(card.product);
    if (el && el.dataset.sig === sig) {          // 内容没变：只调整位置，img 原地保留
      container.appendChild(el);
    } else {
      const tpl = document.createElement('template');
      tpl.innerHTML = cardHtml(card).trim();
      const node = tpl.content.firstElementChild;
      node.dataset.sig = sig;
      if (el) el.replaceWith(node); else container.appendChild(node);
    }
    seen.add(card.product);
  });
  existing.forEach((el, product) => { if (!seen.has(product)) el.remove(); });
}

function renderIfChanged(key, sig, render) {
  if (sigs[key] === sig) return false;
  sigs[key] = sig;
  render();
  return true;
}

function renderHero(hero) {
  const sig = hero ? JSON.stringify([hero.product, hero.title, hero.meta, hero.buttons, hero.coverBase]) : 'none';
  renderIfChanged('hero', sig, () => {
    const el = $('hero');
    if (!hero) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="hero">
    ${hero.cover ? `<div class="bg" style="background-image:url('${esc(hero.cover)}')"></div>` : ''}
    <div class="veil"></div>
    <div class="body">
      ${hero.cover ? `<img class="thumb" src="${esc(hero.cover)}">` : ''}
      <div class="col">
        <span class="tag">继续游戏</span>
        <h3>${esc(hero.title)}</h3>
        <p class="meta">${esc(hero.meta)}</p>
        <div class="acts">${hero.buttons.map(b => buttonHtml(b, hero.product)).join('')}</div>
      </div>
    </div></div>`;
  });
}

function renderLibrary(cards) {
  $('lib-count').textContent = cards.length + ' 款';
  patchGrid($('lib-grid'), cards);
}

function renderStore() {
  if (!state) return;
  document.querySelectorAll('#filters [data-filter]').forEach(btn =>
    btn.classList.toggle('on', +btn.dataset.filter === filterMode));
  const installed = new Set(state.installedProducts);
  const cards = state.store.filter(c => {
    if (query && !c.title.toLowerCase().includes(query)) return false;
    if (filterMode === 1 && c.adult) return false;
    if (filterMode === 2 && !c.adult) return false;
    if (filterMode === 3 && !installed.has(c.product)) return false;
    return true;
  });
  $('store-count2').textContent = `${cards.length} 款`;
  patchGrid($('store-grid'), cards);
}

/* ---------- 表单页（签名没变化就完全不碰 DOM，避免打断正在输入的内容） ---------- */
function renderAccount(a) {
  const sig = JSON.stringify(a);
  renderIfChanged('account', sig, () => {
    $('page-account').innerHTML = `<div class="cards2">
    <div class="panel">
      <h4>当前登录</h4>
      <p class="muted">${esc(a.state)}</p>
      <p class="muted">密码与验证码只在 DMM 官方网页输入，登录成功后回到客户端即可。</p>
      <div class="row grow">
        <button class="btn" data-act="login">浏览器登录 / 续期</button>
        <button class="btn" data-act="import-login">导入官方登录态</button>
        <button class="btn" data-act="new-account">登录新账号</button>
      </div>
    </div>
    <div class="panel">
      <h4>账号列表</h4>
      <label class="flabel">账号</label>
      <select class="field" id="accountsel">${a.list.map(x =>
        `<option value="${esc(x.number)}" ${x.active ? 'selected' : ''}>${esc(x.number)} · ${esc(x.name)}</option>`).join('')}</select>
      <label class="flabel">账号编号</label><input class="field" id="number" value="${esc(a.number)}">
      <label class="flabel">显示名称</label><input class="field" id="accname" value="${esc(a.name)}">
      <div class="row">
        <button class="btn" data-act="save-account">保存当前账号</button>
        <button class="btn" data-act="switch-account">切换所选账号</button>
      </div>
      <p class="muted">同一编号再保存一次即为改名；切换只替换登录态文件，正在运行的游戏不受影响。</p>
    </div>
    <div class="panel wide"><h4>本地存储</h4><p class="muted">${esc(a.storage)}</p></div>
  </div>`;
  });
}

function renderNetwork(n) {
  const sig = JSON.stringify(n);
  renderIfChanged('network', sig, () => {
    $('page-network').innerHTML = `<div class="cards2">
    <div class="panel">
      <h4>代理</h4>
      <label class="flabel">代理来源</label>
      <select class="field" id="mode">
        <option value="0" ${n.mode ? '' : 'selected'}>内置 Xray 节点池</option>
        <option value="1" ${n.mode ? 'selected' : ''}>外部代理地址</option>
      </select>
      <label class="flabel">外部代理地址</label>
      <input class="field" id="proxy" value="${esc(n.proxy)}" placeholder="http://127.0.0.1:10808 或 socks5h://127.0.0.1:10808">
      <p class="muted">外部代理也用于首次下载 Xray 内核；已有内核时，内置模式不依赖 v2rayN。</p>
      <div class="row">
        <button class="btn" data-act="settings">保存代理设置</button>
        <button class="btn" data-act="test">测试 DMM 连接</button>
      </div>
      <div class="switchline">
        <input type="checkbox" id="sysproxy" ${n.sysproxy ? 'checked' : ''}>
        <label for="sysproxy">开启系统代理（给本机浏览器临时用）</label>
      </div>
      <p class="muted">开启后本机浏览器经本客户端节点访问 DMM（用于商店加库）；关闭软件或再点一次即还原。</p>
    </div>
    <div class="panel">
      <h4>节点</h4>
      <select class="field" id="nodesel">${n.list.map(x =>
        `<option value="${esc(x.id)}">${esc(x.name)}${x.enabled ? ' · 已启用' : ' · 已停用'}</option>`).join('')}</select>
      <label class="flabel">添加节点</label>
      <textarea class="field" id="nodetext" rows="3" placeholder="粘贴节点分享链接或单节点 Xray JSON，内容不会明文保存"></textarea>
      <div class="row">
        <button class="btn" data-act="import-node">添加节点</button>
        <button class="btn" data-act="node-file">导入 JSON 文件</button>
      </div>
      <p class="muted">最多 32 个节点，轮换使用，连接失败自动尝试下一节点。
浏览器登录使用 HTTP 代理或内置节点池；SOCKS5 支持 API、更新和下载。</p>
    </div>
    <div class="panel wide"><h4>当前状态</h4><p class="muted">${esc(n.state)}</p></div>
    <div class="panel wide">
      <h4>游戏安装</h4>
      <label class="flabel">默认游戏库（游戏会装到其下的 <product> 子目录）</label>
      <div class="row" style="flex-wrap:nowrap;margin:4px 0 8px">
        <input class="field" id="defaultbase" style="margin:0;flex:1" value="${esc(n.installBase)}"
               placeholder="${esc(n.defaultBase)}">
        <button class="btn" data-act="browse-base">浏览…</button>
      </div>
      <div class="switchline">
        <input type="checkbox" id="askdir" ${n.askInstall ? 'checked' : ''}>
        <label for="askdir">每次安装时询问安装位置</label>
      </div>
      <p class="muted">不勾选时全部游戏直接装到默认游戏库；某款游戏单独选过的位置优先于默认库。
保存代理设置的按钮会一并保存这里的设置。</p>
    </div>
  </div>`;
  });
}

/* ---------- 详情视图（独立的「游戏库」页：左侧全部游戏可筛选，右侧详情） ---------- */
let listFilter = 'all';
let prevPage = 'lib';

function listRows() {
  if (!state) return [];
  const installed = new Set(state.installedProducts || []);
  return state.store.map(c => ({
    product: c.product, title: c.title, cover: c.coverBase || c.cover || '',
    meta: installed.has(c.product) ? '已安装' : '未安装'
  })).filter(r =>
    listFilter === 'all' || (listFilter === 'installed') === (r.meta === '已安装'));
}

function renderGameLib() {
  patchGamelist($('gamelist'), listRows(), detail ? detail.product : '');
  if (!detail) {
    renderIfChanged('pane', 'empty', () => {
      $('gamedetail').innerHTML = '<p class="muted">从左侧选择一款游戏查看详情</p>';
    });
    return;
  }
  const d = detail;
  patchGamelist($('gamelist'), listRows(), d.product);
  const sig = JSON.stringify([d.product, d.title, d.stateLine, d.root, d.buttons, d.shots,
    d.description, d.info, d.specs, d.captions, d.links, d.loading, d.coverBase]);
  renderIfChanged('pane', sig, () => { $('gamedetail').outerHTML = paneHtml(d); });
}

function applyDetail(data) {
  if (!data.product) return;
  detail = data;
  coverBase[data.product] = data.coverBase || coverBase[data.product];
  if (!data.open) return;                    // 只是缓存数据，不开视图
  renderGameLib();
  if (currentPage !== 'gamelib') showPage('gamelib');
  if (!lightbox.classList.contains('hidden')) updateLightbox();   // 大图补抓完成后灯箱自动换成新图
}

function rowHtml(r, active) {
  return `<div class="row ${active ? 'on' : ''}" data-row="${esc(r.product)}">
    ${r.cover ? `<img loading="lazy" src="${esc(r.cover)}">` : '<img>'}
    <div><div class="t">${esc(r.title)}</div><div class="m">${esc(r.meta)}</div></div>
  </div>`;
}

function patchGamelist(container, rows, current) {
  const existing = new Map([...container.querySelectorAll(':scope > .row')]
    .map(el => [el.dataset.row, el]));
  const seen = new Set();
  // 顺序与现状完全一致时跳过 appendChild 挪动（挪 146 个节点会造成整列重排卡顿）
  const domRows = [...container.children];
  const inOrder = rows.length === domRows.length &&
    rows.every((r, i) => domRows[i].dataset.row === r.product);
  rows.forEach(r => {
    const sig = JSON.stringify([r.title, r.meta, !!r.cover, r.product === current]);
    let el = existing.get(r.product);
    if (el && el.dataset.sig === sig) {
      if (!inOrder) container.appendChild(el);
    } else {
      const tpl = document.createElement('template');
      tpl.innerHTML = rowHtml(r, r.product === current).trim();
      const node = tpl.content.firstElementChild;
      node.dataset.sig = sig;
      if (el) el.replaceWith(node); else container.appendChild(node);
    }
    seen.add(r.product);
  });
  existing.forEach((el, product) => { if (!seen.has(product)) el.remove(); });
}

function paneHtml(d) {
  return `<div class="pane" id="gamedetail">
      <div class="block"><div class="detail-head">
        ${d.cover ? `<img class="big" src="${esc(d.cover)}">` : ''}
        <div class="col">
          <h3>${esc(d.title)}</h3>
          <div class="chips">${(d.chips || []).map(c => `<span>${esc(c)}</span>`).join('')}</div>
          <div class="kv"><span class="k">状态</span><span class="v">${esc(d.stateLine)}</span></div>
          ${d.root ? `<div class="kv"><span class="k">目录</span><span class="v">${esc(d.root)}</span></div>` : ''}
          <div class="kv"><span class="k">编号</span><span class="v">${esc(d.product)}</span></div>
        </div></div>
        <div class="links-row" style="margin-top:14px">${(d.buttons || []).map(b => buttonHtml(b, d.product)).join('')}</div>
      </div>
      ${(d.shots && d.shots.length) ? `<div class="block"><h4>截图</h4><div class="shots">${d.shots.map(s =>
        `<img loading="lazy" src="${esc(s.t || s)}" data-full="${esc(s.f || s.t || s)}">`).join('')}</div></div>` : ''}
      ${d.description ? `<div class="block"><h4>简介</h4><div class="desc">${esc(d.description)}</div></div>` : ''}
      ${(d.info && d.info.length) ? `<div class="block"><h4>游戏信息</h4><div class="specgrid">${d.info.map(i =>
        `<span class="k">${esc(i.key)}</span><span class="v" style="user-select:text">${esc(i.value)}</span>`).join('')}</div></div>` : ''}
      ${(d.specs || []).filter(s => s.html).map(s =>
        `<div class="block"><h4>运行环境（${esc(s.os || 'Windows')}）</h4><div class="desc" style="user-select:text">${esc(s.html)}</div></div>`).join('')}
      ${(d.captions && d.captions.length) ? `<div class="block"><h4>注意事项</h4><div class="capt">${d.captions.map(c => '· ' + esc(c)).join('<br>')}</div></div>` : ''}
      ${(d.links && d.links.length) ? `<div class="block"><h4>相关链接</h4><div class="links-row">${d.links.map(l =>
        `<button class="btn" data-url="${esc(l.url)}">${esc(l.text || '打开')}</button>`).join('')}</div></div>` : ''}
      ${d.loading ? '<div class="loading">正在获取商店信息（简介 / 截图 / 运行环境）…</div>' : ''}
      <p class="note">${esc(d.note || '')}</p>
    </div>`;
}

/* ---------- 状态 ---------- */
function setStatus(message) {
  $('status').textContent = message;
  const lb = $('logbox');
  lb.appendChild(document.createTextNode(message + '\n'));   // 追加而非全量重写
  while (lb.childNodes.length > 800) lb.removeChild(lb.firstChild);
  lb.scrollTop = lb.scrollHeight;
}
function setProgress(p) {
  const bar = $('bar');
  if (!p.active) { bar.classList.remove('live', 'indet'); bar.querySelector('i').style.width = '0'; return; }
  bar.classList.add('live');
  bar.classList.toggle('indet', p.percent < 0);
  if (p.percent >= 0) bar.querySelector('i').style.width = p.percent + '%';
  if (p.text) $('status').textContent = p.text;
}

/* 封面后台补齐后只换图片地址：已有 <img> 的原地换 src，占位卡片单独重建 */
function refreshCovers(products) {
  if (!state) return;
  const stamp = Date.now();
  const set = new Set(products);
  const fresh = p => coverBase[p] ? coverBase[p] + '?t=' + stamp : '';
  const patch = cards => cards.forEach(c => {
    if (set.has(c.product) && coverBase[c.product]) c.cover = fresh(c.product);
  });
  patch(state.library); patch(state.store);
  renderLibrary(state.library); renderStore();
  if (state.hero && set.has(state.hero.product) && coverBase[state.hero.product]) {
    state.hero.cover = fresh(state.hero.product);
    const heroEl = document.querySelector('#hero .hero');       // hero 原地换图，不重建
    const thumb = heroEl && heroEl.querySelector('img.thumb');
    if (thumb) {
      thumb.src = fresh(state.hero.product);
      const bg = heroEl.querySelector('.bg');
      if (bg) bg.style.backgroundImage = `url('${fresh(state.hero.product)}')`;
    } else {
      sigs['hero'] = null; renderHero(state.hero);              // 之前没有封面图，只能重建一次
    }
  }
  if (detail && set.has(detail.product) && coverBase[detail.product]) {
    detail.cover = fresh(detail.product);
    const big = document.querySelector('.detail-head img.big');
    if (big) big.src = fresh(detail.product);
    document.querySelectorAll(`.gamelist .row[data-row="${CSS.escape(detail.product)}"] img`)
      .forEach(img => { img.src = fresh(detail.product); });
  }
}

/* ---------- 全局事件委托 ---------- */
document.addEventListener('click', ev => {
  const filter = ev.target.closest('[data-filter]');
  if (filter) { filterMode = +filter.dataset.filter; renderStore(); return; }
  const url = ev.target.closest('[data-url]');
  if (url) { call('open-url', {url: url.dataset.url}); return; }
  const row = ev.target.closest('[data-row]');
  if (row) { call('select-game', {product: row.dataset.row}); return; }
  const cardEl = ev.target.closest('[data-detail]');
  const btn = ev.target.closest('[data-act]');
  if (btn) {
    const act = btn.dataset.act;
    if (act === '__close_detail') { showPage(prevPage === 'gamelib' ? 'lib' : prevPage); return; }
    if (act === 'switch-account') {
      const sel = $('accountsel');
      call(act, {number: sel ? sel.value : ''}); return;
    }
    if (['save-account'].includes(act)) {
      call(act, {number: $('number').value.trim(), name: $('accname').value.trim()}); return;
    }
    if (act === 'settings') {
      call(act, {proxy: $('proxy').value.trim(), mode: $('mode').value === '0',
                 install_base: $('defaultbase') ? $('defaultbase').value.trim() : '',
                 ask: $('askdir') ? $('askdir').checked : true});
      return;
    }
    if (act === 'import-node') { call(act, {text: $('nodetext').value}); return; }
    if (act === 'new-account') { call(act, {number: $('number').value.trim(), name: $('accname').value.trim()}); return; }
    call(act, {product: btn.dataset.product || ''});
    return;
  }
  if (cardEl && !btn) { call('open-detail', {product: cardEl.dataset.detail}); return; }
});

/* 顶栏 */
const TITLES = {lib: ['我的车库', '已安装的游戏'], gamelib: ['游戏库', '全部游戏 · 点选查看详情'],
                store: ['发现游戏', '可安装的游戏'],
                account: ['账号', '登录态与账号快照'], network: ['网络', '代理与节点']};
let currentPage = 'lib';
function showPage(page) {
  if (page !== currentPage) prevPage = currentPage;
  currentPage = page;
  document.querySelectorAll('.nav a').forEach(x => x.classList.toggle('on', x.dataset.page === page));
  ['lib', 'gamelib', 'store', 'account', 'network'].forEach(p =>
    $('page-' + p).classList.toggle('hidden', p !== page));
  $('searchbox').classList.toggle('hidden', page !== 'store');
  $('refresh').textContent = page === 'store' ? '刷新游戏列表' : '刷新';
  $('page-title').textContent = TITLES[page][0];
  $('page-sub').textContent = TITLES[page][1];
  if (page === 'gamelib') renderGameLib();
}
document.querySelectorAll('.nav a').forEach(a => a.onclick = () => showPage(a.dataset.page));
$('refresh').onclick = () => call(currentPage === 'store' ? 'refresh-catalog' : 'refresh');
document.querySelectorAll('#gamelist-filters [data-lf]').forEach(b => b.onclick = () => {
  listFilter = b.dataset.lf;
  document.querySelectorAll('#gamelist-filters [data-lf]').forEach(x => x.classList.toggle('on', x === b));
  renderGameLib();
});
$('search').addEventListener('input', e => { query = e.target.value.trim().toLowerCase(); renderStore(); });
$('pause').onclick = () => call('cancel');
$('logbtn').onclick = () => $('logbox').classList.toggle('hidden');
document.addEventListener('change', ev => {
  if (ev.target.id === 'sysproxy') call('system-proxy', {on: ev.target.checked});
});

/* 截图灯箱：点开放大；←/→ 或按钮切换；点击背景或 Esc 关闭。
   灯箱打开期间详情刷新（大图后台补抓完成）会自动跟随换成最新大图。 */
const lightbox = $('lightbox');
let lightboxIndex = -1;

function lightboxShots() {
  return (detail && detail.shots) || [];
}

function updateLightbox() {
  const shots = lightboxShots();
  if (lightboxIndex < 0 || lightboxIndex >= shots.length) { closeLightbox(); return; }
  const shot = shots[lightboxIndex];
  lightbox.querySelector('img').src = (shot && shot.f) || shot || '';
  lightbox.querySelector('.lb-count').textContent =
    shots.length > 1 ? `${lightboxIndex + 1} / ${shots.length}` : '';
  const single = shots.length < 2;
  lightbox.querySelector('.lb-prev').classList.toggle('hidden', single);
  lightbox.querySelector('.lb-next').classList.toggle('hidden', single);
}

function openLightbox(index) {
  lightboxIndex = index;
  updateLightbox();
  lightbox.classList.remove('hidden');
}

function closeLightbox() {
  lightboxIndex = -1;
  lightbox.classList.add('hidden');
  lightbox.querySelector('img').removeAttribute('src');
}

function stepLightbox(delta) {
  const shots = lightboxShots();
  if (!shots.length) return;
  lightboxIndex = (lightboxIndex + delta + shots.length) % shots.length;
  updateLightbox();
}

document.addEventListener('click', ev => {
  const shot = ev.target.closest('.pane .shots img');
  if (shot) {
    const images = [...document.querySelectorAll('.pane .shots img')];
    openLightbox(images.indexOf(shot));
    return;
  }
  if (ev.target.closest('#lightbox')) {
    if (ev.target.closest('[data-lb=prev]')) { stepLightbox(-1); return; }
    if (ev.target.closest('[data-lb=next]')) { stepLightbox(1); return; }
    closeLightbox();   // 点图片或背景关闭
    return;
  }
});
document.addEventListener('keydown', ev => {
  if (lightbox.classList.contains('hidden')) return;
  if (ev.key === 'Escape') closeLightbox();
  if (ev.key === 'ArrowLeft') stepLightbox(-1);
  if (ev.key === 'ArrowRight') stepLightbox(1);
});

/* ---------- 启动 ---------- */
new QWebChannel(qt.webChannelTransport, channel => {
  bridge = channel.objects.bridge;
  bridge.evt.connect(message => onEvent(message));
  bridge.init();          // 拉第一份快照
});
