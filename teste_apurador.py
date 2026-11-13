"""Teste local do serviço apurador: banco, fonte de placar, modelo e relógio de mentira."""
import os, sys, json, re, io, copy, contextlib
from datetime import datetime, timedelta, timezone, date
sys.path.insert(0, '.')
import apurador as m
m.time.sleep = lambda *_: None

falhas = []
def confere(nome, obtido, esperado):
    ok = obtido == esperado
    if not ok:
        falhas.append(nome)
    print(('ok  ' if ok else 'FALHA ') + nome + ('' if ok else f'  obtido={obtido!r} esperado={esperado!r}'))

RELOGIO = [datetime(2026, 11, 9, 3, 30, tzinfo=timezone.utc)]
m.agora_utc = lambda: RELOGIO[0]

# ---------- fonte de mentira
def time_(tid, nome, curto, ab, casa, placar, linhas=None, venceu=None):
    return {'id': tid, 'homeAway': 'home' if casa else 'away', 'winner': venceu, 'score': str(placar),
            'team': {'id': tid, 'displayName': nome, 'shortDisplayName': curto, 'name': curto, 'abbreviation': ab},
            'linescores': [{'value': float(x), 'displayValue': str(x)} for x in (linhas or [])]}
def ev(eid, data, slug, st, a, b, det=None):
    tipo = {'name': st, 'completed': st.startswith('STATUS_F'), 'state': 'post'}
    return {'id': eid, 'date': data, 'season': {'slug': slug},
            'competitions': [{'id': eid, 'date': data, 'competitors': [a, b], 'status': {'type': tipo}, 'details': det or []}], 'status': {'type': tipo}}
def gol(tid, og=False):
    return {'team': {'id': tid}, 'scoringPlay': True, 'ownGoal': og, 'shootout': False}

FUT = {'20261108': [
   ev('e1', '2026-11-08T14:00Z', '2026-27-english-premier-league', 'STATUS_FULL_TIME',
      time_('331', 'Brighton & Hove Albion', 'Brighton', 'BHA', True, 2), time_('337', 'Brentford', 'Brentford', 'BRE', False, 0), [gol('331'), gol('331')]),
   ev('e6', '2026-11-08T20:00Z', 'league-phase', 'STATUS_FULL_TIME',
      time_('360', 'Manchester United', 'Man United', 'MAN', True, 3), time_('104', 'AS Roma', 'Roma', 'ROM', False, 2),
      [gol('104'), gol('104'), gol('360'), gol('360'), gol('360')]),
   ev('e8', '2026-11-08T16:00Z', '2026-27-italian-serie-a', 'STATUS_FULL_TIME',
      time_('110', 'Genoa', 'Genoa', 'GEN', True, 2), time_('103', 'AC Milan', 'Milan', 'MIL', False, 3),
      [gol('110'), gol('110', og=True), gol('103'), gol('103'), gol('103')]),
 ], '20261109': [
   ev('e7', '2026-11-09T19:00Z', '2026-27-italian-serie-a', 'STATUS_SCHEDULED',
      time_('112', 'Lazio', 'Lazio', 'LAZ', True, 0), time_('114', 'Napoli', 'Napoli', 'NAP', False, 0)),
 ], '20261113': [
   ev('e9', '2026-11-13T19:00Z', '2026-27-italian-serie-a', 'STATUS_SCHEDULED',
      time_('120', 'Empoli', 'Empoli', 'EMP', True, 0), time_('121', 'Parma', 'Parma', 'PAR', False, 0)),
 ]}
NBA = {'20261108': [
   ev('n1', '2026-11-08T20:30Z', '2026-27', 'STATUS_FINAL',
      time_('4', 'Chicago Bulls', 'Bulls', 'CHI', True, 121, [30, 30, 30, 31]), time_('20', 'Philadelphia 76ers', '76ers', 'PHI', False, 127, [32, 30, 35, 30])),
   ev('n2', '2026-11-09T03:00Z', '2026-27', 'STATUS_FINAL',
      time_('12', 'LA Clippers', 'Clippers', 'LAC', True, 110, [25, 25, 30, 30]), time_('13', 'Los Angeles Lakers', 'Lakers', 'LAL', False, 100, [25, 25, 25, 25])),
 ], '20261109': [
   ev('n3', '2026-11-10T00:00Z', '2026-27', 'STATUS_SCHEDULED',
      time_('16', 'Minnesota Timberwolves', 'Timberwolves', 'MIN', True, 0), time_('30', 'Charlotte Hornets', 'Hornets', 'CHA', False, 0)),
 ]}
