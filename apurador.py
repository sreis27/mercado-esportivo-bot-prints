"""Apurador: acompanha as apostas e propõe o resultado (green/red) quando o jogo termina.

Roda sozinho, em ciclos. Para cada aposta do período: lê o texto e monta a ficha (modelo), acha o jogo numa
fonte pública de placar e, quando o jogo acaba, apura pela regra do mercado.

EM SOMBRA: o robô só escreve na tabela `apuracoes` (a proposta e a prova). Não altera nem apaga aposta nenhuma
e não manda mensagem pra lugar nenhum. Na dúvida, não propõe; e quando propõe com ressalva, diz qual.
"""
import os, re, sys, json, time, hashlib, unicodedata, traceback, concurrent.futures as cf
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
import requests

SB_URL = 'https://yfdrifvhsiumdxgypkjm.supabase.co'
SB_KEY = os.environ.get('SUPABASE_KEY', '')
MODELO = os.environ.get('MODELO_FICHA', 'claude-sonnet-5-5')
CICLO = int(os.environ.get('AP_CICLO', '300') or 300)          # segundos entre um ciclo e outro
ATRAS = int(os.environ.get('AP_DIAS_ATRAS', '4') or 4)         # olha apostas com data de até N dias atrás
FRENTE = 2                                                     # e até 2 dias à frente
LOTE = 12                                                      # apostas por chamada ao modelo
MAX_FICHAS = 120                                               # fichas novas por ciclo, no máximo
BRT = timezone(timedelta(hours=-3))
ESPN = 'https://site.api.espn.com/apis/site/v2/sports'
CAM = {'futebol': 'soccer/all', 'nba': 'basketball/nba', 'nhl': 'hockey/nhl', 'nfl': 'football/nfl', 'mma': 'mma/ufc'}
ROTULO = {'Futebol': 'futebol', 'Futebol Feminino': 'futebol', 'NBA': 'nba', 'NHL': 'nhl', 'NFL': 'nfl',
          'MMA': 'mma', 'UFC': 'mma', '(sem esporte)': None}
PULAR = {'015db6d4-9953-40c7-b29a-96b43a49f198'}               # aposta consolidada: o robô não olha
USO = {'ficha': [0, 0, 0, 0, 0], 'casa': [0, 0, 0, 0, 0]}      # entrada nova, saída, chamadas, cache lido, cache escrito
N_REQ = [0]


def agora_utc():
    return datetime.now(timezone.utc)

def log(*a):
    print(*a, flush=True)

def iso(d):
    return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if d else None

def de_iso(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


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
        USO[uso][0] += getattr(u, 'input_tokens', 0) or 0
        USO[uso][1] += getattr(u, 'output_tokens', 0) or 0
        USO[uso][3] += getattr(u, 'cache_read_input_tokens', 0) or 0
        USO[uso][4] += getattr(u, 'cache_creation_input_tokens', 0) or 0
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
- Não use o que você lembra de elenco: jogador troca de time. Não ponha dúvida por achar que o jogador não é de nenhum dos times do evento.
- O "esporte" informado pode estar errado. Se os times não combinam com ele (ex.: clubes de basquete da Euroliga ou do NBB marcados como Futebol), use o esporte certo e explique em "duvida".
- Em luta, se a perna cita só um lutador, ponha esse lutador em "a".

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
                        'liga': ' '.join(x for x in ((e.get('season') or {}).get('slug'), c.get('altGameNote')) if x), 'utc': utc,
                        'dia_brt': utc.astimezone(BRT).date(), 'dia_espn': d_espn,
                        'status': tipo.get('name') or '', 'completo': bool(tipo.get('completed')),
                        'estado': tipo.get('state') or '', 'detalhe': tipo.get('shortDetail') or tipo.get('detail') or '',
                        'times': times, 'detalhes': c.get('details') or []})
        except Exception:
            log('E|normaliza', traceback.format_exc()[-300:].replace('\n', ' / '))
    return out

PLACARES, RESUMOS = {}, {}      # PLACARES é limpo a cada ciclo; RESUMOS guarda (quando, dados)

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
    out = {'times': {}, 'jogadores': [], 'linhas': {}, 'fonte_box': None, 'parciais': []}
    for k in (r.get('keyEvents') or []):                     # placar corrido, do texto de cada gol
        if isinstance(k, dict) and k.get('scoringPlay') and not k.get('shootout'):
            m = re.search(r'(\d+), [^.]*? (\d+)\.', k.get('text') or '')
            out['parciais'].append((int(m.group(1)), int(m.group(2))) if m else None)
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
    """Resumo do jogo (estatísticas e jogadores). Jogo recente é relido a cada 10 min: os números ainda mudam."""
    k = (jogo['esp'], jogo['ev_id'])
    v = RESUMOS.get(k)
    validade = 600 if agora_utc() - jogo['utc'] < timedelta(hours=8) else 172800
    if v and time.time() - v[0] < validade:
        return v[1]
    r = espn(f"{ESPN}/{CAM[jogo['esp']]}/summary?event={jogo['ev_id']}")
    d = enxuga(jogo['esp'], r) if isinstance(r, dict) else None
    if len(RESUMOS) > 500:
        for kk in sorted(RESUMOS, key=lambda x: RESUMOS[x][0])[:200]:
            RESUMOS.pop(kk, None)
    RESUMOS[k] = (time.time(), d)
    time.sleep(0.12)
    return d

RE_FEM = re.compile(r'women|nwsl|femen|femin|frauen|damen|dames|vrouwen|kvinn|ladies|damall|liga-f|wsl|she-?believes', re.I)

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
            if s >= 0.86:          # igual, contido ou quase igual; parecido de longe (Clippers x Lakers) não vale
                achados.append((prioridade(j, D), -s, j, ia))
        elif a or b:
            n = a or b
            s0, s1 = sim_time(n, t0), sim_time(n, t1)
            s, ix = (s0, 0) if s0 >= s1 else (s1, 1)
            if s >= 0.9 and prioridade(j, D) <= 1:      # um time só é prova fraca: só vale jogo do próprio dia
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
    """Perna de jogador sem jogo no texto: procura o jogador nos jogos do dia que já terminaram."""
    esp = p['esp']
    if esp not in ('nba', 'nhl', 'nfl'):
        return None, None, 'sem_jogo'
    cands = candidatos(esp, D)
    for pr in (0, 1, 2):
        grupo = [c for c in cands if prioridade(c, D) == pr]
        achou = []
        for j in grupo:
            if not j['completo']:
                continue
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
        if pr <= 1 and any(not j['completo'] for j in grupo):
            return None, None, 'aguarda'          # o jogo dele pode ser um dos que ainda não acabaram
    return None, None, 'sem_jogo'

