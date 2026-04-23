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
    """Carrega listas de cadastros do Supabase pra contextualizar o Claude.
    Tipsters, bookies e esportes inativos são filtrados (nao aparecem como opcoes)."""
    return {
        'tipsters': sb_get('tipsters?select=id,nome&ativo=eq.true'),
        'bookies': sb_get('bookies?select=id,nome&ativo=eq.true'),
        'operadores': sb_get('operadores?select=id,nome'),
        'esportes': sb_get('esportes?select=id,nome&ativo=eq.true'),
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

[R2] CRIAR APOSTA = 1 ITEM SÓ. Detecte Criar Aposta por DUAS VIAS:

   VIA A — palavra explícita: se o print tem "CRIAR APOSTA", "BET BUILDER", "ACUMULADOR DO JOGO", "MEU BILHETE" ou "JOGO DO SEU JEITO" escrita explicitamente (geralmente em destaque no topo).

   VIA B — SINAIS ESTRUTURAIS (sem palavra explícita, comum no JDF-VIP e outros): o print mostra
   - Múltiplas seleções (2+) DO MESMO JOGADOR ou DO MESMO JOGO
   - UMA ÚNICA cotação/odd total (ex: "COTAÇÕES 30.57", "45.81")
   - UM ÚNICO botão APOSTAR com valor (ex: "APOSTAR R$ 1", "APOSTAR R$ 0,5")
   - UM ÚNICO valor de "Ganhos potenciais" / "Retornos potenciais"
   - Ícones de trash (🗑️) em cada linha da seleção
   - Sem botões/campos de aposta individuais por linha
   - OPCIONAL: texto "N Seleções" / "N Selections" em destaque no topo (sinal definitivo)

   Quando detectar por qualquer uma das vias, é UMA aposta única com tipo_aposta = "Criar Aposta". JAMAIS quebre em várias apostas. Concatene TODAS as seleções no campo entrada, separadas por vírgula. A odd é única.

   EXEMPLO NEGATIVO (PROIBIDO): ver print com "3 Seleções" em destaque + Paolo Banchero 30+ pontos + Paolo Banchero 10+ rebotes + Paolo Banchero 6+ assistências + odd 45.81 + R$ 1 e registrar 3 itens como Simples. ERRADO. O correto é: 1 item, tipo "Criar Aposta", entrada "Paolo Banchero 30+ Pontos, 10+ Rebotes, 6+ Assistências", odd 45.81.

   MESMO PADRÃO NO JDF-VIP: seleções do mesmo jogador empilhadas + UMA cotação + UM valor APOSTAR = Criar Aposta SEMPRE, independente de a palavra estar escrita ou não.

   INVERSO TAMBÉM PROIBIDO: sem NENHUM dos sinais acima, NÃO classifique como Criar Aposta. Se há 1 seleção só, é Simples.

[R3] BET365 COMO BOOKIE: quando a descrição começa com "365" (ex: "365 deia", "365 will in in"), o bookie é SEMPRE Bet365. Nunca deixe bookie null nesse caso.

[R4] ESPORTE — ESPECÍFICO PREVALECE SOBRE GENÉRICO: se a lista de esportes tem "NBA" cadastrado separadamente de "Basquete", jogos da NBA (Lakers, Spurs, Trail Blazers, Warriors, Celtics, Heat, Nets, Bulls, Knicks, Rockets, Mavericks, Suns, Grizzlies, 76ers, Kings, Clippers, Pelicans, Hawks, Raptors, Wizards, Magic, Hornets, Pacers, Pistons, Thunder, Jazz, Nuggets, Timberwolves, Bucks) → esporte: NBA. Mesma lógica vale pra outras ligas específicas cadastradas (WNBA, Euroliga, NCAA, Futebol Feminino, etc.). Se só tem "Basquete" genérico, use Basquete.

[R5] RÓTULO DA CASA PREVALECE SEMPRE: o texto que a CASA DE APOSTAS pinta explicitamente acima/ao lado da seleção no bilhete é a FONTE PRIMÁRIA do mercado. Nunca ignorar rótulos da casa como:
   - "Total de pontos" / "Total games" / "Total de rebotes" / "Total de cartões" / "Total chutes" → mapeia pro mercado cadastrado correspondente (Pontos, Games, Rebotes, Cartões, Chutes, etc.)
   - "Handicap de Games" / "Handicap de Set" / "Handicap de Rondas" / "Handicap do Jogo" / "Handicap de Rounds" → mercado correspondente do cadastro
   - "Vencedor do Mapa" / "ML Mapa" / "Map Winner" → "ML Mapa" (se cadastrado, DIFERENTE de "Vencedor" / "ML" genérico)
   - "Resultado/Ambos Marcam" / "Ambos Marcam" / "Both Teams to Score" → mercado do cadastro
   - "Jogador a Marcar" / "Jogador a Dar Assistência" / "Marca Gol" → mercado do cadastro
   Fazer match semântico com a lista de Mercados cadastrada (incluindo traduções: "Anytime Goalscorer" ↔ "Anytimes", "Match Winner" ↔ "Resultado Final", etc). JAMAIS retornar "ML" ou null quando há rótulo explícito da casa no print.

   ESPECIFICIDADE IMPORTA: "Chutes" ≠ "Chutes no gol" ≠ "Chutes a gol". Cada um é um mercado distinto. Nunca generalizar ou especificar além do que o rótulo literal do print indica. "Total chutes" mapeia pra "Chutes" (geral). "Chutes no gol" / "Shots on target" mapeia pra "Chutes no gol" (específico).

[R6] TIPO DE APOSTA — SIMPLES É O DEFAULT: se há 1 seleção = Simples. Só desvie dessa regra quando houver SINAL INEQUÍVOCO:
   - Palavra "CRIAR APOSTA"/"BET BUILDER" escrita OU sinais estruturais de bet builder (ver R2) → Criar Aposta
   - Palavra "DUPLA"/"TRIPLA"/"MÚLTIPLA"/"Múltipla" escrita + múltiplas seleções + uma odd total → tipo correspondente
   - Marcador de bônus ("SUPER AUMENTADA", "APOSTA AUMENTADA") → Super Aumentada (se cadastrado)
   Sem esses sinais, mesmo com layouts complexos ou múltiplas linhas visuais, é Simples.

[R8] ODD DA DESCRIÇÃO PREVALECE SEMPRE SOBRE ODD DO PRINT. Quando a descrição do operador contém "odd X.XX" ou "odd X,XX" (palavra "odd" literal seguida de valor, incluindo typos "od", "ood", "odds"), esse valor SUBSTITUI a odd do print. Sem exceção. Motivos comuns: drops ao vivo, bônus aplicado no print, odd real pega divergindo do sugerido.
   Exemplo: print mostra "Odds: 2.50" e descrição diz "365 deia odd 2,37" → odd: 2.37 (NÃO 2.50).
   Exemplo: print mostra "1.79x" e descrição diz "pinnacle odd 1.746" → odd: 1.746.
   
   IMPORTANTE — ODD DA LEGENDA DO TIPSTER: quando a legenda do tipster contém "Odds: X.XX" ou "Odd: X.XX" em linha rotulada (ex: "Stake: 0.50u\nOdds: 3.40"), esse é o valor primário de odd. NUNCA deixe odd em 0 quando há "Odds: X.XX" ou "Odd: X.XX" no texto — extraia diretamente.

[R9] "BH" NO CABEÇALHO = "BH CS" + COUNTER-STRIKE. Quando o cabeçalho do print contém "BH" (tipicamente "BH Tipster"), o tipster é SEMPRE "BH CS" e o esporte é SEMPRE Counter-Strike. Essa regra tem prioridade absoluta sobre outros identificadores no mesmo cabeçalho (ex: "@GuiaDasApostas" no final não muda o tipster pra GDA quando há "BH" no início).

[R9.1] "Projeto Fezinha" NO CABEÇALHO = SEMPRE 1 APOSTA MÚLTIPLA, NUNCA INDIVIDUAIS. Tipsters do Projeto Fezinha SÓ enviam combinadas — nunca são feitas as entradas individuais. Regra:
   - Cabeçalho contém "Projeto Fezinha" ou "Fezinha" → gere EXATAMENTE 1 aposta no array, tipo_aposta conforme número de seleções: 2=Dupla, 3=Tripla, 4+=Multipla.
   - NUNCA gere 1 aposta por seleção mesmo se o bilhete mostrar cada jogo listado com sua própria odd.
   - NUNCA gere individuais + 1 múltipla (duplicação). Retorne SOMENTE a múltipla.
   - Campo odd = "Odd total" do bilhete (SEMPRE presente, geralmente no rodapé: "Odd total 142.62", "Múltipla de 6 = 107.90", etc).
   - Campo stake_unidades = única stake do bilhete (geralmente no rodapé: "0.15", "Valor apostado 0.15").
   - Campo entrada = seleções concatenadas separadas por " + " (ex: "Menos de 7.5 + Mais de 29.5 + Mais de 0.5 + ..."). Mantenha curto.
   - Campo evento = "Múltipla" (simples, sem concatenar nomes de jogos).
   - Campo mercado = "Múltiplos" se houver mercados variados.
   - Campo esporte = null se houver esportes mistos.

[R9.5] LEGENDA DO TIPSTER É FONTE PRIMÁRIA DE SELEÇÃO. Quando o bilhete mostra múltiplas opções (ex: Resultado Final 1x2 com Bahia 1.80 / Empate 3.60 / Santos 4.50, Over/Under, handicaps) SEM destaque visual de qual foi escolhida, a LEGENDA DO TIPSTER (texto abaixo ou próximo do bilhete) é a ÚNICA fonte confiável da seleção real. Padrão comum: "[seleção] @[odd] / [stake]" ou "[seleção] @[odd]" ou apenas uma linha narrativa tipo "[seleção] vence".
   - Exemplo: bilhete mostra Bahia 1.80 / Empate 3.60 / Santos 4.50 e legenda diz "Bahia vence @1.80 / 1,25u" → entrada: "Bahia", odd: 1.80, stake_unidades: 1.25, mercado: ML (resultado final).
   - Exemplo: bilhete Over/Under 2.5 com ambas odds visíveis e legenda "Under 2.5 @1.95 / 0.5u" → entrada: "Under 2.5", odd: 1.95, stake_unidades: 0.5.
   - Exemplo: bilhete 1x2 sem destaque e legenda "Empate @3.20" → entrada: "Empate", odd: 3.20.
   NUNCA pegue a primeira odd visível ou a odd maior por default. Se a legenda existe, ELA RESOLVE tudo — entrada, odd e stake saem da linha `@odd / Xu`.

[R10] STAKE É DADO CRÍTICO — SEMPRE PROCURAR. Extração de stake deve acontecer SEMPRE. Procure em TODA a imagem (bilhete + legenda do tipster + anotações) E na descrição do operador. Padrões comuns que DEVEM ser extraídos:
   - "Xu", "X.Yu" (ex: "1u", "0.5u", "1.25u", "0.75u")
   - "X%" (ex: "0.75%", "1%", "2%") — equivalente a unidades
   - "@odd / Xu" (ex: "@5.50 / 0.75u", "@1.75 / 1u")
   - "Stake: Xu", "Stake X", "Stake 0.50u", "Stake: 1u" (linha rotulada em legenda do tipster — SEMPRE extrair)
   - "Xu - Min XXX", "Xu // Min XXX" (a letra "u" indica stake; "Min X.XX" ao lado é odd mínima, NUNCA interprete "Min" como stake)
   - "Odds: X.XX" + linha separada "Stake: Yu" ou apenas "Yu" → odd vem de "Odds:" e stake vem do "Yu"
   - "X unid", "X unidade"
   - "[entrada] - Xu" (ex: "Buse -4.5 - 1u")
   - "Xu" sozinho numa linha da legenda do tipster (ex: linhas soltas "LIVE", "1u", "Min - 1.62" — o "1u" É A STAKE, "Min - 1.62" é odd mínima)
   - Número sozinho em linha (ex: "846" → stake_reais 846)
   stake_unidades null só é aceitável quando NÃO há absolutamente nenhum número associado a "u", "%" ou "stake" em lugar nenhum.
   
   ATENÇÃO: legendas de tipster frequentemente vêm em formato estruturado com linhas rotuladas tipo "Stake: Xu" / "Odds: Y.YY" / "Min: Z.ZZ". Esses rótulos são FONTE PRIMÁRIA — extraia o valor diretamente da linha rotulada. NUNCA deixe stake em zero quando há "Stake: Xu" ou "Xu" sozinho em linha no texto da mensagem.

[R7] CHECKLIST FINAL ANTES DE RESPONDER: antes de emitir o JSON, verifique mentalmente:
   - O print tem "CRIAR APOSTA"/"BET BUILDER" escrito OU sinais estruturais de bet builder? → 1 item só. Se NÃO, não invente.
   - Data que coloquei veio de texto EXPLÍCITO do print/descrição? → Se não, use HOJE ({data_hoje}).
   - Times conhecidos? → Esporte marcado (específico se existir, NBA/WNBA/Futebol Feminino/etc).
   - Descrição começa com bookie? → Preenchi o bookie.
   - Palavra após bookie? → Preenchi a conta (CRÍTICO).
   - Rótulo da casa no bilhete? → Preenchi o mercado via match semântico.
   - Bilhete mostra múltiplas opções (1x2, Over/Under) sem destaque visual? → Usei a LEGENDA DO TIPSTER pra definir entrada/odd/stake. Não peguei odd aleatória.
   - Legenda do tipster tem "Stake: Xu" ou "Xu" sozinho em linha? → stake_unidades PREENCHIDA (NUNCA zero).
   - Legenda do tipster tem "Odds: X.XX"? → odd PREENCHIDA (NUNCA zero).
   - "Xu" ou "X%" em QUALQUER parte do texto? → Preenchi stake_unidades.
   - Descrição tem "odd X.XX"? → USEI esse valor, não o do print.
   - Tipo_aposta: 1 seleção sem marcador = Simples. Não inventar Criar Aposta.

═══════════════════════════════════════════════════════════════

REGRAS DETALHADAS:

A. OPERADOR: use o nome "{operador_msg}" (quem enviou o print). Se bater com algum nome da lista de operadores, use o nome exato do cadastro. Se não bater, deixe como está.

B. TIPSTER: deduza pelo cabeçalho/título do print. O nome que aparece no cabeçalho pode ser:
   (i) o nome do GRUPO diretamente (ex: "BH Tipster" → "BH CS")
   (ii) um USUÁRIO/APELIDO que assina o print em nome do grupo (ex: "Italo Cards" aparece no print mas o grupo está cadastrado como "Italo Cartões")
   (iii) o nome ESTILIZADO com separadores decorativos — pontos, traços, espaços entre letras (ex: "P.R.O.P.H.E.T" → "PROPHET", "J D F - V I P" → "JDF-VIP", "S.E.T.T.A" → "SETTA"). NORMALIZE removendo pontos, hífens e espaços extras antes de comparar com a lista.
   (iv) MÚLTIPLOS NOMES unidos por "&", "|", "/", "+", "e" (ex: "Syro & GuiaDasApostas", "NBA 25/26 | Live | Syro & GuiaDasApostas"). Nesse caso, tente casar CADA palavra/nome individualmente com a lista — o cadastro pode usar só parte do nome, abreviações ou reordenação (ex: "Syro & GuiaDasApostas" pode estar cadastrado como "GDA Syro", "Syro GDA", "Syro", etc). Se alguma palavra casa, use o nome exato do cadastro.
   (v) COM DECORAÇÕES VISUAIS (bandeiras, emojis, ícones, separadores decorativos). Ex: "O Apostador 🇧🇷🇬🇧 - Free eSports 🎮", "Vôlei - Staking - @Guia", "VIP Tradicional - @GuiaDasApostas". IGNORE as decorações visuais (emojis, bandeiras) e extraia APENAS os termos textuais como candidatos a tipster. Do exemplo "O Apostador 🇧🇷🇬🇧 - Free eSports 🎮", os candidatos são: ["O Apostador", "Free eSports"] — tente ambos no match.
   (vi) COM "@[palavra]" indicando handle/identificador do tipster (ex: "@Guia", "@GuiaDasApostas", "@Syro"). A palavra após o "@" é um identificador forte — use ela no match por tokens contra a lista de tipsters.

   CONHECIMENTO DE DOMÍNIO SOBRE SIGLAS:
   - "GDA" = abreviação de "GuiaDasApostas". Portanto, cabeçalhos contendo "GuiaDasApostas", "Guia Das Apostas", "Guia", "@Guia", "@GuiaDasApostas", "@GDA" DEVEM casar com tipsters cadastrados que começam com "GDA " (ex: "GDA Syro", "GDA Vôlei", "GDA Tradicional", "GDA CS"). Use o contexto (esporte, mercado) pra decidir qual GDA exato se houver múltiplos.
   - "BH" pode ser prefixo de "BH Tipster" → tipsters cadastrados que começam com "BH " (ex: "BH CS")

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

   SINAIS DE UNIDADE EM QUALQUER FONTE (print OU descrição): letra "u" ("2u", "1.5u", "0.5unidade", "0.75 unid") e sinal "%" ("0.75%", "1.5%", "2%") são sinais EQUIVALENTES e ambos representam UNIDADES. Aplique em qualquer texto/número que esteja na imagem (bilhete, legenda do tipster, anotações) ou na descrição do operador.

   EXTRAÇÃO É IMPRESCINDÍVEL — reconheça "Xu" ou "X.Yu" ou "X%" em QUALQUER posição do texto, independente do formato ao redor. Padrões comuns que DEVEM ser extraídos (não ignorar nenhum):
   - "0.75u" sozinho em linha
   - "Stake: 0.75u"
   - "Odds: 2.62 / 0.75u"
   - "@5.50 / 0.75u" (formato comum em bilhetes com tipster estilizado)
   - "Buse -4.5 - 1u" (valor no final com múltiplos traços)
   - "0.5u — Vale até X" (valor seguido de nota)
   - "1% - limite 50" (% no início, seguido de limite)
   - "0.75 unidade" / "0.75 unid"
   - "1.5u" dentro de um texto corrido
   - "Min-1.60 Stake 0.5u" (combinado com outras infos)
   Menções de alternativas ("Vale até X", "também pode pegar Y") NÃO alteram o stake principal. A stake é o número que está visivelmente associado a "u" ou "%".

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
   - CONTA É DADO CRÍTICO: sempre tente extrair. Qualquer palavra não-reservada próxima ao bookie é conta. Palavras reservadas: "in", "out", "odd", "ood", "od", "odds", e números. Tudo o mais é candidato a conta.
   - NOMES COMUNS DE CONTA (extrair SEMPRE que aparecerem na descrição): marta, will, deia, dany, ellian, nicolas, greice, leite, danyel, sorgetz, reis, kapola, vini, roger, gabi, fejao, gabriel, edson, william, chay, luciadritrich, deiav, cromo, dudu, phgpedrinho, dudulemos, geospich, pedrinho, maiato, cassiel, lucia. Essa lista NÃO é exaustiva — qualquer nome próprio ou apelido curto após bookie deve ser tratado como conta.
   - PALAVRAS QUALITATIVAS (não são contas específicas): quando a descrição tem "Todas", "Várias", "All", "Geral" antes ou depois do bookie (ex: "Todas AG - 846 odd 1,89"), isso indica uso genérico de várias contas SEM especificar quais. Deixe contas_utilizadas null E CONTINUE extraindo normalmente os outros campos (bookie, stake, odd). NÃO deixe essa palavra bloquear a extração dos tokens seguintes.
     Exemplo: "Todas AG - 846 odd 1,89" → bookie: Aposta Ganha, contas: null, stake_reais: 846, odd: 1.89.
   - ATENÇÃO — BOOKIE DO PRINT ≠ BOOKIE DA APOSTA: quando o print do TIPSTER mostra "LINK:", "Casa:", "Disponível em:", "Código:", ou um link de uma casa (ex: "https://www.bet365.com/..."), isso indica apenas ONDE O TIPSTER ACHOU A ODD, NÃO onde o operador efetivamente apostou. O bookie REAL da aposta SEMPRE vem da descrição do operador. Se a descrição menciona uma casa diferente da do print, a descrição PREVALECE sem exceção.
     Ex: print do PROPHET mostra "LINK: Superbet" e descrição do operador diz "betfair dany 2.20" → bookie: Betfair, conta: dany. NÃO Superbet.
   - PADRÕES COMUNS: "[bookie] [conta]" (ex: "bet365 luciadritrich", "365 deia", "365 marta", "cassino greice") OU "[conta] [bookie]" (ex: "ellian betano", "dany betfair"). Interprete flexível.
   - BOOKIES CONHECIDOS (lista extensível): Bet365, Betano, Betfair, BETBRA, BetFast, BetVIP, Betfair, Pinnacle, Cassino, vBet (ou VBet), PixBet (ou BetPix, BetPix365), VaideBet, Aposta Ganha, Superbet, Stake, Esportes da Sorte, Rei do Pitaco, Sportingbet, KTO, entre outros. Qualquer palavra na descrição que pareça nome de casa deve ser considerada.
   - ABREVIAÇÕES DE BOOKIE (conservadoras, só use quando inequívoco):
     * "365" → Bet365
     * "AG" → Aposta Ganha
   - NOTAÇÃO `[conta]+N`: significa "conta X + N outras contas" (ex: "ellian+1", "reis+1", "dany+2"). PRESERVE LITERAL no campo contas_utilizadas, não expanda.
   - CONTAS MÚLTIPLAS: quando a descrição lista várias contas da mesma casa separadas por "+" (ex: "ellian + nicolas", "gabriel + william + edson"), preencha o campo contas_utilizadas concatenado ("ellian + nicolas").
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
   - Palavra explícita "TRIPLA", "DUPLA", "MÚLTIPLA", "Múltipla de N seleções", "COMBO", "ACUMULADA" no print
   - OU marcadores de oferta especial: "APOSTA AUMENTADA", "SUPER AUMENTADA", "GANHOS AUMENTADOS", "ODD BOOSTED", "APOSTA TURBINADA"
   - Uma ODD TOTAL/COMBINADA calculada (ex: "Odd total 15.10" ou odd única em destaque)
   - UM ÚNICO valor apostado (ex: "R$ 0.25", "APOSTE JÁ R$0,25")
   - UM ÚNICO possível ganho/retorno (ex: "Ganhos Potenciais R$ X")
   - NENHUM campo de stake ou botão "Apostar" individual por seleção (as seleções aparecem apenas como lista informativa)

   CRITÉRIO DECISIVO CENÁRIO 2 vs CENÁRIO 4 (CRÍTICO):
   - Cenário 2 (N+1 apostas): cada seleção tem seu PRÓPRIO campo de stake, botão de apostar, valor individualizado. O print permite apostar em cada uma separadamente.
   - Cenário 4 (1 aposta só): apenas a combinada tem stake/valor. Seleções individuais são lista informativa, sem campo próprio de aposta.
   Se a descrição do operador só menciona UMA casa/conta (ex: "betano ellian") e o print mostra marcador de combinada + stake única, é SEMPRE Cenário 4.

   No Cenário 4, retorne 1 item único:
   - tipo_aposta: ver regra I (marcador de bônus tem precedência sobre contagem de seleções)
   - evento: apenas "Dupla" / "Tripla" / "Múltipla" (conforme o tipo). NÃO concatenar nomes de jogos.
   - entrada: concatenar as seleções separadas por " + " (ex: "Corinthians e Sim + Vasco da Gama e Sim" ou "Mais de 9.5 Chutes + Menos de 33.5 Total de gols + CS Minaur +1.5 Handicap")
   - mercado: se os mercados forem iguais em todos os jogos, preencher (match semântico); se mistos, usar "Múltiplos"
   - esporte: se todos os jogos são do mesmo esporte, preencher. Se mistos (ex: Futebol + Futebol Feminino, NBA + WNBA, CS + LOL), null.
   - odd: a odd total/final (ex: 27.50)
   - stake: o único valor do bilhete

   Ex 1: Print com 3 seleções de futebol, odd total 15.10, 0.50u apostado → 1 item:
   {{"tipo_aposta": "Tripla", "evento": "Tripla", "entrada": "RC Lens Menos de 1.5 + Girona Mais de 0.5 + Brighton Mais de 2.5", "odd": 15.10, "stake_unidades": 0.5, "esporte": "Futebol", "mercado": null}}

   Ex 2: Print com "APOSTA AUMENTADA" + 2 seleções de futebol, odd 27.50, R$50 apostado → 1 item:
   {{"tipo_aposta": "Super Aumentada", "evento": "Dupla", "entrada": "Corinthians e Sim + Vasco da Gama e Sim", "odd": 27.50, "stake_reais": 50, "esporte": "Futebol", "mercado": "Ambos Marcam"}}

   Ex 3: Print com "TRIPLA (B x M / B x H x S)" + 3 seleções mistas + odd 8.83 + R$ 0.25 → 1 item:
   {{"tipo_aposta": "Tripla", "evento": "Tripla", "entrada": "Mais de 9.5 Chutes + Menos de 33.5 Total de gols + CS Minaur +1.5 Handicap", "odd": 8.83, "stake_unidades": 0.25, "esporte": null, "mercado": "Múltiplos"}}

   REGRA-CHAVE: marcador explícito de combinada OU de bônus + odd total + valor único + ausência de stakes individuais por seleção = Cenário 4 (1 item). Múltiplas odds COM stakes individuais por seleção = Cenário 2 (N itens mapeados pela descrição). Bet builder do mesmo jogo/jogador = Cenário 1.

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

   MULTI-CASA NA MESMA LINHA (padrão avançado): a descrição de UMA linha pode conter MÚLTIPLAS apostas na MESMA seleção feitas em casas diferentes. Formato:
   `[casa1] [conta1a] + [conta1b] [stake1] odd [odd1] + [casa2] [conta2a] + [conta2b] [stake2] odd [odd2] + ...`

   DIFERENCIAÇÃO DO "+" (CRÍTICO):
   - "+" seguido de NOME DE CASA CONHECIDA (betano, 365, bet365, AG, vbet, pinnacle, betfair, cassino, etc.) → início de NOVA aposta na mesma seleção
   - "+" seguido de OUTRA PALAVRA (nome de conta, etc.) → conta adicional da mesma casa

   Exemplo: `betano ellian + nicolas 114,00 odd 2.42 + 365 deia + marta 586,00 odd 2.62` → 2 apostas na mesma seleção:
   1. Betano, contas "ellian + nicolas", stake_reais 114.00, odd 2.42
   2. Bet365 (via "365"), contas "deia + marta", stake_reais 586.00, odd 2.62

   Exemplo: `betano ellian + nicolas 399,00 odd 2.32 + AG edson + chay + gabriel 287,00 odd 2.14` → 2 apostas:
   1. Betano, contas "ellian + nicolas", stake_reais 399.00, odd 2.32
   2. Aposta Ganha (via "AG"), contas "edson + chay + gabriel", stake_reais 287.00, odd 2.14

   NUNCA RETORNE LISTA VAZIA POR COMPLEXIDADE: mesmo que o print+descrição sejam complexos (5+ apostas, múltiplas casas, múltiplas contas), EXTRAIA TUDO que conseguir identificar. Melhor 7 apostas corretas + 2 null do que 0 apostas. Se alguma linha específica for ambígua, extraia as outras que estão claras.

N. PRINTS DE TELA DE CONFIRMAÇÃO DA CASA (R$ é REAL DE VERDADE):
   Quando o print é claramente uma TELA DE CONFIRMAÇÃO DA CASA DE APOSTAS (não um bilhete do tipster), os valores em R$ são REAIS DE VERDADE, não unidades. Sinais que indicam tela de confirmação:
   - Mostra "Aposta" ou "Aposta total" + valor em R$ (valor fixo, sem campo editável)
   - Mostra "Ganhos Potenciais" / "Retornos Potenciais" / "Total de ganhos" + valor em R$
   - Tem "Mais Detalhes" ou similar
   - Mostra status tipo "APOSTAS SIMPLES" ou "Aposta Feita"

   Nesse caso, preencha `stake_reais` diretamente com o valor apostado (ex: "R$ 225,00" → stake_reais: 225). O código vai converter pra unidades automaticamente usando o valor da unidade do tipster.

   Diferenciação:
   - Print do BILHETE PRONTO PRA APOSTAR (botão "Apostar", "Fazer aposta", "MÁX", campo editável "R$ 0,00") → valores "R$" no campo da stake são placeholder/exemplo do tipster (unidades).
   - Print de APOSTA JÁ CONFIRMADA/FEITA → valores "R$" são reais de verdade.

I. TIPO DE APOSTA — use MATCH SEMÂNTICO com a lista de Tipos de Aposta cadastrados (acima). Use o nome EXATO do cadastro quando reconhecer a intenção.

   HIERARQUIA DE DECISÃO (topo tem precedência):
   1) MARCADOR EXPLÍCITO DE OFERTA ESPECIAL NO PRINT → use o tipo correspondente do cadastro, IGNORANDO a contagem de seleções. Marcadores que disparam essa regra:
      - "SUPER AUMENTADA", "APOSTA AUMENTADA", "GANHOS AUMENTADOS" → "Super Aumentada" (se cadastrado)
      - "APOSTA TURBINADA", "ODD BOOSTED", "SUPER ODDS", "BOOST +X%" → tipo correspondente do cadastro
      - Transição visual de odd (ex: "21.37 >> 27.50") indicando aumento → idem
      - "CRIAR APOSTA", "BET BUILDER", "ACUMULADOR DO JOGO", "MEU BILHETE", "JOGO DO SEU JEITO" → "Criar Aposta"
      Importante: uma Dupla com marcador "APOSTA AUMENTADA" é "Super Aumentada", NÃO "Dupla". O marcador prevalece.
   2) CONTAGEM DE SELEÇÕES (quando não há marcador de oferta especial):
      - "Simples": 1 única seleção
      - "Dupla": 2 seleções combinadas de JOGOS/MERCADOS DIFERENTES, odd única
      - "Tripla": 3 seleções combinadas de jogos/mercados diferentes, odd única
      - "Múltipla": 4+ seleções combinadas de jogos/mercados diferentes, odd única

   Se houver marcador especial mas NENHUM tipo correspondente na lista cadastrada, use "Criar Aposta" como fallback.
   Se não der pra inferir de forma alguma, null.

