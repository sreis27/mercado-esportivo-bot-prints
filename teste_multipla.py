"""Teste local: múltipla planilhada no dia do 1º jogo (pedido do Samuel, 15/11).
Perna do dia perdeu → red ali. Ganhou → a aposta passa pro dia do jogo seguinte, com histórico, e o aviso do jogo diz isso.
Banco, placar, modelo e Telegram de mentira; times e apostas inventados."""
import os, sys, json, re, io, copy, contextlib
from datetime import datetime, timedelta, timezone, date
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apurador as m
m.time.sleep = lambda *_: None

falhas = []
def confere(nome, obtido, esperado):
    ok = obtido == esperado
    if not ok:
        falhas.append(nome)
    print(('ok  ' if ok else 'FALHA ') + nome + ('' if ok else f'\n      obtido={obtido!r}\n      esperado={esperado!r}'))

RELOGIO = [datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)]
m.agora_utc = lambda: RELOGIO[0]

# ---------- placar de mentira
def time_(tid, nome, casa, placar):
    return {'id': tid, 'homeAway': 'home' if casa else 'away', 'score': str(placar),
            'team': {'id': tid, 'displayName': nome, 'shortDisplayName': nome, 'name': nome, 'abbreviation': nome[:3].upper()}, 'linescores': []}
def ev(eid, quando, st, estado, a, b):
    tipo = {'name': st, 'completed': st.startswith('STATUS_F'), 'state': estado, 'shortDetail': 'FT' if estado == 'post' else ''}
    return {'id': eid, 'date': quando, 'season': {'slug': 'liga'},
            'competitions': [{'id': eid, 'date': quando, 'competitors': [a, b], 'status': {'type': tipo}, 'details': []}], 'status': {'type': tipo}}
def marcado(eid, quando, ca, fo):
    return ev(eid, quando, 'STATUS_SCHEDULED', 'pre', time_(eid + 'a', ca, True, 0), time_(eid + 'b', fo, False, 0))
def ao_vivo(eid, quando, ca, fo, pa, pb):
    e = ev(eid, quando, 'STATUS_FIRST_HALF', 'in', time_(eid + 'a', ca, True, pa), time_(eid + 'b', fo, False, pb))
    for t in (e['status']['type'], e['competitions'][0]['status']['type']):
        t['completed'], t['shortDetail'] = False, "10'"
    return e
def acabou(eid, quando, ca, fo, pa, pb):
    return ev(eid, quando, 'STATUS_FULL_TIME', 'post', time_(eid + 'a', ca, True, pa), time_(eid + 'b', fo, False, pb))
FUT = {
    '20261120': [marcado('e20', '2026-11-20T18:00Z', 'Alfa FC', 'Beta FC')],                 # 15h em Brasília
    '20261121': [marcado('e21', '2026-11-21T22:30Z', 'Gama FC', 'Delta FC')],                # 19h30
    '20261123': [marcado('e23', '2026-11-23T20:00Z', 'Epsilon FC', 'Zeta FC')],              # segunda, 17h
}
def espn_falso(url):
    mm = re.search(r'/sports/(.+?)/(scoreboard|summary)\?(?:dates=(\d+)|event=(\w+))', url)
    cam, tipo, d, e = mm.groups()
    if tipo == 'scoreboard':
        return {'events': copy.deepcopy(FUT.get(d, [])) if cam == 'soccer/all' else []}
    return {'boxscore': {}, 'header': {'competitions': [{}]}}
m.espn = espn_falso

# ---------- banco de mentira (com o filtro de datas de verdade)
def ap(i, tipo, evento, entrada, data, ficha, status='PENDING', esporte='Futebol'):
    return {'id': i, 'esporte_id': esporte, 'tipo_aposta': tipo, 'mercado': 'Resultado Final', 'evento': evento, 'entrada': entrada,
            'status': status, 'data_evento': data, 'odd': '1.90', 'moeda': 'BRL', 'stake_unidades': 1.0, 'stake_reais': 100, 'stake_usd': None,
            'lucro_unidades': None, 'lucro_reais': None, 'lucro_usd': None, 'bookie_id': 'k1', '_ficha': ficha}