def casa_pelo_modelo(cli, pendentes):
    """pendentes: [(chave, perna, aposta, D)]. Devolve {chave: (jogo, ia)} só para o que o modelo casou."""
    grupos = {}
    for chave, p, ap, D in pendentes:
        grupos.setdefault((p['esp'], D), []).append((chave, p, ap))
    achados = {}

    def um(item):
        (esp, D), itens = item
        cands = [j for j in candidatos(esp, D) if prioridade(j, D) <= 1]
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
    """Faixa (mínimo, máximo) da estatística de cada time, na ordem de jogo['times'].
    Cartão com vermelho vira faixa: cada casa conta o vermelho e o segundo amarelo de um jeito."""
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
            out.append((max(y - v, 0), y + 2 * v))
        elif met in STAT_FUT:
            x = num(st.get(STAT_FUT[met]))
            if x is None:
                raise NaoSei('sem_dado', 'fonte sem ' + met + ' deste jogo')
            out.append((x, x))
        else:
            raise NaoSei('mercado', 'métrica ' + str(met))
    return out

# contagem que varia de uma casa pra outra: perto da linha o robô propõe, mas não fecha sozinho
FOLGA = {'cartoes': 1.0, 'escanteios': 1.0, 'chutes_no_gol': 1.0, 'impedimentos': 1.0, 'defesas': 1.0, 'chutes': 2.0, 'faltas': 2.0}

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
    """True/False se o time chegou a abrir 2 gols de vantagem; None se a fonte não deixa saber."""
    if jogo['status'] != 'STATUS_FULL_TIME':
        return None
    ts = jogo['times']
    fim = (ts[0]['placar'] or 0, ts[1]['placar'] or 0)
    # 1) pela lista de gols do placar (quando não tem gol contra)
    gols = [d for d in jogo['detalhes'] if isinstance(d, dict) and d.get('scoringPlay') and not d.get('shootout')]
    if len(gols) == sum(fim) and not any(g.get('ownGoal') for g in gols):
        s, ids, ok = [0, 0], [t['id'] for t in ts], True
        for g in gols:
            tid = str((g.get('team') or {}).get('id'))
            if tid not in ids:
                ok = False
                break
            s[ids.index(tid)] += 1
            if s[idx] - s[1 - idx] >= 2:
                return True
        if ok and tuple(s) == fim:
            return False
    # 2) pelo placar corrido no texto de cada gol ("Goal! Casa 1, Fora 0."), conferindo se fecha com o final
    par = (resumo(jogo) or {}).get('parciais') or []
    if not par or None in par or len(par) != sum(fim):
        return None
    casa = 0 if ts[0]['casa'] else 1
    ant = (0, 0)
    for p in par:
        if sum(p) != sum(ant) + 1 or p[0] < ant[0] or p[1] < ant[1]:
            return None
        ant = p
    if tuple(ant) != (ts[casa]['placar'], ts[1 - casa]['placar']):
        return None
    meu = 0 if idx == casa else 1
    return any(p[meu] - p[1 - meu] >= 2 for p in par)

def apura_perna(p, jogo, ia, ap):
    """Devolve (status, prova, ressalva). Levanta NaoSei quando não dá pra apurar com segurança."""
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
            return 'WON', ev, None
        if ele['venceu'] is True and eu['venceu'] is not True:
            return 'LOST', ev, None
        raise NaoSei('sem_dado', 'luta sem vencedor na fonte')

    if tipo in ('1x2', 'empate_anula', 'dupla_chance', 'ambos_marcam'):
        s = placar(jogo, per, True)
        ga, gb = s[ia], s[ib]
        ev += f' reg {ga}-{gb}' + ('' if per == 'jogo' else ' ' + per)
        if tipo == '1x2':
            if lado not in ('a', 'b', 'empate'):
                raise NaoSei('ficha', 'lado ' + str(lado))
            ganhou = (ga > gb) if lado == 'a' else (gb > ga) if lado == 'b' else (ga == gb)
            if not ganhou and lado in ('a', 'b') and esp == 'futebol' and per == 'jogo' and (ga if lado == 'a' else gb) >= 2:
                abriu = liderou_por_2(jogo, ia if lado == 'a' else ib)
                if p.get('regra') == 'pa':
                    if abriu is None:
                        raise NaoSei('regra_casa', 'pagamento antecipado: não deu pra ver se abriu 2 gols')
                    if abriu:
                        return 'WON', ev + ' (abriu 2 gols: pagamento antecipado)', None
                elif abriu is not False:
                    return 'LOST', ev, 'abriu 2 gols de vantagem e não ganhou: se a casa paga antecipado, é green'
            return ('WON' if ganhou else 'LOST'), ev, None
        if tipo == 'empate_anula':
            if lado not in ('a', 'b'):
                raise NaoSei('ficha', 'lado ' + str(lado))
            if ga == gb:
                return 'VOID', ev, None
            return ('WON' if (ga > gb) == (lado == 'a') else 'LOST'), ev, None
        if tipo == 'dupla_chance':
            ok = {'a_ou_empate': ga >= gb, 'b_ou_empate': gb >= ga, 'a_ou_b': ga != gb}.get(lado)
            if ok is None:
                raise NaoSei('ficha', 'lado ' + str(lado))
            return ('WON' if ok else 'LOST'), ev, None
        if lado not in ('sim', 'nao'):
            raise NaoSei('ficha', 'lado ' + str(lado))
        return ('WON' if (ga > 0 and gb > 0) == (lado == 'sim') else 'LOST'), ev, None

    if tipo == 'vencedor':
        if esp == 'futebol':
            raise NaoSei('mercado', 'futebol: classificação/prorrogação')
        if lado not in ('a', 'b'):
            raise NaoSei('ficha', 'lado ' + str(lado))
        s = placar(jogo, per, False)
        ga, gb = s[ia], s[ib]
        if ga == gb:
            raise NaoSei('regra_casa', 'empate no período/jogo em mercado de 2 lados')
        return ('WON' if (ga > gb) == (lado == 'a') else 'LOST'), ev, None

    padrao = 'gols' if esp in ('futebol', 'nhl') else 'pontos'
    if tipo in ('handicap', 'total'):
        if linha is None:
            raise NaoSei('ficha', tipo + ' sem linha')
        if tipo == 'handicap' and lado not in ('a', 'b'):
            raise NaoSei('ficha', 'handicap sem lado')
        if tipo == 'total' and lado not in ('over', 'under'):
            raise NaoSei('ficha', 'total sem lado')
        if tipo == 'handicap' and ao_vivo:
            raise NaoSei('regra_casa', 'handicap ao vivo depende do placar na hora da aposta')
        de_placar = (met or padrao) == padrao
        if de_placar:
            s = placar(jogo, per, esp == 'futebol')
            f = [(s[0], s[0]), (s[1], s[1])]
        else:
            if per != 'jogo':
                raise NaoSei('mercado', 'estatística por período')
            f = stat_time(jogo, met)
        if tipo == 'handicap':
            meu, outro = (f[ia], f[ib]) if lado == 'a' else (f[ib], f[ia])
            r1, r2 = cmp_handicap(meu[0], outro[1], linha), cmp_handicap(meu[1], outro[0], linha)
            folga = min(abs(meu[0] + linha - outro[1]), abs(meu[1] + linha - outro[0]))
            prova = ev + f' hcp {linha:+g}' + ('' if de_placar else f' {met}')
        else:
            qual = p.get('time')
            if qual == 'a':
                lo, hi = f[ia]
            elif qual == 'b':
                lo, hi = f[ib]
            else:
                lo, hi = f[0][0] + f[1][0], f[0][1] + f[1][1]
            r1, r2 = cmp_total(lo, linha, lado), cmp_total(hi, linha, lado)
            folga = min(abs(lo - linha), abs(hi - linha))
            prova = ev + f' {met or padrao}={lo:g}' + ('' if lo == hi else f'..{hi:g}') + f' {lado} {linha:g}'
        if r1 != r2 or r1 is None:
            raise NaoSei('regra_casa', 'o resultado depende de como a casa conta cartão vermelho')
        ressalva = None
        if not de_placar and folga < FOLGA.get(met, 1.0):
            ressalva = f'{met} a {folga:g} da linha: a contagem da casa pode ser outra'
        return r1, prova, ressalva

    if tipo == 'jogador':
        if linha is None or lado not in ('over', 'under') or not p.get('jog'):
            raise NaoSei('ficha', 'jogador sem linha, lado ou nome')
        if per != 'jogo':
            raise NaoSei('mercado', 'jogador por período')
        v = stat_jogador(jogo, p['jog'], met)
        if v == 'VOID':
            return 'VOID', ev + f" {p['jog']} não jogou", 'jogador fora do jogo: confere se a casa devolveu'
        ressalva = None
        if esp == 'futebol' and met != 'gols' and abs(v - linha) < 1.0:
            ressalva = f'{met} do jogador a {abs(v - linha):g} da linha: a contagem da casa pode ser outra'
        return cmp_total(v, linha, lado), ev + f" {p['jog']} {met}={v:g} {lado} {linha:g}", ressalva

    raise NaoSei('mercado', 'tipo ' + str(tipo))