J. CONFIANÇA: se NÃO tiver certeza de algum campo ESPECÍFICO, deixe esse campo como NULL. É melhor null do que errado — o operador confere depois.

K. INDEPENDÊNCIA DOS CAMPOS (CRÍTICO): cada campo é extraído SEPARADAMENTE dos outros. Incerteza, impasse ou dificuldade em UM campo NUNCA deve fazer outros campos virarem null por tabela. Exemplos:
   - Se a odd 33.48 está claramente visível, registre 33.48 — mesmo que você tenha dúvida sobre stake, mercado ou qualquer outro campo.
   - Se o tipster está claro no cabeçalho, registre o tipster — mesmo que tenha dúvida sobre a data do evento.
   - Se você conseguiu identificar o evento e o esporte, registre — mesmo que não tenha certeza do tipo de aposta.
   NUNCA "desista" de um bilhete inteiro por causa de um campo duvidoso. Extraia TUDO que você consegue, e deixa null SÓ os campos em que você realmente não tem certeza.

L. EXTRAÇÃO DE EVENTO E MERCADO DIRETO DO PRINT: quando o print mostra claramente os times e o mercado, EXTRAIA. Não deixe null por excesso de cautela.
   - EVENTO: o print sempre mostra os times/participantes. Aceite qualquer separador: "Time A x Time B", "Time A - Time B", "Time A vs Time B", "Time A @ Time B", "Time A v Time B". Todos viram o campo evento no formato "Time A x Time B". Ex do print "Operário-PR - Fluminense" → evento: "Operário-PR x Fluminense".
   - EVENTO EM MERCADOS DE JOGADOR (CRÍTICO): em mercados tipo "Jogador a Marcar", "Jogador a Dar Assistência", "Pontos/Rebotes/Assistências do Jogador", "Cartão do Jogador", a estrutura do print tem:
     (i) NOME DO JOGADOR (quem) → vai pra `entrada` (ex: "Davide Zappacosta", "LeBron James 10+ Pontos")
     (ii) MERCADO → vai pra `mercado` (ex: "Assistências", "Anytimes", "Pontos")
     (iii) PARTIDA "Time A v Time B" → vai pra `evento` (ex: "Atalanta x Lazio", "Bayer Leverkusen x Bayern de Munique")
     NUNCA coloque o nome do jogador no campo evento. O evento é SEMPRE os times da partida, geralmente aparecem logo abaixo do nome do jogador em fonte menor/mais sutil.
   - EVENTO INFERÍVEL DE TABELA DE OPÇÕES: quando o print não mostra "Time A x Time B" explícito mas lista os dois times como opções de resultado (ex: tabela com colunas/linhas "Flamengo / Vitória / Empate"), extraia os dois times como evento ("Flamengo x Vitória"). A palavra "Empate" nunca é time.
   - EVENTO TRUNCADO: quando o print mostra o nome dos times cortado ("Houston Rock...", "San Antonio Spu..."), NÃO completar por inferência. Use o texto parcial como aparece ou deixe null.
   - ENTRADA EM TABELA CRUZADA: quando o print mostra uma matriz tipo "Sim/Não × Resultados" (ex: colunas Sim/Não e linhas Flamengo/Vitória/Empate), a entrada é LINHA + COLUNA selecionada conforme descrição do operador. Ex: "Flamengo e Não @1.90" → entrada: "Flamengo e Não".
   - MERCADO — HIERARQUIA DE INFERÊNCIA (ver também [R5] nas regras absolutas):
     1) RÓTULO DO MERCADO ESCRITO PELA CASA NO BILHETE (fonte primária, sempre prevalece): é o texto que a casa pinta acima/ao lado da seleção no próprio bilhete. Sempre que houver rótulo explícito, USE ele (com match semântico à lista cadastrada). Exemplos: "Jogador a Dar Assistência", "Jogador a Marcar", "Total de pontos", "Total games", "Total de Rebotes", "Total de Cartões Mais/Menos", "Resultado Final", "Resultado/Ambos Marcam", "Ambos Marcam", "Handicap Asiático", "Handicap de Rondas", "Handicap de Set", "Handicap de Games", "Handicap do Jogo", "Mais/Menos Gols", "Qualificar-se", "Partida - Vencedor - 2 Opções", "Rebotes", "Pontos", "Pontos + Rebotes", "Pontos + Rebotes + Assistências", "Match Winner 2-Way".
     2) DESCRIÇÃO/MÉTODO DO TIPSTER (apenas confirmação): "Método: Gol", "Método: Assistência". Serve pra confirmar, nunca sobrescrever o rótulo da casa.
     3) ASSOCIAÇÃO POR TIPSTER (último recurso): nunca classifique só porque "tipster X costuma postar Y".
   - HANDICAP GENÉRICO EM ESPORTS: quando o print mostra apenas "Handicap" sem especificação adicional em jogos de esports (CS, LOL, DOTA, Valorant) e o valor é tipicamente -1.5/-2.5/+1.5/+2.5 (indicando mapas de vantagem em BO3/BO5), assuma "Handicap de Partida" se estiver cadastrado. Se o valor tiver contexto de "rondas" ou "mapa X", use "Handicap de Rondas" ou "Handicap de Mapas" conforme cadastro.
   - MATCH SEMÂNTICO COM LISTA CADASTRADA: após identificar o mercado pela hierarquia acima, faça match semântico com a lista de Mercados cadastrada (pode vir em qualquer idioma — português, inglês, espanhol — e com descrições variadas conforme a casa). Use o nome EXATO do cadastro quando reconhecer a intenção.
     * Exemplos de match semântico:
       - Rótulo da casa "Marcar a Qualquer Momento" / "Anytime Goalscorer" / "Anytime Scorer" → mercado cadastrado "Anytimes" (se existir)
       - Rótulo "Jogador a Dar Assistência" / "Anytime Assist" → "Assistências" (se cadastrado)
       - Rótulo "Match Winner" / "Moneyline" / "ML" / "Resultado Final" / "Vencedor" / "Partida - Vencedor" → "Resultado Final" (se cadastrado)
       - Rótulo "Both Teams to Score" / "BTTS" / "Ambos Marcam" → "Ambos Marcam" (se cadastrado)
       - Rótulo "Resultado/Ambos Marcam" → "ML e Ambos Marcam" (se cadastrado)
       - Rótulo "Over/Under Cards" / "Total de Cartões Mais/Menos" → "Cartões Mais/Menos" ou similar
       - Rótulo "Handicap de Rondas" / "Round Handicap" / "Map Handicap" → "Handicap de Rondas" (se cadastrado)
       - Rótulo "Total games" (tênis) → "Games" ou "Total de Games" (conforme cadastro)
     * Se NENHUM mercado da lista corresponder semanticamente, extraia o texto como aparece no print.
     * JAMAIS retornar "ML" literal ou null quando há rótulo explícito da casa no print — sempre tente match semântico primeiro.

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

