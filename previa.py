"""Prévia das mensagens do apurador com dado de verdade: só lê e escreve no log. Não manda nada no Telegram nem grava nada."""
import os, sys, traceback
for k in ('TELEGRAM_TOKEN', 'TELEGRAM_CHAT', 'AP_TELEGRAM_DESDE'):
    os.environ.pop(k, None)
os.environ['AP_TELEGRAM'] = 'ensaio'
import apurador_previa as m
from datetime import datetime, timezone

def nada(*a, **k):
    raise RuntimeError('a prévia não escreve')
m.requests.post = nada
m.tg = nada
print('P| modo', m.TG_MODO, '| manda de verdade?', m.tg_vivo(), flush=True)

def rodada(nome, quando=None):
    m.MEM.clear(); m.URGENTE.clear()
    if quando:
        m.agora_utc = lambda: quando
    print(f'P| ===== {nome} =====', flush=True)
    try:
        m.avisos()
    except Exception:
        print('P| ERRO', traceback.format_exc()[-1500:].replace('\n', ' / '), flush=True)

rodada('agora: painel de hoje + fechamento de ontem')
rodada('ontem 17h: como saem os jogos de ontem', datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc))
rodada('amanha 7h01: fechamento de hoje do jeito que esta agora', datetime(2026, 11, 14, 10, 1, tzinfo=timezone.utc))
print('P| fim', flush=True)
sys.stdout.flush()