ORDEM_CLASSE = ['fora', 'ficha', 'sem_jogo', 'ambiguo', 'aguarda', 'nao_terminou', 'mercado', 'sem_dado', 'regra_casa', 'erro']

def junta_pernas(res):
    """res: [(status|None, classe, texto, ressalva)]. Devolve (status|None, classe, texto, ressalvas)."""
    textos = ' || '.join(t for _, _, t, _ in res if t)
    sts = [s for s, _, _, _ in res]
    if len(res) == 1:
        return res[0][0], res[0][1], textos, ([res[0][3]] if res[0][3] else [])
    perdidas = [r for r in res if r[0] == 'LOST']
    if perdidas:                                    # uma perna perdida decide a múltipla
        firmes = [r for r in perdidas if not r[3]]
        return 'LOST', None, textos, ([] if firmes else [r[3] for r in perdidas])
    if None in sts:
        cl = sorted([c for s, c, _, _ in res if s is None], key=lambda c: ORDEM_CLASSE.index(c) if c in ORDEM_CLASSE else 99)[0]
        return None, cl, textos, []
    if any(s in ('HALF WON', 'HALF LOST') for s in sts):
        return None, 'regra_casa', textos + ' (múltipla com meia perna)', []
    ress = [r[3] for r in res if r[3]]
    if all(s == 'VOID' for s in sts):
        return 'VOID', None, textos, ress
    return 'WON', None, textos, ress

def apura_aposta(a, f, casados):
    """Apura todas as pernas da aposta. Devolve dict com status, classe, motivo, ressalvas e os jogos casados."""
    res, jogos = [], []
    for k, p in enumerate(f.get('pernas') or []):
        if not isinstance(p, dict) or p.get('esp') not in CAM:
            res.append((None, 'fora', 'esporte sem fonte de placar', None))
            continue
        j, ia, como = casados.get((a['id'], k), (None, None, 'sem_jogo'))
        if not j:
            res.append((None, como or 'sem_jogo', f"não achei o jogo: {p.get('a') or p.get('jog')} x {p.get('b')}", None))
            continue
        jogos.append((k, j, ia, como))
        try:
            s, ev, ressalva = apura_perna(p, j, ia, a)
            if s is None:
                raise NaoSei('regra_casa', 'meia linha sem regra')
            if como == 'jogador_outro_jogo':
                ressalva = '; '.join(x for x in (ressalva, 'o jogo do jogador não é o que está escrito no evento') if x)
            res.append((s, None, ev, ressalva))
        except NaoSei as e:
            res.append((None, e.classe, f'{rotulo_jogo(j)} → {e.motivo}', None))
        except Exception:
            res.append((None, 'erro', traceback.format_exc()[-300:].replace('\n', ' / '), None))
    if not res:
        return {'status': None, 'classe': 'ficha', 'motivo': 'ficha sem pernas', 'ressalvas': [], 'jogos': []}
    st, cl, txt, ress = junta_pernas(res)
    duvida = (f.get('duvida') or '').strip()
    if st and duvida:
        ress = ress + ['leitura com dúvida: ' + duvida]
    return {'status': st, 'classe': cl, 'motivo': txt, 'ressalvas': ress, 'jogos': jogos}


# ------------------------------------------------------------------ banco
def _cab(extra=None):
    h = {'apikey': SB_KEY, 'Authorization': 'Bearer ' + SB_KEY, 'Content-Type': 'application/json'}
    if extra:
        h.update(extra)
    return h

def sb_get(path):
    r = requests.get(f'{SB_URL}/rest/v1/{path}', headers=_cab(), timeout=40)
    r.raise_for_status()
    return r.json()

def sb_todos(path):
    out, off = [], 0
    while True:
        lote = sb_get(f'{path}&limit=1000&offset={off}')
        out += lote
        if len(lote) < 1000:
            return out
        off += 1000

