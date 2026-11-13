"""Teste local dos avisos do Telegram: ensaio (só log) e ligado (Telegram de mentira)."""
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
    print(('ok  ' if ok else 'FALHA ') + nome + ('' if ok else f'  obtido={obtido!r} esperado={esperado!r}'))

RELOGIO = [datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)]
m.agora_utc = lambda: RELOGIO[0]

def time_(tid, nome, casa, placar, linhas=None):
    return {'id': tid, 'homeAway': 'home' if casa else 'away', 'score': str(placar),
            'team': {'id': tid, 'displayName': nome, 'shortDisplayName': nome, 'name': nome, 'abbreviation': nome[:3].upper()},
            'linescores': [{'value': float(x)} for x in (linhas or [])]}
def ev(eid, data, st, estado, detalhe, a, b, det=None):
    tipo = {'name': st, 'completed': st.startswith('STATUS_F'), 'state': estado, 'shortDetail': detalhe}
    return {'id': eid, 'date': data, 'season': {'slug': 'liga'},
            'competitions': [{'id': eid, 'date': data, 'competitors': [a, b], 'status': {'type': tipo}, 'details': det or []}], 'status': {'type': tipo}}
def gol(tid): return {'team': {'id': tid}, 'scoringPlay': True, 'ownGoal': False, 'shootout': False}
FUT = {'20261109': [ev('e7', '2026-11-09T19:00Z', 'STATUS_SCHEDULED', 'pre', '11/9 - 2:00 PM', time_('112', 'Lazio', True, 0), time_('114', 'Napoli', False, 0))]}
NBA = {'20261109': [ev('n3', '2026-11-10T00:00Z', 'STATUS_SCHEDULED', 'pre', '7:00 PM', time_('16', 'Minnesota Timberwolves', True, 0), time_('30', 'Charlotte Hornets', False, 0))]}
def espn_falso(url):
    mm = re.search(r'/sports/(.+?)/(scoreboard|summary)\?(?:dates=(\d+)|event=(\w+))', url)
    cam, tipo, d, e = mm.groups()
    if tipo == 'scoreboard':
        return {'events': copy.deepcopy({'soccer/all': FUT, 'basketball/nba': NBA}.get(cam, {}).get(d, []))}
    return {'boxscore': {}, 'header': {'competitions': [{}]}}
m.espn = espn_falso

def ap(i, esporte, tipo, evento, entrada, status, moeda, stake, u, odd, lucro, lucro_u, ficha):
    return {'id': i, 'esporte_id': esporte, 'tipo_aposta': tipo, 'mercado': '', 'evento': evento, 'entrada': entrada, 'status': status, 'data_evento': '2026-11-09',
            'odd': odd, 'moeda': moeda, 'stake_unidades': u, 'stake_reais': stake if moeda == 'BRL' else None, 'stake_usd': stake if moeda == 'USD' else None,
            'lucro_unidades': lucro_u, 'lucro_reais': lucro if moeda == 'BRL' else None, 'lucro_usd': lucro if moeda == 'USD' else None, '_ficha': ficha}
APOSTAS = [
 ap('a1', 'Futebol', 'Simples', 'Lazio x Napoli', 'Over 2.5', 'PENDING', 'BRL', 500, 1.0, '1.90', None, None, [dict(esp='futebol', a='Lazio', b='Napoli', tipo='total', met='gols', lado='over', linha=2.5)]),
 ap('a2', 'Futebol', 'Simples', 'Lazio x Napoli', 'Lazio', 'PENDING', 'USD', 50, 0.5, '2.10', None, None, [dict(esp='futebol', a='Lazio', b='Napoli', tipo='1x2', lado='a')]),
 ap('a3', 'NBA', 'Simples', 'Timberwolves x Hornets', 'Mais de 215.5', 'PENDING', 'BRL', 250, 0.5, '1.85', None, None,
    [dict(esp='nba', a='Minnesota Timberwolves', b='Charlotte Hornets', tipo='total', met='pontos', lado='over', linha=215.5)]),
 ap('a4', 'Handebol', 'Simples', 'Kiel x Flensburg', 'Kiel', 'WON', 'BRL', 500, 1.0, '1.90', 450, 0.9, None),
 ap('a5', 'Futebol', 'Simples', 'Time X x Time Y', 'Time X', 'LOST', 'BRL', 500, 1.0, '2.00', -500, -1.0, [dict(esp='futebol', a='Time X', b='Time Y', tipo='1x2', lado='a')]),
]
TABELA, AVISOS_T = {}, {}
def sb_get_falso(path):
    if path.startswith('esportes'):
        return [{'id': e, 'nome': e} for e in sorted({a['esporte_id'] for a in APOSTAS})]
    if path.startswith('apostas?'):
        if 'offset=0' not in path:
            return []
        return [{k: v for k, v in a.items() if not k.startswith('_')} for a in APOSTAS]
    if path.startswith('apuracoes?'):
        ids = re.search(r'in\.\((.*)\)', path).group(1).split(',')
        return [copy.deepcopy(TABELA[i]) for i in ids if i in TABELA]
    if path.startswith('apuracao_avisos?'):
        return [copy.deepcopy(v) for v in AVISOS_T.values()]
    raise AssertionError(path)
