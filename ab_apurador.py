"""Apurador piloto (teste): o robô consegue fechar as apostas sozinho?

Pega as apostas JÁ FECHADAS de um período, entende cada entrada, acha o jogo numa fonte pública de placar,
apura sozinho e compara com o que foi marcado à mão. A marcação humana é a verdade do teste.

Só leitura: não grava nada no banco, não manda nada no Telegram. Roda uma vez e sai; o relatório sai no log.
"""
import os, re, sys, json, time, unicodedata, traceback, concurrent.futures as cf
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
import requests

SB_URL = 'https://yfdrifvhsiumdxgypkjm.supabase.co'
SB_KEY = os.environ.get('SUPABASE_KEY', '')
MODELO = os.environ.get('MODELO_FICHA', 'claude-sonnet-5-5')
DE = os.environ.get('AP_DE', '2026-11-05')
ATE = os.environ.get('AP_ATE', '2026-11-11')
MAXIMO = int(os.environ.get('AP_MAX', '70') or 0)      # 1ª rodada: amostra pequena, pra conferir o encanamento
LOTE = 12
BRT = timezone(timedelta(hours=-3))
ESPN = 'https://site.api.espn.com/apis/site/v2/sports'
CAM = {'futebol': 'soccer/all', 'nba': 'basketball/nba', 'nhl': 'hockey/nhl', 'nfl': 'football/nfl', 'mma': 'mma/ufc'}
ROTULO = {'Futebol': 'futebol', 'Futebol Feminino': 'futebol', 'NBA': 'nba', 'NHL': 'nhl', 'NFL': 'nfl',
          'MMA': 'mma', 'UFC': 'mma', '(sem esporte)': None}
USO = {'ficha': [0, 0, 0], 'casa': [0, 0, 0]}   # entrada, saída, chamadas
N_REQ = [0]


def log(*a):
    print(*a, flush=True)
    time.sleep(0.012)   # o Railway descarta linhas acima de ~500/s


# ------------------------------------------------------------------ texto
def sem_acento(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(s or '')) if not unicodedata.combining(c))

RUIDO = set('fc cf sc ac afc cfc cd ud sd ca rc rcd sk fk bk if ik sv ssc as us ss club clube de da do del la el los las the'.split())

def norm(s):
    s = sem_acento(s).lower().replace('&', ' and ')
    s = re.sub(r'[\(\[][^\)\]]{0,5}[\)\]]', ' ', s)      # (F), [W], (w), (Fem)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return ' '.join(t for t in s.split() if t not in RUIDO and len(t) > 1 and not t.isdigit())

