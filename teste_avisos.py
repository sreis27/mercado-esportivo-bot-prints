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
NBA_PRE = ev('n3', '2026-11-10T00:00Z', 'STATUS_SCHEDULED', 'pre', '7:00 PM', time_('16', 'Minnesota Timberwolves', True, 0), time_('30', 'Charlotte Hornets', False, 0))
NBA = {'20261109': [copy.deepcopy(NBA_PRE)]}
def espn_falso(url):
    mm = re.search(r'/sports/(.+?)/(scoreboard|summary)\?(?:dates=(\d+)|event=(\w+))', url)
    cam, tipo, d, e = mm.groups()
    if tipo == 'scoreboard':
        return {'events': copy.deepcopy({'soccer/all': FUT, 'basketball/nba': NBA}.get(cam, {}).get(d, []))}
    return {'boxscore': {}, 'header': {'competitions': [{}]}}
m.espn = espn_falso

def ap(i, esporte, tipo, evento, entrada, status, moeda, stake, u, odd, lucro, lucro_u, ficha, casa='k1', data='2026-11-09'):
    return {'id': i, 'esporte_id': esporte, 'tipo_aposta': tipo, 'mercado': '', 'evento': evento, 'entrada': entrada, 'status': status, 'data_evento': data,
            'odd': odd, 'moeda': moeda, 'stake_unidades': u, 'stake_reais': stake if moeda == 'BRL' else None, 'stake_usd': stake if moeda == 'USD' else None,
            'lucro_unidades': lucro_u, 'lucro_reais': lucro if moeda == 'BRL' else None, 'lucro_usd': lucro if moeda == 'USD' else None,
            'bookie_id': casa, '_ficha': ficha}
def apostas_do_dia():
    return [
     ap('a1', 'Futebol', 'Simples', 'Lazio x Napoli', 'Over 2.5', 'PENDING', 'BRL', 500, 1.0, '1.90', None, None, [dict(esp='futebol', a='Lazio', b='Napoli', tipo='total', met='gols', lado='over', linha=2.5)]),
     ap('a2', 'Futebol', 'Simples', 'Lazio x Napoli', 'Lazio', 'PENDING', 'USD', 50, 0.5, '2.10', None, None, [dict(esp='futebol', a='Lazio', b='Napoli', tipo='1x2', lado='a')], casa='k2'),
     ap('a3', 'NBA', 'Simples', 'Timberwolves x Hornets', 'Mais de 215.5', 'PENDING', 'BRL', 250, 0.5, '1.90', None, None,
        [dict(esp='nba', a='Minnesota Timberwolves', b='Charlotte Hornets', tipo='total', met='pontos', lado='over', linha=215.5)]),
     ap('a4', 'Handebol', 'Simples', 'Kiel x Flensburg', 'Kiel', 'WON', 'BRL', 500, 1.0, '1.90', 450, 0.9, None),
     ap('a5', 'Futebol', 'Simples', 'Time X x Time Y', 'Time X', 'LOST', 'BRL', 500, 1.0, '2.00', -500, -1.0, [dict(esp='futebol', a='Time X', b='Time Y', tipo='1x2', lado='a')], casa=None),
    ]
