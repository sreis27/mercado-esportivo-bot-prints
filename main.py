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
    """Adiciona reação à mensagem. emoji: '✅', '⚠️', '❌', '🤔'"""
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
        'stakes': sb_get('stakes_historico?select=tipster_id,valor_reais,vigente_a_partir'),
    }

def extrair_aposta(imagem_bytes, descricao_msg, cadastros, data_hoje, operador_msg=None):
    """Usa Claude Vision pra extrair dados da imagem + descrição da mensagem."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    tipsters_lista = [t['nome'] for t in cadastros['tipsters']]
    bookies_lista = [b['nome'] for b in cadastros['bookies']]
    operadores_lista = [o['nome'] for o in cadastros['operadores']]
    esportes_lista = [e['nome'] for e in cadastros['esportes']]

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

REGRAS IMPORTANTES:

A. OPERADOR: use o nome "{operador_msg}" (quem enviou o print). Se bater com algum nome da lista de operadores, use o nome exato do cadastro. Se não bater, deixe como está.

B. TIPSTER: deduza pelo cabeçalho/título do print. Se aparecer "BH Tipster", provavelmente é "BH CS". Se aparecer "GOA", pode ser "GOA TOM", "GOA Fut Fem", "GOA Tradicional" (use contexto da aposta pra decidir qual). Correlacione com a lista de tipsters.

C. ESPORTE: INFIRA pelo contexto — nome dos times, torneio, tipo de mercado. Exemplos:
   - "1W x GenOne" + "Handicap de rondas" → Counter-Strike
   - "Nice x Le Havre" → Futebol
   - "Lakers x Warriors" → Basquete
   - Se ver "Mapa 3", "rondas", "kills" → Counter-Strike/Esports
   Use o nome do esporte EXATAMENTE como aparece no cadastro.

D. DATA DO EVENTO: se o print diz "LIVE" ou "AO VIVO", é HOJE. Se você reconhece os times e sabe que há uma partida marcada entre eles numa data específica, use essa data. Se não, use HOJE ({data_hoje}).

E. STAKE: o print normalmente mostra em unidades (ex: "2u", "1u", "0.5u"). Se mostrar R$, use o valor direto e deixe pro dashboard converter. Se a descrição do operador contradizer o print (ex: print diz 2u mas operador escreveu "apostei 1u"), use o da descrição.

F. BOOKIE e CONTAS: vêm da descrição do operador. Ex: "bet365 luciadritrich" → bookie: Bet365, contas: luciadritrich. Se a casa só aparece sozinha sem conta, apenas informe o bookie.

G. MÚLTIPLAS APOSTAS: se o print contém vários bilhetes independentes (ex: duas entradas separadas), retorne múltiplos itens. Se é UMA aposta (mesmo múltipla combinada dentro de 1 bilhete), retorne 1 item com tipo_aposta correto.

H. TIPO DE APOSTA: "Simples" (1 seleção), "Dupla" (2), "Tripla" (3), "Múltipla" (4+), "Criar Aposta" (bet builder da casa). Se não der pra inferir, null.

I. CONFIANÇA: se NÃO tiver certeza de algum campo, deixe NULL. É melhor null do que errado — o operador confere depois.

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
      "tipo_aposta": "Simples|Dupla|Tripla|Múltipla|Criar Aposta" ou null,
      "tipster": "nome igual ao cadastro" ou null,
      "operador": "nome igual ao cadastro" ou null,
      "bookie": "nome igual ao cadastro" ou null,
      "contas_utilizadas": "separadas por vírgula" ou null
    }}
  ]
}}

Responda APENAS com o JSON."""

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
    data_ev = ap.get('data_evento')
    sr = None
    if su and tipster_id and data_ev:
        sv = get_stake_valor(tipster_id, data_ev, cadastros['stakes'])
        if sv:
            sr = su * sv

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
            tg_react(msg_id, '❌')
            return

        data_hoje = datetime.now(BRT).strftime('%Y-%m-%d')
        # Nome de quem enviou o print (operador)
        from_user = msg.get('from') or {}
        operador_nome = from_user.get('first_name') or from_user.get('username') or ''
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
            tg_react(msg_id, '✅')
        elif sucesso > 0:
            print(f"  ⚠️ {sucesso}/{len(apostas)} apostas registradas")
            tg_react(msg_id, '⚠️')
        else:
            print(f"  ❌ Nenhuma aposta salva")
            tg_react(msg_id, '❌')

    except Exception as e:
        print(f"  ❌ Exception: {e}")
        traceback.print_exc()
        tg_react(msg_id, '❌')

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
