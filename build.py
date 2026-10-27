#!/usr/bin/env python3
"""Fantasy Control Room: pull fresh data, rebuild index.html.

Sources (all public, no logins):
  - Sleeper API: rosters, weekly lineups and points, transaction log
  - nflverse: NFL player game stats
  - ESPN public scoreboard data: NFL schedule, college football box scores
The Eddie Pineiro (ESPN) team is read from data/eddie.json and edited by hand.

If one source fails, the last good copy in data/cache_*.json is used so the page still builds.
"""
import datetime as dt
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
ET = ZoneInfo('America/New_York')
SESSION = requests.Session()
SESSION.headers['User-Agent'] = 'fantasy-control-room'


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, tries=3):
    err = None
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=60)
            if r.status_code == 200:
                return r
            err = r.status_code
        except Exception as e:  # network hiccup
            err = e
        time.sleep(2 * (i + 1))
    raise RuntimeError('GET failed %s: %s' % (url, err))


def gj(url):
    return get(url).json()


def cache_path(name):
    return os.path.join(DATA, 'cache_%s.json' % name)


def with_cache(name, fn):
    """Run fn; save the result. On failure fall back to the saved copy."""
    try:
        out = fn()
        with open(cache_path(name), 'w') as f:
            json.dump(out, f, separators=(',', ':'))
        return out
    except Exception as e:
        log('WARNING: %s failed (%s); using cached copy' % (name, e))
        try:
            with open(cache_path(name)) as f:
                return json.load(f)
        except Exception:
            return None


# ----------------------------------------------------------------- Sleeper
SL = 'https://api.sleeper.app/v1/'
SLP = 'https://api.sleeper.com/'
USER = 'pmelillo3'
LEAGUES = [
    ('redraft', 'Melillo Redraft', 'Redraft', 'Redraft', '1385446563223973888'),
    ('mdyn', 'Melillo Dynasty', 'Dynasty PPR', 'Dynasty', '1312897424762626048'),
    ('dphi', 'DPhi Dynasty', 'Dynasty SF PPR', 'DPhi', '1365815149679624192'),
]
SLOTMAP = {'FLEX': 'WRT', 'SUPER_FLEX': 'WRTQ'}
SUFFIXES = {'jr', 'sr', 'ii', 'iii', 'iv', 'v'}
ELIG = {
    'QB': ['QB'], 'RB': ['RB'], 'WR': ['WR'], 'TE': ['TE'],
    'WRT': ['RB', 'WR', 'TE'], 'WRTQ': ['QB', 'RB', 'WR', 'TE'],
    'K': ['K'], 'DEF': ['DEF'],
}


def name_key(first, last, pos):
    last_tokens = [t for t in re.sub(r"[.'’]", '', last or '').split() if t.lower() not in SUFFIXES]
    return '%s %s|%s' % ((first or '?')[:1].lower(), (last_tokens[-1] if last_tokens else '').lower(), pos)


class Players:
    def __init__(self):
        self.p = gj(SL + 'players/nfl')

    def info(self, pid):
        """(display name, position) for a Sleeper player id."""
        p = self.p.get(pid)
        if pid.isalpha() and (p is None or p.get('position') == 'DEF'):
            return '%s D/ST' % pid, 'DEF'
        if not p:
            return 'Player %s' % pid, '?'
        first = (p.get('first_name') or '?')[:1]
        return '%s. %s' % (first, p.get('last_name') or ''), p.get('position') or '?'

    def key(self, pid):
        p = self.p.get(pid) or {}
        return name_key(p.get('first_name'), p.get('last_name'), p.get('position'))


