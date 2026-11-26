const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
const clamp = (value, low = 0, high = 1) => Math.min(high, Math.max(low, value));
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

/* ---------- Dot-matrix drawing ---------- */
const GLYPHS = {
  '0': ['01110', '10001', '10011', '10101', '11001', '10001', '01110'], '1': ['00100', '01100', '00100', '00100', '00100', '00100', '01110'],
  '2': ['01110', '10001', '00001', '00010', '00100', '01000', '11111'], '3': ['11110', '00001', '00001', '01110', '00001', '00001', '11110'],
  '4': ['00010', '00110', '01010', '10010', '11111', '00010', '00010'], '5': ['11111', '10000', '11110', '00001', '00001', '10001', '01110'],
  '6': ['00110', '01000', '10000', '11110', '10001', '10001', '01110'], '7': ['11111', '00001', '00010', '00100', '01000', '01000', '01000'],
  '8': ['01110', '10001', '10001', '01110', '10001', '10001', '01110'], '9': ['01110', '10001', '10001', '01111', '00001', '00010', '01100'],
  '+': ['00000', '00100', '00100', '11111', '00100', '00100', '00000'], ' ': ['00000', '00000', '00000', '00000', '00000', '00000', '00000'],
  A: ['01110', '10001', '10001', '11111', '10001', '10001', '10001'], K: ['10001', '10010', '10100', '11000', '10100', '10010', '10001'],
  O: ['01110', '10001', '10001', '10001', '10001', '10001', '01110'],
};
// The Cloudhop mark: a filled dot (where the app runs) hops along an orange arc to an open ring (its new home).
const MARK = ['.....oooo.....', '...oo....oo...', '..o........o..', '.o..........o.', '..............', '.www......www.', 'wwwww....w...w', 'wwwww....w...w', 'wwwww....w...w', '.www......www.'];
// Pixel icons for the pinned device, 15 x 13.
const ICONS = {
  rocket: ['.......o.......', '......ooo......', '.....oo.oo.....', '.....o...o.....', '.....o.c.o.....', '.....o...o.....', '....oo...oo....', '...o.o...o.o...', '...o.ooooo.o...', '...oo.....oo...', '......o.o......', '.....o...o.....', '.......o.......'],
  hop: ['...............', '.....ooooo.....', '...oo.....oo...', '..o.........o..', '.o...........o.', '...............', '.www.......ooo.', 'wwwww.....o...o', 'wwwww.....o...o', 'wwwww.....o...o', '.www.......ooo.', '...............', '...............'],
  pulse: ['...............', '...............', '...............', '......o........', '.....o.o.......', '.....o.o.......', 'ooooo...o...ooo', '........o.o....', '.........o.o...', '.........o..o..', '...............', '...............', '...............'],
  pause: ['...............', '...............', '...ooo...ooo...', '...ooo...ooo...', '...ooo...ooo...', '...ooo...ooo...', '...ooo...ooo...', '...ooo...ooo...', '...ooo...ooo...', '...ooo...ooo...', '...ooo...ooo...', '...............', '...............'],
};
const TERMINAL = ['...............', '.o.............', '..o............', '...o...........', '..o............', '.o.....ooooo...', '...............'];

function paint(target, rows, map) {
  const width = rows[0].length;
  if (target.childElementCount !== width * rows.length) {
    target.style.gridTemplateColumns = `repeat(${width}, var(--dot, 14px))`;
    target.replaceChildren(...Array.from({ length: width * rows.length }, () => document.createElement('i')));
  }
  const dots = target.children;
  rows.forEach((row, y) => [...row].forEach((cell, x) => { dots[y * width + x].className = map(cell) || ''; }));
}
const textRows = (text, width) => {
  const chars = [...text.padStart(width)];
  return Array.from({ length: 9 }, (_, y) => (y === 0 || y === 8 ? '.'.repeat(chars.length * 6 + 1)
    : '.' + chars.map((char) => (GLYPHS[char] ?? GLYPHS[' '])[y - 1]).join('.') + '.'));
};
const ledMap = (lit) => (cell) => (cell === '1' ? lit : 'g');