def sb_grava(linhas):
    """Escrita do robô: só nas tabelas dele (apuracoes aqui, apuracao_avisos nos avisos). Não existe escrita em apostas neste arquivo."""
    for i in range(0, len(linhas), 200):
        r = requests.post(f'{SB_URL}/rest/v1/apuracoes?on_conflict=aposta_id',
                          headers=_cab({'Prefer': 'resolution=merge-duplicates,return=minimal'}),
                          data=json.dumps(linhas[i:i + 200], ensure_ascii=False).encode('utf-8'), timeout=60)
        r.raise_for_status()

ESPORTES = {'quando': 0.0, 'mapa': {}}

def esportes():
    if time.time() - ESPORTES['quando'] > 3600 or not ESPORTES['mapa']:
        ESPORTES['mapa'] = {e['id']: e['nome'] for e in sb_get('esportes?select=id,nome')}
        ESPORTES['quando'] = time.time()
    return ESPORTES['mapa']

def carrega_apostas(hoje):
    """Apostas com data de -ATRAS a +FRENTE dias (qualquer status) e as pendentes mais velhas, até 15 dias."""
    d0, d1, d15 = hoje - timedelta(days=ATRAS), hoje + timedelta(days=FRENTE), hoje - timedelta(days=15)
    out = sb_todos('apostas?select=id,data_evento,evento,mercado,entrada,tipo_aposta,status,odd,esporte_id'
                   f'&or=(and(data_evento.gte.{d0},data_evento.lte.{d1}),and(status.eq.PENDING,data_evento.gte.{d15},data_evento.lt.{d0}))'
                   '&order=data_evento.asc,id.asc')
    mapa, bons = esportes(), []
    for a in out:
        if a['id'] in PULAR or not a.get('data_evento'):
            continue
        a['esporte'] = mapa.get(a.get('esporte_id')) or '(sem esporte)'
        a['D'] = date.fromisoformat(a['data_evento'])
        bons.append(a)
    return bons

def carrega_linhas(ids):
    out = {}
    for i in range(0, len(ids), 120):
        for l in sb_get('apuracoes?select=*&aposta_id=in.(' + ','.join(ids[i:i + 120]) + ')'):
            out[l['aposta_id']] = l
    return out

def resumo_txt(a):
    return hashlib.md5('|'.join(str(a.get(k) or '') for k in
                                ('evento', 'mercado', 'entrada', 'tipo_aposta', 'esporte', 'data_evento')).encode('utf-8')).hexdigest()

COLUNAS = ('aposta_id', 'texto_hash', 'ficha', 'ficha_em', 'jogos', 'inicio', 'status_proposto', 'classe', 'motivo', 'fecharia',
           'proposto_em', 'status_na_hora', 'tentativas', 'proxima_em', 'final', 'visto_em')

def linha_nova(aposta_id, h, agora, **kw):
    l = {c: None for c in COLUNAS}
    l.update(aposta_id=aposta_id, texto_hash=h, fecharia=False, tentativas=0, final=False, visto_em=iso(agora))
    l.update(kw)
    return l


# ------------------------------------------------------------------ casar e agendar
JA_PERGUNTEI = set()      # (aposta, perna, texto) que o modelo já tentou casar nesta execução

def casa_lote(cli, vez, linhas):
    """Casa cada perna das apostas da vez com um jogo. Devolve {(aposta_id, k): (jogo|None, ia, como|motivo)}."""
    casados, pendentes = {}, []
    for a in vez:
        l = linhas[a['id']]
        f = l.get('ficha') or {}
        guardados = {g.get('k'): g for g in (l.get('jogos') or []) if isinstance(g, dict) and g.get('como') == 'modelo'}
        fem_ap = a['esporte'] == 'Futebol Feminino'
        for k, p in enumerate(f.get('pernas') or []):
            if not isinstance(p, dict) or p.get('esp') not in CAM:
                continue
            chave = (a['id'], k)
            try:
                g = guardados.get(k)
                if g:                                   # o modelo já casou esta perna antes: reaproveita
                    j = next((x for x in candidatos(p['esp'], a['D']) if x['id'] == str(g.get('id'))), None)
                    if j:
                        casados[chave] = (j, int(g.get('ia') or 0), 'modelo')
                        continue
                if p.get('a') or p.get('b'):
                    j, ia, motivo = acha_jogo(p, a['D'], fem_ap or bool(p.get('fem')))
                    if j:
                        casados[chave] = (j, ia, 'nome')
                    elif p.get('tipo') == 'jogador' and p.get('jog') and p['esp'] in ('nba', 'nhl', 'nfl'):
                        j, ia, motivo = acha_jogo_do_jogador(p, a['D'])
                        casados[chave] = (j, ia, 'jogador_outro_jogo') if j else (None, None, motivo)
                    elif (a['id'], k, l['texto_hash']) not in JA_PERGUNTEI:
                        pendentes.append((chave, p, a, a['D']))
                    else:
                        casados[chave] = (None, None, motivo or 'sem_jogo')
                elif p.get('tipo') == 'jogador' and p.get('jog'):
                    j, ia, motivo = acha_jogo_do_jogador(p, a['D'])
                    casados[chave] = (j, ia, 'jogador') if j else (None, None, motivo)
                else:
                    casados[chave] = (None, None, 'sem_jogo')
            except Exception:
                log('ERRO ao casar', a['id'][:8], traceback.format_exc()[-400:].replace('\n', ' / '))
    if pendentes:
        extra = casa_pelo_modelo(cli, pendentes)
        for chave, p, a, D in pendentes:
            JA_PERGUNTEI.add((chave[0], chave[1], linhas[chave[0]]['texto_hash']))
            casados[chave] = (extra[chave][0], extra[chave][1], 'modelo') if chave in extra else (None, None, 'sem_jogo')
    return casados

DUR = {'futebol': 115, 'nba': 150, 'nhl': 165, 'nfl': 200, 'mma': 30}      # minutos do início ao fim, em geral
PARADO = ('STATUS_POSTPONED', 'STATUS_CANCELED', 'STATUS_ABANDONED', 'STATUS_SUSPENDED', 'STATUS_DELAYED')