def ordinal(n):
    n = int(n)
    return '%d%s' % (n, 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th'))


def fmt_date(ms):
    d = dt.datetime.fromtimestamp(ms / 1000, ET)
    return d.strftime('%b ') + str(d.day)


def optimal(week):
    pool = [{'name': x[1], 'pos': x[2], 'pts': x[3] or 0} for x in week['s']]
    pool += [{'name': x[0], 'pos': x[1], 'pts': x[2] or 0} for x in week.get('b') or []]
    slots = [x[0] for x in week['s']]
    used, picks, total = set(), [], 0.0
    for sl in ['QB', 'RB', 'WR', 'TE', 'K', 'DEF', 'WRT', 'WRTQ']:
        for _ in [s for s in slots if s == sl]:
            best = None
            for i, p in enumerate(pool):
                if i in used or p['pos'] not in ELIG[sl]:
                    continue
                if best is None or p['pts'] > pool[best]['pts']:
                    best = i
            if best is not None:
                used.add(best)
                total += pool[best]['pts']
                picks.append((sl, pool[best]['name'], pool[best]['pts']))
    return round(total, 2), picks


def sleeper_team(tid, name, typ, label, lid, players, state):
    league = gj(SL + 'league/%s' % lid)
    users = gj(SL + 'league/%s/users' % lid)
    rosters = gj(SL + 'league/%s/rosters' % lid)
    uid = next(u['user_id'] for u in users if (u.get('display_name') or '').lower() == USER)
    mine = next(r for r in rosters if r.get('owner_id') == uid or uid in (r.get('co_owners') or []))
    rid = mine['roster_id']
    slots = [SLOTMAP.get(x, x) for x in league['roster_positions'] if x != 'BN']
    out_of_play = set(mine.get('reserve') or []) | set(mine.get('taxi') or [])
    done_through = int(state['week']) - 1  # weeks before the current one are final

    weeks = {}
    for w in range(1, done_through + 1):
        ms = gj(SL + 'league/%s/matchups/%d' % (lid, w))
        e = next((m for m in ms if m['roster_id'] == rid), None)
        if not e or not e.get('starters'):
            continue
        sp = e.get('starters_points') or []
        if sum(float(x or 0) for x in sp) == 0:
            continue
        pp = e.get('players_points') or {}
        s = []
        for i, pid in enumerate(e['starters']):
            if pid in (None, '0'):
                continue
            nm, pos = players.info(pid)
            slot = slots[i] if i < len(slots) else 'WRT'
            s.append([slot, nm, pos, round(float(sp[i] or 0), 2)])
        starters = set(e['starters'])
        b = []
        for pid in e.get('players') or []:
            if pid in starters or pid in out_of_play:
                continue
            nm, pos = players.info(pid)
            if pos not in ELIG['WRTQ'] + ['K', 'DEF']:
                continue
            b.append([nm, pos, round(float(pp.get(pid) or 0), 2)])
        weeks[str(w)] = {'s': s, 'b': b}

    team = {'id': tid, 'name': name, 'platform': 'Sleeper', 'type': typ, 'weeks': weeks}

    # roster snapshot (for position depth and parlay tags)
    active = [p for p in (mine.get('players') or []) if p not in out_of_play]
    pos_count = {}
    for pid in active:
        pos = players.info(pid)[1]
        pos_count[pos] = pos_count.get(pos, 0) + 1
    all_keys = [players.key(p) for p in (mine.get('players') or [])]

    # transactions
    rid_user = {}
    uname = {u['user_id']: (u.get('display_name') or u['user_id']) for u in users}
    for r in rosters:
        rid_user[r['roster_id']] = uname.get(r.get('owner_id'), 'team %s' % r['roster_id'])
    txs = []
    for rnd in range(1, int(state['week']) + 2):
        try:
            txs += gj(SL + 'league/%s/transactions/%d' % (lid, rnd))
        except Exception:
            break
    season_start = dt.datetime(int(state['season']), 8, 1, tzinfo=ET).timestamp() * 1000
    seen, lines = set(), []
    for t in sorted(txs, key=lambda x: x.get('created', 0)):
        if t.get('status') != 'complete' or rid not in (t.get('roster_ids') or []) or t.get('created', 0) < season_start:
            continue
        if t['transaction_id'] in seen:
            continue
        seen.add(t['transaction_id'])
        adds = [players.info(p)[0] for p, r in (t.get('adds') or {}).items() if r == rid]
        drops = [players.info(p)[0] for p, r in (t.get('drops') or {}).items() if r == rid]
        if t['type'] == 'trade':
            def pick(p):
                return '%s %s-round pick' % (p['season'], ordinal(p['round']))
            got = adds + [pick(p) for p in t.get('draft_picks') or [] if p['owner_id'] == rid and p['previous_owner_id'] != rid]
            gave = drops + [pick(p) for p in t.get('draft_picks') or [] if p['previous_owner_id'] == rid and p['owner_id'] != rid]
            other = ', '.join(rid_user.get(x, '?') for x in t['roster_ids'] if x != rid)
            txt = '<b>Trade</b> with %s: sent %s, received %s' % (other, ', '.join(gave) or 'nothing', ', '.join(got) or 'nothing')
        else:
            kind = 'Waiver' if t['type'] == 'waiver' else 'Free agent'
            bid = (t.get('settings') or {}).get('waiver_bid')
            parts = []
            if adds:
                parts.append('added ' + ', '.join(adds))
            if drops:
                parts.append('dropped ' + ', '.join(drops))
            txt = '%s: %s%s' % (kind, '; '.join(parts) or 'no change', (' ($%s)' % bid) if bid else '')
        lines.append('%s: %s' % (fmt_date(t['created']), txt))

    st = mine.get('settings') or {}
    record = '%s-%s%s' % (st.get('wins', 0), st.get('losses', 0), ('-%s' % st['ties']) if st.get('ties') else '')
    moves = '<b>%s</b> (%s)' % (name, record)
    moves += ''.join('<br>' + l for l in lines) if lines else ': no roster moves yet'
    rec = ((league.get('scoring_settings') or {}).get('rec')) or 0
    return {
        'team': team, 'moves': moves, 'pos_count': pos_count, 'slots': slots,
        'keys': all_keys, 'label': label, 'record': record,
        'roster': {'active': active, 'starters': [x for x in (mine.get('starters') or []) if x not in (None, '0')], 'rec': rec},
    }


# ---------------------------------------------------------------- insights
def f1(x):
    return '%.1f' % x


def insights_for(res):
    team, slots, pos_count = res['team'], res['slots'], res['pos_count']
    ws = sorted(team['weeks'], key=int)
    if not ws:
        return {'weak': ['No completed weeks yet.'], 'moves': []}
    leaks, beaters, slot_pts = [], {}, {}
    for w in ws:
        wk = team['weeks'][w]
        total, picks = optimal(wk)
        start = sum(x[3] or 0 for x in wk['s'])
        leaks.append((int(w), round(total - start, 2)))
        names = {x[1] for x in wk['s']}
        for sl, nm, pts in picks:
            if nm not in names:
                beaters[nm] = beaters.get(nm, 0) + 1
        for x in wk['s']:
            slot_pts.setdefault(x[0], []).append(x[3] or 0)
    n = len(ws)
    tot = sum(l for _, l in leaks)
    over = sum(1 for _, l in leaks if l >= 10)
    big = max(leaks, key=lambda x: x[1])
    weak = ['Total left on the bench over %d weeks: %s points, with 10 or more in %d of them.' % (n, f1(tot), over),
            'Biggest miss: Week %d, %s points.' % (big[0], f1(big[1]))]
    avg = {k: sum(v) / len(v) for k, v in slot_pts.items()}
    skill = {k: v for k, v in avg.items() if k not in ('K', 'DEF')}
    if skill:
        k = min(skill, key=skill.get)
        label = {'WRT': 'a flex slot', 'WRTQ': 'the superflex slot'}.get(k, 'the %s slot' % k)
        weak.append('The lowest-scoring starting slot is %s, averaging %s a week.' % (label, f1(skill[k])))
    if 'K' in avg or 'DEF' in avg:
        weak.append('Kicker averages %s and D/ST averages %s a week.' % (f1(avg.get('K', 0)), f1(avg.get('DEF', 0))))
    top = sorted(beaters.items(), key=lambda x: -x[1])[:3]
    for nm, c in top:
        if c >= 2:
            weak.append('%s would have been in the best possible lineup in %d of %d weeks.' % (nm, c, n))
    mv = []
    if top and top[0][1] >= 2:
        mv.append('Look at starting %s more often, or trade depth for help at the slot they keep beating.' % ', '.join(nm for nm, c in top if c >= 2))
    qb_slots = max(1, slots.count('QB') + slots.count('WRTQ'))
    te_slots = max(1, slots.count('TE'))
    for pos, n_slots in (('QB', qb_slots), ('TE', te_slots)):
        have = pos_count.get(pos, 0)
        if have >= n_slots + 3:
            mv.append('You carry %d %ss and only %d slot(s) can start one. Packaging a spare for a position of need could help.' % (have, pos, n_slots))
    if avg.get('K', 99) < 7 or avg.get('DEF', 99) < 6:
        mv.append('Stream the kicker and D/ST by matchup each week. They are the cheapest points to improve.')
    mv.append('These ideas come only from your own scoring history. Check who is on waivers and current trade values in your league before acting.')
    return {'weak': weak, 'moves': mv}


# --------------------------------------------------------------------- NFL
def nfl_state():
    return gj(SL + 'state/nfl')


_NFLV = {}


def load_nflverse(season):
    if season not in _NFLV:
        url = 'https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_%s.csv' % season
        _NFLV[season] = pd.read_csv(io.BytesIO(get(url).content))
    return _NFLV[season].copy()


def nfl_legs(state, own):
    season = state['season']
    d = load_nflverse(season)
    d = d[(d.season_type == 'REG') & d.position.isin(['QB', 'RB', 'WR', 'TE'])].copy()
    max_week = int(d.week.max())
    d['tot'] = d.rushing_yards + d.receiving_yards
    d['td'] = d.rushing_tds + d.receiving_tds
    allp = d.copy()
    d = d[(d.attempts + d.carries + d.targets) > 0]

    nxt = max(int(state['week']), max_week + 1)
    sch = {}
    fx = {'LAR': 'LA', 'WSH': 'WAS'}
    try:
        sb = gj('https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?seasontype=2&week=%d' % nxt)
        for e in sb.get('events', []):
            comp = e['competitions'][0]['competitors']
            home = next(c for c in comp if c['homeAway'] == 'home')['team']['abbreviation']
            away = next(c for c in comp if c['homeAway'] == 'away')['team']['abbreviation']
            home, away = fx.get(home, home), fx.get(away, away)
            sch[home] = (away, 'vs')
            sch[away] = (home, '@')
    except Exception as ex:
        log('WARNING: NFL schedule failed (%s); matchup tags will be missing' % ex)

    SM = {'pass yds': 'passing_yards', 'pass TDs': 'passing_tds', 'rush yds': 'rushing_yards', 'rec': 'receptions',
          'rec yds': 'receiving_yards', 'rush + rec yds': 'tot', 'TD': 'td'}
    dg = allp.groupby('opponent_team').week.nunique()
    allow = {}
    for pos in ['QB', 'RB', 'WR', 'TE']:
        x = allp[allp.position == pos].groupby('opponent_team')[list(set(SM.values()))].sum()
        allow[pos] = x.div(dg, axis=0)

    def mu(pos, label, opp):
        if '+ ' not in label:
            return None
        stat = SM.get(label.split('+ ', 1)[1])
        if stat is None or opp not in allow[pos].index:
            return None
        col = allow[pos][stat]
        rk = int(col.rank(ascending=False, method='min')[opp])
        n = len(col)
        q = round(n / 4)
        return {'a': round(float(col[opp]), 1), 'avg': round(float(col.mean()), 1), 'rk': rk,
                'g': 'soft' if rk <= q else ('tough' if rk > n - q else 'mid')}

    M = {
        'QB': [('passing_yards', 'pass yds', [225, 250, 275, 300]), ('passing_tds', 'pass TDs', [2, 3]), ('rushing_yards', 'rush yds', [30, 40, 50])],
        'RB': [('rushing_yards', 'rush yds', [50, 60, 70, 80, 100]), ('receptions', 'rec', [3, 4, 5, 6]), ('tot', 'rush + rec yds', [70, 80, 100, 120]), ('td', 'TD', [1])],
        'WR': [('receptions', 'rec', [4, 5, 6, 7, 8]), ('receiving_yards', 'rec yds', [50, 60, 70, 80, 100]), ('td', 'TD', [1])],
        'TE': [('receptions', 'rec', [4, 5, 6, 7]), ('receiving_yards', 'rec yds', [40, 50, 60, 70, 80]), ('td', 'TD', [1])],
    }
    out = []
    for _, g in d.groupby('player_id'):
        g = g.sort_values('week')
        n = len(g)
        if n < 4:
            continue
        pos = g.position.iloc[-1]
        ms = []
        for col, lbl, ths in M[pos]:
            v = [int(x) for x in g[col].tolist()]
            best = None
            for t in ths:
                c = sum(x >= t for x in v)
                if c / n >= 0.8:
                    best = (t, c)
            if best:
                ms.append({'m': '%d+ %s' % (best[0], lbl), 'h': best[1], 'n': n, 'v': v})
        if not ms:
            continue
        nm = g.player_display_name.iloc[-1]
        parts = nm.split()
        key = name_key(parts[0], ' '.join(parts[1:]), pos)
        tm = g.team.iloc[-1]
        r = {'p': nm, 'pos': pos, 'tm': tm, 'ms': ms, 'own': sorted(own.get(key, [])), 'opp': None}
        if tm in sch:
            o, ha = sch[tm]
            r['opp'], r['ha'] = o, ha
            for m in ms:
                x = mu(pos, m['m'], o)
                if x:
                    m['mu'] = x
        out.append(r)
    out.sort(key=lambda r: (-sum(m['h'] == m['n'] for m in r['ms']), -len(r['ms'])))
    return {'rows': out, 'week': max_week}


# ----------------------------------------------------------------- college
CFB = 'https://site.api.espn.com/apis/site/v2/sports/football/college-football/'
CONF = {'1': 'ACC', '4': 'Big 12', '5': 'Big Ten', '8': 'SEC'}


def toint(x):
    try:
        return int(float(x))
    except Exception:
        return 0


def college(state):
    today = dt.datetime.now(ET).date()
    season = int(state['season'])
    start = dt.date(season, 8, 22)
    days = []
    d = start
    while d <= today + dt.timedelta(days=7):
        if d.weekday() in (2, 3, 4, 5, 6):  # Wed to Sun
            days.append(d)
        d += dt.timedelta(days=1)

    def board(day):
        try:
            return day, gj(CFB + 'scoreboard?groups=80&dates=%s&limit=400' % day.strftime('%Y%m%d'))
        except Exception:
            return day, {'events': []}

    tc, done, upcoming = {}, {}, []
    with ThreadPoolExecutor(8) as ex:
        boards = list(ex.map(board, days))
    for day, j in boards:
        for e in j.get('events', []):
            comp = e['competitions'][0]['competitors']
            for c in comp:
                cid = str(c['team'].get('conferenceId'))
                if cid in CONF:
                    tc[str(c['team']['id'])] = CONF[cid]
            if e['status']['type']['completed']:
                done[e['id']] = day
            elif day >= today:
                home = next(c for c in comp if c['homeAway'] == 'home')['team']
                away = next(c for c in comp if c['homeAway'] == 'away')['team']
                upcoming.append((day, str(home['id']), str(away['id']), home['shortDisplayName'], away['shortDisplayName']))

    def summary(eid):
        try:
            return eid, gj(CFB + 'summary?event=%s' % eid)
        except Exception:
            return eid, None

    with ThreadPoolExecutor(8) as ex:
        sums = list(ex.map(summary, list(done)))

    games = []
    for eid, s in sums:
        if not s:
            continue
        pl = (s.get('boxscore') or {}).get('players') or []
        if len(pl) < 2 or not any(str(p['team']['id']) in tc for p in pl):
            continue
        sides = []
        for p in pl:
            side = {'tid': str(p['team']['id']), 'tn': p['team'].get('shortDisplayName'), 'pl': {}, 'py': 0, 'ry': 0, 'ptd': 0}
            for c in p.get('statistics', []):
                if c.get('name') not in ('passing', 'rushing', 'receiving'):
                    continue
                for a in c.get('athletes', []):
                    ath = a['athlete']
                    o = side['pl'].setdefault(str(ath['id']), {'nm': ath.get('displayName'), 'pa': 0, 'py': 0, 'ptd': 0, 'car': 0, 'ry': 0, 'rtd': 0, 'rec': 0, 'cy': 0, 'ctd': 0})
                    v = a.get('stats') or []
                    if c['name'] == 'passing' and len(v) >= 4:
                        o['pa'] = toint(str(v[0]).split('/')[-1]); o['py'] = toint(v[1]); o['ptd'] = toint(v[3])
                        side['py'] += o['py']; side['ptd'] += o['ptd']
                    elif c['name'] == 'rushing' and len(v) >= 4:
                        o['car'] = toint(v[0]); o['ry'] = toint(v[1]); o['rtd'] = toint(v[3])
                        side['ry'] += o['ry']
                    elif c['name'] == 'receiving' and len(v) >= 4:
                        o['rec'] = toint(v[0]); o['cy'] = toint(v[1]); o['ctd'] = toint(v[3])
            sides.append(side)
        games.append({'id': eid, 'd': done[eid], 'sides': sides})
    games.sort(key=lambda g: (g['d'], g['id']))

    tg, P, al = {}, {}, {}
    for g in games:
        for i, side in enumerate(g['sides'][:2]):
            if side['tid'] not in tc:
                continue
            opp = g['sides'][1 - i]
            tg.setdefault(side['tid'], []).append(g['id'])
            x = al.setdefault(side['tid'], {'n': 0, 'py': 0, 'ry': 0, 'ptd': 0, 'tn': side['tn']})
            x['n'] += 1; x['py'] += opp['py']; x['ry'] += opp['ry']; x['ptd'] += opp['ptd']
            for pid, o in side['pl'].items():
                k = (side['tid'], pid)
                P.setdefault(k, {'nm': o['nm'], 'tid': side['tid'], 'tn': side['tn'], 'g': {}})['g'][g['id']] = o

    # defence ranks among Power 5 teams
    teams = list(al)
    rank = {}
    for f in ('py', 'ry', 'ptd'):
        order = sorted(teams, key=lambda t: -(al[t][f] / al[t]['n']))
        for i, t in enumerate(order):
            rank[(t, f)] = i + 1
    N = len(teams)
    q = round(N / 4)

    nxt = {}
    for day, h, a, hn, an in sorted(upcoming, key=lambda x: x[0]):
        nxt.setdefault(h, (an, a, 'vs'))
        nxt.setdefault(a, (hn, h, '@'))

    MS = [('py', 'pass yds', [250, 300, 350], 'Q'), ('ptd', 'pass TDs', [2, 3], 'Q'), ('ry', 'rush yds', [50, 75, 100], 'R'),
          ('rec', 'rec', [4, 5, 6, 7], 'W'), ('cy', 'rec yds', [50, 75, 100], 'W'),
          ('tot', 'rush + rec yds', [75, 100, 125], 'R'), ('td', 'TD', [1], 'R')]
    out = []
    for (tid, pid), p in P.items():
        gl = tg[tid]
        n = len(gl)
        if n < 5:
            continue
        arr = [p['g'].get(g, {}) for g in gl]
        sm = lambda f: sum(x.get(f, 0) for x in arr)
        pos = 'QB' if sm('pa') >= 20 else ('RB' if sm('car') >= sm('rec') else 'WR/TE')
        ms = []
        for f, lbl, ths, kind in MS:
            if (kind == 'Q' and pos != 'QB') or (kind == 'W' and pos == 'QB') or (f == 'tot' and pos != 'RB') \
                    or (f == 'td' and pos == 'QB') or (f == 'ry' and pos == 'WR/TE'):
                continue
            if f == 'tot':
                v = [x.get('ry', 0) + x.get('cy', 0) for x in arr]
            elif f == 'td':
                v = [x.get('rtd', 0) + x.get('ctd', 0) for x in arr]
            else:
                v = [x.get(f, 0) for x in arr]
            best = None
            for t in ths:
                c = sum(1 for x in v if x >= t)
                if c / n >= 0.8:
                    best = (t, c)
            if best:
                ms.append({'m': '%d+ %s' % (best[0], lbl), 'h': best[1], 'n': n, 'v': v})
        if not ms:
            continue
        r = {'p': p['nm'], 'pos': pos, 'tm': p['tn'], 'cf': tc[tid], 'ms': ms, 'opp': None}
        if tid in nxt:
            opn, oid, ha = nxt[tid]
            r['opp'], r['ha'] = opn, ha
            if oid in al:
                o = al[oid]
                for m in ms:
                    lab = m['m'].split('+ ', 1)[1]
                    if lab in ('pass yds', 'rec', 'rec yds'):
                        rk, txt = rank[(oid, 'py')], 'allows %d pass yds/g' % round(o['py'] / o['n'])
                    elif lab == 'pass TDs':
                        rk, txt = rank[(oid, 'ptd')], 'allows pass TDs'
                    elif lab == 'rush yds':
                        rk, txt = rank[(oid, 'ry')], 'allows %d rush yds/g' % round(o['ry'] / o['n'])
                    else:
                        rk = round((rank[(oid, 'py')] + rank[(oid, 'ry')]) / 2)
                        txt = 'allows %d pass, %d rush yds/g' % (round(o['py'] / o['n']), round(o['ry'] / o['n']))
                    m['mu'] = {'rk': rk, 't': txt, 'g': 'soft' if rk <= q else ('tough' if rk > N - q else 'mid')}
        out.append(r)
    out.sort(key=lambda r: (-sum(m['h'] == m['n'] for m in r['ms']), -len(r['ms'])))
    last = max((g['d'] for g in games), default=today)
    return {'rows': out, 'through': last.strftime('%b ') + str(last.day), 'teams': N}


# ------------------------------------------------------------------- build
OUT_STATUS = {'Out', 'IR', 'PUP', 'Sus', 'Suspended', 'NA', 'COV'}
FX = {'LAR': 'LA', 'WSH': 'WAS', 'JAC': 'JAX'}


def start_context(state):
    """Upcoming week, opponents, matchup ranks and Sleeper projections (all best effort)."""
    season = int(state['season'])
    d = load_nflverse(season)
    d = d[(d.season_type == 'REG') & d.position.isin(['QB', 'RB', 'WR', 'TE'])]
    max_week = int(d.week.max()) if len(d) else 0
    nxt = max(int(state['week']), max_week + 1)
    ctx = {'week': nxt, 'opp': {}, 'rank': {}, 'proj': {}, 'sched': False}
    try:
        sb = gj('https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?seasontype=2&week=%d' % nxt)
        for e in sb.get('events', []):
            cs = e['competitions'][0]['competitors']
            t = [FX.get(c['team']['abbreviation'], c['team']['abbreviation']) for c in cs]
            if len(t) == 2:
                ctx['opp'][t[0]], ctx['opp'][t[1]] = t[1], t[0]
        ctx['sched'] = bool(ctx['opp'])
    except Exception as ex:
        log('start/sit schedule unavailable:', ex)
    try:
        g = d.groupby(['position', 'opponent_team', 'week']).fantasy_points_ppr.sum().reset_index()
        g = g.groupby(['position', 'opponent_team']).fantasy_points_ppr.mean()
        for pos in ['QB', 'RB', 'WR', 'TE']:
            ser = g[pos].sort_values(ascending=False)  # most points allowed first = softest
            ctx['rank'][pos] = {FX.get(t, t): i + 1 for i, t in enumerate(ser.index)}
    except Exception as ex:
        log('start/sit matchup ranks unavailable:', ex)
    try:
        url = SLP + 'projections/nfl/%s/%d?season_type=regular&position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=K&position[]=DEF' % (season, nxt)
        for row in gj(url):
            st = row.get('stats') or {}
            if st:
                ctx['proj'][str(row['player_id'])] = st
    except Exception as ex:
        log('start/sit projections unavailable:', ex)
    return ctx


def start_sit(res, players, ctx):
    ro = res['roster']
    rec = ro['rec']
    key = 'pts_ppr' if rec >= 1 else 'pts_half_ppr' if rec >= 0.5 else 'pts_std'
    recent = {}
    for w in sorted(res['team']['weeks'], key=int):
        wk = res['team']['weeks'][w]
        for x in wk['s']:
            recent.setdefault((x[1], x[2]), []).append(x[3])
        for x in wk['b']:
            recent.setdefault((x[0], x[1]), []).append(x[2])
    cands = {}
    for pid in ro['active']:
        p = players.p.get(pid) or {}
        nm, pos = players.info(pid)
        if pos not in ELIG['WRTQ'] + ['K', 'DEF']:
            continue
        team = FX.get(p.get('team') or (pid if pos == 'DEF' else ''), p.get('team') or (pid if pos == 'DEF' else ''))
        inj = p.get('injury_status') or ''
        opp = ctx['opp'].get(team)
        bye = ctx['sched'] and team and opp is None
        st = ctx['proj'].get(pid) or {}
        if key in st:
            base, src = float(st[key]), 'proj'
        else:
            h = (recent.get((nm, pos)) or [])[-3:]
            base, src = (sum(h) / len(h) if h else 0.0), 'avg'
        adj, tag = base, ''
        rk = (ctx['rank'].get(pos) or {}).get(opp)
        if rk and pos in ('QB', 'RB', 'WR', 'TE'):
            if rk <= 8:
                adj, tag = base * 1.05, 'soft'
            elif rk >= 25:
                adj, tag = base * 0.95, 'tough'
        flag = ''
        if bye:
            adj, flag = 0.0, 'bye'
        elif inj in OUT_STATUS:
            adj, flag = 0.0, inj
        elif inj in ('Doubtful',):
            adj, flag = adj * 0.4, 'Doubtful'
        elif inj == 'Questionable':
            adj, flag = adj * 0.9, 'Questionable'
        cands[pid] = {'pid': pid, 'name': nm, 'pos': pos, 'score': adj, 'proj': round(base, 1), 'src': src,
                      'opp': opp or ('BYE' if bye else ''), 'tag': tag, 'flag': flag}
    used, lineup = set(), []
    for sl in ['QB', 'RB', 'WR', 'TE', 'K', 'DEF', 'WRT', 'WRTQ']:
        for _ in [x for x in res['slots'] if x == sl]:
            best = None
            for pid, c in cands.items():
                if pid in used or c['pos'] not in ELIG[sl]:
                    continue
                if best is None or c['score'] > cands[best]['score']:
                    best = pid
            if best:
                used.add(best)
                lineup.append((sl, cands[best]))
    cur = set(ro['starters'])
    rows = [[sl, c['name'], c['pos'], c['proj'], round(c['score'], 1), c['opp'], c['tag'], c['flag'], 'in' if c['pid'] not in cur else ''] for sl, c in lineup]
    changes = []
    ins = [c for _, c in lineup if c['pid'] not in cur]
    outs = [cands[p] for p in cur if p in cands and p not in used]
    for i in sorted(ins, key=lambda c: -c['score']):
        match = next((o for o in sorted(outs, key=lambda o: o['score']) if o['pos'] == i['pos']), None) or \
            (min(outs, key=lambda o: o['score']) if outs else None)
        if not match:
            continue
        outs.remove(match)
        why = ('%s is %s' % (match['name'], match['flag'] if match['flag'] != 'bye' else 'on a bye')) if match['flag'] else \
            ('%s projects %.1f vs %.1f' % (i['name'], i['score'], match['score']))
        changes.append({'start': i['name'], 'sit': match['name'], 'gain': round(i['score'] - match['score'], 1), 'why': why})
    # close calls: best bench player vs the weakest starter who could be replaced by him
    bench = []
    for pid, c in sorted(cands.items(), key=lambda kv: -kv[1]['score']):
        if pid in used:
            continue
        sl_scores = [x[1]['score'] for x in lineup if x[1]['pos'] == c['pos'] or (x[0] in ('WRT', 'WRTQ') and c['pos'] in ELIG[x[0]])]
        low = min(sl_scores) if sl_scores else None
        close = low is not None and low - c['score'] <= 1.5
        bench.append([c['name'], c['pos'], c['proj'], round(c['score'], 1), c['flag'] or ('close call' if close else '')])
    warn = [x[1] + ' (' + x[7] + ')' for x in rows if x[7]]
    return {'week': ctx['week'], 'lineup': rows, 'changes': changes, 'bench': bench[:8], 'warn': warn,
            'source': 'Sleeper projections' if any(c['src'] == 'proj' for c in cands.values()) else 'recent average'}


def read(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as f:
        return f.read()


def main():
    os.makedirs(DATA, exist_ok=True)
    state = nfl_state()
    log('NFL state: season %s week %s' % (state['season'], state['week']))

    players = Players()
    results = []
    for tid, name, typ, label, lid in LEAGUES:
        r = with_cache('sleeper_' + tid, lambda: sleeper_team(tid, name, typ, label, lid, players, state))
        if r:
            results.append(r)
    with open(os.path.join(DATA, 'eddie.json')) as f:
        eddie = json.load(f)

    start = {}
    try:
        ctx = start_context(state)
        for r in results:
            start[r['team']['id']] = with_cache('start_' + r['team']['id'], lambda r=r: start_sit(r, players, ctx))
    except Exception as ex:
        log('start/sit skipped:', ex)
        for r in results:
            c = cache_path('start_' + r['team']['id'])
            if os.path.exists(c):
                start[r['team']['id']] = json.load(open(c))

    teams = [eddie] + [r['team'] for r in results]
    insights = {'eddie': {
        'weak': ['Only the weeks entered by hand are on file for this team. The ESPN league is not part of the automatic refresh.'],
        'moves': ['Add screenshots or numbers to data/eddie.json, or ask Claude to update it, and this tab fills in like the others.']}}
    for r in results:
        insights[r['team']['id']] = insights_for(r)
    moves = [[r['moves']] for r in results] + [['<b>Eddie Pineiro (ESPN)</b>: entered by hand, nothing on file beyond what is in data/eddie.json.']]

    # who is on your teams (for tags in Parlay Pal)
    own = {}
    for r in results:
        for k in r['keys']:
            own.setdefault(k, set()).add(r['label'])
    for wk in eddie['weeks'].values():
        for x in wk['s']:
            parts = x[1].replace('.', '').split()
            own.setdefault(name_key(parts[0], ' '.join(parts[1:]), x[2]), set()).add('Eddie Pineiro')

    nfl = with_cache('nfl', lambda: nfl_legs(state, own))
    cfb = with_cache('college', lambda: college(state))
    through = max([int(w) for t in teams for w in t['weeks']] or [1])

    core = read('core_head.js') + 'const TEAMS = ' + json.dumps(teams, separators=(',', ':')) + ';\n\n' + read('core_tail.js')
    ins = 'const INSIGHTS = ' + json.dumps(insights) + ';\nconst MOVES = ' + json.dumps(moves) + ';\nconst START = ' + json.dumps(start) + ';\n'
    meta = {'nflWeek': (nfl or {}).get('week', through), 'cfbThrough': (cfb or {}).get('through', '')}
    parlay = read('parlay.js').replace('__N__', json.dumps((nfl or {}).get('rows', []), separators=(',', ':'))) \
        .replace('__C__', json.dumps((cfb or {}).get('rows', []), separators=(',', ':'))) \
        .replace('__META__', json.dumps(meta))
    updated = dt.datetime.now(ET).strftime('%b ') + str(dt.datetime.now(ET).day) + dt.datetime.now(ET).strftime(', %Y at %I:%M %p ET')
    html = read('template.html').replace('/*CORE*/', core).replace('/*INSIGHTS*/', ins).replace('<!--PARLAY-->', parlay) \
        .replace('__THROUGH__', str(through)).replace('__UPDATED__', updated)
    with open(os.path.join(ROOT, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    log('Wrote index.html (%d bytes), through week %d' % (len(html), through))


if __name__ == '__main__':
    main()