def stats(d): return [{'name': k, 'displayValue': str(v)} for k, v in d.items()]
def nba_box(tid, jog):
    return {'team': {'id': tid}, 'statistics': [{'keys': ['minutes', 'points', 'rebounds', 'assists'],
            'athletes': [{'athlete': {'displayName': n}, 'didNotPlay': False, 'stats': [str(x) for x in s]} for n, s in jog]}]}
RES = {
 'e1': {'boxscore': {'teams': [{'team': {'id': '331'}, 'statistics': stats({'yellowCards': 4, 'redCards': 0, 'wonCorners': 7})},
                              {'team': {'id': '337'}, 'statistics': stats({'yellowCards': 2, 'redCards': 1, 'wonCorners': 3})}]},
        'header': {'competitions': [{'boxscoreSource': 'full', 'competitors': []}]}},
 'e8': {'boxscore': {'teams': []}, 'header': {'competitions': [{}]},
        'keyEvents': [{'scoringPlay': True, 'text': 'Goal! Genoa 1, AC Milan 0. Fulano (Genoa) right footed shot.'},
                      {'scoringPlay': True, 'text': 'Own Goal by Beltrano, AC Milan. Genoa 2, AC Milan 0.'},
                      {'scoringPlay': True, 'text': 'Goal! Genoa 2, AC Milan 1. Sicrano (AC Milan) header.'},
                      {'scoringPlay': True, 'text': 'Goal! Genoa 2, AC Milan 2. Sicrano (AC Milan) header.'},
                      {'scoringPlay': True, 'text': 'Goal! Genoa 2, AC Milan 3. Sicrano (AC Milan) header.'}]},
 'n1': {'boxscore': {'players': [nba_box('20', [('LeBron James', [33, 22, 5, 5]), ('Joel Embiid', [29, 18, 10, 5]), ('Dean Wade', [18, 0, 1, 1])]),
                                 nba_box('4', [('Josh Giddey', [35, 20, 11, 9])])]}, 'header': {'competitions': [{}]}},
 'n2': {'boxscore': {'players': [nba_box('13', [('Luka Doncic', [38, 31, 9, 8])])]}, 'header': {'competitions': [{}]}},
}
pedidos = []
def espn_falso(url):
    pedidos.append(url)
    mm = re.search(r'/sports/(.+?)/(scoreboard|summary)\?(?:dates=(\d+)|event=(\w+))', url)
    cam, tipo, d, e = mm.groups()
    if tipo == 'scoreboard':
        return {'events': copy.deepcopy({'soccer/all': FUT, 'basketball/nba': NBA}.get(cam, {}).get(d, []))}
    return copy.deepcopy(RES.get(e))
m.espn = espn_falso

# ---------- banco de mentira
def ap(i, esporte, tipo, mercado, evento, entrada, status, data, ficha):
    return {'id': i, 'esporte_id': esporte, 'tipo_aposta': tipo, 'mercado': mercado, 'evento': evento, 'entrada': entrada, 'status': status,
            'data_evento': data, 'odd': '1.90', '_ficha': ficha}