$$('[data-art="mark"]').forEach((target) => paint(target, MARK, (cell) => ({ w: 'w', o: 'o' })[cell]));
$$('[data-letter]').forEach((target) => paint(target, textRows(target.dataset.letter, 1), ledMap('o')));
$$('[data-terminal]').forEach((target) => {
  let on = true;
  const draw = () => { paint(target, TERMINAL.map((row, y) => (y === 5 && !on ? row.replace(/o/g, '.') : row)), (cell) => (cell === 'o' ? 'o' : 'g')); on = !on; };
  draw();
  if (!reduce) setInterval(draw, 600);
});

// Hero mark assembles dot by dot, then the headline words drop in.
const heroMark = $('.opener .mark-art');
[...heroMark.children].forEach((dot) => { dot.style.transitionDelay = `${Math.round(Math.random() * 700)}ms`; });
setTimeout(() => heroMark.classList.add('lit'), 150);
setTimeout(() => [...heroMark.children].forEach((dot) => { dot.style.transitionDelay = ''; }), 2000);
const headline = $('[data-words]');
const wrapWords = (node) => {
  for (const child of [...node.childNodes]) {
    if (child.nodeType === 3) {
      child.replaceWith(...child.textContent.split(/(\s+)/).filter(Boolean).map((part) => {
        if (!part.trim()) return document.createTextNode(part);
        const span = document.createElement('span');
        span.className = 'word-drop';
        span.textContent = part;
        return span;
      }));
    } else wrapWords(child);
  }
};
wrapWords(headline);
$$('.word-drop', headline).forEach((word, index) => setTimeout(() => word.classList.add('in'), 700 + index * 90));

