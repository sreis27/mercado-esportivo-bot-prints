"""Sonda temporária: confere de onde o Railway alcança resultados de jogos e que formato vem.
Só leitura de fontes públicas. Não lê nem grava nada no banco, não manda nada no Telegram. Roda uma vez e sai."""
import json, sys, time
import requests

UA = {'User-Agent': 'rm-apurador-piloto/0.1 (python-requests)'}
ESPN = 'https://site.api.espn.com/apis/site/v2/sports'
DIA = '20261108'

def pega(url, t=25):
    try:
        r = requests.get(url, headers=UA, timeout=t)
        try:
            j = r.json()
        except Exception:
            j = None
        return r.status_code, j, len(r.content)
    except Exception as e:
        return 0, None, repr(e)[:160]

def poda(o, prof=0, maxprof=6, maxlist=2, maxstr=60):
    if isinstance(o, dict):
        if prof >= maxprof:
            return '{%d chaves}' % len(o)
        return {k: poda(v, prof + 1, maxprof, maxlist, maxstr) for k, v in list(o.items())[:45]}
    if isinstance(o, list):
        if prof >= maxprof:
            return '[%d itens]' % len(o)
        r = [poda(v, prof + 1, maxprof, maxlist, maxstr) for v in o[:maxlist]]
        if len(o) > maxlist:
            r.append('...+%d' % (len(o) - maxlist))
        return r
    if isinstance(o, str) and len(o) > maxstr:
        return o[:maxstr] + '…'
    return o

