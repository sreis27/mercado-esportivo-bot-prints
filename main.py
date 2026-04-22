"""
Bot de Prints — Mercado Esportivo
Monitora o grupo 'RM Office - Planilhar', extrai dados de prints de apostas
via Claude Vision e registra automaticamente no Supabase como PENDING.
"""

import os, json, re, base64, time, traceback
from datetime import datetime, timezone, timedelta
import requests
import anthropic

# ============================================================
# CREDENCIAIS
# ============================================================
SUPABASE_URL    = "https://yfdrifvhsiumdxgypkjm.supabase.co"
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID         = int(os.environ.get("TELEGRAM_CHAT_ID", "-4711785999"))
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_KEY", "")

BRT = timezone(timedelta(hours=-3))

# ============================================================
# HELPERS
# ============================================================
def sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=representation'
    }

def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers(), timeout=30)
    r.raise_for_status()
    return r.json()

def sb_insert(table, body):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=sb_headers(), json=body, timeout=30
    )
    if not r.ok:
        print(f"sb_insert erro: {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()

def tg_call(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
                      json=params, timeout=30)
    return r.json()

def tg_get_file_bytes(file_id):
    """Baixa uma imagem do Telegram como bytes."""
    info = tg_call('getFile', file_id=file_id)
    if not info.get('ok'):
        return None
    path = info['result']['file_path']
    r = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{path}", timeout=60)
    return r.content if r.ok else None

def tg_react(message_id, emoji):
    """Adiciona reação à mensagem. emoji: '👀' processando, '🔥' sucesso, '🤨' parcial, '🤔' não entendi, '💩' erro."""
    try:
        return tg_call('setMessageReaction',
                       chat_id=CHAT_ID, message_id=message_id,
                       reaction=[{'type': 'emoji', 'emoji': emoji}])
    except Exception as e:
        print(f"Erro ao reagir: {e}")

# ============================================================
# EXTRAÇÃO VIA CLAUDE VISION
# ============================================================
def carregar_cadastros():
    """Carrega listas de cadastros do Supabase pra contextualizar o Claude."""
    return {
        'tipsters': sb_get('tipsters?select=id,nome'),
        'bookies': sb_get('bookies?select=id,nome'),
        'operadores': sb_get('operadores?select=id,nome'),
        'esportes': sb_get('esportes?select=id,nome'),
        'mercados': sb_get('mercados?select=id,nome'),
        'tipos_aposta': sb_get('tipos_aposta?select=id,nome'),
        'stakes': sb_get('stakes_historico?select=tipster_id,valor_reais,vigente_a_partir'),
    }

def extrair_aposta(imagem_bytes, descricao_msg, cadastros, data_hoje, operador_msg=None):
    """Usa Claude Vision pra extrair dados da imagem + descrição da mensagem."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    tipsters_lista = [t['nome'] for t in cadastros['tipsters']]
    bookies_lista = [b['nome'] for b in cadastros['bookies']]
    operadores_lista = [o['nome'] for o in cadastros['operadores']]
    esportes_lista = [e['nome'] for e in cadastros['esportes']]
    mercados_lista = [m['nome'] for m in cadastros.get('mercados', [])]
    tipos_aposta_lista = [t['nome'] for t in cadastros.get('tipos_aposta', [])]

    prompt = f"""Você é o sistema automatizado de planilhamento do Mercado Esportivo. Opera com julgamento humano.

Analise o PRINT DE APOSTA em anexo combinado com a descrição que o operador adicionou.

QUEM ENVIOU (operador): "{operador_msg or '(desconhecido)'}"
DESCRIÇÃO: "{descricao_msg or '(sem descrição)'}"
DATA DE HOJE: {data_hoje}

CADASTROS EXISTENTES — use o nome EXATAMENTE como aparece aqui:
- Tipsters: {json.dumps(tipsters_lista, ensure_ascii=False)}
- Bookies: {json.dumps(bookies_lista, ensure_ascii=False)}
- Operadores: {json.dumps(operadores_lista, ensure_ascii=False)}
- Esportes: {json.dumps(esportes_lista, ensure_ascii=False)}
- Mercados: {json.dumps(mercados_lista, ensure_ascii=False)}
- Tipos de Aposta: {json.dumps(tipos_aposta_lista, ensure_ascii=False)}

═══════════════════════════════════════════════════════════════
REGRAS ABSOLUTAS (NUNCA QUEBRE, IMPORTÂNCIA MÁXIMA)
═══════════════════════════════════════════════════════════════

[R1] DATA: só use uma data se estiver ESCRITA EXPLICITAMENTE no print ou na descrição (rótulos "Data:", "Entrada para:", "Jogo em:", ou formato inequívoco DD/MM/AAAA). NUNCA infira data a partir de nomes de grupo, canal, temporada ou temporada-ano. Formatos como "2026.03 [nome]", "T03", "Season X" são IDENTIFICADORES DE GRUPO, NÃO DATAS. Se não há data explícita, use HOJE ({data_hoje}) como fallback. Proibido inventar datas.

[R2] "CRIAR APOSTA" EXPLÍCITO = 1 ITEM SÓ: se o print tem a palavra "CRIAR APOSTA", "BET BUILDER", "ACUMULADOR DO JOGO", "MEU BILHETE" ou "JOGO DO SEU JEITO" escrita explicitamente (geralmente em destaque no topo do bilhete), é UMA aposta única com tipo_aposta = "Criar Aposta". JAMAIS quebre em várias apostas. Concatene TODAS as seleções no campo entrada, separadas por vírgula. A odd é única (a única odd do bilhete).

[R3] BET365 COMO BOOKIE: quando a descrição começa com "365" (ex: "365 deia", "365 will in in"), o bookie é SEMPRE Bet365. Nunca deixe bookie null nesse caso.

[R4] ESPORTE DE BASQUETE (NBA): times como "POR Trail Blazers", "SA Spurs", "Portland Trail Blazers", "San Antonio Spurs", "Lakers", "Warriors", "Celtics", "Heat", "Nets", "Bulls", "Knicks", "Rockets", "Mavericks", "Suns", "Grizzlies" e qualquer outro time da NBA → esporte: Basquete. Isto é inequívoco, não deixe null.

[R5] CHECKLIST FINAL ANTES DE RESPONDER: antes de emitir o JSON, verifique mentalmente:
   - Eu vi "CRIAR APOSTA" / "BET BUILDER" no print? → Se sim, retorno 1 item só, não vários.
   - Eu inferi uma data? → Ela vem de texto inequívoco OU é hoje ({data_hoje})?
   - Há times conhecidos no print? → Marquei o esporte correto?
   - A descrição começa com bookie conhecido ("365")? → Preenchi o bookie?
   - Todos os campos que EU DEVERIA ter preenchido estão preenchidos? → Não deixei null por cautela excessiva.

═══════════════════════════════════════════════════════════════

REGRAS DETALHADAS:

A. OPERADOR: use o nome "{operador_msg}" (quem enviou o print). Se bater com algum nome da lista de operadores, use o nome exato do cadastro. Se não bater, deixe como está.

B. TIPSTER: deduza pelo cabeçalho/título do print. O nome que aparece no cabeçalho pode ser:
   (i) o nome do GRUPO diretamente (ex: "BH Tipster" → "BH CS")
   (ii) um USUÁRIO/APELIDO que assina o print em nome do grupo (ex: "Italo Cards" aparece no print mas o grupo está cadastrado como "Italo Cartões")
   (iii) o nome ESTILIZADO com separadores decorativos — pontos, traços, espaços entre letras (ex: "P.R.O.P.H.E.T" → "PROPHET", "J D F - V I P" → "JDF-VIP", "S.E.T.T.A" → "SETTA"). NORMALIZE removendo pontos, hífens e espaços extras antes de comparar com a lista.
   Quando houver similaridade temática entre o nome no print e um item da lista (cards ↔ cartões, traduções, abreviações, variações), PRIORIZE o match do cadastro em vez de usar o texto literal do print. Use o nome EXATAMENTE como está na lista de tipsters.
   Se aparecer "GOA", pode ser "GOA TOM", "GOA Fut Fem", "GOA Tradicional" — use o contexto da aposta (esporte, tipo de mercado) pra decidir qual.

C. ESPORTE: INFIRA por COMBINAÇÃO DE SINAIS (nome de time + mercado + torneio + contexto). NUNCA decida esporte baseado apenas em um nome de time, porque nomes são ambíguos:
   - "Fluminense" pode ser futebol, vôlei, basquete — precisa mercado ou contexto pra decidir
   - "Flamengo" pode ser futebol ou basquete — idem
   - "Barça eSports" existe em LOL, CS, Valorant, etc — precisa termo específico do jogo
   Só infira esporte quando houver PELO MENOS 2 SINAIS CONVERGENTES. Se houver só um nome isolado sem mais pistas, deixe null.

   SINAIS DE MERCADO que identificam o esporte:
   - Futebol: "gols", "escanteios", "cartões" (Mais/Menos de X.5), "ambos marcam", "anytime goalscorer", "marcar a qualquer momento", "qualificar-se", "resultado final" em contexto de clube
   - Basquete: "pontos", "rebotes", "assistências", "bloqueios" (do jogador), "pontos totais do jogo"
   - Counter-Strike (CS): "Mapa 1/2/3", "rondas" (ou "rodada"), "kills", "CT/TR", "pistol round", "handicap de rondas"
   - League of Legends (LOL): "barão", "dragão", "nashor", "torres", "inibidores", "first blood", "first lane"
   - DOTA 2: "roshan", "rax", "creep score", "first blood" em contexto DOTA
   - Valorant: "agentes" específicos (Jett, Omen, Phoenix), "plant", "defuse" sem contexto CS
   - Tênis: "sets", "games", "aces", "break points"
   - F1/Automobilismo: "volta mais rápida", "pole", "podium"
   - Os esports citados são os mais comuns mas NÃO são lista fechada — se identificar outro (Rocket League, KoF, etc), use o nome correto.

   SINAIS DE TORNEIO/LIGA também ajudam: "Champions League", "Premier League", "NBA", "LEC", "LPL", "CBLOL", "Major CS", etc.

   REGRA FINAL: se houver mercado genérico ("Match Winner", "Moneyline", "Vencedor 2-Way") SEM outro sinal que identifique o esporte, deixe null. É melhor null do que chutar.

   Use o nome do esporte EXATAMENTE como aparece no cadastro.

D. DATA DO EVENTO — prioridade de fontes (use a primeira disponível):
   1. DATA EXPLÍCITA NO PRINT: se o print mostra a data em qualquer formato ("21/04/2026", "21 abr", "Data: 2026-04-21 15:45:00", "Entrada para 21/04/2026"), extraia essa data como YYYY-MM-DD.
   2. DATA EXPLÍCITA NA DESCRIÇÃO: se o operador escreveu uma data na descrição, use essa.
   3. "LIVE" ou "AO VIVO" no print: a partida é HOJE ({data_hoje}).
   4. Nenhum dos anteriores: use HOJE ({data_hoje}) como fallback.
   NUNCA infira data de evento por conhecimento prévio de calendário esportivo — o Claude Vision não tem acesso a dados em tempo real e pode errar. Só use datas que estejam EXPLICITAMENTE escritas no print ou na descrição.

   CUIDADO — IDENTIFICADORES DE GRUPO/TEMPORADA NÃO SÃO DATAS: strings em formato "AAAA.MM [texto]", "T[N]", "Season [N]", "[AAAA].[MM] [nome]" no cabeçalho, logo ou nome do canal do tipster são identificadores de grupo/temporada, NÃO são data do evento. Exemplos:
   - "2026.03 Courtside" → nome do grupo, NÃO é data 2026-03-XX
   - "T03 Esports" → temporada 3 do grupo, NÃO é 03/XX/2026
   - "Season 26.03" → idem
   Data válida deve estar rotulada ("Data:", "Jogo em:", "Entrada para:", "LIVE") OU aparecer dentro do bilhete em formato inequívoco com dia/mês/ano (ex: "21/04/2026 15:45") ou sendo óbvia a relação com a partida.

E. STAKE (prioridade absoluta: DESCRIÇÃO DO OPERADOR > PRINT):

   SINAIS DE UNIDADE EM QUALQUER FONTE (print OU descrição): letra "u" ("2u", "1.5u") e sinal "%" ("0.75%", "1.5%", "2%") são sinais EQUIVALENTES e ambos representam UNIDADES. Aplique em qualquer texto/número que esteja na imagem (bilhete, legenda do tipster, anotações) ou na descrição do operador.

   CASO 1 — valor vem do PRINT (operador não falou de stake na descrição): preencha stake_unidades com o valor numérico. Considere TODO o texto da imagem (bilhete + legenda + anotações do tipster), não só o campo de stake da interface da casa. Exemplos:
   - "2u" → stake_unidades: 2
   - "0.5u" → stake_unidades: 0.5
   - "0.75%" escrito abaixo do bilhete → stake_unidades: 0.75
   - "1% - limite 50" → stake_unidades: 1
   - "R$ 2" no bilhete da casa → stake_unidades: 2 (convenção do tipster, NÃO é dois reais)
   - "R$ 0,5" no bilhete da casa → stake_unidades: 0.5 (convenção do tipster, NÃO é cinquenta centavos)
   Nunca faça conversão de moeda neste caso — apenas extraia o número pra stake_unidades.

   CASO 2 — operador informou stake na DESCRIÇÃO: a descrição PREVALECE sobre o print. Interprete conforme o operador escreveu, distinguindo unidades de reais pelos sinais:
   - SINAIS DE UNIDADES → preencher stake_unidades: letra "u" no valor ("1u", "1.5u"), sinal "%" no valor ("0.50%" = 0.5u, "1%" = 1u, "2%" = 2u — "%" é sinônimo de unidade em alguns tipsters), valores baixos típicos (0.5, 1, 2, 3, 5).
     Exemplos: "apostei 1u" → stake_unidades: 1 / "2u total" → stake_unidades: 2 / "0.50%" → stake_unidades: 0.5
   - SINAIS DE REAIS → preencher stake_reais: palavras "total", "R$", "reais", "em reais", ou valores altos sem "u" nem "%" (ex: 400, 1000, 250).
     Exemplos: "400,00 total" → stake_reais: 400 / "apostei R$ 250" → stake_reais: 250 / "1000 em reais" → stake_reais: 1000
   - Preencha APENAS UM dos dois campos (stake_unidades OU stake_reais), nunca os dois ao mesmo tempo.
   - Em caso de ambiguidade real, prefira stake_unidades.
   
   IMPORTANTE — descrição com MÚLTIPLAS LINHAS de stake: se o operador mandar várias linhas, cada linha (ou par de linhas) geralmente corresponde a UMA aposta diferente. Exemplos:

   FORMATO A — uma linha por aposta (stake e identificador juntos):
   "0.50%     (linha 1 → stake da aposta 1)
    0.50%     (linha 2 → stake da aposta 2)
    0.10% @39.48   (linha 3 → stake da aposta 3, a @odd identifica qual aposta)"

   FORMATO B — par de linhas por aposta (conta/bookie + stake/odd):
   "gabi betano     (linha 1 → identifica aposta 1: conta 'gabi' na Betano)
    487,35 1.90    (linha 2 → stake_reais: 487.35, odd de confirmação: 1.90)
    mcgames        (linha 3 → identifica aposta 2: bookie MCGames)
    312,65 1.85"   (linha 4 → stake_reais: 312.65, odd: 1.85)
   → 2 apostas: aposta 1 (Betano, gabi, stake_reais 487.35, odd 1.90) e aposta 2 (MCGames, stake_reais 312.65, odd 1.85). Valores altos como 487.35 e 312.65 SÃO REAIS, não unidades — preencha stake_reais.

   Mapeie linha por linha pras apostas correspondentes do print.

F. BOOKIE e CONTAS: vêm da descrição do operador, podem aparecer em qualquer ordem.
   - ATENÇÃO — BOOKIE DO PRINT ≠ BOOKIE DA APOSTA: quando o print do TIPSTER mostra "LINK:", "Casa:", "Disponível em:", "Código:", ou um link de uma casa (ex: "https://www.bet365.com/..."), isso indica apenas ONDE O TIPSTER ACHOU A ODD, NÃO onde o operador efetivamente apostou. O bookie REAL da aposta SEMPRE vem da descrição do operador. Se a descrição menciona uma casa diferente da do print, a descrição PREVALECE sem exceção.
     Ex: print do PROPHET mostra "LINK: Superbet" e descrição do operador diz "betfair dany 2.20" → bookie: Betfair, conta: dany. NÃO Superbet.
   - PADRÕES COMUNS: "[bookie] [conta]" (ex: "bet365 luciadritrich", "365 deia") OU "[conta] [bookie]" (ex: "ellian betano", "dany betfair"). Interprete flexível.
   - ABREVIAÇÕES DE BOOKIE conhecidas (lista conservadora, só use quando inequívoco):
     * "365" → Bet365
     * "AG" → Aposta Ganha
     Outras abreviações genéricas (ex: "bet", "bra") são ambíguas e NÃO devem ser expandidas — se a descrição usa abreviação ambígua, deixe bookie null.
   - NOTAÇÃO `[conta]+N`: significa "conta X + N outras contas" (ex: "ellian+1" = conta ellian + 1 outra; "dany+2" = dany + 2 outras). PRESERVE LITERAL no campo contas_utilizadas como veio ("ellian+1"), não tente expandir.
   - Se a casa só aparece sozinha sem conta, preencha bookie e deixe contas null.
   - Se só aparece a conta sem bookie, preencha contas e deixe bookie null.

F2. ODD REAL NA DESCRIÇÃO: quando a descrição contém explicitamente "odd X.XX" ou "odds X.XX" ou um valor numérico isolado/par de valores que claramente represente odd (ex: "3.80" depois da stake, ou "373,44 1.72" no Formato B onde 1.72 é a odd), esse valor PREVALECE sobre a odd mostrada no print. É a odd real que o operador conseguiu pegar na casa (odds caem em live, por exemplo).

F3. ODD COM BÔNUS DA CASA: quando o print contém marcadores de bônus explícitos, a odd exibida no print está COM o bônus aplicado — NÃO é a odd real da aposta. Marcadores típicos:
   - "GANHOS AUMENTADOS DE X%"
   - "BOOST +X%", "ODD BOOSTED", "SUPER ODDS"
   - "APOSTA TURBINADA", "SUPER AUMENTADA", "AUMENTO DE X%"
   - Ícones com setas/chamas indicando aumento de odd
   - Exibição de duas odds lado a lado com seta entre elas (ex: "2.10 >> 3.00" — a odd original 2.10 virou 3.00 com bônus; a REAL da aposta é 2.10 se não houver outra info, ou a odd que o tipster/operador informar na descrição)
   Regra: quando houver marcador de bônus E a descrição informar uma odd, use a odd da DESCRIÇÃO sem hesitar. Ela representa a odd real (sem bônus) que deve ser registrada.

G. QUANTAS APOSTAS RETORNAR — QUATRO CENÁRIOS (CRÍTICO, fonte comum de erro):

   Primeiro, verifique se há MARCADOR EXPLÍCITO DE COMBINADA: o print contém a palavra "DUPLA", "TRIPLA", "MÚLTIPLA", "COMBO", "ACUMULADA", "TRIPLE", "DOUBLE", "ACCUMULATOR" + uma ODD TOTAL calculada + UM ÚNICO valor apostar/valor apostado + UM ÚNICO possível ganho? Se SIM, vá direto pro Cenário 4.

   Caso contrário, CONTE no print: quantas odds DISTINTAS aparecem? Cada odd é uma potencial aposta.

   CENÁRIO 1 — Bet Builder / "Criar Aposta" (1 aposta):
   Várias seleções DO MESMO JOGO com UMA ÚNICA odd combinada e UM ÚNICO botão APOSTAR.
   IDENTIFICADORES EXPLÍCITOS (quando aparecerem, tipo_aposta = "Criar Aposta" SEM hesitar):
   - Palavra "CRIAR APOSTA" (em destaque, maiúsculas)
   - "BET BUILDER"
   - "ACUMULADOR DO JOGO"
   - "JOGO DO SEU JEITO"
   - "MEU BILHETE", "BILHETE DA CASA"
   - Ícone de construção/lego ao lado do bilhete
   ENTRADA (CRÍTICO): concatene TODAS as seleções do bet builder separadas por vírgula, NUNCA apenas a primeira.
   Ex 1: Donovan Clingan 15+ pontos + 12+ rebotes + 2+ assistências, odd 28.76, apostar R$ 0,5 → 1 item:
   - tipo_aposta: "Criar Aposta"
   - entrada: "Donovan Clingan 15+ pontos, 12+ rebotes, 2+ assistências" (TODAS as 3 seleções)
   - odd: 28.76
   Ex 2: bilhete POR x SA com 3 seleções (Mais de 221.5 Pontos Total, POR Trail Blazers Mais de 91.5, SA Spurs Mais de 102.5), odd 2.05, apostar R$ 250 → 1 item:
   - tipo_aposta: "Criar Aposta"
   - entrada: "Mais de 221.5 Pontos (Total), POR Trail Blazers Mais de 91.5 Pontos, SA Spurs Mais de 102.5 Pontos" (TODAS as 3)
   - odd: 2.05 (ou a odd real da descrição se houver bônus)

   CENÁRIO 2 — Apostas paralelas no mesmo print (N apostas):
   Várias seleções, cada uma com sua ODD INDIVIDUAL visível, possivelmente com uma linha/seção adicional "Dupla/Tripla/Múltipla" mostrando uma ODD COMBINADA SEPARADA. Cada odd individual = 1 aposta simples potencial. A odd combinada = 1 aposta ADICIONAL.
   ATENÇÃO: quando o print mostra N odds individuais E uma seção destacada ao final tipo "Dupla", "Tripla", "Múltipla" com sua PRÓPRIA odd combinada (ex: "Tripla 104.50"), existem N+1 APOSTAS POTENCIAIS, não N nem 1. Ex: 3 jogadores com odds individuais 11.00, 4.00, 6.00 + "Dupla 66.00" no final = 3 simples + 1 dupla = 4 apostas potenciais. A descrição diz quais efetivamente aconteceram.
   A DESCRIÇÃO DO OPERADOR determina quais apostas foram efetivamente realizadas (tipicamente todas, mas pode ter "out" marcando que pulou alguma — ver regra M).

   Ex: Print mostra:
   - Vitória (F) — odd 9.75
   - Chapecoense — odd 4.05
   - Dupla = 1 — odd 39.48
   Descrição: "0.50% / 0.50% / 0.10% @39.48 / dupla ellian / chape ellian / vitória ellian"
   → Retornar 3 apostas:
     1. tipo_aposta: "Simples", entrada: "Vitória (F)", odd: 9.75, stake_unidades: 0.5, contas_utilizadas: "ellian"
     2. tipo_aposta: "Simples", entrada: "Chapecoense", odd: 4.05, stake_unidades: 0.5, contas_utilizadas: "ellian"
     3. tipo_aposta: "Dupla", evento: "Dupla", entrada: "Vitória (F) + Chapecoense", odd: 39.48, stake_unidades: 0.1, contas_utilizadas: "ellian"

   Use pistas da descrição pra mapear: "@39.48" identifica a aposta pela odd, "dupla" identifica tipo, nomes mencionados ("chape", "vitória") identificam qual seleção.

   CENÁRIO 3 — Aposta única simples (1 aposta):
   Uma seleção, uma odd, um valor apostar. Retorne 1 item.

   CENÁRIO 4 — Combinada explícita da casa (1 aposta única):
   O print mostra MÚLTIPLAS seleções com odds individuais visíveis, MAS há sinais claros de que é UMA única aposta combinada:
   - Palavra explícita "TRIPLA", "DUPLA", "MÚLTIPLA", "COMBO", "ACUMULADA" no print
   - Uma ODD TOTAL/COMBINADA calculada (ex: "Odd total 15.10")
   - UM ÚNICO valor apostado (ex: "Valor apostado 0.50")
   - UM ÚNICO possível ganho/retorno
   Nesse caso as odds individuais são só INFORMATIVAS (mostrando como a combinada foi calculada) — NÃO representam apostas separadas. Retorne 1 item único:
   - tipo_aposta: "Tripla" (ou Dupla/Múltipla conforme número de seleções)
   - evento: apenas "Tripla" (ou "Dupla"/"Múltipla") — NÃO concatenar os eventos dos jogos
   - entrada: concatenar as seleções separadas por " + " (ex: "RC Lens - Toulouse Menos de 1.5 + Girona - Betis Mais de 0.5 + Brighton - Chelsea Mais de 2.5")
   - mercado: null (mercados diferentes)
   - esporte: se todos os jogos são do mesmo esporte, preencher; se mistos, null
   - odd: a odd total (15.10)
   - stake: o único valor do bilhete

   Ex: Print com 3 seleções de futebol, odd total 15.10, 0.50u apostado → 1 item:
   {{"tipo_aposta": "Tripla", "evento": "Tripla", "entrada": "RC Lens - Toulouse Menos de 1.5 + Girona - Betis Mais de 0.5 + Brighton - Chelsea Mais de 2.5", "odd": 15.10, "stake_unidades": 0.5, "esporte": "Futebol", "mercado": null}}

   REGRA-CHAVE: marcador explícito de combinada + odd total + valor único = Cenário 4 (1 item). Múltiplas odds sem marcador de combinada = Cenário 2 (N itens mapeados pela descrição). Bet builder do mesmo jogo = Cenário 1.

H. DESCRIÇÃO = N APOSTAS AUTOSSUFICIENTES (importante complemento ao Cenário 2):
   Quando a descrição contém várias linhas, cada linha representa UMA aposta que se diferencia do bilhete visual em algum aspecto (casa diferente, stake diferente, odd real pega, conta diferente). O número de apostas registradas deve bater com o número de LINHAS ÚTEIS da descrição, não com o número de odds do print.
   - Campos INFORMADOS na linha → usar os valores da descrição
   - Campos OMISSOS na linha → herdar do print (seleção, mercado, evento, esporte, etc.)
   - Mapeamento da seleção: se a descrição tem identificador textual ("dupla", "chape", "vitória", "linha KOLESIE +4.5"), use. Se não, mapeie pela odd (linha com odd 1.72 mapeia pra seleção do print cuja odd é 1.72).
   - Se o operador escreveu a linha, é porque há algo diferente do print — NUNCA ignore uma linha da descrição assumindo que é redundante.

M. GRAMÁTICA DE ESTADOS POSICIONAIS NA DESCRIÇÃO (padrão crescente de uso):
   Um padrão comum de descrição é: "[bookie] [conta] [estado_1] [estado_2] [estado_3] ..."
   Onde cada estado mapeia POSICIONALMENTE para as apostas do print, NA ORDEM em que aparecem (incluindo combinadas listadas ao final do print).

   Estados possíveis:
   - "out" → operador NÃO apostou essa. NÃO REGISTRAR essa aposta (pular completamente).
   - "in" → operador apostou essa conforme o print (usa stake e odd do print como estão).
   - NÚMERO SOZINHO (ex: "105", "105,00", "95.33") → operador apostou essa com STAKE em REAIS igual ao número. Odd segue o print (salvo ajustes por outros estados ou por regras F2/F3).
   - "odd X.XX" → operador apostou essa com ODD REAL igual a X.XX (sobrescreve a odd do print, típico em drops ao vivo).
   - Combinações: "105 odd 95.33" → stake_reais 105 + odd 95.33 pra mesma aposta.

   REGRA DE DESAMBIGUAÇÃO CRÍTICA: número SOZINHO sempre é stake em reais; odd SÓ é odd quando precedida da palavra "odd". NUNCA inferir que um número grande é odd sem a palavra "odd" na frente.

   Exemplo: descrição "365 will out in in in 95,33" com print mostrando 3 simples + 1 tripla:
   - bookie: 365 (Bet365)
   - conta: will
   - aposta 1 (1ª simples): "out" → NÃO REGISTRAR
   - aposta 2 (2ª simples): "in" → registrar com stake/odd do print
   - aposta 3 (3ª simples): "in" → registrar com stake/odd do print
   - aposta 4 (tripla): "95,33" → registrar com stake_reais 95.33 (odd da tripla segue o print)
   Total: 3 apostas registradas (não 4).

   Quando a descrição segue essa gramática, ela é a fonte DEFINITIVA de quais apostas foram feitas. Não invente apostas a mais nem a menos.

I. TIPO DE APOSTA — use MATCH SEMÂNTICO com a lista de Tipos de Aposta cadastrados (acima). Use o nome EXATO do cadastro quando reconhecer a intenção. Tipos padrão:
   - "Simples": 1 única seleção
   - "Dupla": 2 seleções combinadas de JOGOS/MERCADOS DIFERENTES, odd única
   - "Tripla": 3 seleções combinadas de jogos/mercados diferentes, odd única
   - "Múltipla": 4+ seleções combinadas de jogos/mercados diferentes, odd única
   - "Criar Aposta": bet builder — múltiplas seleções DO MESMO JOGO combinadas pela casa, odd única (ex: jogador X faz pontos + rebotes + assistências no mesmo jogo)
   Podem existir outros tipos cadastrados (ex: "Super Aumentada", "Aposta Turbinada"). Quando o print mostrar marcador EXPLÍCITO de uma dessas modalidades especiais (ex: faixa "SUPER AUMENTADA", "GANHOS AUMENTADOS", "ODD BOOSTED"), use o tipo correspondente do cadastro se existir. Se o marcador existe mas não há tipo correspondente na lista, use "Criar Aposta" como fallback.
   Se não der pra inferir de forma alguma, null.

J. CONFIANÇA: se NÃO tiver certeza de algum campo ESPECÍFICO, deixe esse campo como NULL. É melhor null do que errado — o operador confere depois.

K. INDEPENDÊNCIA DOS CAMPOS (CRÍTICO): cada campo é extraído SEPARADAMENTE dos outros. Incerteza, impasse ou dificuldade em UM campo NUNCA deve fazer outros campos virarem null por tabela. Exemplos:
   - Se a odd 33.48 está claramente visível, registre 33.48 — mesmo que você tenha dúvida sobre stake, mercado ou qualquer outro campo.
   - Se o tipster está claro no cabeçalho, registre o tipster — mesmo que tenha dúvida sobre a data do evento.
   - Se você conseguiu identificar o evento e o esporte, registre — mesmo que não tenha certeza do tipo de aposta.
   NUNCA "desista" de um bilhete inteiro por causa de um campo duvidoso. Extraia TUDO que você consegue, e deixa null SÓ os campos em que você realmente não tem certeza.

L. EXTRAÇÃO DE EVENTO E MERCADO DIRETO DO PRINT: quando o print mostra claramente os times e o mercado, EXTRAIA. Não deixe null por excesso de cautela.
   - EVENTO: o print sempre mostra os times/participantes. Aceite qualquer separador: "Time A x Time B", "Time A - Time B", "Time A vs Time B", "Time A @ Time B". Todos viram o campo evento. Ex do print "Operário-PR - Fluminense" → evento: "Operário-PR x Fluminense".
   - MERCADO — HIERARQUIA DE INFERÊNCIA (prioridade absoluta do topo pro fundo):
     1) RÓTULO DO MERCADO ESCRITO PELA CASA NO BILHETE (fonte primária, sempre prevalece): é o texto que a casa pinta acima/ao lado da seleção no próprio bilhete de apostas. Exemplos de rótulos da casa: "Jogador a Dar Assistência", "Jogador a Marcar", "Total de Rebotes", "Total de Cartões Mais/Menos", "Resultado Final", "Ambos Marcam", "Handicap Asiático", "Handicap de Rondas", "Mais/Menos Gols", "Qualificar-se", "Partida - Vencedor - 2 Opções".
     2) DESCRIÇÃO/MÉTODO DO TIPSTER (apenas confirmação): texto tipo "Método: Gol", "Método: Assistência", "Tipo: Cartão" que o tipster coloca junto ao print. Serve para CONFIRMAR o rótulo da casa, nunca para sobrescrever. Se o rótulo da casa diz "Jogador a Dar Assistência" e o tipster diz "Método: Assistência", ambos concordam → Assistências. Se conflitarem (raro), prevalece a casa.
     3) ASSOCIAÇÃO POR TIPSTER (último recurso): só use se (1) e (2) estiverem indisponíveis. Nunca classifique um mercado só porque "tipster X geralmente posta mercado Y" — o mesmo tipster pode operar mercados diferentes.
   - MATCH SEMÂNTICO COM LISTA CADASTRADA: após identificar o mercado pela hierarquia acima, faça match semântico com a lista de Mercados cadastrada (pode vir em qualquer idioma — português, inglês, espanhol — e com descrições variadas conforme a casa). Use o nome EXATO do cadastro quando reconhecer a intenção.
     * Exemplos de match semântico:
       - Rótulo da casa "Marcar a Qualquer Momento" / "Anytime Goalscorer" / "Anytime Scorer" → mercado cadastrado "Anytimes" (se existir)
       - Rótulo "Jogador a Dar Assistência" / "Anytime Assist" → "Assistências" (se cadastrado)
       - Rótulo "Match Winner" / "Moneyline" / "Resultado Final" / "Vencedor" / "Partida - Vencedor" → "Resultado Final" (se cadastrado)
       - Rótulo "Both Teams to Score" / "BTTS" / "Ambos Marcam" → "Ambos Marcam" (se cadastrado)
       - Rótulo "Over/Under Cards" / "Total de Cartões Mais/Menos" → "Cartões Mais/Menos" ou similar
       - Rótulo "Handicap de Rondas" / "Round Handicap" / "Map Handicap" → "Handicap de Rondas" (se cadastrado)
     * Se NENHUM mercado da lista corresponder semanticamente, extraia o texto como aparece no print.
     * Só deixe null se não houver rótulo de mercado identificável no print.