ANTES DE RESPONDER, EXECUTE O CHECKLIST [R7]:
1. Bet builder? → Print tem "CRIAR APOSTA"/"BET BUILDER" OU sinais estruturais (múltiplas seleções do mesmo jogador/jogo + 1 odd + 1 valor APOSTAR + sem botões individuais + opcionalmente "N Seleções" em destaque)? → 1 item só, tipo "Criar Aposta", entrada concatenada. Se NÃO, não invente.
2. Data veio de texto EXPLÍCITO do print/descrição? → Se não, use HOJE ({data_hoje}).
3. Times conhecidos? → Esporte marcado (específico: NBA/WNBA/Futebol Feminino se existir).
4. Descrição começa com bookie? → Preenchi o bookie (incluindo "365"→Bet365, "AG"→Aposta Ganha).
5. Palavra após bookie? → Preenchi a conta. Exceto se for palavra qualitativa ("Todas", "Várias") = null mas continua extração.
6. Rótulo da casa no bilhete? → Preenchi o mercado via match semântico respeitando especificidade exata ("Chutes" ≠ "Chutes no gol"; "Vencedor" ≠ "Vencedor do Mapa").
7. STAKE: há "Xu", "X%", "@odd / Xu", "Stake: X" em QUALQUER parte do texto (imagem + descrição)? → Preenchi stake_unidades. Nunca null se há sinal.
8. ODD: descrição tem "odd X.XX" (ou typos "od"/"ood")? → USEI esse valor, NÃO o do print.
9. "BH" no cabeçalho? → tipster "BH CS" + esporte "Counter-Strike".
10. Tipo_aposta: 1 seleção sem marcador especial = Simples. Não inventar Criar Aposta.
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

    text_raw = resp.content[0].text
    text = text_raw.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)

    # Proteção contra resposta vazia/não-JSON (modelo pode retornar texto explicativo)
    if not text or not text.startswith('{'):
        print(f"  ⚠️ Resposta do modelo não é JSON válido:")
        print(f"     Raw: {text_raw[:500]}")
        # Tenta extrair JSON no meio do texto (modelo pode ter prefixado com explicação)
        match = re.search(r'\{.*"apostas".*\}', text_raw, re.DOTALL)
        if match:
            text = match.group(0)
            print(f"  ✅ JSON extraído do meio da resposta")
        else:
            # Sem JSON → retorna estrutura vazia pra tratar como "não entendi"
            return {'apostas': []}

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  ⚠️ JSONDecodeError: {e}")
        print(f"     Texto: {text[:500]}")
        return {'apostas': []}

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
    # Substring bidirecional (preserva comportamento anterior)
    for x in arr:
        if nome_l in x['nome'].lower() or x['nome'].lower() in nome_l:
            return x['id']
    # Match por tokens: quebra em palavras e checa interseção significativa
    # Útil pra nomes compostos tipo "Syro & GuiaDasApostas" vs cadastro "GDA Syro"
    STOP = {'&', 'e', '+', '-', '|', '/', 'de', 'da', 'do', 'das', 'dos', ''}
    tokens_in = {t.strip('.,;:()[]') for t in re.split(r'[\s&|/+]+', nome_l) if t}
    tokens_in = {t for t in tokens_in if t and t not in STOP and len(t) >= 3}
    if not tokens_in:
        return None
    for x in arr:
        tokens_x = {t.strip('.,;:()[]') for t in re.split(r'[\s&|/+]+', x['nome'].lower()) if t}
        tokens_x = {t for t in tokens_x if t and t not in STOP and len(t) >= 3}
        if tokens_in & tokens_x:
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