P = lambda a, b, lado: dict(esp='futebol', a=a, b=b, tipo='1x2', lado=lado)
def apostas_iniciais():
    return [
        ap('d01', 'Dupla', 'Dupla', 'Alfa FC + Gama FC', '2026-11-20', [P('Alfa FC', 'Beta FC', 'a'), P('Gama FC', 'Delta FC', 'a')]),
        ap('d02', 'Dupla', 'Dupla', 'Beta FC + Gama FC', '2026-11-20', [P('Alfa FC', 'Beta FC', 'b'), P('Gama FC', 'Delta FC', 'a')]),
        ap('d03', 'Dupla', 'Dupla', 'Alfa FC + Epsilon FC', '2026-11-20', [P('Alfa FC', 'Beta FC', 'a'), P('Epsilon FC', 'Zeta FC', 'a')]),
        ap('d04', 'Dupla', 'Dupla', 'Alfa FC + Time Sumido', '2026-11-20', [P('Alfa FC', 'Beta FC', 'a'), P('Time Sumido', 'Outro Sumido', 'a')]),
        ap('s01', 'Simples', 'Gama FC x Delta FC', 'Gama FC', '2026-11-21', [P('Gama FC', 'Delta FC', 'a')]),
        # dupla de esportes diferentes (futebol + tênis): o robô lê a perna de futebol; a de tênis não tem placar
        ap('m01', 'Dupla', 'Dupla', 'Beta FC + Jogador de Tênis', '2026-11-20', [P('Alfa FC', 'Beta FC', 'b'), dict(esp='outro')], esporte='Múltiplos'),
        ap('m02', 'Dupla', 'Dupla', 'Alfa FC + Outro Tenista', '2026-11-20', [P('Alfa FC', 'Beta FC', 'a'), dict(esp='outro')], esporte='Múltiplos'),
    ]
APOSTAS = apostas_iniciais()
TABELA, AVISOS_T, HIST, MUDANCAS = {}, {}, [], []
def cond(c, a):
    campo, op, val = c.split('.', 2)
    v = a.get(campo)
    if v is None:
        return False
    return {'eq': v == val, 'gte': v >= val, 'lte': v <= val, 'gt': v > val, 'lt': v < val}[op]
def filtra(path):
    """Entende o or=(and(...),and(...)) que o robô usa nas datas."""
    mm = re.search(r'&or=\((.*)\)&order', path)
    if not mm:
        return list(APOSTAS)
    grupos = re.findall(r'and\(([^)]*)\)', mm.group(1))
    return [a for a in APOSTAS if any(all(cond(c, a) for c in g.split(',')) for g in grupos)]
CONSULTAS = []
def sb_get_falso(path):
    if path.startswith('esportes'):
        return [{'id': 'Futebol', 'nome': 'Futebol'}, {'id': 'Múltiplos', 'nome': 'Múltiplos'}]
    if path.startswith('bookies'):
        return [{'id': 'k1', 'nome': '1win'}]
    if path.startswith('apostas?'):
        CONSULTAS.append(path)
        if 'offset=0' not in path:
            return []
        return [{k: v for k, v in a.items() if not k.startswith('_')} for a in filtra(path)]
    if path.startswith('apuracoes?'):
        if 'status_proposto=not.is.null' in path:
            return []
        ids = re.search(r'in\.\((.*)\)', path).group(1).split(',')
        return [copy.deepcopy(TABELA[i]) for i in ids if i in TABELA]
    if path.startswith('apuracao_divergencias?') or path.startswith('apuracao_avisos?'):
        return [copy.deepcopy(v) for v in AVISOS_T.values()] if path.startswith('apuracao_avisos?') else []
    raise AssertionError(path)
def sb_grava_falso(linhas):
    for l in linhas:
        assert set(l.keys()) == set(m.COLUNAS), l.keys()
        json.dumps(l)
        TABELA[l['aposta_id']] = copy.deepcopy(l)
