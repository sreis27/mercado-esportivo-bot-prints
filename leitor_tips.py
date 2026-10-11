"""
Leitor de tips — Mercado Esportivo

Lê, com a conta do Telegram do Samuel, SÓ os grupos/canais de tipster cadastrados em `tips_fontes`,
extrai cada tip com o Claude, grava em `tips` e liga as apostas registradas a ela (apostas.tip_id).
Tip sem aposta depois de 3h vira 'sem_aposta' (o motivo é marcado no dash).

Segurança:
- Login por QR numa página protegida por token (LOGIN_TOKEN); senha de duas etapas só na página.
- A sessão fica no volume do Railway (/data), nunca no banco nem no GitHub.
- Nunca manda mensagem nem marca nada como lido. Mensagem de chat que não é fonte é ignorada sem registro.
- Aparece nos Dispositivos do Telegram como "Mercado Esportivo · leitor de tips" (dá pra encerrar lá).
"""
import os, re, json, hmac, time, base64, asyncio, difflib, unicodedata, traceback
from datetime import datetime, timedelta, timezone
from html import escape

import requests
import anthropic
from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PasswordHashInvalidError
from telethon.tl.types import ChannelParticipantsAdmins
import qrcode
import qrcode.image.svg

# ============================================================
# CONFIGURAÇÃO
# ============================================================
SUPABASE_URL  = "https://yfdrifvhsiumdxgypkjm.supabase.co"
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
API_ID        = int(os.environ.get("TG_API_ID", "0") or 0)
API_HASH      = os.environ.get("TG_API_HASH", "")
LOGIN_TOKEN   = os.environ.get("LOGIN_TOKEN", "")
SESSAO        = os.environ.get("SESSAO_PATH", "/data/leitor")
PORTA         = int(os.environ.get("PORT", "8080"))
DIAS_HIST     = int(os.environ.get("DIAS_HISTORICO", "14"))
MODELO        = os.environ.get("MODELO_TIPS", "claude-sonnet-4-5")

JANELA_H     = 6      # aposta até 6h depois da tip pode casar com ela
SEM_APOSTA_H = 3      # tip sem aposta depois de 3h → 'sem_aposta'
LIMIAR       = 0.5    # nota mínima (0–1) pra ligar aposta e tip

DISPOSITIVO = dict(device_model="Mercado Esportivo · leitor de tips", system_version="Railway",
                   app_version="1.0", lang_code="pt", system_lang_code="pt-BR")

# ============================================================
# SUPABASE
# ============================================================
def _h(prefer=None):
    h = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}', 'Content-Type': 'application/json'}
    if prefer:
        h['Prefer'] = prefer
    return h

def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=_h(), timeout=30)
    r.raise_for_status()
    return r.json()

def sb_upsert(table, row, on_conflict):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                      headers=_h('resolution=merge-duplicates,return=minimal'), json=row, timeout=30)
    if not r.ok:
        print(f"  ⚠️ upsert {table}: {r.status_code} {r.text[:300]}", flush=True)
    r.raise_for_status()

def sb_patch(table, where, body):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}?{where}", headers=_h('return=minimal'),
                       json=body, timeout=30)
    if not r.ok:
        print(f"  ⚠️ patch {table}: {r.status_code} {r.text[:300]}", flush=True)
    r.raise_for_status()

def _utc_q(dt):
    """Timestamp pro filtro do PostgREST (sem '+', que quebraria a URL)."""
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def _ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')) if isinstance(s, str) else s

