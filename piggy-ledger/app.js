/* ============================================================
   养猪房账单 · 看板前端
   只消费 window.LEDGER_DATA。数据更新时本文件不需要改动。
   ============================================================ */
(function () {
  'use strict';

  var DATA = window.LEDGER_DATA;
  if (!DATA) {
    document.getElementById('introSub').textContent =
      '没有读到数据集：请先运行 pipeline/run_pipeline.py 生成 app/data.js。';
    return;
  }

  /* ---------------------------------------------------------- 颜色语义 */
  var C = {
    zhu: 'var(--blue)',
    ting: 'var(--pink)',
    public: 'var(--teal)',
    pocket: 'var(--orange)',
    total: 'var(--blue)',
    green: 'var(--green)',
    purple: 'var(--purple)',
    indigo: 'var(--indigo)'
  };

  /* ---------------------------------------------------------- 小工具 */
  var $ = function (id) { return document.getElementById(id); };

  function h(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function svg(tag, attrs) {
    var n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (var k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    return n;
  }
  function money(n, dec) {
    return Number(n).toLocaleString('zh-CN', {
      minimumFractionDigits: dec == null ? 2 : dec,
      maximumFractionDigits: dec == null ? 2 : dec
    });
  }
  function compact(n) {
    var v = Math.abs(n);
    if (v >= 10000) {
      var w = n / 10000;
      return (Math.abs(w) >= 100 ? w.toFixed(0) : w.toFixed(1).replace(/\.0$/, '')) + '万';
    }
    return money(n, 0);
  }
  function ymLabel(ym) {
    return ym.slice(2, 4) + '年' + parseInt(ym.slice(5, 7), 10) + '月';
  }
  function ymShort(ym) {
    return ym.slice(5, 7).replace(/^0/, '') + '月';
  }
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  /* ---------------------------------------------------------- 动效：三次贝塞尔求解（Apple 弹簧的 CSS 映射曲线） */
  function bezier(x1, y1, x2, y2) {
    var cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
    var cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    function fx(t) { return ((ax * t + bx) * t + cx) * t; }
    function fy(t) { return ((ay * t + by) * t + cy) * t; }
    function dfx(t) { return (3 * ax * t + 2 * bx) * t + cx; }
    return function (x) {
      if (x <= 0) return 0;
      if (x >= 1) return 1;
      var t = x;
      for (var i = 0; i < 8; i++) {
        var d = fx(t) - x;
        if (Math.abs(d) < 1e-5) break;
        var dd = dfx(t);
        if (Math.abs(dd) < 1e-6) break;
        t -= d / dd;
      }
      return fy(clamp(t, 0, 1));
    };
  }
  /* damping 0.8 / response 0.3 —— 抽屉与图表的轻微过冲 */
  var EASE_SHEET = bezier(0.34, 1.28, 0.5, 1);
  /* damping 1.0 / response 0.4 —— 临界阻尼，位移与淡入 */
  var EASE_MOVE = bezier(0.32, 0.72, 0, 1);

  var prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------------------------------------------------------- 状态 */
  /* 默认年份 = 当年。取不到当年时（刚跨年但账单还没更新，或看的是历史快照）
     退回数据集里最新的那一年，避免首屏空白。 */
  function defaultYear() {
    var list = DATA.years.map(function (y) { return String(y.year); });
    if (!list.length) return 'all';
    var now = String(new Date().getFullYear());
    if (list.indexOf(now) >= 0) return now;
    return list.reduce(function (a, b) { return Number(b) > Number(a) ? b : a; });
  }

  var state = {
    year: defaultYear(),
    trend: 'total',
    who: '全部',
    kind: '全部',
    sort: 'time',
    q: '',
    shown: 60
  };
  var PAGE = 60;

  var monthCache = {};
  function months(y) {
    if (!monthCache[y]) {
      monthCache[y] = y === 'all'
        ? DATA.months
        : DATA.months.filter(function (m) { return String(m.year) === String(y); });
    }
    return monthCache[y];
  }
  function currentScope() { return months(state.year); }
  function latestMonth() {
    var s = currentScope();
    return s[s.length - 1];
  }

  /* ---------------------------------------------------------- 分段控件 */
  function buildSegmented(host, pill, items, active, onPick) {
    host.querySelectorAll('button').forEach(function (b) { b.remove(); });
    items.forEach(function (it) {
      var b = h('button', null, it.label);
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(it.value === active));
      b.dataset.value = it.value;
      b.addEventListener('click', function () { onPick(it.value); });
      host.appendChild(b);
    });
    requestAnimationFrame(function () { syncPill(host, pill); });
    host._sync = function () { syncPill(host, pill); };
  }
  function syncPill(host, pill) {
    var active = host.querySelector('button[aria-selected="true"]');
    if (!active || !pill) return;
    pill.style.width = active.offsetWidth + 'px';
    pill.style.transform = 'translate3d(' + active.offsetLeft + 'px,0,0)';
  }

  /* 年份控件的横向滚动容器：按滚动位置切换两端渐隐。
     到左边界 → is-start（收掉左侧渐隐），到右边界 → is-end（收掉右侧渐隐）；
     内容没溢出时两者同时成立，CSS 的 .is-start.is-end 会把 mask 整个关掉。 */
  function syncScrollEdges(host) {
    if (!host) return;
    var max = host.scrollWidth - host.clientWidth;
    host.classList.toggle('is-start', host.scrollLeft <= 1);
    host.classList.toggle('is-end', host.scrollLeft >= max - 1);
  }

  /* ---------------------------------------------------------- 提示气泡 */
  var tip = $('tooltip');
  function showTip(event, html) {
    tip.innerHTML = html;
    tip.classList.add('is-on');
    tip.setAttribute('aria-hidden', 'false');
    moveTip(event);
  }
  function moveTip(event) {
    var x = event.clientX, y = event.clientY - 8;
    tip.style.left = clamp(x, 90, window.innerWidth - 90) + 'px';
    tip.style.top = (y < 120 ? y + 190 : y) + 'px';
  }
  function hideTip() {
    tip.classList.remove('is-on');
    tip.setAttribute('aria-hidden', 'true');
  }
  function tipRows(rows) {
    return rows.map(function (r) {
      return '<div class="tt-row"><span>' + r[0] + '</span><span>' + r[1] + '</span></div>';
    }).join('');
  }

  /* ---------------------------------------------------------- KPI */
  var KPI_DEFS = [
    { key: 'grandTotal', label: '累计总支出', color: C.total, unit: '元' },
    { key: 'avg', label: '月均支出', color: C.public, unit: '元' },
    { key: 'people', label: '猪总 / 婷总', color: C.purple, unit: '元' },
    { key: 'latest', label: '最新月支出', color: C.pocket, unit: '元' }
  ];

  function renderKpi() {
    var scope = currentScope();
    var host = $('kpiGrid');
    host.innerHTML = '';

    var total = scope.reduce(function (a, m) { return a + m.grandTotal; }, 0);
    var complete = scope.filter(function (m) { return !m.ongoing; });
    var base = complete.length ? complete : scope;
    var avg = base.reduce(function (a, m) { return a + m.grandTotal; }, 0) / base.length;
    var details = scope.reduce(function (a, m) { return a + m.detailCount; }, 0);
    var items = (function () {
      var set = {};
      DATA.details.forEach(function (d) {
        if (state.year === 'all' || String(d.year) === String(state.year)) set[d.item] = 1;
      });
      return Object.keys(set).length;
    })();
    var last = scope[scope.length - 1];
    var zhu = scope.reduce(function (a, m) { return a + m.zhuTotal; }, 0);
    var ting = scope.reduce(function (a, m) { return a + m.tingTotal; }, 0);

    var values = {
      grandTotal: [money(total, 0), scope.length + ' 个月 · ' + scope[0].ym + ' 起'],
      avg: [money(avg, 0), complete.length < scope.length ? '仅计入 ' + complete.length + ' 个完整月' : '共 ' + base.length + ' 个完整月'],
      people: [money(zhu, 0) + ' / ' + money(ting, 0), '累计 ' + money(zhu + ting, 0) + ' 元'],
      latest: [money(last.grandTotal, 0), last.ym + (last.ongoing ? ' · 进行中' : '')]
    };

    KPI_DEFS.forEach(function (def) {
      var card = h('div', 'kpi' + (def.key === 'people' ? ' kpi-people' : ''));
      var label = h('div', 'kpi-label');
      var dot = h('span', 'kpi-dot');
      dot.style.background = def.color;
      label.appendChild(dot);
      // 年份筛选下首个 KPI 是「该年合计」，标签跟着变，免得读成历史总计
      label.appendChild(h('span', null,
        def.key === 'grandTotal' && state.year !== 'all' ? '本年总支出' : def.label));
      card.appendChild(label);

      var v = h('div', 'kpi-value');
      v.style.color = def.color;
      v.appendChild(document.createTextNode(values[def.key][0]));
      if (def.unit) v.appendChild(h('span', 'unit', def.unit));
      card.appendChild(v);
      card.appendChild(h('div', 'kpi-sub', values[def.key][1]));
      host.appendChild(card);
    });
  }

  /* ---------------------------------------------------------- 结算卡 */
  function renderSettlement() {
    var m = latestMonth();
    $('settleValue').textContent = money(m.tingToZhu);
    var badge = $('settlementBadge');
    badge.textContent = m.ym + (m.ongoing ? ' · 进行中' : '');
    badge.className = 'badge' + (m.ongoing || m.diff ? ' is-warn' : '');

    var rows = [
      ['猪公共', m.zhuPublic], ['猪零花', m.zhuPocket],
      ['婷公共', m.tingPublic], ['婷零花', m.tingPocket],
      ['公共总', m.publicTotal], ['公共分（各半）', m.publicHalf],
      ['猪总', m.zhuTotal], ['婷总', m.tingTotal],
      ['本月总支出', m.grandTotal]
    ];
    var host = $('settleRows');
    host.innerHTML = '';
    rows.forEach(function (r) {
      var li = h('li');
      li.appendChild(h('span', 'k', r[0]));
      li.appendChild(h('span', 'dots'));
      li.appendChild(h('span', 'v', money(r[1])));
      if (r[0] === '本月总支出') li.lastChild.style.color = C.total;
      if (r[0] === '猪总') li.lastChild.style.color = C.zhu;
      if (r[0] === '婷总') li.lastChild.style.color = C.ting;
      host.appendChild(li);
    });

    var notes = [];
    notes.push('结算方向：婷补给猪。本月共同开销 ' + money(m.publicTotal) + ' 元，一人一半各 ' + money(m.publicHalf) + ' 元。');
    if (m.ongoing) notes.push('该月只有「猪」两列有数，视为进行中，尚未计入月均。');
    if (m.diff) {
      notes.push('源表手填的结算块与公式值存在差异：' + Object.keys(m.diff).map(function (k) {
        return k + ' ' + (m.diff[k] > 0 ? '+' : '') + money(m.diff[k]);
      }).join('、') + '（多为人工对冲项，未并入公式值）。');
    }
    $('settleNote').textContent = notes.join(' ');
  }

  /* ---------------------------------------------------------- 趋势图 */
  function trendSeries() {
    return currentScope().map(function (m) {
      if (state.trend === 'structure') {
        return { ym: m.ym, stacks: [
          { name: '公共', value: m.publicTotal, color: C.public },
          { name: '零花', value: m.pocketTotal, color: C.pocket }] };
      }
      if (state.trend === 'people') {
        return { ym: m.ym, stacks: [
          { name: '猪', value: m.zhuTotal, color: C.zhu },
          { name: '婷', value: m.tingTotal, color: C.ting }] };
      }
      return { ym: m.ym, stacks: [{ name: '本月总支出', value: m.grandTotal, color: C.total }] };
    });
  }

  function niceCeil(v) {
    if (!(v > 0)) return 1;
    var e = Math.pow(10, Math.floor(Math.log10(v)));
    var f = v / e;
    var steps = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (var i = 0; i < steps.length; i++) if (f <= steps[i] + 1e-9) return steps[i] * e;
    return 10 * e;
  }

  function renderTrend() {
    var host = $('trendHost');
    host.innerHTML = '';
    var series = trendSeries();
    var stackNames = series[0].stacks.map(function (s) { return s.name; });
    var colors = series[0].stacks.map(function (s) { return s.color; });

    var totals = series.map(function (s) {
      return s.stacks.reduce(function (a, x) { return a + x.value; }, 0);
    });
    var yMax = niceCeil(Math.max.apply(null, totals));

    var W = 760, H = 300, padL = 54, padR = 14, padT = 16, padB = 36;
    var pw = W - padL - padR, ph = H - padT - padB;
    var n = series.length;
    var slot = pw / n;
    var barW = Math.min(15, Math.max(4, slot * 0.6));
    var baseline = padT + ph;

    var root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img',
      'aria-label': '月度支出趋势' });

    // 网格与 Y 轴
    for (var g = 0; g <= 4; g++) {
      var gy = padT + ph - (ph * g / 4);
      root.appendChild(svg('line', { x1: padL, y1: gy, x2: W - padR, y2: gy, class: 'grid-line' }));
      var lab = svg('text', { x: padL - 9, y: gy + 4, class: 'axis-label', 'text-anchor': 'end' });
      lab.textContent = g === 0 ? '0' : compact(yMax * g / 4);
      root.appendChild(lab);
    }

    // 柱子
    var bars = [];
    series.forEach(function (s, i) {
      var cx = padL + slot * (i + 0.5);
      var x = cx - barW / 2;
      var acc = 0;
      var group = svg('g', {});
      var rects = [];
      s.stacks.forEach(function (st) {
        if (st.value <= 0) { acc += st.value; return; }
        var hh = (st.value / yMax) * ph;
        var y = baseline - (acc / yMax) * ph - hh;
        var rect = svg('rect', {
          x: x, y: y, width: barW, height: hh,
          rx: Math.min(3.5, barW / 2.4),
          fill: st.color, class: 'bar-rect'
        });
        rect.dataset.finalY = y;
        rect.dataset.finalH = hh;
        // 本段下方已堆叠的高度：入场动画要按它把整根柱子从基线一起抬起
        rect.dataset.below = String((acc / yMax) * ph);
        rect.dataset.delay = String(Math.round(i * 14));
        group.appendChild(rect);
        rects.push(rect);
        acc += st.value;
      });
      root.appendChild(group);
      bars.push(rects);

      // 悬停热区
      var hit = svg('rect', { x: padL + slot * i, y: padT, width: slot, height: ph, class: 'hit' });
      var rows = [['<b>' + ymLabel(s.ym) + '</b>', '']];
      s.stacks.forEach(function (st) {
        rows.push([st.name, money(st.value)]);
      });
      rows.push(['合计', money(totals[i])]);
      hit.addEventListener('mouseenter', function (e) {
        bars.forEach(function (g2) { g2.forEach(function (r) { r.classList.remove('is-on'); }); });
        rects.forEach(function (r) { r.classList.add('is-on'); });
        showTip(e, tipRows(rows));
      });
      hit.addEventListener('mousemove', moveTip);
      hit.addEventListener('mouseleave', function () {
        rects.forEach(function (r) { r.classList.remove('is-on'); });
        hideTip();
      });
      root.appendChild(hit);
    });

    // X 轴标签：年跨度大时只标年份边界，单年时逐月标注
    if (n <= 16) {
      series.forEach(function (s, i) {
        var t = svg('text', {
          x: padL + slot * (i + 0.5), y: H - 14, class: 'axis-label', 'text-anchor': 'middle'
        });
        t.textContent = (i === 0 ? s.ym.slice(0, 4) + '/' : '') + ymShort(s.ym);
        root.appendChild(t);
      });
    } else {
      var lastYear = null;
      series.forEach(function (s, i) {
        var y = s.ym.slice(0, 4);
        if (y === lastYear) return;
        lastYear = y;
        var t = svg('text', {
          x: padL + slot * i + 1, y: H - 14, class: 'axis-label',
          'text-anchor': 'start', style: 'font-weight:640;fill:var(--label-2)'
        });
        t.textContent = y + '年';
        root.appendChild(t);
      });
    }

    host.appendChild(root);

    // 入场：柱子从底部弹起（stagger + 弹簧曲线）
    if (prefersReduced) {
      bars.forEach(function (g2) {
        g2.forEach(function (r) { r.setAttribute('y', r.dataset.finalY); r.setAttribute('height', r.dataset.finalH); });
      });
    } else {
      bars.forEach(function (g2) {
        g2.forEach(function (r) {
          r.setAttribute('y', baseline);
          r.setAttribute('height', 0);
          r.dataset.t0 = '';
        });
      });
      var start = null;
      var dur = 520;
      (function step(ts) {
        if (start == null) start = ts;
        var done = true;
        series.forEach(function (s, i) {
          var delay = i * 14;
          var p = clamp(((ts - start) - delay) / dur, 0, 1);
          if (p < 1) done = false;
          var e = EASE_SHEET(p);
          bars[i].forEach(function (r) {
            var fh = parseFloat(r.dataset.finalH);
            var below = parseFloat(r.dataset.below) || 0;
            var hh = fh * e;
            r.setAttribute('height', Math.max(0, hh));
            // 整根柱子从基线一起升起：本段底边随 e 上移 = 基线 − 下方高度·e，再减去本段高度
            r.setAttribute('y', baseline - below * e - hh);
          });
        });
        if (!done) requestAnimationFrame(step);
      })(performance.now());
    }

    // 图例
    var lg = $('trendLegend');
    lg.innerHTML = '';
    stackNames.forEach(function (name, i) {
      var li = h('li');
      var sw = h('span', 'sw');
      sw.style.background = colors[i];
      li.appendChild(sw);
      li.appendChild(h('span', null, name));
      lg.appendChild(li);
    });
    var li2 = h('li');
    li2.appendChild(h('span', null, '共 ' + n + ' 个月 · 峰值 ' + compact(Math.max.apply(null, totals)) + ' 元'));
    lg.appendChild(li2);
  }

  /* ---------------------------------------------------------- 环图 */
  function drawDonut(hostId, legendId, segs, centerV, centerL, ariaLabel) {
    var total = segs.reduce(function (a, s) { return a + s.value; }, 0);

    var host = $(hostId);
    host.innerHTML = '';
    var S = 168, R = 68, SW = 20, cx = S / 2, cy = S / 2;
    var circum = 2 * Math.PI * R;
    var root = svg('svg', { viewBox: '0 0 ' + S + ' ' + S, role: 'img', 'aria-label': ariaLabel });

    var ring = svg('circle', { cx: cx, cy: cy, r: R, fill: 'none',
      stroke: 'var(--track)', 'stroke-width': SW });
    root.appendChild(ring);

    var offset = 0;
    var arcs = [];
    segs.forEach(function (s) {
      var frac = total ? s.value / total : 0;
      var len = circum * frac;
      var arc = svg('circle', {
        cx: cx, cy: cy, r: R, fill: 'none',
        stroke: s.color, 'stroke-width': SW,
        'stroke-dasharray': '0 ' + circum,
        'stroke-dashoffset': String(-offset),
        'stroke-linecap': 'butt',
        transform: 'rotate(-90 ' + cx + ' ' + cy + ')',
        class: 'donut-arc'
      });
      arc.dataset.len = String(len);
      arc.dataset.offset = String(offset);
      arc.addEventListener('mouseenter', function (e) {
        arcs.forEach(function (a) { a.classList.remove('is-on'); });
        arc.classList.add('is-on');
        showTip(e, tipRows([['<b>' + s.name + '</b>', ''],
          ['金额', money(s.value)], ['占比', (frac * 100).toFixed(1) + '%']]));
      });
      arc.addEventListener('mousemove', moveTip);
      arc.addEventListener('mouseleave', function () { arc.classList.remove('is-on'); hideTip(); });
      root.appendChild(arc);
      arcs.push(arc);
      offset += len;
    });

    var vTxt = svg('text', { x: cx, y: cy + 1, 'text-anchor': 'middle',
      fill: 'var(--label)', class: 'donut-center-v' });
    vTxt.textContent = centerV;
    root.appendChild(vTxt);
    var lTxt = svg('text', { x: cx, y: cy + 18, 'text-anchor': 'middle',
      fill: 'var(--label-3)', class: 'donut-center-l' });
    lTxt.textContent = centerL;
    root.appendChild(lTxt);

    host.appendChild(root);

    var anim = function (p) {
      arcs.forEach(function (a) {
        var len = parseFloat(a.dataset.len);
        var off = parseFloat(a.dataset.offset);
        var cur = len * p;
        a.setAttribute('stroke-dasharray', cur + ' ' + (circum - cur));
        a.setAttribute('stroke-dashoffset', String(-(off)));
      });
    };
    if (prefersReduced) anim(1);
    else { var st = performance.now(); (function s(ts) {
      var p = clamp((ts - st) / 620, 0, 1);
      anim(EASE_MOVE(p));
      if (p < 1) requestAnimationFrame(s);
    })(st); anim(0); }

    var lg = $(legendId);
    lg.innerHTML = '';
    segs.forEach(function (s) {
      var li = h('li');
      var sw = h('span', 'sw');
      sw.style.background = s.color;
      li.appendChild(sw);
      var nm = h('span', 'n', s.name);
      li.appendChild(nm);
      var vv = h('span', 'v');
      vv.appendChild(document.createTextNode(money(s.value)));
      var pct = h('span', 'pct', total ? (s.value / total * 100).toFixed(1) + '%' : '—');
      vv.appendChild(pct);
      li.appendChild(vv);
      lg.appendChild(li);
    });
  }

  function renderDonut() {
    var scope = currentScope();
    var pub = scope.reduce(function (a, m) { return a + m.publicTotal; }, 0);
    var pocket = scope.reduce(function (a, m) { return a + m.pocketTotal; }, 0);
    drawDonut('donutHost', 'donutLegend', [
      { name: '公共开销', value: pub, color: C.public },
      { name: '个人零花', value: pocket, color: C.pocket }
    ], compact(pub + pocket), '累计支出', '公共与零花占比');

    var zhu = 0, ting = 0;
    scope.forEach(function (m) { zhu += m.zhuTotal; ting += m.tingTotal; });
    drawDonut('donutHost2', 'donutLegend2', [
      { name: '猪总', value: zhu, color: C.zhu },
      { name: '婷总', value: ting, color: C.ting }
    ], compact(zhu + ting), '累计支出', '猪与婷累计支出占比');
  }

  /* ---------------------------------------------------------- 猪 / 婷 分摊 */
  function renderSplit() {
    var scope = currentScope();
    var zhu = { pub: 0, pocket: 0 }, ting = { pub: 0, pocket: 0 };
    scope.forEach(function (m) {
      zhu.pub += m.zhuPublic; zhu.pocket += m.zhuPocket;
      ting.pub += m.tingPublic; ting.pocket += m.tingPocket;
    });
    var zTotal = zhu.pub + zhu.pocket, tTotal = ting.pub + ting.pocket;
    var max = Math.max(zTotal, tTotal) || 1;

    var host = $('splitHost');
    host.innerHTML = '';
    [
      { name: '猪', total: zTotal, pub: zhu.pub, pocket: zhu.pocket, color: C.zhu },
      { name: '婷', total: tTotal, pub: ting.pub, pocket: ting.pocket, color: C.ting }
    ].forEach(function (p) {
      var row = h('div', 'split-row');
      var top = h('div', 'split-top');
      top.appendChild(h('span', null, p.name + ' · 名下支出'));
      top.appendChild(h('span', 'amt', money(p.total) + ' 元'));
      row.appendChild(top);

      var track = h('div', 'split-track');
      // 外层按最大者归一化，内部再按公共/零花拆分
      var outer = h('span');
      outer.style.width = (p.total / max * 100) + '%';
      outer.style.background = p.color;
      outer.style.opacity = '.28';
      outer.style.borderRadius = '980px';
      outer.style.display = 'flex';
      outer.style.overflow = 'hidden';
      if (p.total > 0) {
        var innerPub = h('span');
        innerPub.style.width = (p.pub / p.total * 100) + '%';
        innerPub.style.background = C.public;
        var innerPoc = h('span');
        innerPoc.style.flex = '1';
        innerPoc.style.background = p.color;
        outer.appendChild(innerPub);
        outer.appendChild(innerPoc);
      }
      track.appendChild(outer);
      row.appendChild(track);

      var lg = h('div', 'split-legend');
      lg.innerHTML = '<span><i style="background:' + C.public + '"></i>公共 ' + money(p.pub) + '</span>'
        + '<span><i style="background:' + p.color + '"></i>零花 ' + money(p.pocket) + '</span>'
        + '<span>占两人合计 ' + (zTotal + tTotal ? (p.total / (zTotal + tTotal) * 100).toFixed(1) : '0') + '%</span>';
      row.appendChild(lg);
      host.appendChild(row);
    });

    var gap = h('div', 'year-note',
      '结算口径：共同开销对半分担。' + (zTotal + tTotal
        ? '合计' + money(zTotal + tTotal) + ' 元中，猪承担 ' + money(zTotal) + '、婷承担 ' + money(tTotal) + '。'
        : ''));
    host.appendChild(gap);
  }

  /* ---------------------------------------------------------- 年度对比 */
  function renderYears() {
    var host = $('yearHost');
    host.innerHTML = '';
    /* 「年度对比」天然是跨年视图：跟着年份筛选只剩一行没有意义，
       所以这里恒用全量年份（其余卡片仍随年份筛选）。 */
    var list = DATA.years;
    var max = Math.max.apply(null, list.map(function (y) { return y.grandTotal; })) || 1;

    list.forEach(function (y) {
      var row = h('div', 'year-row');
      row.appendChild(h('span', 'y', y.year + '年'));
      var track = h('div', 'track');
      var fill = h('i');
      fill.style.width = '0%';
      fill.style.background = 'linear-gradient(90deg, ' + C.total + ', ' + C.purple + ')';
      track.appendChild(fill);
      row.appendChild(track);
      var amt = h('span', 'amt');
      amt.textContent = money(y.grandTotal, 0) + ' 元';
      row.appendChild(amt);
      host.appendChild(row);

      requestAnimationFrame(function () {
        fill.style.width = Math.max(3, y.grandTotal / max * 100) + '%';
      });

      var note = h('div', 'year-note');
      note.textContent = y.months + ' 个月 · 月均 ' + money(y.avg, 0)
        + ' 元 · 公共 ' + compact(y.publicTotal) + ' / 零花 ' + compact(y.pocketTotal);
      host.appendChild(note);
    });
  }

  /* ---------------------------------------------------------- 排行 */
  function renderRank() {
    var scope = currentScope();
    var allow = {};
    scope.forEach(function (m) { allow[m.ym] = 1; });
    var agg = {};
    DATA.details.forEach(function (d) {
      if (!allow[d.ym]) return;
      var r = agg[d.item] || (agg[d.item] = { name: d.item, count: 0, total: 0 });
      r.count++; r.total += d.amount;
    });
    var list = Object.keys(agg).map(function (k) { return agg[k]; })
      .sort(function (a, b) { return b.total - a.total; }).slice(0, 15);
    var max = list.length ? list[0].total : 1;

    var host = $('rankList');
    host.innerHTML = '';
    list.forEach(function (r, i) {
      var li = h('li', 'rank-item');
      li.appendChild(h('span', 'rank-no', String(i + 1)));
      li.appendChild(h('span', 'rank-name', r.name));
      var track = h('div', 'rank-track');
      var fill = h('i');
      fill.style.width = '0%';
      track.appendChild(fill);
      li.appendChild(track);
      var amt = h('div', 'rank-amt');
      amt.appendChild(document.createTextNode(money(r.total, 0)));
      amt.appendChild(h('em', null, r.count + ' 次'));
      li.appendChild(amt);
      host.appendChild(li);
      requestAnimationFrame(function () { fill.style.width = Math.max(2, r.total / max * 100) + '%'; });
    });
    $('rankHint').textContent = 'TOP ' + list.length + ' · 按累计金额（当前范围）';
  }

  /* ---------------------------------------------------------- 明细表 */
  function filteredDetails() {
    var allow = {};
    currentScope().forEach(function (m) { allow[m.ym] = 1; });
    var q = state.q.trim().toLowerCase();
    var out = DATA.details.filter(function (d) {
      if (!allow[d.ym]) return false;
      if (state.who !== '全部' && d.who !== state.who) return false;
      if (state.kind !== '全部' && d.kind !== state.kind) return false;
      if (q && d.item.toLowerCase().indexOf(q) === -1) return false;
      return true;
    });
    if (state.sort === 'amount') out.sort(function (a, b) { return b.amount - a.amount; });
    else out.sort(function (a, b) { return a.ym < b.ym ? 1 : a.ym > b.ym ? -1 : b.src_row - a.src_row; });
    return out;
  }

  function renderDetails() {
    var rows = filteredDetails();
    var body = $('detailBody');
    body.innerHTML = '';
    var slice = rows.slice(0, state.shown);

    if (!slice.length) {
      var tr = h('tr');
      var td = h('td', 'empty', '没有符合条件的明细');
      td.colSpan = 5;
      tr.appendChild(td);
      body.appendChild(tr);
    } else {
      var frag = document.createDocumentFragment();
      slice.forEach(function (d) {
        var tr = h('tr');
        tr.appendChild(h('td', null, d.ym));
        var w = h('td'); w.appendChild(h('span', 'tag tag-' + (d.who === '猪' ? 'zhu' : 'ting'), d.who)); tr.appendChild(w);
        var k = h('td'); k.appendChild(h('span', 'tag tag-' + (d.kind === '公共' ? 'public' : 'pocket'), d.kind)); tr.appendChild(k);
        tr.appendChild(h('td', 'item-cell', d.item));
        tr.appendChild(h('td', 'num', money(d.amount)));
        frag.appendChild(tr);
      });
      body.appendChild(frag);
    }

    var sum = rows.reduce(function (a, d) { return a + d.amount; }, 0);
    $('detailHint').textContent = rows.length + ' 条 · 合计 ' + money(sum, 0) + ' 元';
    $('loadMore').disabled = rows.length <= state.shown;
    $('loadMore').textContent = rows.length <= state.shown
      ? '已全部显示' : '显示更多（还有 ' + (rows.length - state.shown) + ' 条）';
  }

  /* ---------------------------------------------------------- 校验 */
  var showTiny = false;
  function renderChecks() {
    var host = $('checkList');
    host.innerHTML = '';
    var list = DATA.checks.filter(function (c) {
      return state.year === 'all' || c.ym.slice(0, 4) === String(state.year);
    });
    var major = list.filter(function (c) { return c.level !== 'tiny'; });
    var tiny = list.filter(function (c) { return c.level === 'tiny'; });

    $('checkHint').textContent = list.length
      ? major.length + ' 条需关注' + (tiny.length ? ' · ' + tiny.length + ' 条尾差' : '')
      : '未发现异常';
    $('checkCard').style.display = list.length ? '' : 'none';

    function rowFor(c) {
      var li = h('li', 'check-item' + (c.level === 'tiny' ? ' is-tiny' : ''));
      var dot = h('span', 'check-dot');
      if (c.level === 'tiny') dot.style.background = 'var(--label-4)';
      li.appendChild(dot);
      li.appendChild(h('span', 'check-ym', c.ym));
      li.appendChild(h('span', 'check-msg', c.message));
      return li;
    }

    major.forEach(function (c) { host.appendChild(rowFor(c)); });

    if (tiny.length) {
      var toggle = h('li', 'check-item check-toggle');
      var btn = h('button', 'chip', showTiny
        ? '收起尾差（' + tiny.length + ' 条）'
        : '展开尾差（' + tiny.length + ' 条，均 ≤ 1 元）');
      btn.type = 'button';
      btn.addEventListener('click', function () { showTiny = !showTiny; renderChecks(); });
      toggle.appendChild(h('span', 'check-dot'));
      toggle.appendChild(btn);
      host.appendChild(toggle);
      if (showTiny) tiny.forEach(function (c) { host.appendChild(rowFor(c)); });
    }
  }

  /* ---------------------------------------------------------- 汇总刷新 */
  function renderAll() {
    monthCache = {};
    renderKpi();
    renderSettlement();
    renderTrend();
    renderDonut();
    renderSplit();
    renderYears();
    renderRank();
    state.shown = PAGE;
    renderDetails();
    renderChecks();

    // 分段控件的指示器在布局后归位
    requestAnimationFrame(function () {
      if ($('yearSeg')._sync) $('yearSeg')._sync();
      if ($('trendSeg')._sync) $('trendSeg')._sync();
    });
  }

  /* ---------------------------------------------------------- 筛选芯片 */
  function buildChips(hostId, values, key) {
    var host = $(hostId);
    host.innerHTML = '';
    values.forEach(function (v) {
      var b = h('button', 'chip', v);
      b.type = 'button';
      b.setAttribute('aria-pressed', String(state[key] === v));
      b.addEventListener('click', function () {
        state[key] = v;
        host.querySelectorAll('.chip').forEach(function (c) {
          c.setAttribute('aria-pressed', String(c.textContent === v));
        });
        state.shown = PAGE;
        renderDetails();
      });
      host.appendChild(b);
    });
  }

  /* ---------------------------------------------------------- 启动 */
  function boot() {
    var meta = DATA.meta;
    $('brandSub').textContent = meta.firstMonth + ' – ' + meta.lastMonth + ' · ' + meta.monthCount + ' 个月';
    $('introSub').textContent =
      '共 ' + meta.detailCount.toLocaleString('zh-CN') + ' 条明细、' + meta.itemCount +
      ' 个消费项目，累计支出 ' + money(meta.grandTotal, 0) + ' 元。数据生成于 ' +
      meta.generatedAt.slice(0, 16).replace('T', ' ') + '。';

    // 顺序：全部 → 2026年 → 2025年 → …（最近的年份在前，越靠左越新）
    var segItems = [{ value: 'all', label: '全部' }].concat(
      DATA.years.map(function (y) { return String(y.year); })
        .sort(function (a, b) { return Number(b) - Number(a); })
        .map(function (v) { return { value: v, label: v + '年' }; })
    );
    // 年份控件：窄屏下放不下时可横滑，两端按滚动位置渐隐提示「还有更多」
    var yearScroll = $('yearScroll');
    var syncEdges = function () { syncScrollEdges(yearScroll); };
    if (yearScroll) {
      yearScroll.addEventListener('scroll', syncEdges, { passive: true });
      window.addEventListener('resize', syncEdges, { passive: true });
      if (typeof ResizeObserver === 'function') new ResizeObserver(syncEdges).observe(yearScroll);
    }

    buildSegmented($('yearSeg'), $('yearPill'), segItems, state.year, function (v) {
      state.year = v;
      $('yearSeg').querySelectorAll('button').forEach(function (b) {
        b.setAttribute('aria-selected', String(b.dataset.value === v));
      });
      if ($('yearSeg')._sync) $('yearSeg')._sync();
      syncEdges();
      renderAll();
    });
    syncEdges();   // 先按当前状态初始化一次，rAF 里再照真实布局校正

    buildSegmented($('trendSeg'), $('trendPill'), [
      { value: 'total', label: '总支出' },
      { value: 'structure', label: '公共 / 零花' },
      { value: 'people', label: '猪 / 婷' }
    ], state.trend, function (v) {
      state.trend = v;
      $('trendSeg').querySelectorAll('button').forEach(function (b) {
        b.setAttribute('aria-selected', String(b.dataset.value === v));
      });
      if ($('trendSeg')._sync) $('trendSeg')._sync();
      renderTrend();
    });

    buildChips('whoChips', ['全部', '猪', '婷'], 'who');
    buildChips('kindChips', ['全部', '公共', '零花'], 'kind');

    var sortHost = $('sortChips');
    [['time', '按时间'], ['amount', '按金额']].forEach(function (pair) {
      var b = h('button', 'chip', pair[1]);
      b.type = 'button';
      b.dataset.sort = pair[0];
      b.setAttribute('aria-pressed', String(state.sort === pair[0]));
      b.addEventListener('click', function () {
        state.sort = pair[0];
        sortHost.querySelectorAll('.chip').forEach(function (c) {
          c.setAttribute('aria-pressed', String(c.dataset.sort === state.sort));
        });
        state.shown = PAGE;
        renderDetails();
      });
      sortHost.appendChild(b);
    });

    var timer = null;
    $('detailSearch').addEventListener('input', function (e) {
      clearTimeout(timer);
      var v = e.target.value;
      timer = setTimeout(function () { state.q = v; state.shown = PAGE; renderDetails(); }, 180);
    });
    $('loadMore').addEventListener('click', function () { state.shown += PAGE; renderDetails(); });

    $('themeBtn').addEventListener('click', function () {
      var next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      document.documentElement.dataset.themePref = next;
      try { localStorage.setItem('piggy-theme', next); } catch (e) { }
      renderAll();
    });
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
      if (document.documentElement.dataset.themePref === 'auto') {
        document.documentElement.dataset.theme =
          window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        renderAll();
      }
    });

    $('footMeta').textContent =
      '数据源：腾讯文档「' + meta.title + '」 · file_id ' + meta.source.fileId +
      ' · 管线生成于 ' + meta.generatedAt.replace('T', ' ').slice(0, 19) +
      '。金额单位：人民币元。源账本只读，看板不写回。';

    renderAll();

    // 入场（弹簧 + 交错）
    var items = document.querySelectorAll('.reveal');
    if (prefersReduced || !('IntersectionObserver' in window)) {
      items.forEach(function (n) { n.classList.add('is-in'); });
    } else {
      items.forEach(function (n, i) { n.style.transitionDelay = Math.min(i * 55, 260) + 'ms'; });
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting) { en.target.classList.add('is-in'); io.unobserve(en.target); }
        });
      }, { rootMargin: '0px 0px -8% 0px', threshold: 0.06 });
      items.forEach(function (n) { io.observe(n); });
    }

    // 年份控件两端渐隐：首帧后再算一次（字体 / 布局就绪）
    requestAnimationFrame(syncEdges);

    // 导航栏：滚动到位才浮现材质
    var nav = $('navbar');
    var onScroll = function () {
      nav.classList.toggle('is-stuck', window.scrollY > 8);
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