APOSTAS = apostas_do_dia()
DUVIDA = {'Mais de 215.5': 'linha pouco legível'}        # o modelo leu esta com dúvida: a proposta sai com ressalva
TABELA, AVISOS_T = {}, {}
CONSULTAS = []
def sb_get_falso(path):
    if path.startswith('esportes'):
        return [{'id': e, 'nome': e} for e in sorted({a['esporte_id'] for a in APOSTAS})]
    if path.startswith('bookies'):
        return [{'id': 'k1', 'nome': '1win'}, {'id': 'k2', 'nome': 'Polymarket'}]
    if path.startswith('apostas?'):
        CONSULTAS.append(path)
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
        return json.dumps([{'i': it['i'], 'pernas': next(x for x in APOSTAS if x['entrada'] == it['entrada'])['_ficha'],
                            'duvida': DUVIDA.get(it['entrada'], '')} for it in dados])
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
    APOSTAS[:] = apostas_do_dia()
    TABELA.clear(); AVISOS_T.clear(); TG.clear(); m.MEM.clear(); m.MEM_OK[0] = False; m.URGENTE.clear(); m.ENVIOS.clear(); m.JA_PERGUNTEI.clear(); m.JA_ADIANTE.clear()
    m.TG_MODO, m.TG_TOKEN, m.TG_CHAT, m.TG_DESDE = modo, 'x', '-100', None
    FUT['20261109'][0] = ev('e7', '2026-11-09T19:00Z', 'STATUS_SCHEDULED', 'pre', '2:00 PM', time_('112', 'Lazio', True, 0), time_('114', 'Napoli', False, 0))
    NBA['20261109'][0] = copy.deepcopy(NBA_PRE)
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
confere('painel: cabeçalho e totais, uma frase por linha', '''📅 Hoje · seg 09/11 · 15:00

🎯 Entradas 5 · investido 4,00u
💰 R$ 1.750,00 · US$ 50,00
⏳ Pendentes 3 · 2,00u · R$ 750,00 · US$ 50,00
✅ Resultado marcado -0,10u · R$ -50,00 · US$ +0,00

🕒 A seguir
⚽ 16:00 Lazio x Napoli · 2 entradas
🏀 21:00 Minnesota Timberwolves x Charlotte Hornets · 1 entrada
''' in p['antes'], True)
confere('painel: jogo de outro dia leva o dia da semana', m.SEMANA[0], 'seg')
confere('antes do jogo não tem mensagem de jogo', 'jogo:' in p['antes'], False)
confere('a consulta dos avisos pega os 3 dias e as pendentes mais velhas, com a casa',
        any('bookie_id' in c and 'or=(and(data_evento.gte.2026-11-08,data_evento.lte.2026-11-10),and(status.eq.PENDING,data_evento.gte.2026-10-10,data_evento.lt.2026-11-08))' in c
            for c in CONSULTAS), True)
print('--- começou (ensaio):'); print(p['comecou'])
confere('começou: a partida e em que entrada estamos (odd, valor na moeda dela, casa)', '''⚽ Ao vivo · Lazio 1x0 Napoli · 34'
🎯 Over 2.5 @1,90 · R$ 500,00 · 1win
🎯 Lazio @2,10 · US$ 50,00 · Polymarket
''' in p['comecou'], True)
confere('começou: painel mostra ao vivo', "▶️ Ao vivo\n⚽ Lazio 1x0 Napoli · 34' · 2 entradas" in p['comecou'], True)
confere('sem mudança não repete', p['igual'], '')
confere('acabou: pede apuração já', p['urgente'], ['a1', 'a2'])
confere('apurou as duas e limpou a urgência', p['propostas'], ('WON', 'WON', []))
print('--- fim (ensaio):'); print(p['fim'])
confere('fim: resultado por aposta', '''🏁 Fim · Lazio 2x1 Napoli
🟢 Over 2.5 @1,90 · R$ 500,00 · 1win · green (robô)
🟢 Lazio @2,10 · US$ 50,00 · Polymarket · green (robô)
''' in p['fim'], True)
confere('fim: painel com a parcial do robô', '\n\n🤖 Com o que o robô já apurou (2): +1,35u · R$ +400,00 · US$ +55,00\n' in p['fim'], True)
confere('depois do fim não repete o resultado', 'Fim ·' in p['depois'], False)