/* ---------- Counters ---------- */
function countTo(node, target, from = 0, duration = 1400) {
  const prefix = node.dataset.prefix ?? '';
  const suffix = node.dataset.suffix ?? '';
  const start = performance.now();
  const step = (now) => {
    const t = reduce ? 1 : clamp((now - start) / duration);
    node.textContent = `${prefix}${Math.round(from + (target - from) * (1 - Math.pow(1 - t, 3)))}${suffix}`;
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function ledCount(target) {
  const to = Number(target.dataset.counter);
  const from = Number(target.dataset.from ?? 0);
  const suffix = target.dataset.suffix ?? '';
  const width = Math.max(String(to).length, String(from).length) + suffix.length;
  const lit = target.hasAttribute('data-dim') ? 'w' : 'o';
  const show = (value) => {
    paint(target, textRows(`${value}${suffix}`, width), ledMap(lit));
    if (lit === 'w') for (const dot of target.children) dot.style.background = dot.className === 'w' ? '#b9c1bc' : '';
  };
  show(reduce ? to : from);
  return () => {
    const start = performance.now();
    const tick = (now) => {
      const t = reduce ? 1 : clamp((now - start) / 1600);
      show(Math.round(from + (to - from) * (1 - Math.pow(1 - t, 3))));
      if (t < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };
}
const once = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    once.unobserve(entry.target);
    entry.target.dispatchEvent(new Event('enter'));
  }
}, { threshold: .35 });
$$('[data-counter]').forEach((target) => { target.addEventListener('enter', ledCount(target)); once.observe(target); });
$$('.cases [data-count]').forEach((node) => { node.addEventListener('enter', () => countTo(node, Number(node.dataset.count))); once.observe(node); });
$$('[data-gauge]').forEach((path) => {
  const svg = path.closest('svg');
  svg.addEventListener('enter', () => { path.style.strokeDashoffset = String(100 - Number(path.dataset.gauge)); });
  once.observe(svg);
  if (reduce) path.style.strokeDashoffset = String(100 - Number(path.dataset.gauge));
});

/* ---------- Reveal on scroll ---------- */
const revealer = new IntersectionObserver((entries) => {
  for (const entry of entries) if (entry.isIntersecting) { entry.target.classList.add('in'); revealer.unobserve(entry.target); }
}, { threshold: .15 });
$$('.reveal').forEach((node) => revealer.observe(node));

/* ---------- Toasts above the dock ---------- */
const toast = $('[data-toast]');
let toastText = '';
let toastTimer = 0;
function showToast(text) {
  if (text === toastText) return;
  toastText = text;
  toast.classList.remove('show');
  clearTimeout(toastTimer);
  if (text) toastTimer = setTimeout(() => { toast.querySelector('span').textContent = text; toast.classList.add('show'); }, 200);
}

/* ---------- How it works: scroll-driven stage ---------- */
const flow = $('[data-scene="flow"]');
const stops = $$('.rail .stop');
const belt = $('.rail .belt');
const titles = $$('.titles h2');
const views = $$('.screen-wrap .view');
const typedTarget = $('[data-typed]');
const MESSAGE = 'Deploy storefront to my cheapest cloud and keep the Postgres data.';
const TOASTS = ['Request sent', 'Plan ready', 'Approved', 'Live on Hetzner'];
let currentStep = -1;
function enterStep(step) {
  if (step === currentStep) return;
  currentStep = step;
  stops.forEach((stop, index) => { stop.classList.toggle('on', index === step); stop.classList.toggle('done', index < step); });
  titles.forEach((title, index) => title.classList.toggle('on', index === step));
  views.forEach((view, index) => view.classList.toggle('on', index === step));
  const width = stops[0].offsetWidth;
  belt.style.transform = `translateX(${-(step + .5) * width}px)`;
  belt.style.setProperty('--fill', `${(step + .5) * width}px`);
  const view = views[step];
  $$('[data-count]', view).forEach((node) => countTo(node, Number(node.dataset.count)));
  $$('[data-w]', view).forEach((bar) => { bar.style.width = '0'; setTimeout(() => { bar.style.width = `${bar.dataset.w}%`; }, 60); });
}
function progress(section) {
  const box = section.getBoundingClientRect();
  return clamp(-box.top / Math.max(1, box.height - innerHeight));
}

/* ---------- Providers drifting up ---------- */
const PROVIDERS = [['AWS', 'cloud'], ['Vercel', 'platform'], ['Google Cloud', 'cloud'], ['Netlify', 'platform'], ['DigitalOcean', 'cloud'], ['Cloudflare Pages', 'platform'],
  ['Hetzner', 'cloud'], ['Render', 'platform'], ['Hostinger', 'your VPS'], ['Koyeb', 'platform'], ['GitHub', 'code'], ['Railway', 'platform'], ['GitLab', 'code'], ['Heroku', 'platform'],
  ['Bitbucket', 'code'], ['Fly.io', 'platform'], ['Azure DevOps', 'code'], ['Your server', 'SSH']];
const drift = $('[data-drift]');
for (const pass of [0, 1]) for (const [name, kind] of PROVIDERS) {
  const item = document.createElement('div');
  item.className = 'name';
  item.setAttribute('role', pass ? 'presentation' : 'listitem');
  item.textContent = name;
  const small = document.createElement('small');
  small.textContent = kind;
  item.append(small);
  if (pass) item.setAttribute('aria-hidden', 'true');
  drift.append(item);
}

/* ---------- Services: pinned device follows the item in view ---------- */
const iconScreen = $('[data-icon-screen]');
const iconLabel = $('[data-icon-label]');
const items = $$('.svc .item');
const LABELS = { rocket: 'CH-01 deploy', hop: 'CH-02 move', pulse: 'CH-03 watch', pause: 'CH-04 pause' };
let currentIcon = '';
function showIcon(name) {
  if (name === currentIcon) return;
  currentIcon = name;
  // Scramble for a moment, then settle on the new icon.
  if (iconScreen.childElementCount && !reduce) for (const dot of iconScreen.children) if (Math.random() < .45) dot.className = Math.random() < .5 ? 'o' : 'g';
  setTimeout(() => paint(iconScreen, ICONS[name], (cell) => ({ o: 'o', w: 'w', c: 'c' })[cell] || 'g'), reduce ? 0 : 170);
  iconLabel.textContent = LABELS[name];
}
showIcon('rocket');
items[0].classList.add('on');
const itemObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) if (entry.isIntersecting) {
    items.forEach((item) => item.classList.toggle('on', item === entry.target));
    showIcon(entry.target.dataset.icon);
  }
}, { rootMargin: '-45% 0px -45% 0px' });
items.forEach((item) => itemObserver.observe(item));