def agenda(r, a, agora, hoje, tentativas):
    """(final, proxima_em): se o robô desiste desta aposta ou quando olha de novo."""
    cl, D = r['classe'], a['D']
    if r['status'] or cl in ('fora', 'mercado', 'ficha', 'ambiguo', 'regra_casa'):
        return True, None
    if cl == 'nao_terminou':
        if hoje > D + timedelta(days=3):
            return True, None
        ts = []
        for _, j, _, _ in r['jogos']:
            if j['completo']:
                continue
            fim = j['utc'] + timedelta(minutes=DUR.get(j['esp'], 120))
            if j['status'] in PARADO:
                ts.append(agora + timedelta(hours=6))
            elif fim > agora:
                ts.append(fim)
            elif agora - fim > timedelta(hours=6):
                ts.append(agora + timedelta(hours=1))
            else:
                ts.append(agora + timedelta(minutes=5))
        return False, (min(ts) if ts else agora + timedelta(minutes=10))
    if cl == 'aguarda':
        return (True, None) if hoje > D + timedelta(days=2) else (False, agora + timedelta(minutes=30))
    if cl == 'sem_jogo':
        return (True, None) if hoje > D + timedelta(days=1) else (False, agora + timedelta(hours=3))
    if cl == 'sem_dado':
        return (True, None) if hoje > D + timedelta(days=2) else (False, agora + timedelta(minutes=30))
    return (True, None) if tentativas >= 6 else (False, agora + timedelta(minutes=30))

def nome_time(t):
    return (t['nomes'] or ['?'])[0]

def atualiza(l, a, r, agora, hoje):
    """Passa o resultado da tentativa pra linha da aposta."""
    tent = (l.get('tentativas') or 0) + 1
    final, prox = agenda(r, a, agora, hoje, tent)
    cl = r['classe']
    if r['status']:
        cl = 'conferir' if r['ressalvas'] else 'apurou'
    elif cl == 'aguarda' and final:
        cl = 'sem_jogo'
    motivo = (r['motivo'] or '') + ((' | ressalva: ' + '; '.join(r['ressalvas'])) if r['ressalvas'] else '')
    jogos = []
    for k, j, ia, como in r['jogos']:
        t0, t1 = j['times']
        casa, fora = (t0, t1) if t0['casa'] else (t1, t0)
        jogos.append({'k': k, 'id': j['id'], 'ia': ia, 'como': como, 'esp': j['esp'], 'casa': nome_time(casa), 'fora': nome_time(fora),
                      'inicio': iso(j['utc']), 'situacao': j['status'], 'dia_espn': str(j['dia_espn'])})
    l.update(jogos=jogos or None, inicio=iso(min(j['utc'] for _, j, _, _ in r['jogos'])) if r['jogos'] else None,
             status_proposto=r['status'], classe=cl, motivo=motivo[:1500], fecharia=bool(r['status'] and not r['ressalvas']),
             proposto_em=iso(agora) if r['status'] else None, status_na_hora=a['status'] if r['status'] else None,
             tentativas=tent, proxima_em=iso(prox), final=final, visto_em=iso(agora))
    return l


# ------------------------------------------------------------------ ciclo
def curto(s, n):
    s = re.sub(r'\s+', ' ', str(s or ''))
    return s if len(s) <= n else s[:n - 1] + '…'

def ciclo(cli):
    agora = agora_utc()
    hoje = agora.astimezone(BRT).date()
    PLACARES.clear()
    req0, uso0 = N_REQ[0], [USO['ficha'][2], USO['casa'][2]]
    apostas = carrega_apostas(hoje)
    linhas = carrega_linhas([a['id'] for a in apostas]) if apostas else {}
    gravar = {}

    # 1) ficha de quem ainda não tem (ou mudou de texto)
    pedir, fora = [], 0
    for a in apostas:
        h = resumo_txt(a)
        l = linhas.get(a['id'])
        if l and l.get('texto_hash') == h:
            continue
        if a['esporte'] not in ROTULO:
            linhas[a['id']] = gravar[a['id']] = linha_nova(a['id'], h, agora, classe='fora', motivo='esporte sem fonte de placar', final=True)
            fora += 1
        else:
            pedir.append((a, h))
    pedir = pedir[:MAX_FICHAS]
    feitas = 0
    if pedir:
        for n, (a, _) in enumerate(pedir):
            a['n'] = n
        fichas = faz_fichas(cli, [a for a, _ in pedir])
        for a, h in pedir:
            f = fichas.get(a['n'])
            if f and isinstance(f.get('pernas'), list):
                linhas[a['id']] = gravar[a['id']] = linha_nova(
                    a['id'], h, agora, ficha={'pernas': f['pernas'], 'duvida': str(f.get('duvida') or '')}, ficha_em=iso(agora))
                feitas += 1

    # 2) apura quem está na vez (jogo do dia ou de antes, e hora de olhar)
    limite = hoje + timedelta(days=1)
    vez = []
    for a in apostas:
        l = linhas.get(a['id'])
        if not l or l.get('final') or not l.get('ficha') or a['D'] > limite or l.get('texto_hash') != resumo_txt(a):
            continue
        px = de_iso(l.get('proxima_em'))
        if px and px > agora and a['id'] not in URGENTE:
            continue
        vez.append(a)
    casados = casa_lote(cli, vez, linhas) if vez else {}
    propostas = 0
    for a in vez:
        l = linhas[a['id']]
        try:
            r = apura_aposta(a, l['ficha'], casados)
        except Exception:
            r = {'status': None, 'classe': 'erro', 'motivo': traceback.format_exc()[-300:].replace('\n', ' / '), 'ressalvas': [], 'jogos': []}
        atualiza(l, a, r, agora, hoje)
        gravar[a['id']] = l
        if r['status']:
            propostas += 1
            igual = '' if a['status'] == 'PENDING' else (' = marcação' if a['status'] == r['status'] else ' ≠ MARCAÇÃO')
            log(f"PROPOSTA {a['id'][:8]} {a['data_evento']} {a['esporte']} | {curto(a.get('evento'), 40)} ~ {curto(a.get('entrada'), 60)} | "
                f"robô {r['status']} ({l['classe']}) | aposta {a['status']}{igual} | {curto(l['motivo'], 220)}")

    URGENTE.clear()
    if gravar:
        sb_grava(list(gravar.values()))
    abertas = sum(1 for a in apostas if (linhas.get(a['id']) or {}).get('ficha') and not (linhas.get(a['id']) or {}).get('final'))
    log(f"ciclo {agora.astimezone(BRT).strftime('%d/%m %H:%M')} | apostas na janela {len(apostas)} | fichas novas {feitas} | sem fonte {fora} | "
        f"na vez {len(vez)} | propostas {propostas} | abertas {abertas} | placar {N_REQ[0] - req0} pedidos | "
        f"modelo {USO['ficha'][2] - uso0[0]}+{USO['casa'][2] - uso0[1]} chamadas")
    return {'apostas': len(apostas), 'fichas': feitas, 'vez': len(vez), 'propostas': propostas, 'gravadas': len(gravar)}