F1 = lambda **k: [dict(esp='futebol', **k)]
APOSTAS = [
 ap('b01', 'Futebol', 'Simples', 'Resultado Final', 'Brighton x Brentford', 'Brighton', 'WON', '2026-11-08', F1(a='Brighton', b='Brentford', tipo='1x2', lado='a')),
 ap('b02', 'Futebol', 'Simples', 'Cartões', 'Brighton x Brentford', 'Mais de 4.5', 'WON', '2026-11-08', F1(a='Brighton', b='Brentford', tipo='total', met='cartoes', lado='over', linha=4.5)),
 ap('b03', 'Futebol', 'Simples', 'Cartões', 'Brighton x Brentford', 'Under 6.5', 'PENDING', '2026-11-08', F1(a='Brighton', b='Brentford', tipo='total', met='cartoes', lado='under', linha=6.5)),
 ap('b04', 'Futebol', 'Simples', 'Resultado Final', 'Man Utd x Roma', 'Roma', 'WON', '2026-11-08', F1(a='Manchester United', b='AS Roma', tipo='1x2', lado='b')),
 ap('b05', 'Futebol', 'Simples', 'Resultado Final', 'Man Utd x Roma', 'Roma com PA', 'PENDING', '2026-11-08', F1(a='Manchester United', b='AS Roma', tipo='1x2', lado='b', regra='pa')),
 ap('b19', 'Futebol', 'Simples', 'Resultado Final', 'Man Utd x Roma', 'Roma (sem PA)', 'PENDING', '2026-11-08', F1(a='Manchester United', b='AS Roma', tipo='1x2', lado='b', regra='sem_pa')),
 ap('b06', 'NBA', 'Simples', 'Pontos', 'Bulls x 76ers', 'Dean Wade +3.5 Pontos', 'LOST', '2026-11-08',
    [dict(esp='nba', a='Chicago Bulls', b='Philadelphia 76ers', tipo='jogador', jog='Dean Wade', met='pontos', lado='over', linha=3.5)]),
 ap('b07', 'NBA', 'Dupla', 'Múltiplos', 'Dupla', 'LeBron James Acima 20.5 Pontos + Joel Embiid 9.5+ Rebotes', 'PENDING', '2026-11-08',
    [dict(esp='nba', tipo='jogador', jog='LeBron James', met='pontos', lado='over', linha=20.5), dict(esp='nba', tipo='jogador', jog='Joel Embiid', met='rebotes', lado='over', linha=9.5)]),
 ap('b08', 'Handebol', 'Simples', 'ML', 'Kiel x Flensburg', 'Kiel', 'WON', '2026-11-08', None),
 ap('b09', 'Futebol', 'Simples', 'Handicap Asiático', 'Laholms FK x Lunds BK', 'Laholms FK -1.5', 'PENDING', '2026-11-08', F1(a='Laholms FK', b='Lunds BK', tipo='handicap', lado='a', linha=-1.5)),
 ap('b10', 'Futebol', 'Simples', 'Gols', 'Lazio x Napoli', 'Over 2.5', 'PENDING', '2026-11-09', F1(a='Lazio', b='Napoli', tipo='total', met='gols', lado='over', linha=2.5)),
 ap('b11', 'NBA', 'Dupla', 'Múltiplos', 'Dupla', 'Anthony Edwards Acima 25.5 Pontos', 'PENDING', '2026-11-09',
    [dict(esp='nba', tipo='jogador', jog='Anthony Edwards', met='pontos', lado='over', linha=25.5)]),
 ap('b12', 'Futebol', 'Simples', 'ML', 'Time Velho x Outro', 'Time Velho', 'PENDING', '2026-10-30', F1(a='Time Velho', b='Outro', tipo='1x2', lado='a')),
 ap('015db6d4-9953-40c7-b29a-96b43a49f198', 'Futebol', 'Simples', 'ML', 'Consolidado', 'x', 'PENDING', '2026-11-08', F1(a='x', b='y', tipo='1x2', lado='a')),
 ap('b14', 'Futebol', 'Simples', 'ML', 'Fora da janela', 'x', 'WON', '2026-11-20', F1(a='x', b='y', tipo='1x2', lado='a')),
 ap('b15', 'Futebol', 'Simples', 'Gols', 'Lazio x Napoli', 'Under 4.5', 'PENDING', '2026-11-11', F1(a='Lazio', b='Napoli', tipo='total', met='gols', lado='under', linha=4.5)),
 ap('b16', 'Futebol', 'Simples', 'Resultado Final', 'Genoa x AC Milan', 'Genoa', 'LOST', '2026-11-08', F1(a='Genoa', b='AC Milan', tipo='1x2', lado='a')),
 ap('b17', 'NBA', 'Simples', 'Pontos total', 'Clippers x Lakers', 'Mais de 200.5', 'WON', '2026-11-08',
    [dict(esp='nba', a='Los Angeles Clippers', b='Los Angeles Lakers', tipo='total', met='pontos', lado='over', linha=200.5)]),
 # registrada com a data do dia em que entrou; o jogo é cinco dias depois
 ap('b18', 'Futebol', 'Simples', 'Resultado Final', 'Empoli x Parma', 'Empoli', 'PENDING', '2026-11-08', F1(a='Empoli', b='Parma', tipo='1x2', lado='a')),
]
TABELA, GRAVACOES = {}, []
def sb_get_falso(path):
    if path.startswith('esportes'):
        return [{'id': e, 'nome': e} for e in sorted({a['esporte_id'] for a in APOSTAS})]
    if path.startswith('apostas?'):
        if 'offset=0' not in path:
            return []
        hoje = RELOGIO[0].astimezone(m.BRT).date()
        d0, d1, d15 = hoje - timedelta(days=m.ATRAS), hoje + timedelta(days=m.FRENTE), hoje - timedelta(days=15)
        assert f'data_evento.gte.{d0}' in path and f'data_evento.lte.{d1}' in path and 'status.eq.PENDING' in path, path
        out = []
        for a in APOSTAS:
            D = date.fromisoformat(a['data_evento'])
            if d0 <= D <= d1 or (a['status'] == 'PENDING' and d15 <= D < d0):
                out.append({k: v for k, v in a.items() if not k.startswith('_')})
        return out
    if path.startswith('apuracoes?'):
        ids = re.search(r'in\.\((.*)\)', path).group(1).split(',')
        return [copy.deepcopy(TABELA[i]) for i in ids if i in TABELA]
    raise AssertionError(path)