/* ---------- Canvas: halftone edges ---------- */
const edges = $$('[data-edge]');
function sizeCanvas(canvas) {
  const ratio = Math.min(devicePixelRatio || 1, 2);
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return [ctx, width, height];
}
function drawEdge(canvas, time) {
  const [ctx, width, height] = sizeCanvas(canvas);
  // "down": dark above pours into light below. "up": light above, dark rising from below.
  const down = canvas.dataset.edge === 'down';
  ctx.fillStyle = '#f6f8f7';
  ctx.fillRect(0, 0, width, height);
  const gap = 8;
  for (let x = gap / 2; x < width; x += gap) {
    // The edge moves like liquid: a few slow waves added together.
    const wave = clamp(.5 + .16 * Math.sin(x / 140 + time / 1400) + .08 * Math.sin(x / 47 - time / 900) + .05 * Math.sin(x / 19 + time / 600), .12, .9);
    const edgeY = wave * height;
    for (let y = gap / 2; y < height; y += gap) {
      const from = down ? y : height - y;
      const depth = (edgeY - from) / edgeY;
      if (depth < -.07) continue;
      const near = depth < .05;
      const radius = near ? 1.5 : Math.min(gap / 2 + .4, (depth * 1.3 + .12) * (gap / 2 + .4));
      ctx.fillStyle = near ? (Math.sin(x * 12.9898 + y * 78.233) > .1 ? '#ff5a1f' : '#9aa59f') : '#0d1012';
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  // Solid dark band where the edge meets the dark section, so there is no seam.
  ctx.fillStyle = '#0d1012';
  ctx.fillRect(0, down ? 0 : height - 6, width, 6);
}

/* ---------- Canvas: the tunnel ---------- */
const tunnelSection = $('[data-scene="tunnel"]');
const tunnel = $('[data-tunnel]');
const says = $$('.tunnel .say p');
function drawTunnel(p, time) {
  const [ctx, width, height] = sizeCanvas(tunnel);
  // The room brightens to pale teal in the middle of the scene, then goes dark again for the ending.
  const dawn = ease(clamp((p - .3) / .3)) * (1 - ease(clamp((p - .86) / .12)));
  ctx.fillStyle = `rgb(${Math.round(108 * dawn)}, ${Math.round(150 * dawn)}, ${Math.round(152 * dawn)})`;
  ctx.fillRect(0, 0, width, height);
  const cx = width / 2;
  const cy = height / 2;
  const travel = p * 8 + time / 3000;
  // Dots on the four walls of a long box in perspective, flying past as you scroll.
  for (let ring = 0; ring < 44; ring++) {
    const z = ((ring + 1 - (travel % 1)) / 44) * 9 + .25;
    const scale = 1 / z;
    const halfW = width * .6 * scale;
    const halfH = height * .6 * scale;
    const alpha = clamp(1.2 - z / 9) * .9;
    const size = clamp(scale * 1.8, .5, 3);
    ctx.fillStyle = `rgba(${Math.round(150 + 80 * dawn)}, ${Math.round(215 + 32 * dawn)}, ${Math.round(205 + 40 * dawn)}, ${alpha})`;
    const count = 56;
    for (let i = 0; i <= count; i++) {
      const t = i / count;
      const points = [[cx - halfW + t * 2 * halfW, cy - halfH], [cx - halfW + t * 2 * halfW, cy + halfH], [cx - halfW, cy - halfH + t * 2 * halfH], [cx + halfW, cy - halfH + t * 2 * halfH]];
      for (const [x, y] of points) ctx.fillRect(x - size / 2, y - size / 2, size, size);
    }
  }
  // The glowing frame recedes into the distance as you scroll.
  if (p < .9) {
    const frameScale = 1 / (.75 + p * 8);
    const fw = Math.min(width, height) * 1.05 * frameScale;
    ctx.save();
    ctx.shadowColor = 'rgba(255, 90, 31, .95)';
    ctx.shadowBlur = 34 * frameScale + 8;
    ctx.strokeStyle = `rgba(255, 150, 90, ${clamp(1.1 - p * 1.2)})`;
    ctx.lineWidth = Math.max(1.5, 6 * frameScale);
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(cx - fw / 2, cy - fw / 2, fw, fw, fw * .12); else ctx.rect(cx - fw / 2, cy - fw / 2, fw, fw);
    ctx.stroke();
    ctx.restore();
  }
  // The light at the end.
  const light = 5 + p * 30 + Math.sin(time / 500) * 1.5;
  const reach = light * (4 + dawn * 5);
  const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, reach);
  glow.addColorStop(0, 'rgba(255, 255, 255, .97)');
  glow.addColorStop(.3 + dawn * .2, `rgba(${dawn ? '235, 255, 252' : '160, 255, 240'}, ${.32 + dawn * .4})`);
  glow.addColorStop(1, 'rgba(0, 0, 0, 0)');
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.arc(cx, cy, reach, 0, Math.PI * 2);
  ctx.fill();
  return dawn;
}

/* ---------- Canvas: the opening scene ---------- */
const opener = $('[data-scene="opener"]');
const openerCanvas = $('[data-opener]');
const intro = $('.opener .intro');
const sayLines = $$('[data-say] p');
const ease = (t) => t * t * (3 - 2 * t);
// A point at fraction t along a rounded rectangle, clockwise from the top-left corner's end.
function aroundRoundRect(t, x, y, w, h, r) {
  const straightW = w - 2 * r;
  const straightH = h - 2 * r;
  const arc = Math.PI * r / 2;
  const total = 2 * straightW + 2 * straightH + 4 * arc;
  let d = t * total;
  const corner = (cx, cy, from) => { const a = from + (d / arc) * Math.PI / 2; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; };
  if (d < straightW) return [x + r + d, y]; d -= straightW;
  if (d < arc) return corner(x + w - r, y + r, -Math.PI / 2); d -= arc;
  if (d < straightH) return [x + w, y + r + d]; d -= straightH;
  if (d < arc) return corner(x + w - r, y + h - r, 0); d -= arc;
  if (d < straightW) return [x + w - r - d, y + h]; d -= straightW;
  if (d < arc) return corner(x + r, y + h - r, Math.PI / 2); d -= arc;
  if (d < straightH) return [x, y + h - r - d]; d -= straightH;
  return corner(x + r, y + r, Math.PI);
}
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, h, r); else ctx.rect(x, y, w, h);
}
// The room behind the square: dots on the four walls of a box in perspective, nested frames, and a teal light.
function drawRoom(ctx, width, height, travel, time, alpha) {
  const cx = width / 2;
  const cy = height / 2;
  ctx.fillStyle = '#020506';
  ctx.fillRect(0, 0, width, height);
  for (let ring = 0; ring < 36; ring++) {
    const z = ((ring + 1 - (travel % 1)) / 36) * 8 + .3;
    const scale = 1 / z;
    const halfW = width * .62 * scale;
    const halfH = height * .62 * scale;
    const a = clamp((1.2 - z / 8) * alpha);
    const size = clamp(scale * 2.2, .9, 3.2);
    ctx.fillStyle = `rgba(110, 231, 216, ${a})`;
    const count = 64;
    for (let i = 0; i <= count; i++) {
      const t = i / count;
      ctx.fillRect(cx - halfW + t * 2 * halfW - size / 2, cy - halfH - size / 2, size, size);
      ctx.fillRect(cx - halfW + t * 2 * halfW - size / 2, cy + halfH - size / 2, size, size);
      ctx.fillRect(cx - halfW - size / 2, cy - halfH + t * 2 * halfH - size / 2, size, size);
      ctx.fillRect(cx + halfW - size / 2, cy - halfH + t * 2 * halfH - size / 2, size, size);
    }
  }
  // Thin frames recede toward the light, as the walls do.
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i++) {
    const z = (((i + 1 - ((travel * .5) % 1)) / 4) * 4) + .6;
    const s = 1 / z;
    ctx.strokeStyle = `rgba(110, 231, 216, ${clamp(1 - z / 4.6) * .35 * alpha})`;
    ctx.strokeRect(cx - width * .42 * s, cy - height * .42 * s, width * .84 * s, height * .84 * s);
  }
  const light = 4 + Math.sin(time / 600) * 1.2;
  const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, light * 9);
  glow.addColorStop(0, `rgba(235, 255, 252, ${.95 * alpha})`);
  glow.addColorStop(.22, `rgba(110, 231, 216, ${.35 * alpha})`);
  glow.addColorStop(1, 'rgba(0, 0, 0, 0)');
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.arc(cx, cy, light * 9, 0, Math.PI * 2);
  ctx.fill();
}
function playOpener(p, time) {
  const [ctx, width, height] = sizeCanvas(openerCanvas);
  const cx = width / 2;
  const cy = height / 2;
  const gap = width < 600 ? 10 : 12;
  // Phases, as fractions of the pinned scroll.
  const beam = 1 - ease(clamp((p - .03) / .11));
  const trace = ease(clamp((p - .1) / .12));
  const grow = clamp((p - .24) / .2);
  const inside = clamp((p - .3) / .1);
  const side = Math.min(width, height) * .44 * (1 + Math.pow(grow, 2.3) * 6);
  const sx = cx - side / 2;
  const sy = cy - side / 2;
  const radius = side * .17;
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, width, height);
  // 1. The LED field. A beam from the top lights a column of dots, white-hot at its core.
  for (let y = gap / 2; y < height; y += gap) {
    for (let x = gap / 2; x < width; x += gap) {
      if (inside > 0 && x > sx + 4 && x < sx + side - 4 && y > sy + 4 && y < sy + side - 4) continue;
      const dx = (x - cx) / (width * .04);
      const v = beam * Math.exp(-dx * dx) * clamp(1.05 - y / (height * .6)) * (.82 + .18 * Math.sin(time / 160 + y * .21 + x));
      const halo = beam * Math.exp(-dx * dx / 16) * clamp(.8 - y / (height * .55)) * .35;
      if (v > .6) ctx.fillStyle = `rgba(255, ${Math.round(150 + 105 * (v - .6) / .4)}, ${Math.round(90 + 160 * (v - .6) / .4)}, 1)`;
      else if (v + halo > .04) ctx.fillStyle = `rgba(255, 90, 31, ${clamp(v + halo)})`;
      else ctx.fillStyle = 'rgba(238, 240, 236, .075)';
      const size = v > .3 ? 3 : 2.2;
      ctx.fillRect(x - size / 2, y - size / 2, size, size);
    }
  }
  if (beam > 0) {
    const bloom = ctx.createRadialGradient(cx, 0, 0, cx, 0, height * .7);
    bloom.addColorStop(0, `rgba(255, 120, 60, ${.32 * beam})`);
    bloom.addColorStop(1, 'rgba(0, 0, 0, 0)');
    ctx.fillStyle = bloom;
    ctx.fillRect(0, 0, width, height);
  }
  // 2. Inside the square: the room, clipped to the rounded square as it grows past the screen.
  if (inside > 0) {
    ctx.save();
    roundRect(ctx, sx, sy, side, side, radius);
    ctx.clip();
    drawRoom(ctx, width, height, (p - .3) * 9 + time / 5000, time, inside);
    ctx.restore();
  }
  // 3. The square traces itself in glowing dots, then stays lit as it grows.
  if (trace > 0) {
    const fade = 1 - clamp((p - .4) / .06);
    if (fade > 0) {
      const perimeter = 4 * (side - 2 * radius) + 2 * Math.PI * radius;
      // A neon band: a wide orange glow along the traced part, then three rows of LED dots, hottest in the middle.
      ctx.save();
      ctx.setLineDash([perimeter * trace, perimeter]);
      ctx.shadowColor = 'rgba(255, 90, 31, 1)';
      ctx.shadowBlur = 40;
      ctx.strokeStyle = `rgba(255, 90, 31, ${.5 * fade})`;
      ctx.lineWidth = 16;
      roundRect(ctx, sx - 2, sy - 2, side + 4, side + 4, radius + 2);
      ctx.stroke();
      ctx.restore();
      for (const [inset, color] of [[-6, '255, 120, 60'], [0, '255, 225, 200'], [6, '255, 150, 95']]) {
        const rowSide = side + inset * -2;
        const rowRadius = Math.max(2, radius - inset);
        const steps = Math.floor((4 * (rowSide - 2 * rowRadius) + 2 * Math.PI * rowRadius) / 6);
        for (let i = 0; i < steps * trace; i++) {
          const [x, y] = aroundRoundRect(i / steps, sx + inset, sy + inset, rowSide, rowSide, rowRadius);
          if (x < -10 || y < -10 || x > width + 10 || y > height + 10) continue;
          const head = trace < 1 && i > steps * trace - 14;
          ctx.fillStyle = head ? `rgba(255, 245, 235, ${fade})` : `rgba(${color}, ${fade})`;
          ctx.fillRect(x - 1.5, y - 1.5, 3, 3);
        }
      }
    }
  }
  // 4. The end of the scene darkens to the page's ink, so the poured edge below meets it without a seam.
  const out = clamp((p - .94) / .06);
  if (out > 0) { ctx.fillStyle = `rgba(13, 16, 18, ${out})`; ctx.fillRect(0, 0, width, height); }
  // Headline leaves as the beam goes out.
  const away = clamp((p - .02) / .09);
  intro.style.opacity = String(1 - away);
  intro.style.transform = `translateY(${-away * 60}px) scale(${1 - away * .05})`;
  intro.style.visibility = away >= 1 ? 'hidden' : '';
  // The manifesto plays inside the room, one line at a time, the next one waiting below.
  const t = (p - .46) / .48;
  const current = t < 0 ? -1 : Math.min(sayLines.length - 1, Math.floor(t * sayLines.length));
  sayLines.forEach((line, index) => {
    line.classList.toggle('on', index === current && p < .97);
    line.classList.toggle('next', index === current + 1 && current >= 0);
    line.classList.toggle('gone', index < current || (index === current && p >= .97));
  });
}