def solta(tag, o):
    s = json.dumps(o, ensure_ascii=False, separators=(',', ':'))
    for i in range(0, len(s), 1700):
        print('@%s|%03d|%s' % (tag, i // 1700, s[i:i + 1700]), flush=True)

def sem(o, *chaves):
    return {k: v for k, v in o.items() if k not in chaves} if isinstance(o, dict) else o

def final(ev):
    try:
        return ev['competitions'][0]['status']['type'].get('completed') or ev['status']['type'].get('completed')
    except Exception:
        return False

# ---------- futebol ----------
st, j, n = pega(f'{ESPN}/soccer/all/scoreboard?dates={DIA}&limit=900')
print('@FUT|status', st, 'bytes', n, flush=True)
if j:
    evs = j.get('events') or []
    print('@FUT|chaves', list(j.keys()), 'eventos', len(evs), 'ligas', len(j.get('leagues') or []), flush=True)
    solta('FUTLIGA', poda((j.get('leagues') or [None])[0], maxprof=3))
    ligas = {}
    for e in evs:
        chave = (e.get('season') or {}).get('slug') or (e.get('league') or {}).get('slug') or '?'
        uid = e.get('uid', '')
        ligas[uid.split('~')[1] if '~' in uid else chave] = ligas.get(uid.split('~')[1] if '~' in uid else chave, 0) + 1
    print('@FUT|por_liga_uid', json.dumps(dict(sorted(ligas.items(), key=lambda x: -x[1])[:60])), flush=True)
    print('@FUT|nomes', json.dumps([e.get('name') for e in evs[:60]], ensure_ascii=False), flush=True)
    fin = [e for e in evs if final(e)]
    print('@FUT|finalizados', len(fin), flush=True)
    if fin:
        e = fin[0]
        solta('FUTEV', poda(sem(e, 'links'), maxprof=7))
        c = e['competitions'][0]
        solta('FUTDET', poda(c.get('details'), maxprof=5, maxlist=4))
        solta('FUTCOMPSTAT', poda([x.get('statistics') for x in c.get('competitors', [])], maxprof=4, maxlist=30))
        # resumo do jogo, com e sem a liga no caminho
        liga = None
        for lg in (j.get('leagues') or []):
            pass
        uid = e.get('uid', '')
        print('@FUT|uid', uid, 'season', json.dumps(e.get('season')), flush=True)
        for caminho in ('all',):
            s2, r, n2 = pega(f'{ESPN}/soccer/{caminho}/summary?event={e["id"]}')
            print('@FUTSUM|', caminho, 'status', s2, 'bytes', n2, 'chaves', list(r.keys()) if isinstance(r, dict) else None, flush=True)
            if isinstance(r, dict):
                solta('FUTBOX', poda(r.get('boxscore'), maxprof=6, maxlist=40, maxstr=40))
                ros = r.get('rosters') or []
                if ros:
                    um = ros[0]
                    solta('FUTROS', poda({k: v for k, v in um.items() if k != 'roster'}, maxprof=3))
                    solta('FUTJOG', poda((um.get('roster') or [None])[:2], maxprof=5, maxlist=40, maxstr=40))
                solta('FUTKEY', poda(r.get('keyEvents'), maxprof=4, maxlist=3))
                solta('FUTHEAD', poda((r.get('header') or {}).get('competitions'), maxprof=6, maxlist=4))
    # um jogo de liga grande, pra ver se o detalhe muda
    grandes = [e for e in fin if any(x in e.get('uid', '') for x in ('l:700~', 'l:740~', 'l:630~', 'l:730~', 'l:720~', 'l:710~'))]
    print('@FUT|grandes', len(grandes), [g.get('name') for g in grandes[:8]], flush=True)
    if grandes:
        g = grandes[0]
        s2, r, n2 = pega(f'{ESPN}/soccer/all/summary?event={g["id"]}')
        print('@FUTG|', g.get('name'), 'status', s2, 'bytes', n2, flush=True)
        if isinstance(r, dict):
            solta('FUTGBOX', poda(r.get('boxscore'), maxprof=6, maxlist=40, maxstr=40))
            ros = r.get('rosters') or []
            if ros:
                solta('FUTGJOG', poda((ros[0].get('roster') or [None])[:2], maxprof=5, maxlist=40, maxstr=40))
            solta('FUTGKEY', poda(r.get('keyEvents'), maxprof=4, maxlist=3))
            solta('FUTGDET', poda(g['competitions'][0].get('details'), maxprof=5, maxlist=4))

# ---------- ligas americanas ----------
for tag, caminho in (('NBA', 'basketball/nba'), ('NHL', 'hockey/nhl'), ('NFL', 'football/nfl')):
    st, j, n = pega(f'{ESPN}/{caminho}/scoreboard?dates={DIA}')
    evs = (j or {}).get('events') or []
    print(f'@{tag}|status', st, 'bytes', n, 'eventos', len(evs), [e.get('shortName') for e in evs[:16]], flush=True)
    fin = [e for e in evs if final(e)]
    if fin:
        e = fin[0]
        solta(tag + 'EV', poda(sem(e, 'links'), maxprof=6, maxlist=5, maxstr=40))
        s2, r, n2 = pega(f'{ESPN}/{caminho}/summary?event={e["id"]}')
        print(f'@{tag}SUM|status', s2, 'bytes', n2, 'chaves', list(r.keys()) if isinstance(r, dict) else None, flush=True)
        if isinstance(r, dict):
            box = r.get('boxscore') or {}
            solta(tag + 'BOXT', poda(box.get('teams'), maxprof=5, maxlist=40, maxstr=30))
            pl = box.get('players') or []
            if pl:
                solta(tag + 'BOXP', poda(pl[0], maxprof=6, maxlist=30, maxstr=30))
    time.sleep(0.3)

# ---------- outras fontes: só se alcança e quanto vem ----------
OUTRAS = (
    ('NBACDN', 'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json'),
    ('NHLWEB', 'https://api-web.nhle.com/v1/score/2026-11-08'),
    ('ESPNUFC', f'{ESPN}/mma/ufc/scoreboard?dates=20261107'),
    ('ESPNATP', f'{ESPN}/tennis/atp/scoreboard?dates={DIA}'),
    ('ESPNEURO', f'{ESPN}/basketball/mens-euroleague/scoreboard?dates=20261106'),
    ('TSDBHAND', 'https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d=2026-11-08&s=Handball'),
    ('TSDBVOLEI', 'https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d=2026-11-08&s=Volleyball'),
    ('TSDBBASQ', 'https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d=2026-11-08&s=Basketball'),
    ('TSDBHOQ', 'https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d=2026-11-08&s=Ice_Hockey'),
    ('TSDBESP', 'https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d=2026-11-08&s=ESports'),
    ('POLY', 'https://gamma-api.polymarket.com/events?limit=1&closed=true&tag_slug=sports'),
)
for tag, url in OUTRAS:
    st, j, n = pega(url)
    resumo = None
    if isinstance(j, dict):
        resumo = {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in list(j.items())[:8]}
    elif isinstance(j, list):
        resumo = 'lista %d' % len(j)
    print(f'@{tag}|status', st, 'bytes', n, json.dumps(resumo, ensure_ascii=False)[:300], flush=True)
    if tag.startswith('TSDB') and isinstance(j, dict) and j.get('events'):
        evs = j['events']
        solta(tag + 'EV', poda([{k: e.get(k) for k in ('strEvent', 'strLeague', 'intHomeScore', 'intAwayScore', 'strStatus', 'dateEvent', 'strTime')} for e in evs[:12]], maxprof=3, maxlist=12))
    if tag in ('ESPNUFC', 'ESPNATP', 'ESPNEURO') and isinstance(j, dict):
        print(f'@{tag}|eventos', [e.get('name') for e in (j.get('events') or [])[:6]], flush=True)
    time.sleep(0.3)

print('@FIM', flush=True)
sys.exit(0)