# ---------- ligado: manda de verdade (Telegram de mentira)
p = cena('ligado')
n0, n1, n2, n3, n4, n5, n6 = p['n']
met = [x[0] for x in TG]
confere('ligado: antes = painel novo + fixar', met[:n0], ['sendMessage', 'pinChatMessage'])
confere('ligado: painel sem som', TG[0][1].get('disable_notification'), True)
confere('ligado: começou = mensagem nova COM som + painel editado', (met[n0:n1], TG[n0][1].get('disable_notification')), (['sendMessage', 'editMessageText'], False))
confere('ligado: a mensagem do começo é a do jogo', TG[n0][1]['text'].startswith('⚽ Ao vivo · Lazio 1x0 Napoli'), True)
confere('ligado: segue = edita a do jogo', met[n1:n2], ['editMessageText'])
confere('ligado: seguindo, a mensagem do jogo perde o "Ao vivo" e guarda as entradas',
        TG[n1][1]['text'], "⚽ Lazio 1x0 Napoli · 34'\n🎯 Over 2.5 @1,90 · R$ 500,00 · 1win\n🎯 Lazio @2,10 · US$ 50,00 · Polymarket")
confere('ligado: sem mudança não manda nada', n3 - n2, 0)
confere('ligado: acabou = edita a do jogo (fim de jogo) + painel', met[n3:n4], ['editMessageText', 'editMessageText'])
fim = TG[n4:n5]
confere('ligado: fim = edita a do jogo, resultado novo com som, painel', [x[0] for x in fim], ['editMessageText', 'sendMessage', 'editMessageText'])
confere('ligado: resultado responde a mensagem do jogo e avisa', (fim[1][1].get('disable_notification'), fim[1][1].get('reply_to_message_id')), (False, TG[n0][1] and 1000 + n0 + 1))
confere('ligado: depois do fim, silêncio', n6 - n5, 0)
confere('ligado: estado guardado', sorted(AVISOS_T), ['jogo:futebol:e7', 'jogo:futebol:e7:fim', 'painel:2026-11-09'])
confere('ligado: jogo fechado no estado', AVISOS_T['jogo:futebol:e7']['estado'], 'fim')
confere('ligado: o fixado do painel fica guardado no banco', (AVISOS_T['painel:2026-11-09'].get('dados'), AVISOS_T['jogo:futebol:e7'].get('dados')), ({'fixado': True}, None))
# de madrugada o resultado chega sem som; o silêncio acaba às 7h
RELOGIO[0] = datetime(2026, 11, 10, 2, 30, tzinfo=timezone.utc)      # 23h30 em Brasília
antes = len(TG)
m.av_manda('teste:noite', 'Fim · teste', 'fim', novo=True, avisa=True)
confere('de noite: resultado sem som', (len(TG) - antes, TG[-1][1].get('disable_notification')), (1, True))
m.MEM.pop('teste:noite', None); AVISOS_T.pop('teste:noite', None)
RELOGIO[0] = datetime(2026, 11, 10, 9, 59, tzinfo=timezone.utc); s659 = m.silencio()
RELOGIO[0] = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc); s700 = m.silencio()
confere('silêncio: 6h59 sim, 7h00 não', (s659, s700), (True, False))
RELOGIO[0] = datetime(2026, 11, 9, 20, 52, tzinfo=timezone.utc)
# reinício do serviço: não manda nada de novo
m.MEM.clear(); m.MEM_OK[0] = False
antes = len(TG)
roda(m.avisos)
confere('reinício: nenhuma mensagem repetida, nem fixa de novo (o fixado fica guardado)', [x[0] for x in TG[antes:]], [])

# ---------- fechamento do dia anterior, às 7h
# de madrugada o jogo da NBA acaba; a aposta dele foi lida com dúvida: proposta com ressalva
NBA['20261109'][0] = ev('n3', '2026-11-10T00:00Z', 'STATUS_FINAL', 'post', 'Final', time_('16', 'Minnesota Timberwolves', True, 120, [30, 30, 30, 30]),
                        time_('30', 'Charlotte Hornets', False, 99, [25, 25, 25, 24]))
RELOGIO[0] = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)       # 00h00 de 10/11
roda(lambda: m.ciclo(None))
confere('fechamento: a da NBA virou proposta com ressalva', (TABELA['a3']['status_proposto'], TABELA['a3']['classe']), ('WON', 'conferir'))
antes = len(TG)
roda(m.avisos)
noite = TG[antes:]
confere('madrugada: resultado do jogo sem som e nada de fechamento',
        ([x[0] for x in noite], noite[0][1].get('disable_notification'), any('Fechamento' in (x[1].get('text') or '') for x in noite)),
        (['sendMessage', 'editMessageText'], True, False))