/* ---------- Main loop ---------- */
const topbar = $('#topbar');
const dock = $('#dock');
const tilts = $$('[data-tilt]');
const lightZones = [...$$('section.light'), ...edges];
const partners = $('[data-scene="partners"]');
let lastY = scrollY;
function frame(time) {
  const y = scrollY;
  const vh = innerHeight;
  // Top bar: hidden at the very top and while scrolling down; back when scrolling up.
  if (y < 80) topbar.classList.add('away');
  else if (y > lastY + 2) topbar.classList.add('away');
  else if (y < lastY - 2) topbar.classList.remove('away');
  // The pinned step rail sits where the top bar would, so the bar stays away there.
  { const box = flow.getBoundingClientRect(); if (box.top <= 0 && box.bottom >= innerHeight) topbar.classList.add('away'); }
  topbar.classList.toggle('on-light', lightZones.some((zone) => { const box = zone.getBoundingClientRect(); return box.top < 30 && box.bottom > 30 && zone.tagName !== 'CANVAS'; }));
  lastY = y;
  // Opening scene
  const openerBox = opener.getBoundingClientRect();
  if (openerBox.bottom > 0) playOpener(reduce ? 0 : progress(opener), reduce ? 0 : time);
  // How it works
  const flowBox = flow.getBoundingClientRect();
  const p = progress(flow);
  enterStep(Math.min(3, Math.floor(p * 4)));
  const inFlow = flowBox.top < vh * .4 && flowBox.bottom > vh * .6;
  if (currentStep === 0) typedTarget.textContent = MESSAGE.slice(0, Math.round(MESSAGE.length * (reduce ? 1 : clamp(p * 4 / .75))));
  const typing = currentStep === 0 && typedTarget.textContent.length < MESSAGE.length;
  showToast(inFlow && !typing ? TOASTS[currentStep] : '');
  // Providers drift up with scroll and slowly on their own.
  const half = drift.scrollHeight / 2;
  if (half) drift.style.transform = `translateY(${-((progress(partners) * half * 1.2 + (reduce ? 0 : time / 70)) % half)}px)`;
  // Devices tilt a little as they pass.
  for (const device of tilts) { const box = device.getBoundingClientRect(); const t = clamp((box.top + box.height / 2) / vh); device.style.setProperty('--rx', `${(t - .5) * 22}deg`); }
  // Canvases draw only while on screen.
  for (const canvas of edges) { const box = canvas.getBoundingClientRect(); if (box.bottom > 0 && box.top < vh) drawEdge(canvas, reduce ? 0 : time); }
  const tunnelBox = tunnelSection.getBoundingClientRect();
  const tp = progress(tunnelSection);
  // On the pale room the words turn dark, so they stay readable.
  if (tunnelBox.bottom > 0 && tunnelBox.top < vh) tunnelSection.classList.toggle('dawn', drawTunnel(tp, reduce ? 0 : time) > .5);
  says[0].classList.toggle('on', tp > .42 && tp < .7);
  says[1].classList.toggle('on', tp >= .7);
  // The dock opens into the full menu for the ending.
  dock.classList.toggle('open', tunnelBox.top < vh * .6);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