# ------------------------------------------------------------------ avisos no Telegram
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT = os.environ.get('TELEGRAM_CHAT', '')
TG_MODO = os.environ.get('AP_TELEGRAM', 'desligado').strip().lower()     # desligado | ensaio (só escreve no log) | ligado
TG_DESDE = de_iso(os.environ.get('AP_TELEGRAM_DESDE', ''))               # em ensaio: passa a mandar de verdade a partir desta hora
AVISOS = int(os.environ.get('AP_AVISOS', '120') or 120)                  # segundos entre uma olhada e outra nos jogos ao vivo
TETO_HORA = 90                                                           # mensagens novas por hora, no máximo (trava contra enxurrada)
MEM = {}            # chave -> {'msg_id', 'texto', 'estado', 'quando'}; em modo ligado também vai pra tabela apuracao_avisos
MEM_OK = [False]    # já carregou da tabela?
ENVIOS = []         # horários das mensagens novas (pra trava)
URGENTE = set()     # apostas pra apurar já: o jogo acabou de terminar
SEMANA = ('seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom')

def tg_vivo():
    return bool(TG_TOKEN and TG_CHAT) and (TG_MODO == 'ligado' or (TG_MODO == 'ensaio' and TG_DESDE is not None and agora_utc() >= TG_DESDE))

def silencio():
    """Das 23h às 8h (Brasília) nenhuma mensagem toca; o resultado chega do mesmo jeito, sem som."""
    h = agora_utc().astimezone(BRT).hour
    return h >= 23 or h < 8

def tg(metodo, **campos):
    """Chamada ao Telegram. O erro nunca carrega o endereço (que tem o token)."""
    try:
        r = requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/{metodo}', json=campos, timeout=20)
        j = r.json()
    except Exception as e:
        raise RuntimeError(f'telegram {metodo}: {type(e).__name__}') from None
    if not j.get('ok'):
        raise RuntimeError(f"telegram {metodo}: {j.get('error_code')} {j.get('description')}")
    return j.get('result')

def av_carrega():
    """Na primeira olhada em modo ligado: esquece o que era ensaio e pega da tabela o que já foi mandado."""
    if MEM_OK[0] or not tg_vivo():
        return
    MEM.clear()
    desde = iso(agora_utc() - timedelta(days=3))
    for l in sb_get(f'apuracao_avisos?select=chave,msg_id,texto,estado,atualizado_em&atualizado_em=gte.{desde}&chat_id=eq.{TG_CHAT}'):
        MEM[l['chave']] = {'msg_id': l.get('msg_id'), 'texto': l.get('texto'), 'estado': l.get('estado'), 'quando': de_iso(l.get('atualizado_em'))}
    MEM_OK[0] = True

def av_guarda(chave, **campos):
    e = MEM.setdefault(chave, {'msg_id': None, 'texto': None, 'estado': None, 'quando': None})
    e.update(campos)
    e['quando'] = agora_utc()
    if tg_vivo():
        try:
            r = requests.post(f'{SB_URL}/rest/v1/apuracao_avisos?on_conflict=chave',
                              headers=_cab({'Prefer': 'resolution=merge-duplicates,return=minimal'}),
                              data=json.dumps([{'chave': chave, 'chat_id': int(TG_CHAT), 'msg_id': e['msg_id'], 'texto': e['texto'],
                                                'estado': e['estado'], 'atualizado_em': iso(e['quando'])}], ensure_ascii=False).encode('utf-8'), timeout=30)
            r.raise_for_status()
        except Exception as ex:
            log('ERRO ao guardar aviso', chave, type(ex).__name__)

def av_manda(chave, texto, estado, novo=False, avisa=False, responde=None):
    """Manda ou edita a mensagem da chave. Em ensaio só escreve no log. Devolve True se mudou algo."""
    e = MEM.get(chave) or {}
    if not novo and e.get('texto') == texto and e.get('estado') == estado:
        return False
    vivo = tg_vivo()
    if not vivo:
        log(f"ENSAIO [{chave}] {'NOVA' if (novo or not e.get('msg_id')) else 'edita'}{' (com aviso)' if avisa else ''}:\n{texto}")
        av_guarda(chave, texto=texto, estado=estado, msg_id=e.get('msg_id') or (-1 if not novo else -2))
        return True
    try:
        if e.get('msg_id') and e['msg_id'] > 0 and not novo:
            if e.get('texto') != texto:
                try:
                    tg('editMessageText', chat_id=TG_CHAT, message_id=e['msg_id'], text=texto)
                except RuntimeError as ex:
                    if 'not modified' not in str(ex):
                        raise
            av_guarda(chave, texto=texto, estado=estado)
            return True
        agora = time.time()
        ENVIOS[:] = [t for t in ENVIOS if agora - t < 3600]
        if len(ENVIOS) >= TETO_HORA:
            log('TRAVA: passou de', TETO_HORA, 'mensagens novas em 1h; não mandei', chave)
            return False
        campos = {'chat_id': TG_CHAT, 'text': texto, 'disable_notification': not (avisa and not silencio())}
        if responde and responde > 0:
            campos['reply_to_message_id'] = responde
            campos['allow_sending_without_reply'] = True
        ENVIOS.append(agora)
        m = tg('sendMessage', **campos)
        time.sleep(0.4)
        av_guarda(chave, texto=texto, estado=estado, msg_id=int(m['message_id']))
        return True
    except Exception as ex:
        log('ERRO no Telegram', chave, str(ex)[:200])
        return False

def n2(x):
    s = f'{abs(x):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return s

def din(x, moeda, sinal=False):
    pre = 'US$ ' if moeda == 'USD' else 'R$ '
    return pre + (('+' if x >= 0 else '-') if sinal else ('-' if x < 0 else '')) + n2(x)

def uni(x, sinal=False):
    return (('+' if x >= 0 else '-') if sinal else ('-' if x < 0 else '')) + n2(x) + 'u'

