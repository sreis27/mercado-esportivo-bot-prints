# Apurador

Robô que acompanha as apostas do banco e propõe o resultado (green/red) quando o jogo termina.

- Roda sozinho, em ciclos de 5 minutos (`python apurador.py`).
- Lê as apostas, monta a ficha de cada uma com o modelo, acha o jogo numa fonte pública de placar e apura pela regra do mercado.
- **Em sombra:** só escreve na tabela `apuracoes` (a proposta e a prova). Não altera aposta e não manda mensagem.
- Na dúvida não propõe; quando propõe com ressalva, a ressalva vai escrita.
- Chaves só por variável de ambiente (`SUPABASE_KEY`, `ANTHROPIC_KEY`). Nada de segredo neste repositório.

Teste local, sem rede: `python teste_apurador.py` (banco, placar, modelo e relógio de mentira).