def sb_grava_falso(linhas):
    for l in linhas:
        assert tuple(l.keys()) == m.COLUNAS or set(l.keys()) == set(m.COLUNAS), l.keys()
        json.dumps(l)      # tem que ser serializável
        TABELA[l['aposta_id']] = copy.deepcopy(l)
    GRAVACOES.append(len(linhas))
m.sb_get, m.sb_grava = sb_get_falso, sb_grava_falso
chamadas = {'ficha': 0, 'casa': 0}
def pergunta_falsa(cli, sistema, conteudo, max_tokens, uso):
    chamadas[uso] += 1
    m.USO[uso][2] += 1
    dados = json.loads(conteudo)
    if uso == 'ficha':
        out = []
        for it in dados:
            a = next(x for x in APOSTAS if x['entrada'] == it['entrada'] and x['evento'] == it['evento'])
            out.append({'i': it['i'], 'pernas': a['_ficha'], 'duvida': ''})
        return json.dumps(out, ensure_ascii=False)
    return json.dumps([{'k': p['k'], 'id': None} for p in dados['pernas']])
m.pergunta = pergunta_falsa

def roda():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = m.ciclo(None)
    return r, buf.getvalue()

# ---------- ciclo 1
r, saida = roda()
print(saida[-600:])
L = TABELA
confere('c1 não olhou a consolidada', '015db6d4-9953-40c7-b29a-96b43a49f198' in L, False)
confere('c1 fora da janela não entra', 'b14' in L, False)
esperado = {'b01': ('WON', 'apurou', True, True), 'b02': ('WON', 'conferir', False, True), 'b03': (None, 'regra_casa', False, True),
            'b04': ('LOST', 'conferir', False, True), 'b05': ('WON', 'apurou', True, True), 'b06': ('LOST', 'apurou', True, True),
            'b07': ('WON', 'apurou', True, True), 'b08': (None, 'fora', False, True), 'b09': (None, 'sem_jogo', False, False),
            'b10': (None, 'nao_terminou', False, False), 'b11': (None, 'aguarda', False, False), 'b12': (None, 'sem_jogo', False, True),
            'b15': (None, None, False, False), 'b16': ('LOST', 'conferir', False, True), 'b17': ('WON', 'apurou', True, True),
            'b18': (None, 'nao_terminou', False, False), 'b19': ('LOST', 'apurou', True, True)}
for i, e in esperado.items():
    l = L.get(i) or {}
    confere(f'c1 {i}', (l.get('status_proposto'), l.get('classe'), l.get('fecharia'), l.get('final')), e)