FORMATO DE RESPOSTA (JSON puro, sem markdown):

{{
  "apostas": [
    {{
      "data_evento": "YYYY-MM-DD" ou null,
      "evento": "ex: 1W x GenOne" ou null,
      "esporte": "nome igual ao cadastro" ou null,
      "mercado": "ex: Handicap de rondas, Ambos Marcam" ou null,
      "entrada": "ex: 1W -3.5 Mapa 3, Sim, Over" ou null,
      "odd": number ou null,
      "stake_unidades": number ou null,
      "stake_reais": number ou null,
      "tipo_aposta": "nome igual ao cadastro (Simples|Dupla|Tripla|Múltipla|Criar Aposta|Super Aumentada|outros)" ou null,
      "tipster": "nome igual ao cadastro" ou null,
      "operador": "nome igual ao cadastro" ou null,
      "bookie": "nome igual ao cadastro" ou null,
      "contas_utilizadas": "separadas por vírgula" ou null
    }}
  ]
}}

Responda APENAS com o JSON.

ANTES DE RESPONDER, EXECUTE O CHECKLIST [R5]:
1. O print tem "CRIAR APOSTA"/"BET BUILDER" escrito? → Se sim, 1 item só.
2. Alguma data que coloquei veio de texto EXPLÍCITO do print/descrição? → Se não, use HOJE ({data_hoje}).
3. Tem time conhecido (NBA, futebol brasileiro/europeu, etc)? → Esporte marcado corretamente.
4. Descrição tem "365"? → bookie = Bet365 (não null).
5. Campos preenchíveis que deixei null: são realmente impossíveis de inferir, ou fui cauteloso demais?
"""

    img_b64 = base64.b64encode(imagem_bytes).decode('ascii')

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    )

    text = resp.content[0].text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)

# ============================================================
# CONVERTE DADOS EXTRAÍDOS → LINHA DO SUPABASE
# ============================================================
def find_id(arr, nome):
    if not nome: return None
    nome_l = nome.lower().strip()
    # Exato
    for x in arr:
        if x['nome'].lower() == nome_l:
            return x['id']
    # Partial
    for x in arr:
        if nome_l in x['nome'].lower() or x['nome'].lower() in nome_l:
            return x['id']
    return None

def get_stake_valor(tipster_id, data_evento, stakes):
    if not tipster_id or not data_evento:
        return None
    cand = [s for s in stakes if s['tipster_id'] == tipster_id and s['vigente_a_partir'] <= data_evento]
    if not cand: return None
    cand.sort(key=lambda s: s['vigente_a_partir'], reverse=True)
    return float(cand[0]['valor_reais'])

def montar_linha(ap, cadastros):
    """Converte um item do JSON extraído numa linha do Supabase."""
    tipster_id  = find_id(cadastros['tipsters'], ap.get('tipster'))
    bookie_id   = find_id(cadastros['bookies'], ap.get('bookie'))
    operador_id = find_id(cadastros['operadores'], ap.get('operador'))
    esporte_id  = find_id(cadastros['esportes'], ap.get('esporte'))

    su = ap.get('stake_unidades')
    sr = ap.get('stake_reais')
    data_ev = ap.get('data_evento')

    # Conversão bidirecional: se tem uma ponta + valor da unidade, calcula a outra
    if tipster_id and data_ev:
        sv = get_stake_valor(tipster_id, data_ev, cadastros['stakes'])
        if sv:
            if su and not sr:
                sr = su * sv
            elif sr and not su:
                su = round(sr / sv, 2)

    linha = {
        'data_evento': data_ev,
        'evento': ap.get('evento'),
        'esporte_id': esporte_id,
        'mercado': ap.get('mercado'),
        'entrada': ap.get('entrada'),
        'odd': ap.get('odd'),
        'stake_unidades': su,
        'stake_reais': sr,
        'tipo_aposta': ap.get('tipo_aposta'),
        'tipster_id': tipster_id,
        'operador_id': operador_id,
        'bookie_id': bookie_id,
        'contas_utilizadas': ap.get('contas_utilizadas'),
        'status': 'PENDING',
    }
    return {k: v for k, v in linha.items() if v is not None}

# ============================================================
# PROCESSAMENTO DE UMA MENSAGEM
# ============================================================
def processar_mensagem(msg, cadastros):
    msg_id = msg['message_id']
    foto = msg.get('photo')
    texto = msg.get('caption') or msg.get('text') or ''

    if not foto:
        # Pode ser resposta a um print — verificar reply_to
        reply = msg.get('reply_to_message')
        if reply and reply.get('photo'):
            # Mensagem de texto respondendo um print
            # TODO: poderia combinar descrição adicional com o print original
            return
        return  # Não é print, ignora

    # Pega a maior resolução
    file_id = foto[-1]['file_id']

    print(f"\n📸 Processando msg #{msg_id}: '{texto[:80]}'")

    # Marca como processando
    tg_react(msg_id, '👀')

    try:
        img_bytes = tg_get_file_bytes(file_id)
        if not img_bytes:
            tg_react(msg_id, '💩')
            return

        data_hoje = datetime.now(BRT).strftime('%Y-%m-%d')
        # Nome de quem enviou o print (operador) — mapeia pela INICIAL do first_name
        # S = Samuel, A = Amaral, D = Diego
        from_user = msg.get('from') or {}
        fname = (from_user.get('first_name') or from_user.get('username') or '').strip()
        inicial = fname[:1].upper() if fname else ''
        mapa_operador = {'S': 'Samuel', 'A': 'Amaral', 'D': 'Diego'}
        operador_nome = mapa_operador.get(inicial, fname)
        print(f"  👤 from='{fname}' → inicial='{inicial}' → operador='{operador_nome}'")
        resultado = extrair_aposta(img_bytes, texto, cadastros, data_hoje, operador_nome)

        apostas = resultado.get('apostas', [])
        if not apostas:
            print("  ⚠️ Nenhuma aposta detectada")
            tg_react(msg_id, '🤔')
            return

        # Registra cada aposta no Supabase
        sucesso = 0
        for ap in apostas:
            try:
                linha = montar_linha(ap, cadastros)
                if not linha.get('data_evento'):
                    linha['data_evento'] = data_hoje
                sb_insert('apostas', linha)
                sucesso += 1
            except Exception as e:
                print(f"  ❌ Erro salvando aposta: {e}")
                traceback.print_exc()

        if sucesso == len(apostas):
            print(f"  ✅ {sucesso} aposta(s) registrada(s)")
            tg_react(msg_id, '🔥')
        elif sucesso > 0:
            print(f"  ⚠️ {sucesso}/{len(apostas)} apostas registradas")
            tg_react(msg_id, '🤨')
        else:
            print(f"  ❌ Nenhuma aposta salva")
            tg_react(msg_id, '💩')

    except Exception as e:
        print(f"  ❌ Exception: {e}")
        traceback.print_exc()
        tg_react(msg_id, '💩')

# ============================================================
# LOOP DE POLLING
# ============================================================
def main():
    print("🤖 Bot de Prints iniciando...")
    print(f"  Supabase: {SUPABASE_URL}")
    print(f"  Chat ID: {CHAT_ID}")

    # Descobre o último update_id atual para NÃO processar mensagens antigas
    initial = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                           params={'limit': 1, 'offset': -1}, timeout=15).json()
    last_update = 0
    if initial.get('ok') and initial.get('result'):
        last_update = initial['result'][0]['update_id']
    offset = last_update + 1
    print(f"  Offset inicial: {offset}")

    # Recarrega cadastros a cada 5 min pra captar novos
    ultimo_refresh = 0
    cadastros = None

    while True:
        try:
            # Refresh cadastros se passou 5 min
            agora = time.time()
            if agora - ultimo_refresh > 300:
                print("🔄 Recarregando cadastros...")
                cadastros = carregar_cadastros()
                ultimo_refresh = agora
                print(f"  {len(cadastros['tipsters'])} tipsters, {len(cadastros['bookies'])} bookies, {len(cadastros['operadores'])} operadores")

            # Long polling
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                             params={'offset': offset, 'timeout': 30}, timeout=60)
            if not r.ok:
                time.sleep(5)
                continue

            data = r.json()
            if not data.get('ok'):
                time.sleep(5)
                continue

            for update in data['result']:
                offset = update['update_id'] + 1
                msg = update.get('message') or update.get('channel_post')
                if not msg:
                    continue
                if msg['chat']['id'] != CHAT_ID:
                    continue
                processar_mensagem(msg, cadastros)

        except Exception as e:
            print(f"Loop error: {e}")
            traceback.print_exc()
            time.sleep(5)

if __name__ == '__main__':
    main()
