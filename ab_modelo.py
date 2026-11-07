"""A/B do modelo que lê os prints: claude-sonnet-4-5 (atual) × modelo novo, com o MESMO código (main.py).
Prints reais; a verdade é o que está no banco hoje (depois das correções do time). Roda uma vez e sai.
Não grava nada no banco nem manda nada no Telegram (só getFile pra baixar o print)."""
import os, re, sys, time, threading, importlib.util, concurrent.futures as cf
from datetime import datetime, timedelta, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

def carregar(nome):
    spec = importlib.util.spec_from_file_location(nome, os.path.join(AQUI, 'main.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

ANTIGO, NOVO = carregar('antigo'), carregar('novo')
ANTIGO.MODELO_PRINTS = os.environ.get('MODELO_ANTIGO', 'claude-sonnet-4-5')
NOVO.MODELO_PRINTS = os.environ.get('MODELO_NOVO', 'claude-sonnet-5-5')

def ids_env(nome):
    return [int(x) for x in os.environ.get(nome, '').split(',') if x.strip()]
GRUPOS = {
    'P': ids_env('IDS_PROBLEMA'),     # a odd que o time corrigiu antes do conserto de out/2026
    'C': ids_env('IDS_CORRIGIDOS'),   # corrigidos pelo time nas últimas 3 semanas
    'M': ids_env('IDS_MULTI'),        # print com várias apostas
    'R': ids_env('IDS_BRL'),
    'U': ids_env('IDS_USD'),
}
GRUPO_DE = {i: g for g, ids in GRUPOS.items() for i in ids}
TODOS = [i for ids in GRUPOS.values() for i in ids]

TL = threading.local()
def fake_insert(t, body):
    getattr(TL, 'linhas', []).append(body)
    return [body]
RE_CACHE = re.compile(r'input=(\d+) cache_read=(\d+) cache_write=(\d+) output=(\d+)')
def fake_print(*a, **k):
    s = ' '.join(str(x) for x in a)
    m = RE_CACHE.search(s)
    if m and hasattr(TL, 'uso'):
        TL.uso.append(tuple(int(x) for x in m.groups()))
for M in (ANTIGO, NOVO):
    M.sb_insert = fake_insert
    M.print = fake_print

CAD = NOVO.carregar_cadastros()
ids = ','.join(str(i) for i in TODOS)
PRINTS = {r['tg_msg_id']: r for r in NOVO.sb_get(f"prints_log?select=tg_msg_id,caption,file_id,operador,msg_date&tg_msg_id=in.({ids})")}
CAMPOS = ('odd', 'stake_unidades', 'stake_reais', 'stake_usd', 'moeda', 'tipster_id', 'bookie_id', 'data_evento')
VERDADE = {}
for a in NOVO.sb_get(f"apostas?select=tg_msg_id,{','.join(CAMPOS)}&tg_msg_id=in.({ids})"):
    VERDADE.setdefault(a['tg_msg_id'], []).append(a)

def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def igual(campo, a, b):
    if campo in ('odd', 'stake_reais', 'stake_usd'):
        x, y = num(a), num(b)
        if x is None or y is None:
            return x is None and y is None
        return abs(x - y) <= max(0.011, 0.01 * abs(y))
    if campo == 'stake_unidades':
        x, y = num(a), num(b)
        if x is None or y is None:
            return x is None and y is None
        return abs(x - y) <= 0.011
    return (a or None) == (b or None)

def chave(l):
    return (num(l.get('odd')) or 0, num(l.get('stake_usd')) or num(l.get('stake_reais')) or 0)

def comparar(linhas, verdade):
    """Por campo: quantas apostas batem. Pareia pela ordem de (odd, valor)."""
    res = {c: 0 for c in CAMPOS}
    n = len(verdade)
    if len(linhas) != n:
        return res, False, n
    for l, v in zip(sorted(linhas, key=chave), sorted(verdade, key=chave)):
        for c in CAMPOS:
            res[c] += igual(c, l.get(c), v.get(c))
    tudo = all(res[c] == n for c in CAMPOS)
    return res, tudo, n

def roda(M, img, p):
    TL.linhas, TL.uso = [], []
    data_hoje = (datetime.fromisoformat(p['msg_date']).astimezone(timezone(timedelta(hours=-3))).strftime('%Y-%m-%d')
                 if p.get('msg_date') else datetime.now().strftime('%Y-%m-%d'))
    cap = p.get('caption') or ''
    t0 = time.time()
    res = M.extrair_aposta(img, cap, CAD, data_hoje, p.get('operador'))
    seg = time.time() - t0
    M.registrar_apostas(res, p['tg_msg_id'], CAD, data_hoje, cap, cap, criado_por=p.get('operador'), texto_tipster='')
    return {'linhas': list(TL.linhas), 'uso': list(TL.uso), 'seg': seg}

def caso(mid):
    p = PRINTS.get(mid)
    if not p or not p.get('file_id'):
        return mid, {'erro': 'sem print'}
    img = NOVO.tg_get_file_bytes(p['file_id'])
    if not img:
        return mid, {'erro': 'download falhou'}
    out = {'grupo': GRUPO_DE[mid], 'cap': (p.get('caption') or '')[:30].replace('\n', ' / ')}
    for M, nome in ((ANTIGO, 'antigo'), (NOVO, 'novo')):
        try:
            r = roda(M, img, p)
            r['campos'], r['tudo'], r['n'] = comparar(r['linhas'], VERDADE.get(mid, []))
            out[nome] = r
        except Exception as e:
            out[nome] = {'erro': f"{type(e).__name__}: {e}"[:160]}
    return mid, out

def resumo_linhas(r):
    if 'erro' in r:
        return 'ERRO ' + r['erro']
    return ' ; '.join(f"@{l.get('odd')} {l.get('stake_unidades')}u R${l.get('stake_reais')} US${l.get('stake_usd')} {str(l.get('tipster_id'))[:4]} {str(l.get('bookie_id'))[:4]} {l.get('data_evento')}" for l in sorted(r['linhas'], key=chave))

print(f"A/B modelo: {ANTIGO.MODELO_PRINTS} × {NOVO.MODELO_PRINTS} — {len(TODOS)} prints", flush=True)
RES = {}
with cf.ThreadPoolExecutor(4) as ex:
    for mid, out in ex.map(caso, TODOS):
        RES[mid] = out
        if 'erro' in out:
            print(f"#{mid} {out['erro']}", flush=True)
            continue
        a, n = out['antigo'], out['novo']
        ta, tn = ('✔' if a.get('tudo') else '✘'), ('✔' if n.get('tudo') else '✘')
        if a.get('tudo') and n.get('tudo'):
            print(f"#{mid} [{out['grupo']}] os dois ✔", flush=True)
        else:
            v = ' ; '.join(f"@{x.get('odd')} {x.get('stake_unidades')}u R${x.get('stake_reais')} US${x.get('stake_usd')} {str(x.get('tipster_id'))[:4]} {str(x.get('bookie_id'))[:4]} {x.get('data_evento')}" for x in sorted(VERDADE.get(mid, []), key=chave))
            print(f"#{mid} [{out['grupo']}] verdade {v} | ANTIGO {ta} {resumo_linhas(a)} | NOVO {tn} {resumo_linhas(n)} | cap {out['cap']!r}", flush=True)

for nome in ('antigo', 'novo'):
    rs = [o[nome] for o in RES.values() if nome in o and 'erro' not in o[nome]]
    erros = sum(1 for o in RES.values() if nome in o and 'erro' in o[nome])
    apostas = sum(r['n'] for r in rs)
    por_campo = {c: sum(r['campos'][c] for r in rs) for c in CAMPOS}
    tudo = sum(1 for r in rs if r['tudo'])
    usos = [u for r in rs for u in r['uso']]
    media = lambda k: (sum(u[k] for u in usos) / len(usos)) if usos else 0
    seg = sum(r['seg'] for r in rs) / max(1, len(rs))
    por_grupo = {g: f"{sum(1 for m, o in RES.items() if o.get('grupo') == g and nome in o and o[nome].get('tudo'))}/{sum(1 for o in RES.values() if o.get('grupo') == g)}" for g in GRUPOS}
    print(f"PLACAR {nome} ({(ANTIGO if nome == 'antigo' else NOVO).MODELO_PRINTS}): prints 100% certos {tudo}/{len(rs)} · por grupo {por_grupo} · erros {erros}", flush=True)
    print(f"  por campo (de {apostas} apostas): " + ' · '.join(f"{c} {por_campo[c]}" for c in CAMPOS), flush=True)
    print(f"  tokens médios por chamada: input {media(0):.0f} · cache_read {media(1):.0f} · cache_write {media(2):.0f} · output {media(3):.0f} · {seg:.1f}s por print", flush=True)
print("FIM", flush=True)