def fl(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0

def previsto(a, st):
    """Lucro que a aposta teria com o resultado st: (unidades, dinheiro)."""
    odd, u = fl(a.get('odd')), fl(a.get('stake_unidades'))
    d = fl(a.get('stake_usd')) if a.get('moeda') == 'USD' else fl(a.get('stake_reais'))
    k = {'WON': odd - 1, 'HALF WON': (odd - 1) / 2, 'VOID': 0.0, 'HALF LOST': -0.5, 'LOST': -1.0}.get(st)
    return (None, None) if k is None else (u * k, d * k)

BOLA = {'WON': '🟢', 'HALF WON': '🟢', 'LOST': '🔴', 'HALF LOST': '🔴', 'VOID': '⚪'}
NOME_ST = {'WON': 'green', 'HALF WON': 'meio green', 'LOST': 'red', 'HALF LOST': 'meio red', 'VOID': 'devolvida'}

def rotulo_aposta(a):
    tipo = (a.get('tipo_aposta') or '').strip()
    pre = (tipo + ': ') if tipo.lower() in ('dupla', 'tripla', 'multipla', 'múltipla') else ''
    return pre + curto(a.get('entrada') or a.get('evento'), 70) + (f" @{fl(a.get('odd')):.2f}".replace('.', ',') if a.get('odd') else '')

def hora_brt(d):
    return d.astimezone(BRT).strftime('%H:%M')

def avisos():
    """Uma olhada: painel do dia e uma mensagem por jogo. Lê do banco e do placar; só escreve em apuracao_avisos."""
    if TG_MODO == 'desligado':
        return
    av_carrega()
    agora = agora_utc()
    brt = agora.astimezone(BRT)
    dia = (brt - timedelta(hours=5)).date()                 # o "dia" do painel vira às 5h
    aps = sb_todos('apostas?select=id,data_evento,evento,entrada,tipo_aposta,status,odd,moeda,stake_unidades,stake_reais,stake_usd,'
                   f'lucro_unidades,lucro_reais,lucro_usd&data_evento=gte.{dia - timedelta(days=1)}&data_evento=lte.{dia + timedelta(days=1)}&order=id.asc')
    aps = [a for a in aps if a['id'] not in PULAR]
    linhas = carrega_linhas([a['id'] for a in aps]) if aps else {}
    PLACARES.clear()

    # jogos com aposta: chave -> dados
    jogos = {}
    for a in aps:
        l = linhas.get(a['id']) or {}
        for g in (l.get('jogos') or []):
            ini = de_iso(g.get('inicio'))
            if not ini or not (agora - timedelta(hours=8) <= ini <= agora + timedelta(hours=20)):
                continue
            j = jogos.setdefault((g.get('esp'), str(g.get('id'))), {'esp': g.get('esp'), 'id': str(g.get('id')), 'casa': g.get('casa'), 'fora': g.get('fora'),
                                                                   'inicio': ini, 'dia_espn': g.get('dia_espn'), 'apostas': {}})
            j['apostas'][a['id']] = (a, l)

    # situação de agora de cada jogo que já devia ter começado
    for j in jogos.values():
        j['agora'] = None
        if j['inicio'] > agora + timedelta(minutes=3) or j['esp'] not in CAM or (MEM.get(f"jogo:{j['esp']}:{j['id']}") or {}).get('estado') == 'fim':
            continue
        try:
            if j.get('dia_espn'):
                lista = jogos_do_dia(j['esp'], date.fromisoformat(j['dia_espn']))
            else:
                lista = candidatos(j['esp'], j['inicio'].astimezone(BRT).date())
            j['agora'] = next((x for x in lista if x['id'] == j['id']), None)
        except Exception:
            log('ERRO ao ler o placar ao vivo', j['id'], traceback.format_exc()[-200:].replace('\n', ' / '))

    def placar_txt(j):
        x = j['agora']
        if not x:
            return f"{j['casa']} x {j['fora']}"
        t0, t1 = x['times']
        casa, fora = (t0, t1) if t0['casa'] else (t1, t0)
        if casa['placar'] is None or fora['placar'] is None:
            return f"{j['casa']} x {j['fora']}"
        return f"{nome_time(casa)} {casa['placar']}x{fora['placar']} {nome_time(fora)}"

    def linha_aposta(a, l, com_resultado):
        st = a['status'] if a['status'] != 'PENDING' else (l.get('status_proposto') if com_resultado else None)
        if st in BOLA:
            quem = '' if a['status'] != 'PENDING' else (' (robô)' if l.get('classe') == 'apurou' else ' (robô, confere)')
            return f"{BOLA[st]} {rotulo_aposta(a)} · {NOME_ST[st]}{quem}"
        if com_resultado:
            return f"⚪ {rotulo_aposta(a)} · sem apuração automática"
        return f"· {rotulo_aposta(a)}"

    # uma mensagem por jogo
    vivos, seguir = [], []
    for chave_j, j in sorted(jogos.items(), key=lambda x: x[1]['inicio']):
        chave = f"jogo:{j['esp']}:{j['id']}"
        e = MEM.get(chave) or {}
        x = j['agora']
        lista = sorted(j['apostas'].values(), key=lambda al: al[0]['id'])
        if x is None or x.get('estado') == 'pre':
            if j['inicio'] > agora:
                seguir.append(j)
            continue
        if x.get('estado') == 'in' or (not x['completo'] and x.get('estado') != 'post'):
            vivos.append(j)
            txt = f"{placar_txt(j)} · {x.get('detalhe') or 'ao vivo'}\n" + '\n'.join(linha_aposta(a, l, False) for a, l in lista)
            if e.get('estado') in (None, 'pre'):
                comeco = 'Começou · ' if agora - j['inicio'] < timedelta(minutes=15) else 'Ao vivo · '     # pegou o jogo andando
                av_manda(chave, comeco + txt, 'vivo', novo=not e.get('msg_id'))
            elif e.get('estado') == 'vivo':
                av_manda(chave, txt, 'vivo')
            continue
        if x['completo'] and e.get('estado') != 'fim':
            if agora - j['inicio'] > timedelta(hours=6) and e.get('estado') is None:
                MEM[chave] = {'msg_id': None, 'texto': None, 'estado': 'fim', 'quando': agora}      # jogo antigo: não anuncia
                continue
            abertas = [a for a, l in lista if a['status'] == 'PENDING' and not l.get('final')]
            desde = e.get('fim_visto') or agora
            e2 = MEM.setdefault(chave, {'msg_id': None, 'texto': None, 'estado': e.get('estado'), 'quando': agora})
            e2['fim_visto'] = desde
            if abertas and agora - desde < timedelta(minutes=12):
                URGENTE.update(a['id'] for a in abertas)                   # pede a apuração já e espera a próxima olhada
                if e.get('estado') == 'vivo':
                    av_manda(chave, f"{placar_txt(j)} · fim de jogo\n" + '\n'.join(linha_aposta(a, l, False) for a, l in lista), 'vivo')
                continue
            fim = f"Fim · {placar_txt(j)}\n" + '\n'.join(linha_aposta(a, l, True) for a, l in lista)
            if e.get('msg_id') and e['msg_id'] > 0:
                av_manda(chave, f"{placar_txt(j)} · fim de jogo\n" + '\n'.join(linha_aposta(a, l, True) for a, l in lista), 'vivo')
            if av_manda(chave + ':fim', fim, 'fim', novo=True, avisa=True, responde=(MEM.get(chave) or {}).get('msg_id')):
                MEM.setdefault(chave, {})['estado'] = 'fim'
                av_guarda(chave, estado='fim')

    # painel do dia
    do_dia = [a for a in aps if a['data_evento'] == str(dia)]
    if do_dia or MEM.get(f'painel:{dia}'):
        tot = {'n': 0, 'u': 0.0, 'BRL': 0.0, 'USD': 0.0, 'pn': 0, 'pu': 0.0, 'pBRL': 0.0, 'pUSD': 0.0, 'lu': 0.0, 'lBRL': 0.0, 'lUSD': 0.0,
               'ru': 0.0, 'rBRL': 0.0, 'rUSD': 0.0, 'rn': 0, 'conf': 0}
        for a in do_dia:
            m = 'USD' if a.get('moeda') == 'USD' else 'BRL'
            d = fl(a.get('stake_usd')) if m == 'USD' else fl(a.get('stake_reais'))
            tot['n'] += 1
            tot['u'] += fl(a.get('stake_unidades'))
            tot[m] += d
            l = linhas.get(a['id']) or {}
            if a['status'] == 'PENDING':
                tot['pn'] += 1
                tot['pu'] += fl(a.get('stake_unidades'))
                tot['p' + m] += d
                if l.get('status_proposto'):
                    pu, pd = previsto(a, l['status_proposto'])
                    if pu is not None:
                        tot['rn'] += 1
                        tot['ru'] += pu
                        tot['r' + m] += pd
                    if l.get('classe') == 'conferir':
                        tot['conf'] += 1
            else:
                tot['lu'] += fl(a.get('lucro_unidades'))
                tot['l' + m] += fl(a.get('lucro_usd')) if m == 'USD' else fl(a.get('lucro_reais'))
        p = [f"Hoje · {SEMANA[dia.weekday()]} {dia.strftime('%d/%m')} · {brt.strftime('%H:%M')}",
             f"Entradas {tot['n']} · investido {uni(tot['u'])}",
             f"{din(tot['BRL'], 'BRL')} · {din(tot['USD'], 'USD')}",
             f"Pendentes {tot['pn']} · {uni(tot['pu'])} · {din(tot['pBRL'], 'BRL')} · {din(tot['pUSD'], 'USD')}",
             f"Resultado marcado {uni(tot['lu'], True)} · {din(tot['lBRL'], 'BRL', True)} · {din(tot['lUSD'], 'USD', True)}"]
        if tot['rn']:
            p.append(f"Com o que o robô já apurou ({tot['rn']}): {uni(tot['lu'] + tot['ru'], True)} · {din(tot['lBRL'] + tot['rBRL'], 'BRL', True)} · "
                     f"{din(tot['lUSD'] + tot['rUSD'], 'USD', True)}" + (f" · {tot['conf']} pra conferir" if tot['conf'] else ''))
        if vivos:
            p.append('')
            p.append('Ao vivo')
            for j in vivos[:12]:
                p.append(f"{placar_txt(j)} · {(j['agora'] or {}).get('detalhe') or ''} · {len(j['apostas'])} " + ('entrada' if len(j['apostas']) == 1 else 'entradas'))
        if seguir:
            p.append('')
            p.append('A seguir')
            for j in seguir[:10]:
                quando = j['inicio'].astimezone(BRT)
                outro_dia = '' if quando.date() == brt.date() else SEMANA[quando.weekday()] + ' '
                p.append(f"{outro_dia}{hora_brt(j['inicio'])} {j['casa']} x {j['fora']} · {len(j['apostas'])} " + ('entrada' if len(j['apostas']) == 1 else 'entradas'))
            if len(seguir) > 10:
                p.append(f'e mais {len(seguir) - 10}')
        texto = '\n'.join(p)
        chave = f'painel:{dia}'
        e = MEM.get(chave) or {}
        sem_hora = lambda s: '\n'.join((s or '').split('\n')[1:])
        if not e.get('msg_id'):
            av_manda(chave, texto, 'painel', novo=True)
        elif sem_hora(e.get('texto')) != sem_hora(texto):            # só edita quando muda algo além da hora
            av_manda(chave, texto, 'painel')
        e = MEM.get(chave) or {}
        if tg_vivo() and e.get('msg_id') and e['msg_id'] > 0 and not e.get('fixado') and time.time() - (e.get('tentou_fixar') or 0) > 1800:
            e['tentou_fixar'] = time.time()                           # tenta fixar; se o bot não tem permissão, tenta de novo em meia hora
            try:
                tg('pinChatMessage', chat_id=TG_CHAT, message_id=e['msg_id'], disable_notification=True)
                e['fixado'] = True
            except Exception as ex:
                log('aviso: não consegui fixar o painel:', str(ex)[:160])



def main():
    log(f'apurador EM SOMBRA (só propõe; não altera aposta) | modelo {MODELO} | ciclo {CICLO}s | janela -{ATRAS}/+{FRENTE} dias | '
        f"telegram {TG_MODO}" + (f' (manda de verdade a partir de {iso(TG_DESDE)})' if TG_MODO == 'ensaio' and TG_DESDE else ''))
    if not SB_KEY or not os.environ.get('ANTHROPIC_KEY'):
        log('ERRO: falta SUPABASE_KEY ou ANTHROPIC_KEY nas variáveis do serviço')
        time.sleep(600)
        sys.exit(1)
    if TG_MODO != 'desligado' and not (TG_TOKEN and TG_CHAT):
        log('aviso: telegram sem TELEGRAM_TOKEN ou TELEGRAM_CHAT; fica só no log')
    cli = cliente()
    prox_ciclo = prox_avisos = 0.0
    while True:
        if time.time() >= prox_ciclo or URGENTE:
            try:
                ciclo(cli)
            except Exception:
                log('ERRO no ciclo:', traceback.format_exc()[-1500:].replace('\n', ' / '))
            prox_ciclo = time.time() + CICLO
        if TG_MODO != 'desligado' and time.time() >= prox_avisos:
            try:
                avisos()
            except Exception:
                log('ERRO nos avisos:', traceback.format_exc()[-1200:].replace('\n', ' / '))
            prox_avisos = time.time() + AVISOS
        time.sleep(15)


if __name__ == '__main__':
    main()