# ============================================================
# EXTRAÇÃO DA TIP (Claude)
# ============================================================
PROMPT_TIPS = """Você lê mensagens de grupos/canais de tipster de apostas esportivas (português, inglês ou espanhol) e extrai as TIPS — indicações de aposta que o tipster está passando pra ser feita.

O QUE É TIP: indicação de aposta a fazer, com jogo/evento e seleção, normalmente com odd e stake (unidades). Pode vir em texto, em imagem (bilhete/print da casa) ou nos dois.
O QUE NÃO É TIP: resultado de aposta (green, red, void, "bateu", "✅"), relatório de lucro/ROI, propaganda, conversa, enquete, bom dia, aviso genérico ("hoje tem jogo"), recado sobre tip anterior sem nova indicação.

REGRAS:
- Uma mensagem pode ter várias tips (lista de jogos) → um item por tip. Múltipla/dupla/criar aposta = UM item, com as seleções juntas em "selecao" (separadas por " + ").
- evento: "Time A x Time B" (ou torneio/luta/jogador), sem data e sem horário.
- esporte: futebol, basquete, tênis, CS, LoL, Dota, Valorant, MMA... se der pra saber; senão null.
- mercado: o tipo de aposta como escrito (ex.: "Handicap de mapas", "Total de gols", "Vencedor", "Jogador marca").
- selecao: o que apostar, curto (ex.: "Furia -1.5", "Over 2.5", "Sinner", "Ambos marcam sim").
- odd: número decimal (1.85). "@1.85" = 1.85. Se houver odd mínima ("min 1.70", "até 1.75"), odd = a indicada e odd_minima = a mínima; se só houver a mínima, odd = null e odd_minima = ela.
- unidades: stake em unidades ("2u", "stake 2", "2%", "unidade 1,5") → número. Sem stake → null.
- casa: casa indicada, se houver. ao_vivo: true se a tip é ao vivo/live.
- Na dúvida se é tip, é porque não é: devolva lista vazia e diga o motivo curto.

Responda SÓ o JSON:
{"tips":[{"evento":...,"esporte":...,"mercado":...,"selecao":...,"odd":...,"odd_minima":...,"unidades":...,"casa":...,"ao_vivo":...}],"motivo_sem_tip":"..." ou null}"""

_anthropic = None

def _cliente_ia():
    global _anthropic
    if _anthropic is None:
        _anthropic = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _anthropic