def sb_grava_falso(linhas):
    for l in linhas:
        TABELA[l['aposta_id']] = copy.deepcopy(l)
m.sb_get, m.sb_grava = sb_get_falso, sb_grava_falso
def pergunta_falsa(cli, sistema, conteudo, max_tokens, uso):
    dados = json.loads(conteudo)
    if uso == 'ficha':
        return json.dumps([{'i': it['i'], 'pernas': next(x for x in APOSTAS if x['entrada'] == it['entrada'])['_ficha'], 'duvida': ''} for it in dados])
    return json.dumps([{'k': p['k'], 'id': None} for p in dados['pernas']])
m.pergunta = pergunta_falsa
class Resp:
    headers = {'content-type': 'application/json'}
    def raise_for_status(self): pass
    def json(self): return {}
def post_falso(url, **k):
    assert '/rest/v1/apuracao_avisos' in url, url
    for l in json.loads(k['data'].decode('utf-8')):
        AVISOS_T[l['chave']] = l
    return Resp()
m.requests.post = post_falso
TG = []
def tg_falso(metodo, **campos):
    TG.append((metodo, campos))
    return {'message_id': 1000 + len(TG)}
m.tg = tg_falso

def roda(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()

def cena(modo):
    """Roda o dia inteiro num modo e devolve o que saiu em cada passo."""
    TABELA.clear(); AVISOS_T.clear(); TG.clear(); m.MEM.clear(); m.MEM_OK[0] = False; m.URGENTE.clear(); m.ENVIOS.clear(); m.JA_PERGUNTEI.clear()
    m.TG_MODO, m.TG_TOKEN, m.TG_CHAT, m.TG_DESDE = modo, 'x', '-100', None
    FUT['20261109'][0] = ev('e7', '2026-11-09T19:00Z', 'STATUS_SCHEDULED', 'pre', '2:00 PM', time_('112', 'Lazio', True, 0), time_('114', 'Napoli', False, 0))
    passos = {}
    RELOGIO[0] = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)
    roda(lambda: m.ciclo(None))
    passos['antes'] = roda(m.avisos); n0 = len(TG)
    FUT['20261109'][0] = ev('e7', '2026-11-09T19:00Z', 'STATUS_FIRST_HALF', 'in', "34'", time_('112', 'Lazio', True, 1), time_('114', 'Napoli', False, 0), [gol('112')])
    RELOGIO[0] = datetime(2026, 11, 9, 19, 36, tzinfo=timezone.utc)
    passos['comecou'] = roda(m.avisos); n1 = len(TG)
    RELOGIO[0] = datetime(2026, 11, 9, 19, 38, tzinfo=timezone.utc)
    passos['segue'] = roda(m.avisos); n2 = len(TG)
    passos['igual'] = roda(m.avisos); n3 = len(TG)
    FUT['20261109'][0] = ev('e7', '2026-11-09T19:00Z', 'STATUS_FULL_TIME', 'post', 'FT', time_('112', 'Lazio', True, 2), time_('114', 'Napoli', False, 1), [gol('112'), gol('114'), gol('112')])
    RELOGIO[0] = datetime(2026, 11, 9, 20, 50, tzinfo=timezone.utc)
    passos['acabou'] = roda(m.avisos); n4 = len(TG)
    passos['urgente'] = sorted(m.URGENTE)
    roda(lambda: m.ciclo(None))
    passos['propostas'] = (TABELA['a1']['status_proposto'], TABELA['a2']['status_proposto'], sorted(m.URGENTE))
    RELOGIO[0] = datetime(2026, 11, 9, 20, 52, tzinfo=timezone.utc)
    passos['fim'] = roda(m.avisos); n5 = len(TG)
    passos['depois'] = roda(m.avisos); n6 = len(TG)
    passos['n'] = (n0, n1, n2, n3, n4, n5, n6)
    return passos

# ---------- ensaio: nada sai, tudo no log
p = cena('ensaio')
confere('ensaio não chamou o Telegram', len(TG), 0)
confere('ensaio não gravou estado no banco', AVISOS_T, {})
print('--- painel antes dos jogos (ensaio):'); print(p['antes'])
confere('painel: cabeçalho e totais', all(x in p['antes'] for x in ('Hoje · seg 09/11 · 15:00', 'Entradas 5 · investido 4,00u', 'R$ 1.750,00 · US$ 50,00',
                                                                    'Pendentes 3 · 2,00u · R$ 750,00 · US$ 50,00', 'Resultado marcado -0,10u · R$ -50,00 · US$ +0,00')), True)