confere('c1 b01 status na hora', L['b01']['status_na_hora'], 'WON')
confere('c1 b05 status na hora', L['b05']['status_na_hora'], 'PENDING')
confere('c1 b04 ressalva no motivo', 'abriu 2 gols' in (L['b04']['motivo'] or ''), True)
confere('c1 b16 (gol contra): viu pelo placar corrido', 'abriu 2 gols' in (L['b16']['motivo'] or ''), True)
confere('c1 b02 ressalva de folga', 'da linha' in (L['b02']['motivo'] or ''), True)
confere('c1 b19 "sem PA" escrito: perdeu e pronto, sem a ressalva do pagamento antecipado', 'abriu 2 gols' in (L['b19']['motivo'] or ''), False)
confere('c1 b10 próxima = início + 115 min', L['b10']['proxima_em'], '2026-11-09T20:55:00Z')
confere('c1 b10 guardou o início', L['b10']['inicio'], '2026-11-09T19:00:00Z')
confere('c1 b11 próxima em 30 min', L['b11']['proxima_em'], '2026-11-09T04:00:00Z')
confere('c1 b09 próxima em 3 h', L['b09']['proxima_em'], '2026-11-09T06:30:00Z')
confere('c1 b15 tem ficha e nada mais', (bool(L['b15']['ficha']), L['b15']['tentativas']), (True, 0))
confere('c1 b17 casou Clippers x Lakers certo', 'LA Clippers 110x100' in (L['b17']['motivo'] or ''), True)
confere('c1 b18 (data errada): achou o jogo dias à frente e espera por ele',
        ((L['b18']['jogos'] or [{}])[0].get('como'), L['b18']['inicio'], L['b18']['proxima_em']), ('data', '2026-11-13T19:00:00Z', '2026-11-13T20:55:00Z'))
confere('c1 b09 (sem jogo nenhum à frente) segue sem jogo', (L['b09']['classe'], L['b09']['jogos']), ('sem_jogo', None))
perna = dict(esp='futebol', a='Empoli', b='Parma', tipo='1x2', lado='a')
FUT['20261114'] = [ev('e10', '2026-11-14T19:00Z', 'coppa-italia', 'STATUS_SCHEDULED',
                      time_('120', 'Empoli', 'Empoli', 'EMP', False, 0), time_('121', 'Parma', 'Parma', 'PAR', True, 0))]
m.PLACARES.clear()
confere('busca adiante: dois jogos possíveis = não escolhe', m.acha_jogo_adiante(perna, date(2026, 11, 8), False), (None, None))
del FUT['20261114']
m.PLACARES.clear()
confere('busca adiante: um time só não vale', m.acha_jogo_adiante(dict(esp='futebol', a='Empoli', tipo='1x2', lado='a'), date(2026, 11, 8), False), (None, None))
confere('busca adiante: um jogo só = casa', (m.acha_jogo_adiante(perna, date(2026, 11, 8), False)[0] or {}).get('id'), 'e9')
confere('busca adiante: masculino não casa com pedido de feminino', m.acha_jogo_adiante(perna, date(2026, 11, 8), True), (None, None))
confere('c1 chamadas ao modelo', dict(chamadas), {'ficha': 2, 'casa': 1})
confere('c1 sem erro no log', [x for x in saida.splitlines() if 'ERRO' in x], [])
confere('c1 marcou divergência no log (b04 e b16)', saida.count('≠ MARCAÇÃO'), 1)

# ---------- ciclo 2: nada na vez
RELOGIO[0] += timedelta(minutes=6)
n_ped = len(pedidos)
r, saida = roda()
confere('c2 nada na vez', (r['vez'], r['gravadas'], r['fichas']), (0, 0, 0))
confere('c2 não foi à fonte de placar', len(pedidos), n_ped)
confere('c2 modelo quieto', dict(chamadas), {'ficha': 2, 'casa': 1})

# ---------- ciclo 3: b11 na vez, ainda aguardando
RELOGIO[0] = datetime(2026, 11, 9, 4, 5, tzinfo=timezone.utc)
r, saida = roda()
confere('c3 só o b11', r['vez'], 1)
confere('c3 b11 segue aguardando', (L['b11']['classe'], L['b11']['proxima_em'], L['b11']['tentativas']), ('aguarda', '2026-11-09T04:35:00Z', 2))

# ---------- ciclo 4: os jogos terminaram
FUT['20261109'][0] = ev('e7', '2026-11-09T19:00Z', '2026-27-italian-serie-a', 'STATUS_FULL_TIME',
                        time_('112', 'Lazio', 'Lazio', 'LAZ', True, 2), time_('114', 'Napoli', 'Napoli', 'NAP', False, 1), [gol('112'), gol('114'), gol('112')])
