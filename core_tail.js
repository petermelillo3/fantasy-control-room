// Best possible lineup from starters + bench (fill the most restrictive slots first).
function optimal(week) {
  if (!week.b) return null;
  const pool = [];
  week.s.forEach(x => pool.push({ name: x[1], pos: x[2], pts: x[3] || 0 }));
  week.b.forEach(x => pool.push({ name: x[0], pos: x[1], pts: x[2] || 0 }));
  const order = ['QB','RB','WR','TE','K','DEF','WRT','WRTQ'];
  const slots = week.s.map(x => x[0]);
  let total = 0;
  const used = new Set();
  const picks = [];
  order.forEach(sl => {
    slots.filter(s => s === sl).forEach(() => {
      let best = null;
      pool.forEach((p, i) => {
        if (used.has(i) || !SLOT_ELIG[sl].includes(p.pos)) return;
        if (!best || p.pts > best.p.pts) best = { p, i };
      });
      if (best) { used.add(best.i); total += best.p.pts; picks.push([sl, best.p.name, best.p.pts]); }
    });
  });
  return { total: Math.round(total * 100) / 100, picks };
}

function startTotal(week) {
  return Math.round(week.s.reduce((a, x) => a + (x[3] || 0), 0) * 100) / 100;
}

function weekSummary(team, w) {
  const wk = team.weeks[w];
  if (!wk) return null;
  const start = startTotal(wk);
  const opt = optimal(wk);
  return { start, opt: opt ? opt.total : null, leak: opt ? Math.round((opt.total - start) * 100) / 100 : null, picks: opt ? opt.picks : null };
}

if (typeof module !== 'undefined') module.exports = { TEAMS, optimal, startTotal, weekSummary, SLOT_ELIG };