confere('painel: a seguir', '16:00 Lazio x Napoli · 2 entradas' in p['antes'] and '21:00 Minnesota Timberwolves x Charlotte Hornets · 1 entrada' in p['antes'], True)
confere('painel: jogo de outro dia leva o dia da semana', m.SEMANA[0], 'seg')
confere('antes do jogo não tem mensagem de jogo', 'jogo:' in p['antes'], False)
print('--- começou (ensaio):'); print(p['comecou'])
confere('começou: mensagem do jogo', "Ao vivo · Lazio 1x0 Napoli · 34'" in p['comecou'] and '· Over 2.5 @1,90' in p['comecou'] and '· Lazio @2,10' in p['comecou'], True)
confere('começou: painel mostra ao vivo', "Ao vivo\nLazio 1x0 Napoli · 34' · 2 entradas" in p['comecou'], True)
confere('sem mudança não repete', p['igual'], '')
confere('acabou: pede apuração já', p['urgente'], ['a1', 'a2'])
confere('apurou as duas e limpou a urgência', p['propostas'], ('WON', 'WON', []))
print('--- fim (ensaio):'); print(p['fim'])
confere('fim: resultado por aposta', 'Fim · Lazio 2x1 Napoli' in p['fim'] and '🟢 Over 2.5 @1,90 · green (robô)' in p['fim'] and '🟢 Lazio @2,10 · green (robô)' in p['fim'], True)
confere('fim: painel com a parcial do robô', 'Com o que o robô já apurou (2): +1,35u · R$ +400,00 · US$ +55,00' in p['fim'], True)
confere('depois do fim não repete o resultado', 'Fim ·' in p['depois'], False)

# ---------- ligado: manda de verdade (Telegram de mentira)
p = cena('ligado')
n0, n1, n2, n3, n4, n5, n6 = p['n']
met = [x[0] for x in TG]
confere('ligado: antes = painel novo + fixar', met[:n0], ['sendMessage', 'pinChatMessage'])
confere('ligado: painel sem som', TG[0][1].get('disable_notification'), True)
confere('ligado: começou = mensagem nova sem som + painel editado', (met[n0:n1], TG[n0][1].get('disable_notification')), (['sendMessage', 'editMessageText'], True))
confere('ligado: segue = edita a do jogo', met[n1:n2], ['editMessageText'])
confere('ligado: sem mudança não manda nada', n3 - n2, 0)
confere('ligado: acabou = edita a do jogo (fim de jogo) + painel', met[n3:n4], ['editMessageText', 'editMessageText'])
fim = TG[n4:n5]
confere('ligado: fim = edita a do jogo, resultado novo com som, painel', [x[0] for x in fim], ['editMessageText', 'sendMessage', 'editMessageText'])
confere('ligado: resultado responde a mensagem do jogo e avisa', (fim[1][1].get('disable_notification'), fim[1][1].get('reply_to_message_id')), (False, TG[n0][1] and 1000 + n0 + 1))
confere('ligado: depois do fim, silêncio', n6 - n5, 0)
confere('ligado: estado guardado', sorted(AVISOS_T), ['jogo:futebol:e7', 'jogo:futebol:e7:fim', 'painel:2026-11-09'])
confere('ligado: jogo fechado no estado', AVISOS_T['jogo:futebol:e7']['estado'], 'fim')
# de madrugada o resultado chega sem som
RELOGIO[0] = datetime(2026, 11, 10, 2, 30, tzinfo=timezone.utc)      # 23h30 em Brasília
antes = len(TG)
m.av_manda('teste:noite', 'Fim · teste', 'fim', novo=True, avisa=True)
confere('de noite: resultado sem som', (len(TG) - antes, TG[-1][1].get('disable_notification')), (1, True))
m.MEM.pop('teste:noite', None); AVISOS_T.pop('teste:noite', None)
RELOGIO[0] = datetime(2026, 11, 9, 20, 52, tzinfo=timezone.utc)
# reinício do serviço: não manda nada de novo
m.MEM.clear(); m.MEM_OK[0] = False
antes = len(TG)
roda(m.avisos)
confere('reinício: nenhuma mensagem repetida (só tenta fixar o painel de novo)', [x[0] for x in TG[antes:]], ['pinChatMessage'])
# erro do Telegram não vaza o token
def post_ruim(url, **k): raise ConnectionError('falhou em ' + url)
m.requests.post = post_ruim
m.TG_TOKEN = 'SEGREDO123'
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'apurador.py'), encoding='utf-8').read()
ns = {}
exec('import requests\n' + src[src.index('def tg(metodo, **campos):'):src.index('def av_carrega():')], {'requests': m.requests, 'TG_TOKEN': 'SEGREDO123'}, ns)
try:
    ns['tg']('sendMessage', chat_id=1, text='x'); erro = ''
except Exception as e:
    erro = str(e)
confere('erro do Telegram não carrega o token', ('SEGREDO123' in erro, erro), (False, 'telegram sendMessage: ConnectionError'))
print('FALHAS:', falhas)