confere('madrugada: a com ressalva sai como "confere"', '🟢 Mais de 215.5 @1,90 · R$ 250,00 · 1win · green (robô, confere)' in noite[0][1]['text'], True)
RELOGIO[0] = datetime(2026, 11, 10, 9, 59, tzinfo=timezone.utc)      # 06h59
antes = len(TG)
roda(m.avisos)
confere('fechamento: antes das 7h não sai', len(TG) - antes, 0)
RELOGIO[0] = datetime(2026, 11, 10, 10, 1, tzinfo=timezone.utc)      # 07h01
antes = len(TG)
roda(m.avisos)
saiu = TG[antes:]
confere('fechamento: às 7h sai uma mensagem nova, com som', ([x[0] for x in saiu], saiu[0][1].get('disable_notification')), (['sendMessage'], False))
print('--- fechamento às 7h:'); print(saiu[0][1]['text'])
confere('fechamento: texto das 7h', saiu[0][1]['text'], '''☀️ Fechamento de ontem · seg 09/11

🎯 5 entradas · 4,00u · R$ 1.750,00 · US$ 50,00
✅ Com baixa: 2 · -0,10u · R$ -50,00 · US$ +0,00
⏳ Faltam dar baixa: 3

🤖 Das 3 que faltam, o robô já apurou 3: 🟢 3 · 🔴 0
📊 Resultado do dia até aqui (baixas + robô): +1,80u · R$ +625,00 · US$ +55,00

⚠️ Pra revisar (1)
• Timberwolves x Hornets · Mais de 215.5 @1,90 · 1win · robô: green · leitura com dúvida: linha pouco legível''')
confere('fechamento: guardou o estado', (AVISOS_T.get('fechamento:2026-11-09') or {}).get('estado'), 'fechamento')
antes = len(TG)
roda(m.avisos)
confere('fechamento: sem mudança não repete', len(TG) - antes, 0)
# as baixas entram: uma igual ao robô, uma diferente; aparece uma pendente velha e uma sem fonte de placar
for a in APOSTAS:
    if a['id'] == 'a1':
        a.update(status='WON', lucro_reais=450, lucro_unidades=0.9)
    if a['id'] == 'a2':
        a.update(status='LOST', lucro_usd=-50, lucro_unidades=-0.5)
APOSTAS.append(ap('a6', 'Handebol', 'Simples', 'Kiel x Flensburg', 'Flensburg +2.5', 'PENDING', 'BRL', 100, 0.2, '1.80', None, None, None))
APOSTAS.append(ap('a7', 'Futebol', 'Dupla', 'Dupla', 'Time Velho + Outro Velho', 'PENDING', 'BRL', 100, 0.2, '3.00', None, None,
                  [dict(esp='futebol', a='Time Velho', b='Outro', tipo='1x2', lado='a')], casa='k2', data='2026-11-05'))
RELOGIO[0] = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)      # 09h00
roda(lambda: m.ciclo(None))
antes = len(TG)
roda(m.avisos)
saiu = TG[antes:]
confere('fechamento: com as baixas, edita a mesma mensagem (sem mandar outra)',
        ([x[0] for x in saiu], saiu[0][1].get('message_id')), (['editMessageText'], AVISOS_T['fechamento:2026-11-09']['msg_id']))