m.sb_get, m.sb_grava = sb_get_falso, sb_grava_falso
CHAMADAS = {'ficha': 0, 'casa': 0}
def pergunta_falsa(cli, sistema, conteudo, max_tokens, uso):
    CHAMADAS[uso] += 1
    dados = json.loads(conteudo)
    if uso == 'ficha':
        return json.dumps([{'i': it['i'], 'pernas': next(x for x in APOSTAS if x['entrada'] == it['entrada'])['_ficha'], 'duvida': ''} for it in dados])
    return json.dumps([{'k': p['k'], 'id': None} for p in dados['pernas']])
m.pergunta = pergunta_falsa

class Resp:
    def __init__(self, dados=None): self.dados = dados if dados is not None else {}
    headers = {'content-type': 'application/json'}
    def raise_for_status(self): pass
    def json(self): return self.dados
CORRIDA = [False]          # simula alguém mexendo na aposta no meio do caminho
def patch_falso(url, **k):
    dados = json.loads(k['data'].decode('utf-8'))
    if '/rest/v1/apostas?' in url:
        f = dict(x.split('=', 1) for x in url.split('?', 1)[1].split('&'))
        alvo = [a for a in APOSTAS if a['id'] == f['id'][3:] and a['status'] == f['status'][3:] and a['data_evento'] == f['data_evento'][3:]]
        if CORRIDA[0]:
            alvo = []
        for a in alvo:
            a.update(dados)
        MUDANCAS.append((url, dados, len(alvo)))
        return Resp([{k2: v for k2, v in a.items() if not k2.startswith('_')} for a in alvo])
    raise AssertionError(url)
def post_falso(url, **k):
    linhas = json.loads(k['data'].decode('utf-8'))
    if '/rest/v1/historico_apostas' in url:
        HIST.extend(linhas)
        return Resp()
    if '/rest/v1/apuracao_avisos' in url:
        for l in linhas:
            AVISOS_T[l['chave']] = l
        return Resp()
    if '/rest/v1/apuracao_divergencias' in url:
        return Resp()
    raise AssertionError(url)
m.requests.post, m.requests.patch = post_falso, patch_falso
TG = []
def tg_falso(metodo, **campos):
    TG.append((metodo, campos))
    return {'message_id': 1000 + len(TG)}
m.tg = tg_falso
m.TG_MODO, m.TG_TOKEN, m.TG_CHAT, m.TG_DESDE = 'ligado', 'x', '-100', None