NBA['20261109'][0] = ev('n3', '2026-11-10T00:00Z', '2026-27', 'STATUS_FINAL',
                        time_('16', 'Minnesota Timberwolves', 'Timberwolves', 'MIN', True, 120, [30, 30, 30, 30]), time_('30', 'Charlotte Hornets', 'Hornets', 'CHA', False, 99, [25, 25, 25, 24]))
RES['n3'] = {'boxscore': {'players': [nba_box('16', [('Anthony Edwards', [36, 30, 5, 5])])]}, 'header': {'competitions': [{}]}}
RELOGIO[0] = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)
r, saida = roda()
confere('c4 b10 fechou', (L['b10']['status_proposto'], L['b10']['classe'], L['b10']['final'], L['b10']['status_na_hora']), ('WON', 'apurou', True, 'PENDING'))
confere('c4 b11 achou o jogo do jogador', (L['b11']['status_proposto'], L['b11']['classe']), ('WON', 'apurou'))
confere('c4 b09 desistiu depois do prazo', (L['b09']['classe'], L['b09']['final']), ('sem_jogo', True))
confere('c4 não perguntou de novo ao modelo', dict(chamadas), {'ficha': 2, 'casa': 1})
confere('c4 b01 intocado', L['b01']['tentativas'], 1)

# ---------- ciclo 5: mudaram o texto de uma aposta
b01 = next(a for a in APOSTAS if a['id'] == 'b01')
b01['entrada'] = 'Brentford'; b01['_ficha'] = F1(a='Brighton', b='Brentford', tipo='1x2', lado='b')
RELOGIO[0] += timedelta(minutes=6)
r, saida = roda()
confere('c5 refez a ficha e a proposta', (L['b01']['status_proposto'], L['b01']['classe'], L['b01']['tentativas']), ('LOST', 'apurou', 1))
confere('c5 avisou a diferença no log', '≠ MARCAÇÃO' in saida, True)
confere('c5 b15 entrou na vez (11/11 = amanhã) e não achou jogo nesse dia', (L['b15']['classe'], L['b15']['final']), ('sem_jogo', False))

# ---------- ciclo 6: o jogo da aposta com a data errada está em andamento, cinco dias depois da data dela
FUT['20261113'][0] = ev('e9', '2026-11-13T19:00Z', '2026-27-italian-serie-a', 'STATUS_SECOND_HALF',
                        time_('120', 'Empoli', 'Empoli', 'EMP', True, 1), time_('121', 'Parma', 'Parma', 'PAR', False, 0), [gol('120')])
RELOGIO[0] = datetime(2026, 11, 13, 20, 56, tzinfo=timezone.utc)
r, saida = roda()
confere('c6 b18 não desiste: o prazo conta do dia do jogo', (L['b18']['classe'], L['b18']['final'], L['b18']['proxima_em']), ('nao_terminou', False, '2026-11-13T21:01:00Z'))
confere('c6 b18 reaproveitou o jogo guardado', (L['b18']['jogos'] or [{}])[0].get('como'), 'data')

# ---------- ciclo 7: acabou; a proposta sai com a ressalva da data
FUT['20261113'][0] = ev('e9', '2026-11-13T19:00Z', '2026-27-italian-serie-a', 'STATUS_FULL_TIME',
                        time_('120', 'Empoli', 'Empoli', 'EMP', True, 1), time_('121', 'Parma', 'Parma', 'PAR', False, 0), [gol('120')])
RELOGIO[0] = datetime(2026, 11, 13, 21, 2, tzinfo=timezone.utc)
r, saida = roda()
confere('c7 b18 proposta com ressalva (nunca fecha sozinha)', (L['b18']['status_proposto'], L['b18']['classe'], L['b18']['fecharia'], L['b18']['final']), ('WON', 'conferir', False, True))
confere('c7 b18 diz que a data não bate', 'a data da aposta (2026-11-08) não é a do jogo (2026-11-13)' in (L['b18']['motivo'] or ''), True)
confere('c7 sem erro no log', [x for x in saida.splitlines() if 'ERRO' in x], [])
print('gravações:', GRAVACOES, '| pedidos à fonte:', len(pedidos), '| chamadas:', chamadas)
print('FALHAS:', falhas)