print('--- fechamento depois das baixas:'); print(saiu[0][1]['text'])
confere('fechamento: texto depois das baixas', saiu[0][1]['text'], '''☀️ Fechamento de ontem · seg 09/11

🎯 6 entradas · 4,20u · R$ 1.850,00 · US$ 50,00
✅ Com baixa: 4 · +0,30u · R$ +400,00 · US$ -50,00
⏳ Faltam dar baixa: 2

🤖 Das 2 que faltam, o robô já apurou 1: 🟢 1 · 🔴 0
📊 Resultado do dia até aqui (baixas + robô): +0,75u · R$ +625,00 · US$ -50,00

❗ Marcadas diferente do robô (1)
• Lazio x Napoli · Lazio @2,10 · Polymarket · marcada red, robô green

⚠️ Pra revisar (1)
• Timberwolves x Hornets · Mais de 215.5 @1,90 · 1win · robô: green · leitura com dúvida: linha pouco legível

✋ Sem apuração automática, baixa na mão (1)
• Handebol · Kiel x Flensburg · Flensburg +2.5 @1,80 · 1win

📌 De dias anteriores, ainda sem baixa (1)
• 05/11 · Dupla: Time Velho + Outro Velho @3,00 · Polymarket · ''' + m.situacao(TABELA.get('a7') or {}, RELOGIO[0].astimezone(m.BRT))[1])
confere('fechamento: a velha sem jogo diz por quê', m.situacao(TABELA.get('a7') or {}, RELOGIO[0].astimezone(m.BRT))[1] in ('não achei o jogo', 'ainda procurando o jogo'), True)
# chega a primeira entrada do dia: painel novo, fixado, e o de ontem sai do alto
APOSTAS.append(ap('a8', 'Futebol', 'Simples', 'Time Novo x Time Outro', 'Time Novo', 'PENDING', 'BRL', 300, 0.6, '2.00', None, None,
                  [dict(esp='futebol', a='Time Novo', b='Time Outro', tipo='1x2', lado='a')], data='2026-11-10'))
antes = len(TG)
roda(m.avisos)
saiu = TG[antes:]
confere('dia novo: painel novo, fixa o novo e desafixa o de ontem',
        ([x[0] for x in saiu], saiu[0][1]['text'].startswith('📅 Hoje · ter 10/11'), saiu[2][1].get('message_id')),
        (['sendMessage', 'pinChatMessage', 'unpinChatMessage'], True, AVISOS_T['painel:2026-11-09']['msg_id']))
# reinício às 9h: o fechamento já foi, não manda outro
m.MEM.clear(); m.MEM_OK[0] = False
antes = len(TG)
roda(m.avisos)
confere('fechamento: reinício não manda outro', [x[0] for x in TG[antes:] if x[0] == 'sendMessage'], [])
# depois do meio-dia, um fechamento que não saiu não sai mais
m.MEM.pop('fechamento:2026-11-09', None); AVISOS_T.pop('fechamento:2026-11-09', None)
RELOGIO[0] = datetime(2026, 11, 10, 15, 30, tzinfo=timezone.utc)     # 12h30
antes = len(TG)
roda(m.avisos)
confere('fechamento: depois do meio-dia não sai', [x for x in TG[antes:] if 'Fechamento' in (x[1].get('text') or '')], [])
# dia sem entrada e sem pendência: nada a dizer
confere('fechamento: dia sem entrada não gera texto', m.texto_fechamento(date(2026, 11, 20), RELOGIO[0].astimezone(m.BRT), [], {}), None)
# mensagem comprida: corta linha inteira e conta o que ficou de fora
muitas = [ap(f'z{i:02d}', 'Handebol', 'Simples', 'Um Time de Nome Bem Comprido x Outro Time de Nome Comprido', f'Handicap asiático do time da casa {i} com linha longa',
             'PENDING', 'BRL', 100, 0.2, '1.80', None, None, None) for i in range(60)]
longo = m.texto_fechamento(date(2026, 11, 9), RELOGIO[0].astimezone(m.BRT), muitas, {})
confere('fechamento: nunca passa do tamanho de uma mensagem', (len(longo) <= m.TETO_MSG + 40, 'e mais 48' in longo), (True, True))
m.TETO_MSG, teto = 700, m.TETO_MSG
curto_txt = m.texto_fechamento(date(2026, 11, 9), RELOGIO[0].astimezone(m.BRT), muitas, {})
m.TETO_MSG = teto
confere('fechamento: com pouco espaço, corta antes e diz quantas faltaram', (len(curto_txt) <= 740, bool(re.search(r'\ne mais 5\d$', curto_txt))), (True, True))

# ---------- erro do Telegram não vaza o token
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
