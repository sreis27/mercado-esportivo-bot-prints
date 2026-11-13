# Apurador

Robô que acompanha as apostas do banco e propõe o resultado (green/red) quando o jogo termina.

- Roda sozinho, em ciclos de 5 minutos (`python apurador.py`).
- Lê as apostas, monta a ficha de cada uma com o modelo, acha o jogo numa fonte pública de placar e apura pela regra do mercado.
- **Em sombra:** só escreve nas tabelas dele (`apuracoes`: a proposta e a prova; `apuracao_avisos`: o estado dos avisos; `apuracao_divergencias`: as marcadas diferente do placar). Não altera aposta.
- **Avisos no Telegram** (`AP_TELEGRAM` = `desligado`, `ensaio` ou `ligado`): um painel do dia, uma mensagem por jogo quando ele começa (a partida e cada entrada com odd, valor e casa), editada com o placar, o resultado quando termina e, às 7h, o fechamento do dia anterior (o que tem baixa, o que falta e o que revisar), seguido da lista das apostas marcadas diferente do placar (tabela `apuracao_divergencias`; cada uma sai quando a marcação passa a bater ou quando o dono confirma que a casa pagou assim). Em `ensaio` só escreve no log.
- Na dúvida não propõe; quando propõe com ressalva, a ressalva vai escrita.
- Chaves só por variável de ambiente (`SUPABASE_KEY`, `ANTHROPIC_KEY`, `TELEGRAM_TOKEN`). Nada de segredo neste repositório.

Testes locais, sem rede: `python teste_apurador.py` e `python teste_avisos.py` (banco, placar, modelo, Telegram e relógio de mentira).