def sim(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    if ta <= tb or tb <= ta:
        r = max(r, 0.86 + 0.1 * min(len(ta), len(tb)) / max(len(ta), len(tb)))
    return r

SUFIXO = {'jr', 'sr', 'ii', 'iii', 'iv'}

def norm_p(s):
    s = re.sub(r'[^a-z ]+', ' ', sem_acento(s).lower().replace('-', ' '))
    return ' '.join(t for t in s.split() if t not in SUFIXO)

def sim_p(a, b):
    """Nome de jogador: só casa por regra clara (igual, inicial + sobrenome, apelido que é começo do nome, só o sobrenome)."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = a.split(), b.split()
    if ta[-1] == tb[-1]:
        if len(ta) == 1 or len(tb) == 1:
            return 0.87
        if ta[0][0] == tb[0][0] and (len(ta[0]) == 1 or len(tb[0]) == 1):
            return 0.9
        if len(ta[0]) >= 3 and len(tb[0]) >= 3 and (ta[0].startswith(tb[0]) or tb[0].startswith(ta[0])):
            return 0.88
    return min(SequenceMatcher(None, a, b).ratio(), 0.85)

def num(v):
    try:
        return float(str(v).replace(',', '.'))
    except Exception:
        return None


# ------------------------------------------------------------------ banco (só leitura)
def sb(path):
    r = requests.get(f'{SB_URL}/rest/v1/{path}', timeout=40,
                     headers={'apikey': SB_KEY, 'Authorization': 'Bearer ' + SB_KEY})
    r.raise_for_status()
    return r.json()

def carrega_apostas():
    esportes = {e['id']: e['nome'] for e in sb('esportes?select=id,nome')}
    out, off = [], 0
    while True:
        lote = sb('apostas?select=id,data_evento,evento,mercado,entrada,tipo_aposta,status,odd,esporte_id'
                  f'&data_evento=gte.{DE}&data_evento=lte.{ATE}&status=neq.PENDING'
                  f'&order=data_evento.asc,id.asc&limit=1000&offset={off}')
        out += lote
        if len(lote) < 1000:
            break
        off += 1000
    for a in out:
        a['esporte'] = esportes.get(a.get('esporte_id')) or '(sem esporte)'
    return out


# ------------------------------------------------------------------ modelo
def cliente():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_KEY', ''))

def pergunta(cli, sistema, conteudo, max_tokens, uso):
    resp = cli.messages.create(
        model=MODELO, max_tokens=max_tokens,
        extra_body={'thinking': {'type': 'between_tools'}},      # sem pensar (mesma opção do bot dos prints)
        system=[{'type': 'text', 'text': sistema, 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user', 'content': conteudo}])
    u = getattr(resp, 'usage', None)
    if u is not None:
        USO[uso][0] += (getattr(u, 'input_tokens', 0) or 0) + (getattr(u, 'cache_read_input_tokens', 0) or 0) \
            + (getattr(u, 'cache_creation_input_tokens', 0) or 0)
        USO[uso][1] += getattr(u, 'output_tokens', 0) or 0
    USO[uso][2] += 1
    return ''.join(getattr(b, 'text', '') or '' for b in (getattr(resp, 'content', None) or [])
                   if getattr(b, 'type', '') == 'text')

def so_json(txt):
    i, f = txt.find('['), txt.rfind(']')
    if i < 0 or f < 0:
        raise ValueError('sem array na resposta: ' + txt[:120])
    return json.loads(txt[i:f + 1])

SISTEMA_FICHA = """Você lê registros de apostas esportivas (texto livre, em português do Brasil) e devolve, para cada um, uma ficha estruturada que um programa vai usar para apurar o resultado sozinho. Precisão vale mais que cobertura: se a leitura não for segura, diga em "duvida".

ENTRADA: um array JSON; cada item é {"i","esporte","tipo","mercado","evento","entrada"}.
- "evento": normalmente "Time A x Time B" (o primeiro é o lado "a", o segundo o lado "b"). Pode vir abreviado ("Cavs x Hornets"), cortado com "...", ou ser só "Dupla", "Tripla", "Múltipla" quando as pernas são de jogos diferentes.
- "entrada": a seleção. Em múltiplas de jogos diferentes as pernas vêm separadas por " + ". Em "Criar aposta" (várias seleções do mesmo jogo) vêm separadas por vírgula.
- "mercado" e "tipo" ajudam, mas podem estar vazios ou errados; o texto da entrada manda.

SAÍDA: só um array JSON, um objeto por aposta, na mesma ordem, sem texto antes ou depois:
{"i": <i>, "pernas": [<perna>, ...], "duvida": "<vazio se a leitura é segura; senão, em poucas palavras, o que ficou ambíguo>"}

Cada perna (omita as chaves que não se aplicam):
- "esp": "futebol" | "nba" | "nhl" | "nfl" | "mma" | "outro". Basquete que não é NBA, hóquei que não é NHL, e-sports, tênis, vôlei, handebol etc. = "outro" (nesse caso a perna é só {"esp":"outro"}).
- "fem": true se for futebol feminino (marcas como (F), [W], (w), (Fem), "Feminino").
- "a", "b": os dois lados do jogo, na ordem do evento, com o nome em inglês do jeito que um site internacional de placar escreve (ex.: "Sevilha"→"Sevilla", "Inter de Milão"→"Internazionale", "Colônia"→"FC Cologne", "Cavs"→"Cleveland Cavaliers", "PSG"→"Paris Saint-Germain"). Tire marcas de feminino do nome. Se a perna só cita um time (múltipla de jogos diferentes), ponha esse time em "a" e não mande "b". Se a perna é de jogador e o evento não diz o jogo, não mande "a" nem "b".
- "tipo":
  "1x2" = resultado no tempo regulamentar (lado "a", "b" ou "empate"). No futebol, "ML", "Resultado Final", "Vencedor", "Moneyline" de um time é "1x2".
  "vencedor" = quem ganha contando prorrogação (ML de NBA, NHL, NFL; luta de MMA). Lado "a" ou "b".
  "empate_anula" = lado "a" ou "b"; empate devolve.
  "dupla_chance" = lado "a_ou_empate", "b_ou_empate" ou "a_ou_b".
  "handicap" = lado "a" ou "b" e "linha" com sinal, do ponto de vista do lado apostado (ex.: -1.5, 0, 0.25, 3.5). Duas linhas juntas ("-1.5, -2.0") viram a média (-1.75).
  "total" = mais/menos de uma contagem: lado "over" ou "under", "linha", "met" e, se for de um time só, "time": "a" ou "b".
  "ambos_marcam" = lado "sim" ou "nao".
  "jogador" = estatística de um jogador: "jog" (nome completo), "met", lado "over" ou "under", "linha". Marcar a qualquer momento = met "gols", over, linha 0.5.
  "outro" = qualquer mercado que não cabe acima (método de vitória, placar exato, intervalo/final, primeiro a marcar, etc.).
- "met": o que se conta. Use: "gols", "pontos", "cartoes", "escanteios", "chutes", "chutes_no_gol", "faltas", "impedimentos", "rebotes", "assistencias", "triplos", "roubos", "tocos", "turnovers", "recepcoes", "jardas_recebidas", "jardas_corridas", "jardas_passe", "defesas". Soma de estatísticas numa linha só: junte com "+", ex.: "pontos+rebotes+assistencias". Se não estiver na lista, escreva um nome curto. Em handicap e total, se omitido vale gols (futebol, hóquei) ou pontos (NBA, NFL).
- "per": "jogo" (padrão, pode omitir) | "1t" | "2t" (tempo ou metade) | "q1".."q4" (quarto) | "1p".."3p" (período do hóquei).
- "regra": "pa" quando a entrada diz "com PA", "pagamento antecipado", "2 gols de vantagem" ou parecido.

CONVENÇÕES DO TEXTO
- "Over", "Mais de", "Acima", "X+" e "+X" antes de uma estatística ("+6.5 Assistências", "12.5+ Pontos") = over. "Under", "Menos de", "Abaixo" e "-X" antes de uma estatística ("-9.5 Pontos") = under.
- Em handicap, o número depois do time é a linha dele: "Time -1", "Time (+3.5)"; "Time (3.5)" = +3.5. "1", "H1", "Casa" = lado "a"; "2", "H2", "Fora" = lado "b". "H1 (0)" = handicap do lado "a", linha 0.
- "Jogador Acima 10.5 Pontos, Acima 3.5 Rebotes, Acima 1.5 Assistências" são TRÊS pernas (cada estatística tem a sua linha; a palavra "Acima" pode aparecer só na primeira). "Jogador +19.5 Pontos, Rebotes e Assistências" ou "Acima 43.5 Pontos + Assistências + Rebotes" é UMA perna com a soma.
- Vírgula decimal vale como ponto ("6,5" = 6.5).
- Se a entrada cita um time e um total ("Kilmarnock Over 0.5" no mercado Gols), é total daquele time.
- Mercado diz handicap mas a entrada não tem linha nenhuma (só o nome do time, ou "Time com PA"): leia como "1x2" e explique em "duvida".

EXEMPLOS
Entrada:
[{"i":1,"esporte":"Futebol","tipo":"Simples","mercado":"Resultado Final","evento":"Cuiaba x Juventude","entrada":"Juventude"},
 {"i":2,"esporte":"Futebol","tipo":"Simples","mercado":"Gols","evento":"Atalanta x Lecce","entrada":"Over 3.75"},
 {"i":3,"esporte":"Futebol","tipo":"Simples","mercado":"Gols","evento":"Celtic x Kilmarnock","entrada":"Kilmarnock Over 0.5"},
 {"i":4,"esporte":"Futebol","tipo":"Simples","mercado":"Handicap Asiático","evento":"Rosenborg x IK Start","entrada":"Rosenborg -1.5, -2.0"},
 {"i":5,"esporte":"Futebol Feminino","tipo":"Simples","mercado":"Handicap Asiático","evento":"Lyon (F) x Chelsea [W]","entrada":"H1 (0)"},
 {"i":6,"esporte":"NBA","tipo":"Simples","mercado":"Handicap Asiático","evento":"Cleveland Cavaliers x Philadelphia 76ers","entrada":"Philadelphia 76ers (3.5)"},
 {"i":7,"esporte":"Futebol","tipo":"Simples","mercado":"Cartões","evento":"Veneza x Udinese","entrada":"Mais de 3.5"},
 {"i":8,"esporte":"Futebol","tipo":"Simples","mercado":"Chance Dupla","evento":"FC Nantes x Toulouse FC","entrada":"empate ou Toulouse FC"},
 {"i":9,"esporte":"NBA","tipo":"Simples","mercado":"Pontos + Rebotes + Assistências","evento":"Pacers x Mavericks","entrada":"Ivica Zubac +19.5 Pontos, Rebotes e Assistências"},
 {"i":10,"esporte":"NBA","tipo":"Criar aposta","mercado":"Múltiplos","evento":"Houston Rockets x Golden State ...","entrada":"Georges Niang Acima 10.5 Pontos, Acima 3.5 Rebotes, Acima 1.5 Assistências"},
 {"i":11,"esporte":"NBA","tipo":"Dupla","mercado":"Múltiplos","evento":"Dupla","entrada":"Ace Bailey Acima 17.5 Pontos, 6.5 Rebotes + Julius Randle 24.5+ Pontos"},
 {"i":12,"esporte":"Futebol","tipo":"Dupla","mercado":"Resultado Final","evento":"Dupla","entrada":"Bournemouth + Fenerbahçe"},
 {"i":13,"esporte":"Futebol","tipo":"Simples","mercado":"Resultado Final","evento":"CD Pamplona x Eibar","entrada":"Eibar 1º Tempo"},
 {"i":14,"esporte":"Futebol","tipo":"Simples","mercado":"Handicap Asiático","evento":"Leeds United x Tottenham Hotspur","entrada":"Tottenham Hotspur com PA"},
 {"i":15,"esporte":"Futebol","tipo":"Simples","mercado":"Anytimes","evento":"SK Sturm Graz x NK Celje","entrada":"Oumar Diakite"},
 {"i":16,"esporte":"NHL","tipo":"Simples","mercado":"ML","evento":"Capitals x Senators","entrada":"Capitals"},
 {"i":17,"esporte":"UFC","tipo":"Simples","mercado":"Método de vitória","evento":"Jon Jones x Tom Aspinall","entrada":"Aspinall por KO ou submissão"},
 {"i":18,"esporte":"Basquete","tipo":"Simples","mercado":"ML","evento":"Flamengo x Franca","entrada":"Franca"}]
Saída:
[{"i":1,"pernas":[{"esp":"futebol","a":"Cuiabá","b":"Juventude","tipo":"1x2","lado":"b"}],"duvida":""},
 {"i":2,"pernas":[{"esp":"futebol","a":"Atalanta","b":"Lecce","tipo":"total","met":"gols","lado":"over","linha":3.75}],"duvida":""},
 {"i":3,"pernas":[{"esp":"futebol","a":"Celtic","b":"Kilmarnock","tipo":"total","met":"gols","time":"b","lado":"over","linha":0.5}],"duvida":""},
 {"i":4,"pernas":[{"esp":"futebol","a":"Rosenborg","b":"IK Start","tipo":"handicap","lado":"a","linha":-1.75}],"duvida":""},
 {"i":5,"pernas":[{"esp":"futebol","fem":true,"a":"Lyon","b":"Chelsea","tipo":"handicap","lado":"a","linha":0}],"duvida":""},
 {"i":6,"pernas":[{"esp":"nba","a":"Cleveland Cavaliers","b":"Philadelphia 76ers","tipo":"handicap","lado":"b","linha":3.5}],"duvida":""},
 {"i":7,"pernas":[{"esp":"futebol","a":"Venezia","b":"Udinese","tipo":"total","met":"cartoes","lado":"over","linha":3.5}],"duvida":""},
 {"i":8,"pernas":[{"esp":"futebol","a":"Nantes","b":"Toulouse","tipo":"dupla_chance","lado":"b_ou_empate"}],"duvida":""},
 {"i":9,"pernas":[{"esp":"nba","a":"Indiana Pacers","b":"Dallas Mavericks","tipo":"jogador","jog":"Ivica Zubac","met":"pontos+rebotes+assistencias","lado":"over","linha":19.5}],"duvida":""},
 {"i":10,"pernas":[{"esp":"nba","a":"Houston Rockets","b":"Golden State Warriors","tipo":"jogador","jog":"Georges Niang","met":"pontos","lado":"over","linha":10.5},{"esp":"nba","a":"Houston Rockets","b":"Golden State Warriors","tipo":"jogador","jog":"Georges Niang","met":"rebotes","lado":"over","linha":3.5},{"esp":"nba","a":"Houston Rockets","b":"Golden State Warriors","tipo":"jogador","jog":"Georges Niang","met":"assistencias","lado":"over","linha":1.5}],"duvida":""},
 {"i":11,"pernas":[{"esp":"nba","tipo":"jogador","jog":"Ace Bailey","met":"pontos","lado":"over","linha":17.5},{"esp":"nba","tipo":"jogador","jog":"Ace Bailey","met":"rebotes","lado":"over","linha":6.5},{"esp":"nba","tipo":"jogador","jog":"Julius Randle","met":"pontos","lado":"over","linha":24.5}],"duvida":""},
 {"i":12,"pernas":[{"esp":"futebol","a":"AFC Bournemouth","tipo":"1x2","lado":"a"},{"esp":"futebol","a":"Fenerbahce","tipo":"1x2","lado":"a"}],"duvida":""},
 {"i":13,"pernas":[{"esp":"futebol","a":"CD Pamplona","b":"Eibar","tipo":"1x2","per":"1t","lado":"b"}],"duvida":""},
 {"i":14,"pernas":[{"esp":"futebol","a":"Leeds United","b":"Tottenham Hotspur","tipo":"1x2","lado":"b","regra":"pa"}],"duvida":"mercado diz handicap, mas a entrada não tem linha; li como vitória do Tottenham com pagamento antecipado"},
 {"i":15,"pernas":[{"esp":"futebol","a":"Sturm Graz","b":"NK Celje","tipo":"jogador","jog":"Oumar Diakite","met":"gols","lado":"over","linha":0.5}],"duvida":""},
 {"i":16,"pernas":[{"esp":"nhl","a":"Washington Capitals","b":"Ottawa Senators","tipo":"vencedor","lado":"a"}],"duvida":""},
 {"i":17,"pernas":[{"esp":"mma","a":"Jon Jones","b":"Tom Aspinall","tipo":"outro"}],"duvida":""},
 {"i":18,"pernas":[{"esp":"outro"}],"duvida":""}]"""

SISTEMA_CASA = """Você casa apostas esportivas com jogos de uma lista. Para cada perna, diga qual jogo da lista é o jogo da aposta, ou null se ele não está na lista.
Só case com certeza de que é o MESMO jogo: mesmos times (traduções, apelidos e abreviações valem), mesma categoria. Não case time principal com sub-20, sub-23, B ou reservas, nem masculino com feminino (olhe a liga). Se a perna cita só um time, case com o jogo desse time, desde que só exista um na lista. Na dúvida, null. A maioria das pernas pode não estar na lista.

ENTRADA: {"jogos": ["id | visitante at mandante | liga | data"], "pernas": [{"k","data","evento","entrada","a","b","fem"}]}
SAÍDA: só um array JSON, um item por perna: [{"k": <k>, "id": "<id>" ou null, "a_e": "casa" ou "fora"}]. "a_e" diz se o time "a" da perna (ou o único time citado) é o mandante ("casa") ou o visitante ("fora") do jogo casado."""

def faz_fichas(cli, apostas):
    """apostas: lista de dicts com 'n' (índice). Devolve {n: ficha}."""
    lotes = [apostas[i:i + LOTE] for i in range(0, len(apostas), LOTE)]
    fichas = {}

    def um(lote):
        entrada = [{'i': a['n'], 'esporte': a['esporte'], 'tipo': a.get('tipo_aposta') or '', 'mercado': a.get('mercado') or '',
                    'evento': a.get('evento') or '', 'entrada': a.get('entrada') or ''} for a in lote]
        erro = None
        for _ in range(2):
            try:
                arr = so_json(pergunta(cli, SISTEMA_FICHA, json.dumps(entrada, ensure_ascii=False), 7000, 'ficha'))
                return {int(f['i']): f for f in arr if isinstance(f, dict) and 'i' in f}
            except Exception as e:
                erro = e
                time.sleep(2)
        log('E|ficha', repr(erro)[:300])
        return {}

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(um, lotes):
            fichas.update(r)
    return fichas


# ------------------------------------------------------------------ fonte de placar
SES = requests.Session()
SES.headers['User-Agent'] = 'rm-apurador-piloto/0.1 (python-requests)'

def espn(url):
    for tent in range(3):
        try:
            r = SES.get(url, timeout=30)
            N_REQ[0] += 1
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 404):
                return None
        except Exception:
            pass
        time.sleep(1.5 * (tent + 1))
    return None

def inteiro(v):
    try:
        return int(float(v))
    except Exception:
        return None

def normaliza(esp, e, d_espn):
    out = []
    comps = e.get('competitions') or []
    if esp != 'mma':
        comps = comps[:1]
    for c in comps:
        try:
            cs = c.get('competitors') or []
            if len(cs) != 2:
                continue
            times = []
            for x in cs:
                t = x.get('team') or x.get('athlete') or {}
                nomes = [t.get(k) for k in ('displayName', 'shortDisplayName', 'name', 'fullName') if t.get(k)]      # sem 'location': cidade sozinha confunde (Los Angeles, Manchester)
                linhas = []
                for l in (x.get('linescores') or []):
                    v = l.get('value') if isinstance(l, dict) else None
                    if v is None and isinstance(l, dict):
                        v = l.get('displayValue')
                    linhas.append(inteiro(v))
                sc = x.get('score')
                times.append({'id': str(x.get('id') or t.get('id') or ''), 'nomes': nomes,
                              'vn': sorted({norm(n) for n in nomes} - {''}),
                              'abrev': (t.get('abbreviation') or '').lower(),
                              'casa': x.get('homeAway') == 'home',
                              'placar': inteiro(sc.get('value') if isinstance(sc, dict) else sc),
                              'linhas': linhas, 'venceu': x.get('winner')})
            tipo = ((c.get('status') or e.get('status') or {}).get('type') or {})
            m = re.match(r'(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)', c.get('date') or e.get('date') or '')
            if not m:
                continue
            utc = datetime(*[int(g) for g in m.groups()], tzinfo=timezone.utc)
            out.append({'esp': esp, 'id': str(c.get('id') or e.get('id')), 'ev_id': str(e.get('id')),
                        'liga': (e.get('season') or {}).get('slug') or '', 'utc': utc,
                        'dia_brt': utc.astimezone(BRT).date(), 'dia_espn': d_espn,
                        'status': tipo.get('name') or '', 'completo': bool(tipo.get('completed')),
                        'times': times, 'detalhes': c.get('details') or []})
        except Exception:
            log('E|normaliza', traceback.format_exc()[-300:].replace('\n', ' / '))
    return out

PLACARES, RESUMOS = {}, {}

def jogos_do_dia(esp, d):
    k = (esp, d.strftime('%Y%m%d'))
    if k not in PLACARES:
        j = espn(f'{ESPN}/{CAM[esp]}/scoreboard?dates={k[1]}&limit=900') or {}
        PLACARES[k] = [x for e in (j.get('events') or []) for x in normaliza(esp, e, d)]
        time.sleep(0.12)
    return PLACARES[k]

def candidatos(esp, D):
    vistos = {}
    for dd in (0, -1, 1):
        for j in jogos_do_dia(esp, D + timedelta(days=dd)):
            vistos.setdefault(j['id'], j)
    return [j for j in vistos.values() if abs((j['dia_brt'] - D).days) <= 1]

def enxuga(esp, r):
    """Guarda do resumo do jogo só o que a apuração usa."""
    out = {'times': {}, 'jogadores': [], 'linhas': {}, 'fonte_box': None}
    box = r.get('boxscore') or {}
    for t in (box.get('teams') or []):
        tid = str((t.get('team') or {}).get('id'))
        out['times'][tid] = {s.get('name'): s.get('displayValue') for s in (t.get('statistics') or []) if isinstance(s, dict)}
    for t in (box.get('players') or []):                      # NBA, NHL, NFL
        tid = str((t.get('team') or {}).get('id'))
        for g in (t.get('statistics') or []):
            chaves = g.get('keys') or []
            for a in (g.get('athletes') or []):
                at = a.get('athlete') or {}
                out['jogadores'].append({'time': tid, 'grupo': g.get('name') or '',
                                         'nomes': [n for n in (at.get('displayName'), at.get('fullName')) if n],
                                         'fora': bool(a.get('didNotPlay')) or not (a.get('stats') or []),
                                         'stats': dict(zip(chaves, a.get('stats') or []))})
    for t in (r.get('rosters') or []):                        # futebol
        tid = str((t.get('team') or {}).get('id'))
        for a in (t.get('roster') or []):
            at = a.get('athlete') or {}
            st = {s.get('name'): s.get('value') for s in (a.get('stats') or []) if isinstance(s, dict)}
            jogou = bool(a.get('starter')) or bool(a.get('subbedIn')) or (st.get('appearances') or 0) >= 1
            out['jogadores'].append({'time': tid, 'grupo': 'futebol',
                                     'nomes': [n for n in (at.get('displayName'), at.get('fullName')) if n],
                                     'fora': not jogou, 'stats': st, 'tem_stats': bool(st)})
    hc = ((r.get('header') or {}).get('competitions') or [{}])[0]
    out['fonte_box'] = hc.get('boxscoreSource')
    for c in (hc.get('competitors') or []):
        out['linhas'][str(c.get('id'))] = [inteiro(l.get('displayValue')) for l in (c.get('linescores') or []) if isinstance(l, dict)]
    return out

def resumo(jogo):
    k = (jogo['esp'], jogo['ev_id'])
    if k not in RESUMOS:
        r = espn(f"{ESPN}/{CAM[jogo['esp']]}/summary?event={jogo['ev_id']}")
        RESUMOS[k] = enxuga(jogo['esp'], r) if isinstance(r, dict) else None
        time.sleep(0.12)
    return RESUMOS[k]

RE_FEM = re.compile(r'women|nwsl|femen|femin|frauen|damall|liga-f|wsl|she-?believes', re.I)

def eh_fem(j):
    return bool(RE_FEM.search(j.get('liga') or ''))

def sim_time(n, t):
    if not n:
        return 0.0
    s = max([sim(n, v) for v in t['vn']] or [0.0])
    if t['abrev'] and n.replace(' ', '') == t['abrev']:
        s = max(s, 0.95)
    return s

def prioridade(j, D):
    if j['dia_brt'] == D:
        return 0
    if j['dia_espn'] == D:
        return 1
    return 2

def rotulo_jogo(j):
    t0, t1 = j['times']
    casa, fora = (t0, t1) if t0['casa'] else (t1, t0)
    return f"{(casa['nomes'] or ['?'])[0]} {casa['placar']}x{fora['placar']} {(fora['nomes'] or ['?'])[0]} [{j['status'].replace('STATUS_', '')} {j['dia_brt']}]"

def acha_jogo(p, D, fem):
    """Casa a perna com um jogo pelo nome dos times. Devolve (jogo, ia, motivo)."""
    esp = p['esp']
    a, b = norm(p.get('a')), norm(p.get('b'))
    cands = candidatos(esp, D)
    if esp == 'futebol':
        cands = [j for j in cands if eh_fem(j) == bool(fem)]
    achados = []
    for j in cands:
        t0, t1 = j['times']
        if a and b:
            d = min(sim_time(a, t0), sim_time(b, t1))
            i = min(sim_time(a, t1), sim_time(b, t0))
            s, ia = (d, 0) if d >= i else (i, 1)
            if s >= 0.78:
                achados.append((prioridade(j, D), -s, j, ia))
        elif a or b:
            n = a or b
            s0, s1 = sim_time(n, t0), sim_time(n, t1)
            s, ix = (s0, 0) if s0 >= s1 else (s1, 1)
            if s >= 0.9:
                achados.append((prioridade(j, D), -s, j, ix if a else 1 - ix))
    if not achados:
        return None, None, 'sem_jogo'
    achados.sort(key=lambda x: (x[0], x[1]))
    top = achados[0]
    rivais = [x for x in achados[1:] if x[0] == top[0] and x[2]['id'] != top[2]['id'] and -x[1] >= -top[1] - 0.06]
    if rivais:
        return None, None, 'ambiguo'
    return top[2], top[3], None

def acha_jogador(nome, lista, com_nota=False):
    alvo = norm_p(nome)
    notas = {}
    for j in lista:
        s = max([sim_p(alvo, norm_p(n)) for n in j['nomes']] or [0.0])
        chave = norm_p((j['nomes'] or [''])[0])
        if s > notas.get(chave, 0):
            notas[chave] = s
    ordem = sorted(notas.items(), key=lambda x: -x[1])
    achou = None
    if ordem and ordem[0][1] >= 0.86 and not (len(ordem) > 1 and ordem[1][1] > ordem[0][1] - 0.04):
        achou = ordem[0]
    if com_nota:
        return achou or (None, 0.0)
    return achou[0] if achou else None

def acha_jogo_do_jogador(p, D):
    """Perna de jogador sem jogo no texto: procura o jogador nos jogos do dia."""
    esp = p['esp']
    if esp not in ('nba', 'nhl', 'nfl'):
        return None, None, 'sem_jogo'
    cands = candidatos(esp, D)
    for pr in (0, 1, 2):
        achou = []
        for j in [c for c in cands if prioridade(c, D) == pr]:
            r = resumo(j)
            if r:
                chave, nota = acha_jogador(p.get('jog'), r['jogadores'], True)
                if chave:
                    achou.append((nota, j))
        if achou:
            melhor = max(n for n, _ in achou)
            tops = [j for n, j in achou if n >= melhor - 0.001]
            if len(tops) == 1:
                return tops[0], 0, None
            return None, None, 'ambiguo'
    return None, None, 'sem_jogo'

def casa_pelo_modelo(cli, pendentes):
    """pendentes: [(chave, perna, aposta, D)]. Devolve {chave: (jogo, ia)} só para o que o modelo casou."""
    grupos = {}
    for chave, p, ap, D in pendentes:
        grupos.setdefault((p['esp'], D), []).append((chave, p, ap))
    achados = {}

    def um(item):
        (esp, D), itens = item
        cands = candidatos(esp, D)
        if not cands:
            return {}
        porid = {j['id']: j for j in cands}
        linhas = []
        for j in cands:
            t0, t1 = j['times']
            casa, fora = (t0, t1) if t0['casa'] else (t1, t0)
            linhas.append(f"{j['id']} | {(fora['nomes'] or ['?'])[0]} at {(casa['nomes'] or ['?'])[0]} | {j['liga']} | {j['dia_brt']}")
        pernas = [{'k': n, 'data': str(D), 'evento': ap.get('evento') or '', 'entrada': ap.get('entrada') or '',
                   'a': p.get('a'), 'b': p.get('b'), 'fem': bool(p.get('fem'))} for n, (_, p, ap) in enumerate(itens)]
        try:
            arr = so_json(pergunta(cli, SISTEMA_CASA, json.dumps({'jogos': linhas, 'pernas': pernas}, ensure_ascii=False), 3000, 'casa'))
        except Exception as e:
            log('E|casa', repr(e)[:300])
            return {}
        out = {}
        for r in arr:
            try:
                j = porid.get(str(r.get('id')))
                if not j:
                    continue
                chave = itens[int(r['k'])][0]
                casa_e = (r.get('a_e') or 'casa') == 'casa'
                ia = 0 if j['times'][0]['casa'] == casa_e else 1
                out[chave] = (j, ia)
            except Exception:
                pass
        return out

    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for r in ex.map(um, list(grupos.items())):
            achados.update(r)
    return achados


# ------------------------------------------------------------------ regras de apuração
class NaoSei(Exception):
    def __init__(self, classe, motivo):
        super().__init__(motivo)
        self.classe, self.motivo = classe, motivo

def meias(linha):
    return [linha] if round(linha * 4) % 2 == 0 else [linha - 0.25, linha + 0.25]

def junta(rs):
    if len(rs) == 1:
        return {'W': 'WON', 'L': 'LOST', 'P': 'VOID'}[rs[0]]
    return {'WW': 'WON', 'LL': 'LOST', 'PW': 'HALF WON', 'LP': 'HALF LOST', 'PP': 'VOID'}.get(''.join(sorted(rs)))

def cmp_total(valor, linha, lado):
    rs = []
    for l in meias(linha):
        d = valor - l
        rs.append('P' if abs(d) < 1e-9 else ('W' if (d > 0) == (lado == 'over') else 'L'))
    return junta(rs)

def cmp_handicap(meu, outro, linha):
    rs = []
    for l in meias(linha):
        d = meu + l - outro
        rs.append('P' if abs(d) < 1e-9 else ('W' if d > 0 else 'L'))
    return junta(rs)

REGULAR = {'nba': 4, 'nfl': 4, 'nhl': 3}

def placar(jogo, per, regulamentar):
    """(a0, a1) na ordem de jogo['times']."""
    esp, ts = jogo['esp'], jogo['times']
    fim = (ts[0]['placar'], ts[1]['placar'])
    if esp == 'futebol':
        normal = jogo['status'] == 'STATUS_FULL_TIME'
        if per == 'jogo' and normal:
            pass
        else:
            r = resumo(jogo) or {}
            ls = [r.get('linhas', {}).get(t['id']) or [] for t in ts]
            if any(len(l) < 2 or None in l[:2] for l in ls):
                raise NaoSei('sem_dado', 'sem placar por tempo')
            if per == 'jogo':
                fim = (ls[0][0] + ls[0][1], ls[1][0] + ls[1][1])      # 90 minutos de jogo com prorrogação
            elif per in ('1t', '2t'):
                k = 0 if per == '1t' else 1
                fim = (ls[0][k], ls[1][k])
            else:
                raise NaoSei('mercado', 'período ' + per)
    else:
        ls = [t['linhas'] for t in ts]
        n = REGULAR.get(esp)
        if per == 'jogo':
            if regulamentar:
                if not n or any(len(l) < n or None in l[:n] for l in ls):
                    raise NaoSei('sem_dado', 'sem placar por período')
                fim = (sum(ls[0][:n]), sum(ls[1][:n]))
        else:
            idx = {'q1': [0], 'q2': [1], 'q3': [2], 'q4': [3], '1p': [0], '2p': [1], '3p': [2],
                   '1t': [0, 1], '2t': [2, 3]}.get(per)
            if idx is None or (per in ('1t', '2t') and esp == 'nhl'):
                raise NaoSei('mercado', 'período ' + per)
            if any(len(l) <= max(idx) or None in [l[i] for i in idx] for l in ls):
                raise NaoSei('sem_dado', 'sem placar por período')
            if per == '2t' and n and any(len(l) > n for l in ls):
                raise NaoSei('regra_casa', '2ª metade com prorrogação')
            fim = (sum(ls[0][i] for i in idx), sum(ls[1][i] for i in idx))
    if fim[0] is None or fim[1] is None:
        raise NaoSei('sem_dado', 'sem placar')
    return fim

STAT_FUT = {'escanteios': 'wonCorners', 'chutes': 'totalShots', 'chutes_no_gol': 'shotsOnTarget', 'faltas': 'foulsCommitted',
            'impedimentos': 'offsides', 'defesas': 'saves'}

def stat_time(jogo, met):
    """Faixa (mínimo, máximo) da estatística de cada time, na ordem de jogo['times']. Cartão com vermelho vira faixa."""
    if jogo['esp'] != 'futebol':
        raise NaoSei('mercado', 'estatística de time: ' + str(met))
    r = resumo(jogo)
    if not r:
        raise NaoSei('sem_dado', 'sem resumo do jogo')
    out = []
    for t in jogo['times']:
        st = r['times'].get(t['id']) or {}
        if met == 'cartoes':
            y, v = num(st.get('yellowCards')), num(st.get('redCards'))
            if y is None or v is None:
                raise NaoSei('sem_dado', 'fonte sem cartões deste jogo')
            out.append((y, y + 2 * v))      # cada casa conta o vermelho de um jeito
        elif met in STAT_FUT:
            x = num(st.get(STAT_FUT[met]))
            if x is None:
                raise NaoSei('sem_dado', 'fonte sem ' + met + ' deste jogo')
            out.append((x, x))
        else:
            raise NaoSei('mercado', 'métrica ' + str(met))
    return out

MET_NBA = {'pontos': 'points', 'rebotes': 'rebounds', 'assistencias': 'assists', 'roubos': 'steals', 'tocos': 'blocks',
           'turnovers': 'turnovers', 'triplos': 'threePointFieldGoalsMade-threePointFieldGoalsAttempted'}
MET_FUT = {'gols': 'totalGoals', 'assistencias': 'goalAssists', 'chutes': 'totalShots', 'chutes_no_gol': 'shotsOnTarget',
           'faltas': 'foulsCommitted', 'faltas_cometidas': 'foulsCommitted', 'faltas_sofridas': 'foulsSuffered', 'defesas': 'saves'}
MET_NFL = {'recepcoes': ('receiving', 'receptions'), 'jardas_recebidas': ('receiving', 'receivingYards'),
           'jardas_corridas': ('rushing', 'rushingYards'), 'jardas_passe': ('passing', 'passingYards')}
MET_NHL = {'gols': 'goals', 'assistencias': 'assists', 'chutes': 'shotsTotal', 'chutes_no_gol': 'shotsTotal', 'defesas': 'saves'}

def stat_jogador(jogo, nome, met):
    """Valor da estatística do jogador no jogo, ou 'VOID' se ele não jogou."""
    esp = jogo['esp']
    r = resumo(jogo)
    if not r or not r['jogadores']:
        raise NaoSei('sem_dado', 'fonte sem jogadores deste jogo')
    chave = acha_jogador(nome, r['jogadores'])
    if not chave:
        raise NaoSei('sem_dado', 'jogador não achado: ' + str(nome))
    linhas = [j for j in r['jogadores'] if norm_p((j['nomes'] or [''])[0]) == chave]
    if all(j['fora'] for j in linhas):
        return 'VOID'
    total = 0.0
    for parte in str(met or '').split('+'):
        parte = parte.strip()
        if esp == 'nba':
            k = MET_NBA.get(parte)
            v = linhas[0]['stats'].get(k) if k else None
            if isinstance(v, str) and '-' in v and parte == 'triplos':
                v = v.split('-')[0]
        elif esp == 'futebol':
            if not linhas[0].get('tem_stats'):
                raise NaoSei('sem_dado', 'fonte sem números do jogador')
            k = MET_FUT.get(parte)
            v = linhas[0]['stats'].get(k) if k else None
        elif esp == 'nfl':
            g, k = MET_NFL.get(parte, (None, None))
            achou = [j for j in linhas if j['grupo'] == g]
            v = (achou[0]['stats'].get(k) if achou else 0) if g else None
        elif esp == 'nhl':
            if parte == 'pontos':
                v = (num(linhas[0]['stats'].get('goals')) or 0) + (num(linhas[0]['stats'].get('assists')) or 0) \
                    if 'goals' in linhas[0]['stats'] else None
            else:
                k = MET_NHL.get(parte)
                v = next((j['stats'].get(k) for j in linhas if k in j['stats']), None) if k else None
        else:
            v = None
        v = num(v)
        if v is None:
            raise NaoSei('mercado', 'métrica de jogador: ' + parte)
        total += v
    return total

def liderou_por_2(jogo, idx):
    """True/False se o time chegou a abrir 2 gols; None se a fonte não deixa saber."""
    if jogo['status'] != 'STATUS_FULL_TIME':
        return None
    gols = [d for d in jogo['detalhes'] if isinstance(d, dict) and d.get('scoringPlay') and not d.get('shootout')]
    ts = jogo['times']
    if len(gols) != (ts[0]['placar'] or 0) + (ts[1]['placar'] or 0) or any(g.get('ownGoal') for g in gols):
        return None
    s = [0, 0]
    ids = [t['id'] for t in ts]
    for g in gols:
        tid = str((g.get('team') or {}).get('id'))
        if tid not in ids:
            return None
        s[ids.index(tid)] += 1
        if s[idx] - s[1 - idx] >= 2:
            return True
    return False

def apura_perna(p, jogo, ia, ap):
    """Devolve (status, evidência). Levanta NaoSei quando não dá pra apurar com segurança."""
    esp, tipo = p['esp'], p.get('tipo')
    per, lado, met = p.get('per') or 'jogo', p.get('lado'), p.get('met')
    linha = num(p.get('linha')) if p.get('linha') is not None else None
    if not jogo['completo']:
        raise NaoSei('nao_terminou', jogo['status'])
    if esp == 'futebol' and jogo['status'] not in ('STATUS_FULL_TIME', 'STATUS_FINAL_AET', 'STATUS_FINAL_PEN'):
        raise NaoSei('nao_terminou', jogo['status'])
    ib = 1 - ia
    ev = rotulo_jogo(jogo)
    ao_vivo = (ap.get('tipo_aposta') or '').strip().lower() == 'live'

    if esp == 'mma':
        if tipo != 'vencedor' or lado not in ('a', 'b'):
            raise NaoSei('mercado', 'MMA: só vencedor da luta')
        eu, ele = jogo['times'][ia if lado == 'a' else ib], jogo['times'][ib if lado == 'a' else ia]
        if eu['venceu'] is True and ele['venceu'] is not True:
            return 'WON', ev
        if ele['venceu'] is True and eu['venceu'] is not True:
            return 'LOST', ev
        raise NaoSei('sem_dado', 'luta sem vencedor na fonte')

    if tipo in ('1x2', 'empate_anula', 'dupla_chance', 'ambos_marcam'):
        s = placar(jogo, per, True)
        ga, gb = s[ia], s[ib]
        ev += f' reg {ga}-{gb}' + ('' if per == 'jogo' else ' ' + per)
        if tipo == '1x2':
            if lado not in ('a', 'b', 'empate'):
                raise NaoSei('ficha', 'lado ' + str(lado))
            ganhou = (ga > gb) if lado == 'a' else (gb > ga) if lado == 'b' else (ga == gb)
            if not ganhou and p.get('regra') == 'pa' and lado in ('a', 'b') and esp == 'futebol' and per == 'jogo':
                meu = ga if lado == 'a' else gb
                if meu >= 2:
                    abriu = liderou_por_2(jogo, ia if lado == 'a' else ib)
                    if abriu is None:
                        raise NaoSei('regra_casa', 'pagamento antecipado: não deu pra ver se abriu 2 gols')
                    if abriu:
                        return 'WON', ev + ' (abriu 2 gols: pagamento antecipado)'
            return ('WON' if ganhou else 'LOST'), ev
        if tipo == 'empate_anula':
            if lado not in ('a', 'b'):
                raise NaoSei('ficha', 'lado ' + str(lado))
            if ga == gb:
                return 'VOID', ev
            return ('WON' if (ga > gb) == (lado == 'a') else 'LOST'), ev
        if tipo == 'dupla_chance':
            ok = {'a_ou_empate': ga >= gb, 'b_ou_empate': gb >= ga, 'a_ou_b': ga != gb}.get(lado)
            if ok is None:
                raise NaoSei('ficha', 'lado ' + str(lado))
            return ('WON' if ok else 'LOST'), ev
        ambos = ga > 0 and gb > 0
        if lado not in ('sim', 'nao'):
            raise NaoSei('ficha', 'lado ' + str(lado))
        return ('WON' if ambos == (lado == 'sim') else 'LOST'), ev

    if tipo == 'vencedor':
        if esp == 'futebol':
            raise NaoSei('mercado', 'futebol: classificação/prorrogação')
        if lado not in ('a', 'b'):
            raise NaoSei('ficha', 'lado ' + str(lado))
        s = placar(jogo, per, False)
        ga, gb = s[ia], s[ib]
        if ga == gb:
            raise NaoSei('regra_casa', 'empate no período/jogo em mercado de 2 lados')
        return ('WON' if (ga > gb) == (lado == 'a') else 'LOST'), ev

    padrao = 'gols' if esp in ('futebol', 'nhl') else 'pontos'
    if tipo == 'handicap':
        if linha is None or lado not in ('a', 'b'):
            raise NaoSei('ficha', 'handicap sem linha ou lado')
        if ao_vivo:
            raise NaoSei('regra_casa', 'handicap ao vivo depende do placar na hora da aposta')
        if (met or padrao) == padrao:
            s = placar(jogo, per, esp == 'futebol')
            f = [(s[0], s[0]), (s[1], s[1])]
        else:
            if per != 'jogo':
                raise NaoSei('mercado', 'estatística por período')
            f = stat_time(jogo, met)
        meu, outro = (f[ia], f[ib]) if lado == 'a' else (f[ib], f[ia])
        r1, r2 = cmp_handicap(meu[0], outro[1], linha), cmp_handicap(meu[1], outro[0], linha)
        if r1 != r2 or r1 is None:
            raise NaoSei('regra_casa', 'resultado depende de como a casa conta cartão vermelho')
        return r1, ev + f' hcp {linha:+g}'

    if tipo == 'total':
        if linha is None or lado not in ('over', 'under'):
            raise NaoSei('ficha', 'total sem linha ou lado')
        qual = p.get('time')
        if (met or padrao) == padrao:
            s = placar(jogo, per, esp == 'futebol')
            f = [(s[0], s[0]), (s[1], s[1])]
        else:
            if per != 'jogo':
                raise NaoSei('mercado', 'estatística por período')
            f = stat_time(jogo, met)
        if qual == 'a':
            lo, hi = f[ia]
        elif qual == 'b':
            lo, hi = f[ib]
        else:
            lo, hi = f[0][0] + f[1][0], f[0][1] + f[1][1]
        r1, r2 = cmp_total(lo, linha, lado), cmp_total(hi, linha, lado)
        if r1 != r2 or r1 is None:
            raise NaoSei('regra_casa', 'resultado depende de como a casa conta cartão vermelho')
        return r1, ev + f' {met or padrao}={lo:g}' + ('' if lo == hi else f'..{hi:g}') + f' {lado} {linha:g}'

    if tipo == 'jogador':
        if linha is None or lado not in ('over', 'under') or not p.get('jog'):
            raise NaoSei('ficha', 'jogador sem linha, lado ou nome')
        if per != 'jogo':
            raise NaoSei('mercado', 'jogador por período')
        v = stat_jogador(jogo, p['jog'], met)
        if v == 'VOID':
            return 'VOID', ev + f" {p['jog']} não jogou"
        return cmp_total(v, linha, lado), ev + f" {p['jog']} {met}={v:g} {lado} {linha:g}"

    raise NaoSei('mercado', 'tipo ' + str(tipo))

ORDEM_CLASSE = ['fora', 'ficha', 'sem_jogo', 'ambiguo', 'nao_terminou', 'mercado', 'sem_dado', 'regra_casa', 'erro']

def junta_pernas(res):
    """res: [(status|None, classe, texto)]. Devolve (status|None, classe, texto)."""
    textos = ' || '.join(t for _, _, t in res if t)
    sts = [s for s, _, _ in res]
    if len(res) == 1:
        return res[0][0], res[0][1], textos
    if 'LOST' in sts:
        return 'LOST', None, textos
    if None in sts:
        cl = sorted([c for s, c, _ in res if s is None], key=lambda c: ORDEM_CLASSE.index(c) if c in ORDEM_CLASSE else 99)[0]
        return None, cl, textos
    if any(s in ('HALF WON', 'HALF LOST') for s in sts):
        return None, 'regra_casa', textos + ' (múltipla com meia perna)'
    if all(s == 'VOID' for s in sts):
        return 'VOID', None, textos
    return 'WON', None, textos


# ------------------------------------------------------------------ principal
def main():
    t0 = time.time()
    apostas = carrega_apostas()
    cash = [a for a in apostas if a['status'] == 'CASH OUT']
    apostas = [a for a in apostas if a['status'] != 'CASH OUT']
    if MAXIMO and len(apostas) > MAXIMO:
        passo = len(apostas) / MAXIMO
        apostas = [apostas[int(i * passo)] for i in range(MAXIMO)]
    for n, a in enumerate(apostas):
        a['n'] = n
        a['D'] = date.fromisoformat(a['data_evento'])
    log(f'R|janela {DE} a {ATE} | fechadas {len(apostas)} | cash out fora da conta {len(cash)} | modelo {MODELO}')

    pro_modelo = [a for a in apostas if a['esporte'] in ROTULO]
    cli = cliente()
    fichas = faz_fichas(cli, pro_modelo)
    log(f'R|fichas pedidas {len(pro_modelo)} | recebidas {len(fichas)} | {time.time() - t0:.0f}s')

    # 1) casa cada perna com um jogo
    casados, pendentes = {}, []
    for a in pro_modelo:
        f = fichas.get(a['n'])
        if not f:
            continue
        fem_ap = a['esporte'] == 'Futebol Feminino'
        for k, p in enumerate(f.get('pernas') or []):
            if not isinstance(p, dict) or p.get('esp') not in CAM:
                continue
            try:
                if p.get('a') or p.get('b'):
                    j, ia, motivo = acha_jogo(p, a['D'], fem_ap or bool(p.get('fem')))
                    if j:
                        casados[(a['n'], k)] = (j, ia, 'nome')
                    else:
                        pendentes.append(((a['n'], k), p, a, a['D']))
                elif p.get('tipo') == 'jogador' and p.get('jog'):
                    j, ia, motivo = acha_jogo_do_jogador(p, a['D'])
                    if j:
                        casados[(a['n'], k)] = (j, ia, 'jogador')
                    else:
                        casados[(a['n'], k)] = (None, None, motivo)
                else:
                    casados[(a['n'], k)] = (None, None, 'sem_jogo')
            except Exception:
                log('E|casar', a['n'], traceback.format_exc()[-400:].replace('\n', ' / '))
    log(f'R|pernas casadas pelo nome {sum(1 for v in casados.values() if v[0])} | sem casar {len(pendentes)} | {time.time() - t0:.0f}s')
    if pendentes:
        extra = casa_pelo_modelo(cli, pendentes)
        for chave, p, a, D in pendentes:
            if chave in extra:
                casados[chave] = (extra[chave][0], extra[chave][1], 'modelo')
            else:
                casados[chave] = (None, None, 'sem_jogo')
        log(f'R|casadas pelo modelo {len(extra)} de {len(pendentes)} | {time.time() - t0:.0f}s')

    # 2) apura
    linhas = []
    for a in apostas:
        f = fichas.get(a['n'])
        como = set()
        if a['esporte'] not in ROTULO:
            st, cl, txt, duvida = None, 'fora', '', ''
        elif not f or not (f.get('pernas') or []):
            st, cl, txt, duvida = None, 'ficha', 'sem ficha', ''
        else:
            duvida = (f.get('duvida') or '').strip()
            res = []
            for k, p in enumerate(f['pernas']):
                if not isinstance(p, dict) or p.get('esp') not in CAM:
                    res.append((None, 'fora', 'esporte sem fonte'))
                    continue
                j, ia, c = casados.get((a['n'], k), (None, None, 'sem_jogo'))
                if not j:
                    res.append((None, c or 'sem_jogo', f"não achei: {p.get('a') or p.get('jog')} x {p.get('b')}"))
                    continue
                como.add(c)
                try:
                    s, ev = apura_perna(p, j, ia, a)
                    if s is None:
                        raise NaoSei('regra_casa', 'meia linha sem regra')
                    res.append((s, None, ev))
                except NaoSei as e:
                    res.append((None, e.classe, f'{rotulo_jogo(j)} → {e.motivo}'))
                except Exception:
                    res.append((None, 'erro', traceback.format_exc()[-300:].replace('\n', ' / ')))
            st, cl, txt = junta_pernas(res)
        a.update(robo=st, classe=cl, txt=txt, duvida=duvida, como='+'.join(sorted(como)), ficha=f)
        linhas.append(a)

    # 3) relatório
    def curto(s, n):
        s = re.sub(r'\s+', ' ', str(s or ''))
        return s if len(s) <= n else s[:n - 1] + '…'

    tot = len(linhas)
    auto = [a for a in linhas if a['robo']]
    certos = [a for a in auto if a['robo'] == a['status']]
    log(f'R|TOTAL {tot} | apurou sozinho {len(auto)} ({100 * len(auto) / max(tot, 1):.0f}%) | bateu com a marcação {len(certos)} | divergiu {len(auto) - len(certos)}')
    semd = [a for a in auto if not a['duvida']]
    comd = [a for a in auto if a['duvida']]
    log(f"R|leitura segura: {len(semd)} apuradas, {sum(1 for a in semd if a['robo'] == a['status'])} batem | leitura com dúvida: {len(comd)} apuradas, {sum(1 for a in comd if a['robo'] == a['status'])} batem")
    for nome, chave in (('classe', lambda a: a['classe'] or 'apurou'), ('esporte', lambda a: a['esporte']),
                        ('tipo_aposta', lambda a: a.get('tipo_aposta') or '(vazio)'),
                        ('mercado', lambda a: curto(a.get('mercado') or '(vazio)', 30)),
                        ('casou_por', lambda a: a['como'] or '-')):
        g = {}
        for a in linhas:
            x = g.setdefault(chave(a), [0, 0, 0])
            x[0] += 1
            if a['robo']:
                x[1] += 1
                x[2] += a['robo'] == a['status']
        for k, (n, au, ok) in sorted(g.items(), key=lambda x: -x[1][0]):
            log(f'R|{nome}|{k}|total {n}|apurou {au}|bateu {ok}|divergiu {au - ok}')
    for a in linhas:
        marca = '-' if not a['robo'] else ('ok' if a['robo'] == a['status'] else 'DIF')
        log('B|' + '|'.join(str(x) for x in (
            a['n'], a['data_evento'], a['esporte'], a.get('tipo_aposta') or '', curto(a.get('mercado'), 26), a['status'],
            a['robo'] or '-', marca, a['classe'] or '', 'duvida' if a['duvida'] else '', a['como'],
            curto(a.get('evento'), 44) + ' ~ ' + curto(a.get('entrada'), 90), curto(a['txt'], 260))))
    for a in linhas:
        if (a['robo'] and a['robo'] != a['status']) or a['duvida'] or a['classe'] in ('ficha', 'erro'):
            log('F|' + str(a['n']) + '|' + curto(a['duvida'], 160) + '|' + curto(json.dumps((a['ficha'] or {}).get('pernas'), ensure_ascii=False), 900))
    ligas = sorted({j['liga'] for (e, _), js in PLACARES.items() if e == 'futebol' for j in js})
    log('L|' + str(len(ligas)) + '|' + json.dumps(ligas))
    fontes = {}
    for r in RESUMOS.values():
        if r:
            fontes[str(r.get('fonte_box'))] = fontes.get(str(r.get('fonte_box')), 0) + 1
    log(f"R|uso do modelo: fichas {USO['ficha'][2]} chamadas, {USO['ficha'][0]} entrada, {USO['ficha'][1]} saída | casar {USO['casa'][2]} chamadas, {USO['casa'][0]} entrada, {USO['casa'][1]} saída")
    log(f'R|pedidos à fonte de placar {N_REQ[0]} | resumos {len(RESUMOS)} {json.dumps(fontes)} | tempo {time.time() - t0:.0f}s')
    log('@FIM')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log('E|fatal', traceback.format_exc()[-1500:].replace('\n', ' / '))
    sys.exit(0)