def _tipo_imagem(b):
    if b[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if b[:4] == b'RIFF' and b[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'

def _num(v):
    if v is None or v == '':
        return None
    try:
        return float(str(v).replace(',', '.'))
    except ValueError:
        return None

def extrair_tips(texto, imagem=None):
    """Devolve (tips, motivo_sem_tip). tips = lista de dicts com os campos do PROMPT_TIPS."""
    conteudo = []
    if imagem:
        conteudo.append({"type": "image", "source": {"type": "base64", "media_type": _tipo_imagem(imagem),
                                                     "data": base64.b64encode(imagem).decode('ascii')}})
    conteudo.append({"type": "text", "text": f"MENSAGEM DO TIPSTER:\n{texto or '(sem texto)'}"})
    resp = None
    for tentativa, espera in enumerate([2, 5, 10, None]):
        try:
            resp = _cliente_ia().messages.create(
                model=MODELO, max_tokens=900,
                system=[{"type": "text", "text": PROMPT_TIPS, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": conteudo}])
            break
        except Exception as e:
            status = getattr(e, 'status_code', None)
            if espera is None or status not in (429, 500, 503, 529, None):
                raise
            print(f"  ⏳ Claude {status or 'conexão'} — tentando de novo em {espera}s", flush=True)
            time.sleep(espera)
    bruto = resp.content[0].text.strip()
    bruto = re.sub(r'^```(?:json)?\s*|\s*```$', '', bruto)
    m = re.search(r'\{.*\}', bruto, re.S)
    try:
        dados = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        dados = {}
    tips = []
    for t in (dados.get('tips') or []):
        if not isinstance(t, dict) or not (t.get('selecao') or t.get('evento')):
            continue
        t['odd'] = _num(t.get('odd'))
        t['odd_minima'] = _num(t.get('odd_minima'))
        t['unidades'] = _num(t.get('unidades'))
        tips.append(t)
    try:
        u = resp.usage
        print(f"  💰 tips: in={u.input_tokens} cache_read={getattr(u, 'cache_read_input_tokens', 0)} out={u.output_tokens}", flush=True)
    except Exception:
        pass
    return tips, (dados.get('motivo_sem_tip') or None)

RE_TIP = re.compile(r'(@\s*\d|\b\d{1,2}[.,]\d{2,3}\b|\bodds?\b|\b\d+(?:[.,]\d+)?\s*u\b|\bstake\b|\bunidade)', re.I)

def parece_tip(texto, tem_imagem):
    """Filtro barato antes de gastar Claude: imagem ou texto com odd/stake."""
    return bool(tem_imagem) or bool(RE_TIP.search(texto or ''))

# ============================================================
# CASAMENTO TIP ↔ APOSTA
# ============================================================
def norm(s):
    s = unicodedata.normalize('NFD', (s or '').lower())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r'[^a-z0-9.+\- ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def _tokens(s):
    return {t for t in norm(s).replace(' x ', ' ').replace(' vs ', ' ').split() if len(t) >= 2 or t.isdigit()}

def sim(a, b):
    """0–1: parecença entre dois textos (nomes de time abreviados, ordem diferente, acento)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    cont = len(ta & tb) / min(len(ta), len(tb))
    seq = difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
    return max(jac, 0.5 * seq + 0.5 * cont)

def nota_casamento(tip, ap):
    """Evento 45% · seleção 35% · odd 20% (odd sem dado conta meio)."""
    s_ev = sim(tip.get('evento'), ap.get('evento'))
    s_sel = max(sim(tip.get('selecao'), ap.get('entrada')),
                sim(f"{tip.get('selecao') or ''} {tip.get('mercado') or ''}", f"{ap.get('entrada') or ''} {ap.get('mercado') or ''}"))
    ot, oa = _num(tip.get('odd')), _num(ap.get('odd'))
    s_odd = max(0.0, 1 - abs(oa - ot) / (0.25 * ot)) if (ot and oa) else 0.5
    return 0.45 * s_ev + 0.35 * s_sel + 0.20 * s_odd

def escolher_tip(ap, tips):
    """A tip mais parecida, do mesmo tipster, postada até 6h antes da aposta (2 min de folga pra relógio)."""
    criado = _ts(ap['criado_em'])
    melhor, melhor_nota = None, LIMIAR
    for t in tips:
        if t['tipster_id'] != ap.get('tipster_id'):
            continue
        dt = (criado - _ts(t['postado_em'])).total_seconds()
        if dt < -120 or dt > JANELA_H * 3600:
            continue
        n = nota_casamento(t, ap) - 0.02 * max(dt, 0) / 3600   # leve preferência pela tip mais recente
        if n > melhor_nota:
            melhor, melhor_nota = t, n
    return melhor

def casar(desde):
    """Liga apostas sem tip às tips do mesmo tipster e fecha tip velha sem aposta."""
    agora = datetime.now(timezone.utc)
    tips = sb_get(f"tips?status=neq.descartada&postado_em=gte.{_utc_q(desde - timedelta(hours=JANELA_H))}"
                  "&select=id,tipster_id,postado_em,evento,mercado,selecao,odd,status")
    if not tips:
        return 0, 0
    tipsters = sorted({t['tipster_id'] for t in tips if t.get('tipster_id')})
    if not tipsters:
        return 0, 0
    aps = sb_get(f"apostas?tip_id=is.null&tipster_id=in.({','.join(tipsters)})&criado_em=gte.{_utc_q(desde)}"
                 "&select=id,tipster_id,criado_em,evento,entrada,mercado,odd")
    ligadas = 0
    for ap in aps:
        t = escolher_tip(ap, tips)
        if not t:
            continue
        sb_patch('apostas', f"id=eq.{ap['id']}&tip_id=is.null", {'tip_id': t['id']})
        if t['status'] != 'apostada':
            sb_patch('tips', f"id=eq.{t['id']}", {'status': 'apostada', 'casado_em': agora.isoformat()})
            t['status'] = 'apostada'
        ligadas += 1
    limite = agora - timedelta(hours=SEM_APOSTA_H)
    fechadas = 0
    for t in tips:
        if t['status'] == 'aberta' and _ts(t['postado_em']) < limite:
            sb_patch('tips', f"id=eq.{t['id']}&status=eq.aberta", {'status': 'sem_aposta'})
            fechadas += 1
    if ligadas or fechadas:
        print(f"  🔗 casamento: {ligadas} aposta(s) ligada(s), {fechadas} tip(s) sem aposta", flush=True)
    return ligadas, fechadas

# ============================================================
# TELEGRAM
# ============================================================
client = None
FONTES = {}            # chat_id → fonte
ADMINS = {}            # chat_id → ids de admin (grupos)
ESTADO = {'estado': 'iniciando', 'qr': None, 'nome': None, 'msg': None}
_qr_tarefa = None
_ultimo_acesso_login = datetime.min.replace(tzinfo=timezone.utc)
_leitura_iniciada = False

def svg_qr(url):
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding='unicode')

async def remetente_ok(msg, chat_id):
    """Canal: tudo é do tipster. Grupo: só admin (conversa de membro não é tip)."""
    if getattr(msg, 'post', False) or msg.sender_id in (None, chat_id):
        return True
    if chat_id not in ADMINS:
        try:
            ADMINS[chat_id] = {p.id async for p in client.iter_participants(chat_id, filter=ChannelParticipantsAdmins)}
        except Exception as e:
            print(f"  ⚠️ admins de {chat_id}: {e} — aceito todo mundo", flush=True)
            ADMINS[chat_id] = None
    adm = ADMINS[chat_id]
    return adm is None or msg.sender_id in adm

def _tem_imagem(msg):
    if msg.photo:
        return True
    f = getattr(msg, 'file', None)
    return bool(msg.document and f and (f.mime_type or '').startswith('image/'))

async def processar(msg, fonte, editada=False):
    chat_id = fonte['chat_id']
    if getattr(msg, 'action', None):
        return
    if not await remetente_ok(msg, chat_id):
        return
    texto = msg.message or ''
    tem_img = _tem_imagem(msg)
    base = {'fonte_id': fonte['id'], 'tipster_id': fonte['tipster_id'], 'chat_id': chat_id, 'msg_id': msg.id,
            'postado_em': msg.date.astimezone(timezone.utc).isoformat(), 'texto': texto[:4000], 'tem_imagem': tem_img}
    if editada:
        base['editado_em'] = (msg.edit_date or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    existentes = {r['idx']: r['status'] for r in
                  await asyncio.to_thread(sb_get, f"tips?chat_id=eq.{chat_id}&msg_id=eq.{msg.id}&select=idx,status")}
    if editada and existentes and all(s in ('apostada', 'sem_aposta') for s in existentes.values()):
        return   # já resolvida: edição depois (green/red, "odd caiu") não muda nada
    tips, motivo = [], 'sem odd/stake nem imagem'
    if parece_tip(texto, tem_img):
        img = await client.download_media(msg, bytes) if tem_img else None
        tips, motivo = await asyncio.to_thread(extrair_tips, texto, img)
    if not tips:
        # mensagem sem tip; se a 1ª tip dela já foi resolvida (edição tirou a tip), não mexe
        linhas = [] if existentes.get(0) in ('apostada', 'sem_aposta') else \
                 [{**base, 'idx': 0, 'status': 'descartada', 'extraido': {'motivo': motivo}}]
    else:
        linhas = []
        for i, t in enumerate(tips):
            linha = {**base, 'idx': i, 'evento': t.get('evento'), 'esporte': t.get('esporte'), 'mercado': t.get('mercado'),
                     'selecao': t.get('selecao'), 'odd': t.get('odd'), 'odd_minima': t.get('odd_minima'),
                     'unidades': t.get('unidades'), 'casa': t.get('casa'), 'ao_vivo': t.get('ao_vivo'), 'extraido': t}
            if existentes.get(i) not in ('apostada', 'sem_aposta'):
                linha['status'] = 'aberta'
            linhas.append(linha)
    for linha in linhas:
        await asyncio.to_thread(sb_upsert, 'tips', linha, 'chat_id,msg_id,idx')
    n = max(len(tips), 1)
    if any(i >= n and s == 'aberta' for i, s in existentes.items()):
        await asyncio.to_thread(sb_patch, 'tips', f"chat_id=eq.{chat_id}&msg_id=eq.{msg.id}&idx=gte.{n}&status=eq.aberta",
                                {'status': 'descartada'})
    if tips:
        print(f"  🎯 {fonte.get('titulo') or chat_id} #{msg.id}: {len(tips)} tip(s){' (editada)' if editada else ''}", flush=True)

async def varrer(fonte, desde):
    """Lê as mensagens da fonte desde `desde` e processa as que ainda não estão no banco."""
    vistos = {r['msg_id'] for r in await asyncio.to_thread(
        sb_get, f"tips?chat_id=eq.{fonte['chat_id']}&postado_em=gte.{_utc_q(desde)}&select=msg_id")}
    novos = 0
    async for msg in client.iter_messages(fonte['chat_id']):
        if msg.date < desde:
            break
        if msg.id in vistos:
            continue
        try:
            await processar(msg, fonte)
            novos += 1
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            print(f"  ❌ {fonte.get('titulo')} #{msg.id}: {e}", flush=True)
            traceback.print_exc()
        await asyncio.sleep(0.2)
    if novos:
        print(f"📥 {fonte.get('titulo') or fonte['chat_id']}: {novos} mensagem(ns) lida(s) desde {desde:%d/%m %H:%M}", flush=True)
    return novos

async def atualizar_fontes():
    """Acha o chat de cada fonte nova pelo trecho do nome. Só os grupos que batem aparecem no log."""
    rows = await asyncio.to_thread(sb_get, 'tips_fontes?ativo=eq.true&select=id,busca,tipster_id,chat_id,titulo')
    pendentes = [f for f in rows if not f.get('chat_id')]
    novas = []
    if pendentes:
        achados = {f['id']: [] for f in pendentes}
        async for d in client.iter_dialogs():
            if not (d.is_group or d.is_channel):
                continue
            nome = d.name or ''
            for f in pendentes:
                if f['busca'].strip().lower() in nome.lower():
                    achados[f['id']].append((d.id, nome))
        for f in pendentes:
            a = achados[f['id']]
            if len(a) == 1:
                f['chat_id'], f['titulo'] = a[0]
                await asyncio.to_thread(sb_patch, 'tips_fontes', f"id=eq.{f['id']}", {'chat_id': f['chat_id'], 'titulo': f['titulo'], 'obs': None})
                print(f"📌 Fonte '{f['busca']}' → {f['titulo']} ({f['chat_id']})", flush=True)
                novas.append(f)
            else:
                obs = ('mais de um grupo bate: ' + ' | '.join(n for _, n in a)) if a else 'nenhum grupo com esse nome'
                await asyncio.to_thread(sb_patch, 'tips_fontes', f"id=eq.{f['id']}", {'obs': obs[:500]})
                print(f"⚠️ Fonte '{f['busca']}': {obs}", flush=True)
    FONTES.clear()
    FONTES.update({f['chat_id']: f for f in rows if f.get('chat_id')})
    return novas

async def ciclo_leitura():
    """Depois do login: acha as fontes, puxa o histórico, e de 10 em 10 min revarre as últimas 12h
    (pega o que escapou de atualização perdida) e casa as apostas a cada 2 min."""
    primeira = True
    passo = 0
    while True:
        try:
            if passo % 5 == 0:
                novas = await atualizar_fontes()
                for f in (list(FONTES.values()) if primeira else novas):
                    await varrer(f, datetime.now(timezone.utc) - timedelta(days=DIAS_HIST))
                if not primeira:
                    for f in FONTES.values():
                        await varrer(f, datetime.now(timezone.utc) - timedelta(hours=12))
                if primeira or novas:
                    await asyncio.to_thread(casar, datetime.now(timezone.utc) - timedelta(days=DIAS_HIST))
                primeira = False
            await asyncio.to_thread(casar, datetime.now(timezone.utc) - timedelta(hours=24))
        except Exception as e:
            print(f"❌ ciclo de leitura: {e}", flush=True)
            traceback.print_exc()
        passo += 1
        await asyncio.sleep(120)

async def ao_receber(ev):
    f = FONTES.get(ev.chat_id)
    if not f or ESTADO['estado'] != 'conectado':
        return   # chat que não é fonte: ignora sem registro
    try:
        await processar(ev.message, f, editada=isinstance(ev, events.MessageEdited.Event))
    except Exception as e:
        print(f"❌ mensagem {ev.chat_id}#{ev.id}: {e}", flush=True)
        traceback.print_exc()

async def depois_do_login():
    global _leitura_iniciada
    me = await client.get_me()
    nome = ' '.join(x for x in [me.first_name, me.last_name] if x) or 'conta'
    ESTADO.update(estado='conectado', qr=None, msg=None, nome=nome + (f' (@{me.username})' if me.username else ''))
    print(f"✅ Conectado como {ESTADO['nome']}", flush=True)
    if not _leitura_iniciada:
        _leitura_iniciada = True
        asyncio.create_task(ciclo_leitura())

async def ciclo_qr():
    """Gera QR enquanto a página de login estiver aberta (QR expira em ~30s e é refeito)."""
    while True:
        if datetime.now(timezone.utc) - _ultimo_acesso_login > timedelta(minutes=2):
            ESTADO.update(estado='aguardando_login', qr=None)
            return
        qr = await client.qr_login()
        ESTADO.update(estado='qr', qr=svg_qr(qr.url), msg=None)
        try:
            await qr.wait()
            await depois_do_login()
            return
        except asyncio.TimeoutError:
            continue
        except SessionPasswordNeededError:
            ESTADO.update(estado='senha', qr=None, msg=None)
            return

# ============================================================
# PÁGINA DE LOGIN (token na URL)
# ============================================================
PAGINA = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Leitor de tips</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--txt:#e6edf3;--mut:#8b949e;--ok:#00e5a0;--err:#e05252}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:16px}
main{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;max-width:420px;width:100%}
h1{font-size:18px;margin:0 0 12px}p{margin:8px 0}.mut{color:var(--mut);font-size:13px}.ok{color:var(--ok)}.err{color:var(--err)}
.qr{background:#fff;border-radius:8px;padding:8px;margin:12px auto;max-width:260px}.qr svg{width:100%;height:auto;display:block}
input,button{font:inherit;padding:10px 12px;border-radius:8px;border:1px solid var(--line);width:100%}
input{background:#0d1117;color:var(--txt);margin:8px 0}button{background:var(--ok);color:#000;border:none;font-weight:600;cursor:pointer}
</style></head><body><main><h1>Conectar o leitor de tips</h1><div id="box">Carregando…</div></main>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const box = document.getElementById('box');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let senhaNaTela = false;
async function tick() {
  let d;
  try { d = await (await fetch('/login/estado?t=' + encodeURIComponent(T))).json(); }
  catch (e) { box.innerHTML = '<p class="err">Sem conexão com o servidor. Tentando de novo…</p>'; return setTimeout(tick, 3000); }
  if (d.estado === 'conectado') { box.innerHTML = '<p class="ok">Conectado como <b>' + esc(d.nome) + '</b>.</p><p class="mut">Pode fechar esta página. Pra desligar: Telegram › Configurações › Dispositivos › "Mercado Esportivo · leitor de tips" › Encerrar sessão.</p>'; return; }
  if (d.estado === 'senha') {
    if (!senhaNaTela) {
      senhaNaTela = true;
      box.innerHTML = '<p>Tua conta tem verificação em duas etapas. Digita a senha do Telegram:</p><form id="f"><input type="password" id="s" autocomplete="current-password" required aria-label="Senha do Telegram"><button>Entrar</button></form><p id="m" class="err"></p>';
      document.getElementById('f').onsubmit = async ev => {
        ev.preventDefault();
        const r = await fetch('/login/senha?t=' + encodeURIComponent(T), {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({senha: document.getElementById('s').value})});
        const j = await r.json(); document.getElementById('m').textContent = j.ok ? '' : (j.msg || 'Não deu');
        if (j.ok) { senhaNaTela = false; tick(); }
      };
    }
    return;
  }
  senhaNaTela = false;
  if (d.estado === 'qr') box.innerHTML = '<p>No Telegram do celular: <b>Configurações › Dispositivos › Conectar dispositivo</b> e aponta a câmera pro código.</p><div class="qr">' + d.qr + '</div><p class="mut">O código troca sozinho a cada ~30 segundos.</p>';
  else box.innerHTML = '<p class="' + (d.msg ? 'err' : 'mut') + '">' + esc(d.msg || 'Preparando o código…') + '</p>';
  setTimeout(tick, 2000);
}
tick();
</script></body></html>"""

def _token_ok(request):
    t = request.query.get('t', '')
    return bool(LOGIN_TOKEN) and hmac.compare_digest(t, LOGIN_TOKEN)

async def pagina_login(request):
    if not _token_ok(request):
        return web.Response(status=403, text='403')
    return web.Response(text=PAGINA, content_type='text/html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

async def estado_login(request):
    global _qr_tarefa, _ultimo_acesso_login
    if not _token_ok(request):
        return web.json_response({'estado': 'negado'}, status=403)
    _ultimo_acesso_login = datetime.now(timezone.utc)
    if ESTADO['estado'] not in ('conectado', 'senha', 'sem_api') and (_qr_tarefa is None or _qr_tarefa.done()):
        _qr_tarefa = asyncio.create_task(ciclo_qr())
    return web.json_response({k: ESTADO.get(k) for k in ('estado', 'qr', 'nome', 'msg')}, headers={'Cache-Control': 'no-store'})

async def senha_login(request):
    if not _token_ok(request):
        return web.json_response({'ok': False}, status=403)
    if ESTADO['estado'] != 'senha':
        return web.json_response({'ok': False, 'msg': 'Não está pedindo senha agora.'})
    try:
        senha = (await request.json()).get('senha') or ''
        await client.sign_in(password=senha)
        await depois_do_login()
        return web.json_response({'ok': True})
    except PasswordHashInvalidError:
        return web.json_response({'ok': False, 'msg': 'Senha incorreta.'})
    except Exception as e:
        return web.json_response({'ok': False, 'msg': f'Erro: {type(e).__name__}'})

async def saude(request):
    return web.Response(text=f"leitor de tips: {ESTADO['estado']}")

def criar_app():
    app = web.Application()
    app.router.add_get('/', saude)
    app.router.add_get('/login', pagina_login)
    app.router.add_get('/login/estado', estado_login)
    app.router.add_post('/login/senha', senha_login)
    return app

# ============================================================
# MAIN
# ============================================================
async def main():
    global client
    print("📡 Leitor de tips iniciando…", flush=True)
    runner = web.AppRunner(criar_app())
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORTA).start()
    print(f"  página de login na porta {PORTA}{'' if LOGIN_TOKEN else ' (DESLIGADA: falta LOGIN_TOKEN)'}", flush=True)
    if not (API_ID and API_HASH):
        ESTADO.update(estado='sem_api', msg='Falta TG_API_ID/TG_API_HASH no Railway.')
        print("⚠️ Falta TG_API_ID/TG_API_HASH — esperando configuração", flush=True)
        await asyncio.Event().wait()
    os.makedirs(os.path.dirname(SESSAO) or '.', exist_ok=True)
    client = TelegramClient(SESSAO, API_ID, API_HASH, **DISPOSITIVO)
    client.add_event_handler(ao_receber, events.NewMessage())
    client.add_event_handler(ao_receber, events.MessageEdited())
    await client.connect()
    if await client.is_user_authorized():
        await depois_do_login()
    else:
        ESTADO.update(estado='aguardando_login')
        print("🔑 Sem login — abrir a página /login?t=… pra conectar", flush=True)
    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
