// Small SVG charts: line with crosshair tooltip, status columns, sparkline, and a table view for each.
// Values are numbers from our own API; any text is set with textContent, never HTML.
const Charts = (() => {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = (tag, attrs = {}, ...children) => {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    node.append(...children);
    return node;
  };
  const label = (x, y, value, attrs = {}) => svg('text', { x, y, class: 'axis-text', ...attrs }, document.createTextNode(value));

  function niceMax(value) {
    if (!value || value <= 0) return 1;
    const power = 10 ** Math.floor(Math.log10(value));
    const step = value / power;
    return (step <= 1 ? 1 : step <= 2 ? 2 : step <= 5 ? 5 : 10) * power;
  }

  // Redraws at the container's real width, so text is never stretched.
  function responsive(draw) {
    const holder = document.createElement('div');
    holder.className = 'chart';
    let lastWidth = 0;
    new ResizeObserver(() => {
      const width = Math.floor(holder.clientWidth);
      if (width && width !== lastWidth) {
        lastWidth = width;
        holder.replaceChildren(...draw(width, holder));
      }
    }).observe(holder);
    return holder;
  }

  function tooltip(holder) {
    const box = document.createElement('div');
    box.className = 'tooltip';
    box.hidden = true;
    const show = (x, y, value, detail) => {
      box.replaceChildren(Object.assign(document.createElement('strong'), { textContent: value }), Object.assign(document.createElement('span'), { className: 'muted', textContent: detail }));
      box.hidden = false;
      const left = Math.min(Math.max(x - box.offsetWidth / 2, 0), holder.clientWidth - box.offsetWidth);
      box.style.left = `${left}px`;
      box.style.top = `${Math.max(y - box.offsetHeight - 12, 0)}px`;
    };
    return { box, show, hide: () => { box.hidden = true; } };
  }

  // Keyboard and pointer both move an index along the points.
  function navigable(target, count, select, clear, xToIndex) {
    let index = -1;
    target.setAttribute('tabindex', '0');
    target.addEventListener('pointermove', (event) => { index = xToIndex(event); select(index); });
    target.addEventListener('pointerleave', clear);
    target.addEventListener('blur', clear);
    target.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
      event.preventDefault();
      index = index < 0 ? count - 1 : Math.max(0, Math.min(count - 1, index + (event.key === 'ArrowRight' ? 1 : -1)));
      select(index);
    });
  }

  function timeTicks(from, to, width, formatTime, x) {
    const count = Math.max(2, Math.min(6, Math.floor(width / 110)));
    return Array.from({ length: count }, (_, index) => {
      const t = from + ((to - from) * index) / (count - 1);
      return label(x(t), 0, formatTime(t), { 'text-anchor': index === 0 ? 'start' : index === count - 1 ? 'end' : 'middle' });
    });
  }

  function line({ points, from, to, format, formatTime, height = 220, ariaLabel }) {
    return responsive((width, holder) => {
      const pad = { left: 46, right: 56, top: 14, bottom: 26 };
      const innerW = width - pad.left - pad.right;
      const innerH = height - pad.top - pad.bottom;
      const valid = points.filter((point) => point.v !== null);
      const max = niceMax(Math.max(...valid.map((point) => point.v), 0) * 1.1);
      const x = (t) => pad.left + ((t - from) / (to - from)) * innerW;
      const y = (v) => pad.top + innerH - (v / max) * innerH;
      const chart = svg('svg', { width, height, role: 'img', 'aria-label': ariaLabel });

      for (let tick = 0; tick <= 4; tick++) {
        const value = (max * tick) / 4;
        chart.append(svg('line', { x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value), class: 'gridline' }),
          label(pad.left - 8, y(value) + 4, format(value), { 'text-anchor': 'end' }));
      }
      for (const tick of timeTicks(from, to, innerW, formatTime, x)) {
        tick.setAttribute('y', height - 6);
        chart.append(tick);
      }

      // Break the line where there is no data rather than drawing through the gap.
      const segments = [];
      let current = [];
      for (const point of points) {
        if (point.v === null) {
          if (current.length) segments.push(current);
          current = [];
        } else current.push(point);
      }
      if (current.length) segments.push(current);
      for (const segment of segments) {
        const path = segment.map((point, index) => `${index ? 'L' : 'M'}${x(point.t).toFixed(1)},${y(point.v).toFixed(1)}`).join('');
        chart.append(svg('path', { d: `${path}L${x(segment.at(-1).t).toFixed(1)},${y(0)}L${x(segment[0].t).toFixed(1)},${y(0)}Z`, class: 'area' }));
        chart.append(svg('path', { d: segment.length === 1 ? `${path}h0.1` : path, class: 'line' }));
      }
      const last = valid.at(-1);
      if (last) {
        chart.append(svg('circle', { cx: x(last.t), cy: y(last.v), r: 4, class: 'end-dot' }),
          svg('text', { x: x(last.t) + 8, y: y(last.v) + 4, class: 'end-label' }, document.createTextNode(format(last.v))));
      }

      const holderTip = tooltip(holder);
      const hair = svg('line', { y1: pad.top, y2: pad.top + innerH, class: 'hair', visibility: 'hidden' });
      const dot = svg('circle', { r: 4, class: 'end-dot', visibility: 'hidden' });
      const overlay = svg('rect', { x: pad.left, y: pad.top, width: innerW, height: innerH, class: 'overlay', 'aria-label': `${ariaLabel}. Use arrow keys to read values.` });
      chart.append(hair, dot, overlay);
      const clear = () => { hair.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); holderTip.hide(); };
      const select = (index) => {
        const point = valid[index];
        if (!point) return clear();
        hair.setAttribute('x1', x(point.t)); hair.setAttribute('x2', x(point.t)); hair.setAttribute('visibility', 'visible');
        dot.setAttribute('cx', x(point.t)); dot.setAttribute('cy', y(point.v)); dot.setAttribute('visibility', 'visible');
        holderTip.show(x(point.t), y(point.v), format(point.v), formatTime(point.t, true));
      };
      navigable(overlay, valid.length, select, clear, (event) => {
        const box = chart.getBoundingClientRect();
        const t = from + ((event.clientX - box.left - pad.left) / innerW) * (to - from);
        let best = 0;
        valid.forEach((point, index) => { if (Math.abs(point.t - t) < Math.abs(valid[best].t - t)) best = index; });
        return best;
      });
      return [chart, holderTip.box];
    });
  }

  // One column per time slot; color carries status and the tooltip carries the number.
  function columns({ bars, formatTime, tone, describe, height = 150, ariaLabel }) {
    return responsive((width, holder) => {
      const pad = { left: 46, right: 8, top: 10, bottom: 26 };
      const innerW = width - pad.left - pad.right;
      const innerH = height - pad.top - pad.bottom;
      const slot = innerW / bars.length;
      const barWidth = Math.max(2, Math.min(24, slot - 2));
      const y = (v) => pad.top + innerH - (v / 100) * innerH;
      const chart = svg('svg', { width, height, role: 'img', 'aria-label': ariaLabel });
      for (const value of [0, 50, 100]) {
        chart.append(svg('line', { x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value), class: 'gridline' }),
          label(pad.left - 8, y(value) + 4, `${value}%`, { 'text-anchor': 'end' }));
      }
      bars.forEach((bar, index) => {
        const left = pad.left + index * slot + (slot - barWidth) / 2;
        const value = bar.v === null ? 100 : Math.max(bar.v, 3);
        const top = y(value);
        const radius = Math.min(4, barWidth / 2, y(0) - top);
        // Rounded data end, square at the baseline.
        chart.append(svg('path', {
          d: `M${left},${y(0)}V${top + radius}Q${left},${top} ${left + radius},${top}H${left + barWidth - radius}Q${left + barWidth},${top} ${left + barWidth},${top + radius}V${y(0)}Z`,
          class: `bar-${tone(bar)}`,
        }));
      });
      const ticks = Math.max(2, Math.min(6, Math.floor(innerW / 110)));
      for (let tick = 0; tick < ticks; tick++) {
        const index = Math.round(((bars.length - 1) * tick) / (ticks - 1));
        chart.append(label(pad.left + index * slot + slot / 2, height - 6, formatTime(bars[index].t), { 'text-anchor': tick === 0 ? 'start' : tick === ticks - 1 ? 'end' : 'middle' }));
      }
      const tip = tooltip(holder);
      const highlight = svg('rect', { y: pad.top, height: innerH, width: slot, fill: 'currentColor', opacity: '0.06', visibility: 'hidden' });
      const overlay = svg('rect', { x: pad.left, y: pad.top, width: innerW, height: innerH, class: 'overlay', 'aria-label': `${ariaLabel}. Use arrow keys to read values.` });
      chart.insertBefore(highlight, chart.firstChild);
      chart.append(overlay);
      const clear = () => { highlight.setAttribute('visibility', 'hidden'); tip.hide(); };
      navigable(overlay, bars.length, (index) => {
        const bar = bars[index];
        if (!bar) return clear();
        highlight.setAttribute('x', pad.left + index * slot);
        highlight.setAttribute('visibility', 'visible');
        const [value, detail] = describe(bar);
        tip.show(pad.left + index * slot + slot / 2, pad.top, value, detail);
      }, clear, (event) => Math.max(0, Math.min(bars.length - 1, Math.floor((event.clientX - chart.getBoundingClientRect().left - pad.left) / slot))));
      return [chart, tip.box];
    });
  }

  function sparkline(values) {
    const valid = values.filter((value) => value !== null);
    const chart = svg('svg', { class: 'spark', viewBox: '0 0 100 34', preserveAspectRatio: 'none', 'aria-hidden': 'true' });
    if (valid.length < 2) return chart;
    const max = Math.max(...valid) * 1.15 || 1;
    const step = 100 / (values.length - 1);
    const points = values.map((value, index) => (value === null ? null : [index * step, 32 - (value / max) * 30])).filter(Boolean);
    const path = points.map(([px, py], index) => `${index ? 'L' : 'M'}${px.toFixed(1)},${py.toFixed(1)}`).join('');
    chart.append(svg('path', { d: `${path}L${points.at(-1)[0]},34L${points[0][0]},34Z`, class: 'wash' }), svg('path', { d: path }));
    return chart;
  }

  // Every chart offers its numbers as a table too, so nothing depends on hover or color.
  function dataTable(headers, rows) {
    const cell = (tag, value) => Object.assign(document.createElement(tag), { textContent: value });
    const table = document.createElement('table');
    const head = table.createTHead().insertRow();
    headers.forEach((header) => head.append(cell('th', header)));
    const body = table.createTBody();
    rows.forEach((row) => { const tr = body.insertRow(); row.forEach((value) => tr.append(cell('td', value))); });
    const details = document.createElement('details');
    details.className = 'data-table';
    const wrap = Object.assign(document.createElement('div'), { className: 'table-wrap' });
    wrap.append(table);
    details.append(cell('summary', 'View as table'), wrap);
    return details;
  }

  return { line, columns, sparkline, dataTable };
})();
