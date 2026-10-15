"""A/B da leitura de odd/unidade nos prints REAIS: main_antigo.py (produção) × main.py (novo).
Roda uma vez e sai. Não grava nada no banco nem manda nada no Telegram (só getFile pra baixar o print)."""
import os, sys, threading, importlib.util, concurrent.futures as cf
from datetime import datetime, timedelta, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
import main as NOVO
spec = importlib.util.spec_from_file_location('antigo', os.path.join(AQUI, 'main_antigo.py'))
ANTIGO = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ANTIGO)

PROBLEMA = [18464, 18455, 18381, 18380, 18344, 18339, 18304, 18206, 18194, 18191, 18088, 18082, 18059, 18033, 18004]
CONTROLE = [18460, 18457, 18436, 18369, 18351, 18317, 18265, 18262, 18251, 18171, 18109, 18061, 17954, 17930,
            18560, 18487, 18402, 18370, 18014, 17994, 17969, 17907]
if os.environ.get('IDS_PROBLEMA') is not None:
    PROBLEMA = [int(x) for x in os.environ['IDS_PROBLEMA'].split(',') if x.strip()]
if os.environ.get('IDS_CONTROLE') is not None:
    CONTROLE = [int(x) for x in os.environ['IDS_CONTROLE'].split(',') if x.strip()]
RODADAS_PROBLEMA = int(os.environ.get('RODADAS_PROBLEMA', '2'))

TL = threading.local()
def fake_insert(t, body):
    getattr(TL, 'linhas', []).append(body)
    return [body]
def fake_print(*a, **k):
    s = ' '.join(str(x) for x in a)
    if hasattr(TL, 'log') and any(m in s for m in ('🔀', '🔍', '⚠️', '❌', '🎯')):
        TL.log.append(s.strip()[:160])
for M in (NOVO, ANTIGO):
    M.sb_insert = fake_insert
    M.print = fake_print

CAD = NOVO.carregar_cadastros()
ids = ','.join(str(i) for i in PROBLEMA + CONTROLE)
PRINTS = {r['tg_msg_id']: r for r in NOVO.sb_get(f"prints_log?select=tg_msg_id,caption,file_id,operador,msg_date,foto_w,foto_h&tg_msg_id=in.({ids})")}
VERDADE = {}
for a in NOVO.sb_get(f"apostas?select=tg_msg_id,odd,stake_unidades&tg_msg_id=in.({ids})"):
    VERDADE.setdefault(a['tg_msg_id'], []).append((float(a['odd']) if a['odd'] is not None else None,
                                                    float(a['stake_unidades']) if a['stake_unidades'] is not None else None))

def roda(M, novo, img, p):
    TL.linhas, TL.log = [], []
    data_hoje = (datetime.fromisoformat(p['msg_date']).astimezone(timezone(timedelta(hours=-3))).strftime('%Y-%m-%d')
                 if p.get('msg_date') else datetime.now().strftime('%Y-%m-%d'))
    cap = p.get('caption') or ''
    res = M.extrair_aposta(img, cap, CAD, data_hoje, p.get('operador'))
    linhas_modelo = [a.get('linha_stake_odd') for a in res.get('apostas', [])]
    odds_modelo = [a.get('odd') for a in res.get('apostas', [])]
    kw = {'texto_tipster': ''} if novo else {}
    rel = M.registrar_apostas(res, p['tg_msg_id'], CAD, data_hoje, cap, cap, criado_por=p.get('operador'), **kw)
    return {'odd': [l.get('odd') for l in TL.linhas], 'u': [l.get('stake_unidades') for l in TL.linhas],
            'odd_modelo': odds_modelo, 'linha': linhas_modelo,
            'conf': [x for i in rel['itens'] for x in i['avisos'] + i['duvidas']], 'log': TL.log}

def acerto(odds, verdade, tol):
    vo = sorted(v[0] for v in verdade if v[0] is not None)
    oo = sorted(float(o) for o in odds if o is not None)
    return len(vo) == len(oo) and all(abs(a - b) <= max(0.011, tol * b) for a, b in zip(oo, vo))

def caso(mid):
    p = PRINTS.get(mid)
    if not p or not p.get('file_id'):
        return mid, {'erro': 'sem print'}
    img = NOVO.tg_get_file_bytes(p['file_id'])
    if not img:
        return mid, {'erro': 'download falhou'}
    n = RODADAS_PROBLEMA if mid in PROBLEMA else 1
    out = {'grupo': 'P' if mid in PROBLEMA else 'C', 'tam': f"{p.get('foto_w')}x{p.get('foto_h')}", 'cap': (p.get('caption') or '')[:40],
           'verdade': VERDADE.get(mid), 'antigo': [], 'novo': []}
    for _ in range(n):
        for M, nome in ((ANTIGO, 'antigo'), (NOVO, 'novo')):
            try:
                out[nome].append(roda(M, nome == 'novo', img, p))
            except Exception as e:
                out[nome].append({'erro': f"{type(e).__name__}: {e}"[:120]})
    return mid, out

def fmt(r):
    if 'erro' in r:
        return f"ERRO {r['erro']}"
    return f"odd {r['odd']} u {r['u']}"

print(f"A/B odd×unidade — {len(PROBLEMA)} prints com odd corrigida (x{RODADAS_PROBLEMA}) + {len(CONTROLE)} de controle", flush=True)
res = {}
with cf.ThreadPoolExecutor(4) as ex:
    for mid, out in ex.map(caso, PROBLEMA + CONTROLE):
        res[mid] = out
        if 'erro' in out:
            print(f"#{mid} {out['erro']}", flush=True)
            continue
        v = out['verdade']
        for i, (a, n) in enumerate(zip(out['antigo'], out['novo'])):
            ta = 'erro' not in a and acerto(a['odd'], v, 0.03)
            tn = 'erro' not in n and acerto(n['odd'], v, 0.03)
            print(f"#{mid} [{out['grupo']}] {out['tam']} verdade {v} | ANTIGO {fmt(a)} {'✔' if ta else '✘'} | NOVO {fmt(n)} {'✔' if tn else '✘'}"
                  f" | linha {n.get('linha')} | conf antigo {a.get('conf')} novo {n.get('conf')} | log {n.get('log')} | cap {out['cap']!r}", flush=True)

def placar(grupo, nome, tol):
    tot = ok = 0
    for mid, out in res.items():
        if out.get('grupo') != grupo:
            continue
        for r in out[nome]:
            tot += 1
            ok += ('erro' not in r) and acerto(r['odd'], out['verdade'], tol)
    return f"{ok}/{tot}"

def avisos(nome):
    tot = n = 0
    for out in res.values():
        for r in out.get(nome, []):
            tot += 1
            n += bool(r.get('conf'))
    return f"{n}/{tot}"
print(f"COM AVISO/DÚVIDA (🤨): antigo {avisos('antigo')} · novo {avisos('novo')}", flush=True)
for g, desc in (('P', 'odd corrigida pelo time'), ('C', 'controle (bot tinha acertado)')):
    print(f"PLACAR {desc}: antigo {placar(g, 'antigo', 0.03)} · novo {placar(g, 'novo', 0.03)}"
          f"  (exato: antigo {placar(g, 'antigo', 0)} · novo {placar(g, 'novo', 0)})", flush=True)
print("FIM", flush=True)