def roda(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()
def aposta(i): return next(a for a in APOSTAS if a['id'] == i)
def textos(desde=0): return [c.get('text') for met, c in TG[desde:] if met == 'sendMessage']

# ---------- 20/11, 14h: tudo marcado, nada começou
log1 = roda(lambda: m.ciclo(None))
confere('fichas feitas uma vez', CHAMADAS['ficha'], 1)
confere('antes do jogo: ninguém muda de dia', MUDANCAS, [])
confere('perna 3 dias depois achada adiante, sem ressalva de data errada', (TABELA['d03']['jogos'][1]['como'], 'não é a do jogo' in (TABELA['d03']['motivo'] or '')), ('data', False))
confere('perna sem jogo trava a d04', TABELA['d04']['classe'], 'sem_jogo')

# ---------- 20/11, 17h10: o jogo de hoje acabou 2x0 pro Alfa
FUT['20261120'] = [acabou('e20', '2026-11-20T18:00Z', 'Alfa FC', 'Beta FC', 2, 0)]
RELOGIO[0] = datetime(2026, 11, 20, 20, 10, tzinfo=timezone.utc)
n0 = len(TG)
roda(m.avisos)
confere('fim do jogo: espera a apuração das que dependem dele', sorted(m.URGENTE), ['d01', 'd02', 'd03', 'd04', 'm01', 'm02'])
confere('ainda não mandou o fim', [t for t in textos(n0) if 'Fim' in (t or '')], [])
log2 = roda(lambda: m.ciclo(None))
confere('dupla que perdeu a perna do dia: red na hora', (TABELA['d02']['status_proposto'], TABELA['d02']['classe']), ('LOST', 'apurou'))
confere('d02 fica no dia do jogo que perdeu', aposta('d02')['data_evento'], '2026-11-20')
confere('d01 ganhou a perna do dia: passa pro dia do jogo seguinte', aposta('d01')['data_evento'], '2026-11-21')
confere('d03: passa pro dia do jogo seguinte, 3 dias depois', aposta('d03')['data_evento'], '2026-11-23')
confere('d04: perna sem jogo trava a mudança', aposta('d04')['data_evento'], '2026-11-20')
confere('mista: a perna de futebol perdeu = red, mesmo com a de tênis sem placar', (TABELA['m01']['status_proposto'], TABELA['m01']['classe']), ('LOST', 'apurou'))
confere('mista: a de futebol ganhou, a de tênis o robô não lê = não muda de dia', (aposta('m02')['data_evento'], TABELA['m02']['status_proposto']), ('2026-11-20', None))
confere('só mexeu na data, com o nome do robô', sorted(set(k for _, d, _ in MUDANCAS for k in d)), ['atualizado_em', 'atualizado_por', 'data_evento'])
confere('PATCH só vale pendente e na data lida', all('status=eq.PENDING' in u and 'data_evento=eq.2026-11-20' in u for u, _, _ in MUDANCAS), True)
confere('histórico da aposta', sorted((h['aposta_id'], h['campo_alterado'], h['valor_anterior'], h['valor_novo'], h['alterado_por']) for h in HIST),
        [('d01', 'data_evento', '2026-11-20', '2026-11-21', 'Robô'), ('d03', 'data_evento', '2026-11-20', '2026-11-23', 'Robô')])
confere('log diz a mudança', sorted(re.findall(r'DATA (d0\d) (\S+) → (\S+)', log2)), [('d01', '2026-11-20', '2026-11-21'), ('d03', '2026-11-20', '2026-11-23')])
confere('a linha do robô acompanha a data nova (não refaz a ficha)', TABELA['d01']['texto_hash'], m.resumo_txt(dict(aposta('d01'), esporte='Futebol')))
confere('resultado de cada perna guardado', [g.get('st') for g in TABELA['d01']['jogos']], ['WON', None])
confere('início = o próximo jogo que ainda conta', TABELA['d01']['inicio'], '2026-11-21T22:30:00Z')
confere('d01 segue aberta, esperando o jogo de amanhã', (TABELA['d01']['status_proposto'], TABELA['d01']['classe'], TABELA['d01']['final']), (None, 'nao_terminou', False))

RELOGIO[0] = datetime(2026, 11, 20, 20, 12, tzinfo=timezone.utc)
n1 = len(TG)
roda(m.avisos)
fim = [t for t in textos(n1) if t and t.startswith('🏁 Fim')]
confere('saiu o fim do jogo', len(fim), 1)
linhas_fim = (fim or [''])[0].split('\n')
confere('fim: placar', linhas_fim[0], '🏁 Fim · Alfa FC 2x0 Beta FC')
confere('fim: a dupla que segue diz a próxima perna e o dia novo',
        next((x for x in linhas_fim if 'Alfa FC + Gama FC' in x), None),
        '🟢 Dupla: Alfa FC + Gama FC @1,90 · R$ 100,00 · 1win · green nesta perna; segue amanhã às 19:30 em Gama FC x Delta FC · a aposta fica em 21/11')
confere('fim: a dupla que perdeu é red',
        next((x for x in linhas_fim if 'Beta FC + Gama FC' in x), None), '🔴 Dupla: Beta FC + Gama FC @1,90 · R$ 100,00 · 1win · red (robô)')
confere('fim: a que foi pra segunda também aparece',
        next((x for x in linhas_fim if 'Alfa FC + Epsilon FC' in x), None),
        '🟢 Dupla: Alfa FC + Epsilon FC @1,90 · R$ 100,00 · 1win · green nesta perna; segue seg 23/11 às 17:00 em Epsilon FC x Zeta FC · a aposta fica em 23/11')
confere('fim: a que travou não muda de dia',
        next((x for x in linhas_fim if 'Time Sumido' in x), None), '🟢 Dupla: Alfa FC + Time Sumido @1,90 · R$ 100,00 · 1win · green nesta perna; segue na outra perna')
confere('fim: a simples de amanhã não aparece', any('Gama FC @' in x and 'Dupla' not in x for x in linhas_fim), False)
brt = RELOGIO[0].astimezone(m.BRT)
confere('situação da d01 no fechamento: esperando o jogo de amanhã', m.situacao(TABELA['d01'], brt), ('espera', 'jogo amanhã às 19:30'))

# ---------- de novo no mesmo ciclo: nada muda duas vezes
MUDANCAS.clear(); HIST.clear()
RELOGIO[0] = datetime(2026, 11, 20, 23, 0, tzinfo=timezone.utc)
m.URGENTE.update(['d01'])
roda(lambda: m.ciclo(None))
confere('não muda de novo', (MUDANCAS, HIST), ([], []))
confere('sem ficha nova', CHAMADAS['ficha'], 1)

# ---------- 21/11, 19h40: começa o jogo da 2ª perna; 20h40: o Gama ganhou → dupla green
FUT['20261121'] = [ao_vivo('e21', '2026-11-21T22:30Z', 'Gama FC', 'Delta FC', 0, 0)]
RELOGIO[0] = datetime(2026, 11, 21, 22, 40, tzinfo=timezone.utc)
n_vivo = len(TG)
roda(m.avisos)
comecou = [t for t in textos(n_vivo) if t and 'Começou' in t]
confere('começou o 2º jogo: a dupla viva em jogo, a morta já red',
        (comecou or [''])[0].split('\n')[1:],
        ['🎯 Dupla: Alfa FC + Gama FC @1,90 · R$ 100,00 · 1win', '🔴 Dupla: Beta FC + Gama FC @1,90 · R$ 100,00 · 1win · já red (robô)', '🎯 Gama FC @1,90 · R$ 100,00 · 1win'])
FUT['20261121'] = [acabou('e21', '2026-11-21T22:30Z', 'Gama FC', 'Delta FC', 1, 0)]
RELOGIO[0] = datetime(2026, 11, 21, 23, 40, tzinfo=timezone.utc)
n2 = len(TG)
roda(m.avisos)
confere('fim do jogo de amanhã: pede a apuração da dupla e da simples', sorted(m.URGENTE), ['d01', 's01'])
log3 = roda(lambda: m.ciclo(None))
RELOGIO[0] = datetime(2026, 11, 21, 23, 42, tzinfo=timezone.utc)
roda(m.avisos)
fim2 = [t for t in textos(n2) if t and t.startswith('🏁 Fim')]
confere('fim do 2º jogo: a dupla green, a que já tinha perdido red, a simples green',
        (fim2 or [''])[0].split('\n')[1:],
        ['🟢 Dupla: Alfa FC + Gama FC @1,90 · R$ 100,00 · 1win · green (robô)', '🔴 Dupla: Beta FC + Gama FC @1,90 · R$ 100,00 · 1win · red (robô)',
         '🟢 Gama FC @1,90 · R$ 100,00 · 1win · green (robô)'])
confere('d01 green no dia novo, sem ressalva', (TABELA['d01']['status_proposto'], TABELA['d01']['classe'], TABELA['d01']['fecharia']), ('WON', 'apurou', True))
confere('d01 achou de novo a perna de ontem pelo nome', TABELA['d01']['jogos'][0]['id'], 'e20')
confere('simples do Gama: green', TABELA['s01']['status_proposto'], 'WON')
confere('sem ficha nova', CHAMADAS['ficha'], 1)

# ---------- 23/11, 19h: a d03 fecha; a perna de 20/11 ficou longe da data e foi reaproveitada
FUT['20261123'] = [acabou('e23', '2026-11-23T20:00Z', 'Epsilon FC', 'Zeta FC', 3, 1)]
RELOGIO[0] = datetime(2026, 11, 23, 22, 0, tzinfo=timezone.utc)
log4 = roda(lambda: m.ciclo(None))
confere('d03 green sem ressalva de data', (TABELA['d03']['status_proposto'], TABELA['d03']['classe']), ('WON', 'apurou'))
confere('d03 reaproveitou a perna de 3 dias antes', [g['id'] for g in TABELA['d03']['jogos']], ['e20', 'e23'])
confere('sem erro no log', [x for x in (log1 + log2 + log3 + log4).splitlines() if 'ERRO' in x], [])

# ---------- chave desligada e corrida
def nova_cena():
    APOSTAS[:] = apostas_iniciais()
    TABELA.clear(); AVISOS_T.clear(); HIST.clear(); MUDANCAS.clear(); TG.clear(); m.MEM.clear(); m.MEM_OK[0] = False
    m.URGENTE.clear(); m.JA_PERGUNTEI.clear(); m.JA_ADIANTE.clear(); m.PLACARES.clear()
    FUT['20261120'] = [marcado('e20', '2026-11-20T18:00Z', 'Alfa FC', 'Beta FC')]
    FUT['20261121'] = [marcado('e21', '2026-11-21T22:30Z', 'Gama FC', 'Delta FC')]
    FUT['20261123'] = [marcado('e23', '2026-11-23T20:00Z', 'Epsilon FC', 'Zeta FC')]
    RELOGIO[0] = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)
    roda(lambda: m.ciclo(None))
    FUT['20261120'] = [acabou('e20', '2026-11-20T18:00Z', 'Alfa FC', 'Beta FC', 2, 0)]
    RELOGIO[0] = datetime(2026, 11, 20, 20, 10, tzinfo=timezone.utc)
    m.URGENTE.update(['d01', 'd03'])
    return roda(lambda: m.ciclo(None))

m.MULTIPLA_DATA = False
nova_cena()
confere('chave desligada: não muda data nenhuma', (aposta('d01')['data_evento'], aposta('d03')['data_evento'], MUDANCAS), ('2026-11-20', '2026-11-20', []))
m.MULTIPLA_DATA = True
CORRIDA[0] = True
saida = nova_cena()
confere('aposta mexida no meio do caminho: não muda e avisa', (aposta('d01')['data_evento'], HIST, 'mudou no banco enquanto o robô olhava' in saida), ('2026-11-20', [], True))
confere('aposta mexida: a linha do robô fica com o texto antigo', TABELA['d01']['texto_hash'], m.resumo_txt(dict(aposta('d01'), esporte='Futebol')))
CORRIDA[0] = False

# ---------- regra pura
r = {'status': None, 'pernas': [(0, 'WON', None, {'dia_brt': date(2026, 11, 20)}), (1, None, 'nao_terminou', {'dia_brt': date(2026, 11, 22)})]}
a = {'status': 'PENDING', 'D': date(2026, 11, 20)}
confere('regra: green + jogo noutro dia = muda', m.data_seguinte(a, r), date(2026, 11, 22))
confere('regra: aposta já marcada não muda', m.data_seguinte(dict(a, status='WON'), r), None)
confere('regra: simples não muda', m.data_seguinte(a, dict(r, pernas=r['pernas'][1:])), None)
confere('regra: perna devolvida também segue', m.data_seguinte(a, dict(r, pernas=[(0, 'VOID', None, {'dia_brt': date(2026, 11, 20)})] + r['pernas'][1:])), date(2026, 11, 22))
confere('regra: falta perna hoje ainda = não muda', m.data_seguinte(a, dict(r, pernas=r['pernas'] + [(2, None, 'nao_terminou', {'dia_brt': date(2026, 11, 20)})])), None)
confere('regra: a próxima é a mais cedo', m.data_seguinte(a, dict(r, pernas=r['pernas'] + [(2, None, 'nao_terminou', {'dia_brt': date(2026, 11, 21)})])), date(2026, 11, 21))
confere('regra: aposta registrada no último dia (jeito antigo) não volta', m.data_seguinte(dict(a, D=date(2026, 11, 22)), r), None)
confere('regra: perna que o robô não lê trava', m.data_seguinte(a, dict(r, pernas=r['pernas'] + [(2, None, 'mercado', {'dia_brt': date(2026, 11, 23)})])), None)
confere('regra: nada acabou ainda = não muda', m.data_seguinte(a, dict(r, pernas=[(0, None, 'nao_terminou', {'dia_brt': date(2026, 11, 20)})] + r['pernas'][1:])), None)
print('FALHAS:', falhas)
